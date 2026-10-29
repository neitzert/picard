"""IRC channel topic adapter -- stores chunks in IRC channel TOPIC lines.

Each write_chunk joins a dedicated channel named #picard-{token} and sets
its TOPIC to the encoded chunk. Cold reads JOIN the channel and inspect the
332 RPL_TOPIC response. Channels persist on the server while the topic is set.

This is a best-effort substrate: IRC servers vary in topic length limits
(typically 307 to 512 bytes). No external library required.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "irc-channel-topic"
_CRLF = b"\r\n"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class IrcChannelAdapter(Adapter):

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 6667,
        nick: str = "picard_bot",
        password: str = "",
    ) -> None:
        self._host = host
        self._port = port
        self._nick = nick
        self._password = password
        self._reg: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=400,
            persistent=False,
            ordered=False,
            rate_limit_per_min=30,
            tags={"substrate": "irc-topic", "ephemeral": "true"},
        )

    async def _irc_session(self):
        reader, writer = await asyncio.open_connection(self._host, self._port)
        if self._password:
            writer.write(f"PASS {self._password}{_CRLF.decode()}".encode())
        writer.write(f"NICK {self._nick}\r\nUSER picard 0 * :picard\r\n".encode())
        await writer.drain()

        async def read_until(prefix: str, timeout: float = 5.0) -> str:
            deadline = asyncio.get_event_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    return ""
                try:
                    line_bytes = await asyncio.wait_for(reader.readline(), timeout=remaining)
                    line = line_bytes.decode(errors="replace").strip()
                    if "PING" in line:
                        server = line.split(":", 1)[-1]
                        writer.write(f"PONG :{server}\r\n".encode())
                        await writer.drain()
                    if prefix in line:
                        return line
                except asyncio.TimeoutError:
                    return ""

        await read_until("001")
        return reader, writer

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:12]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        channel = f"#picard-{token}"

        reader, writer = await self._irc_session()
        try:
            writer.write(f"JOIN {channel}\r\n".encode())
            await writer.drain()
            await asyncio.sleep(0.3)
            safe = encoded_chunk.replace("\r", "").replace("\n", "")
            writer.write(f"TOPIC {channel} :{safe}\r\n".encode())
            await writer.drain()
            await asyncio.sleep(0.2)
            writer.write(f"PART {channel}\r\nQUIT\r\n".encode())
            await writer.drain()
        finally:
            writer.close()

        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "channel": channel,
                                 "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token]

        channel = loc["channel"]
        reader, writer = await self._irc_session()
        chunk = ""
        try:
            writer.write(f"JOIN {channel}\r\n".encode())
            await writer.drain()

            deadline = asyncio.get_event_loop().time() + 5.0
            while asyncio.get_event_loop().time() < deadline:
                remaining = deadline - asyncio.get_event_loop().time()
                try:
                    line_bytes = await asyncio.wait_for(reader.readline(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                line = line_bytes.decode(errors="replace").strip()
                if "PING" in line:
                    server = line.split(":", 1)[-1]
                    writer.write(f"PONG :{server}\r\n".encode())
                    await writer.drain()
                # 332 is RPL_TOPIC
                if " 332 " in line and channel.lower() in line.lower():
                    parts = line.split(":", 2)
                    if len(parts) >= 3:
                        chunk = parts[2].strip()
                    break
            writer.write(f"PART {channel}\r\nQUIT\r\n".encode())
            await writer.drain()
        finally:
            writer.close()

        if not chunk:
            raise KeyError(f"irc-channel-topic chunk not found: {token}")
        self._reg[token] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        token = loc["token"]
        self._reg.pop(token, None)
        channel = loc.get("channel", f"#picard-{token}")

        try:
            reader, writer = await self._irc_session()
            writer.write(f"JOIN {channel}\r\nTOPIC {channel} :\r\nPART {channel}\r\nQUIT\r\n".encode())
            await writer.drain()
            await asyncio.sleep(0.3)
            writer.close()
        except Exception:
            pass
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        for token, chunk in self._reg.items():
            if _manifest_id_from_chunk(chunk) == manifest_id:
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token,
                                         "channel": f"#picard-{token}",
                                         "manifest": manifest_id}),
                ))
        return refs

    async def health_check(self) -> bool:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port), timeout=3.0
            )
            writer.write(b"QUIT\r\n")
            await writer.drain()
            writer.close()
            return True
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
