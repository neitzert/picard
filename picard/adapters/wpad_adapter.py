"""WPAD adapter — store chunks in Web Proxy Auto-Discovery PAC files.

Embeds chunk data as base64 in JavaScript comments within wpad.dat.
Every browser on the network fetches this file automatically.

Note: WPAD is fetched automatically by browsers and OS network stacks.
A PAC file with embedded chunk data in comments is distributed to
every machine on the network automatically.

Requires: pip install picard[wpad]  (aiohttp)
"""

from __future__ import annotations

import base64
import re
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_CHUNK_MARKER = "/* PICARD:"
_CHUNK_END = ":DRACP */"


class WpadAdapter(Adapter):
    """Scatter chunks into WPAD PAC file comments."""

    def __init__(
        self,
        wpad_host: str,
        *,
        wpad_port: int = 80,
        update_endpoint: str | None = None,
        max_chunk_bytes: int = 4096,
    ) -> None:
        self._wpad_host = wpad_host
        self._wpad_port = wpad_port
        self._update_url = update_endpoint or f"http://{wpad_host}:{wpad_port}/wpad.dat"
        self._max = max_chunk_bytes
        self._session = None


    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _get_session(self):
        import aiohttp
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="wpad",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=60,
        )

    def _chunk_key(self, encoded_chunk: str) -> str:
        from picard.chunker import Chunk
        try:
            chunk = Chunk.decode(encoded_chunk)
            return f"{chunk.manifest_id}_{chunk.block_index}_{chunk.chunk_index}"
        except (ValueError, Exception):
            return uuid.uuid4().hex

    def _encode_comment(self, key: str, data: str) -> str:
        b64 = base64.b64encode(data.encode()).decode()
        return f"{_CHUNK_MARKER}{key}:{b64}{_CHUNK_END}"

    def _decode_comments(self, pac_content: str) -> dict[str, str]:
        pattern = re.escape(_CHUNK_MARKER) + r"([^:]+):([^*]+)" + re.escape(_CHUNK_END)
        chunks = {}
        for match in re.finditer(pattern, pac_content):
            key = match.group(1)
            b64 = match.group(2)
            chunks[key] = base64.b64decode(b64).decode()
        return chunks

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        key = self._chunk_key(encoded_chunk)
        comment = self._encode_comment(key, encoded_chunk)

        session = await self._get_session()
        # Fetch current PAC file
        async with session.get(self._update_url) as resp:
            if resp.status == 200:
                current = await resp.text()
            else:
                current = 'function FindProxyForURL(url, host) { return "DIRECT"; }'

        new_content = current.rstrip() + "\n" + comment + "\n"

        async with session.put(
            self._update_url,
            data=new_content,
            headers={"Content-Type": "application/x-ns-proxy-autoconfig"},
        ) as resp:
            resp.raise_for_status()

        loc = f"wpad://{self._wpad_host}/{key}"
        return StoredChunkRef(adapter_name="wpad", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        parts = ref.location.replace("wpad://", "").split("/", 1)
        key = parts[1] if len(parts) > 1 else ""

        session = await self._get_session()
        async with session.get(self._update_url) as resp:
            resp.raise_for_status()
            content = await resp.text()

        chunks = self._decode_comments(content)
        if key not in chunks:
            raise KeyError(f"chunk not found in PAC: {key}")
        return chunks[key]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        parts = ref.location.replace("wpad://", "").split("/", 1)
        key = parts[1] if len(parts) > 1 else ""

        try:
            session = await self._get_session()
            async with session.get(self._update_url) as resp:
                content = await resp.text()

            comment = None
            pattern = re.escape(_CHUNK_MARKER) + re.escape(key) + r":[^*]+" + re.escape(_CHUNK_END)
            match = re.search(pattern, content)
            if not match:
                return False

            new_content = content[:match.start()] + content[match.end():]

            async with session.put(
                self._update_url,
                data=new_content,
                headers={"Content-Type": "application/x-ns-proxy-autoconfig"},
            ) as resp:
                return resp.status in (200, 204)
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        try:
            session = await self._get_session()
            async with session.get(self._update_url) as resp:
                content = await resp.text()

            chunks = self._decode_comments(content)
            return [
                StoredChunkRef(
                    adapter_name="wpad",
                    location=f"wpad://{self._wpad_host}/{k}",
                )
                for k in chunks
                if k.startswith(manifest_id)
            ]
        except Exception:
            return []

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            async with session.get(
                f"http://{self._wpad_host}:{self._wpad_port}/wpad.dat"
            ) as resp:
                return resp.status == 200
        except Exception:
            return False
