"""IRC adapter — covert channel via PRIVMSG to an IRC channel.

Chunks are sent as PRIVMSG lines to a channel where a cooperating
daemon collects them.  Reads and deletes use a simple request/response
protocol (PCHK-REQ / PCHK-RSP / PCHK-DEL) over the same channel.

This is an ephemeral, non-persistent transport — similar to the ICMP
adapter.  No external library required (uses asyncio TCP sockets).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class IrcAdapter(Adapter):
    """Scatter chunks as IRC PRIVMSG lines via raw TCP."""

    def __init__(
        self,
        server: str,
        nickname: str,
        channel: str,
        *,
        port: int = 6667,
        password: str | None = None,
        use_ssl: bool = False,
        max_chunk_bytes: int = 400,
        timeout: float = 10.0,
    ) -> None:
        self._server = server
        self._port = port
        self._nickname = nickname
        self._channel = channel if channel.startswith("#") else f"#{channel}"
        self._password = password
        self._use_ssl = use_ssl
        self._max = max_chunk_bytes
        self._timeout = timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def _connect(self) -> None:
        if self._writer is not None:
            return
        self._reader, self._writer = await asyncio.open_connection(
            self._server, self._port, ssl=self._use_ssl,
        )
        if self._password:
            self._send_raw(f"PASS {self._password}")
        self._send_raw(f"NICK {self._nickname}")
        self._send_raw(f"USER {self._nickname} 0 * :{self._nickname}")
        await self._wait_for("001")
        self._send_raw(f"JOIN {self._channel}")
        await self._wait_for("JOIN")

    def _send_raw(self, line: str) -> None:
        self._writer.write(f"{line}\r\n".encode("utf-8"))

    async def _wait_for(self, keyword: str, timeout: float | None = None) -> str:
        deadline = timeout or self._timeout
        end = asyncio.get_event_loop().time() + deadline
        while True:
            remaining = end - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"timed out waiting for {keyword}")
            line_bytes = await asyncio.wait_for(
                self._reader.readline(), timeout=remaining,
            )
            line = line_bytes.decode("utf-8", errors="replace").strip()
            if line.startswith("PING"):
                pong_token = line.split(" ", 1)[1] if " " in line else ""
                self._send_raw(f"PONG {pong_token}")
                continue
            if keyword in line:
                return line

    async def _collect_responses(
        self, prefix: str, timeout: float | None = None,
    ) -> list[str]:
        deadline = timeout or self._timeout
        end = asyncio.get_event_loop().time() + deadline
        results: list[str] = []
        while True:
            remaining = end - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            try:
                line_bytes = await asyncio.wait_for(
                    self._reader.readline(), timeout=remaining,
                )
            except (asyncio.TimeoutError, TimeoutError):
                break
            line = line_bytes.decode("utf-8", errors="replace").strip()
            if line.startswith("PING"):
                pong_token = line.split(" ", 1)[1] if " " in line else ""
                self._send_raw(f"PONG {pong_token}")
                continue
            idx = line.find(prefix)
            if idx >= 0:
                results.append(line[idx:])
        return results

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="irc",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=20,
            ttl_seconds=None,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        await self._connect()
        msg_id = uuid.uuid4().hex[:12]
        self._send_raw(
            f"PRIVMSG {self._channel} :PCHK-STR:{msg_id}:{encoded_chunk}"
        )
        return StoredChunkRef(adapter_name="irc", location=msg_id)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        await self._connect()
        self._send_raw(
            f"PRIVMSG {self._channel} :PCHK-REQ:{ref.location}"
        )
        prefix = f"PCHK-RSP:{ref.location}:"
        line = await self._wait_for(prefix)
        idx = line.find(prefix)
        return line[idx + len(prefix):]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        await self._connect()
        try:
            self._send_raw(
                f"PRIVMSG {self._channel} :PCHK-DEL:{ref.location}"
            )
            return True
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        await self._connect()
        self._send_raw(
            f"PRIVMSG {self._channel} :PCHK-LST:{manifest_id}"
        )
        prefix = "PCHK-ENT:"
        lines = await self._collect_responses(prefix)
        refs: list[StoredChunkRef] = []
        for line in lines:
            entry = line[len(prefix):]
            msg_id = entry.split(":", 1)[0]
            refs.append(StoredChunkRef(adapter_name="irc", location=msg_id))
        return refs

    async def health_check(self) -> bool:
        try:
            await self._connect()
            self._send_raw(f"PING :picard-health")
            await self._wait_for("PONG", timeout=5.0)
            return True
        except Exception:
            return False
