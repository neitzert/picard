"""Syslog adapter -- stores chunks as syslog messages (RFC 5424).

Chunks are sent as structured-data in syslog messages.  Read-back
requires a log aggregation API (Elasticsearch, Loki, Splunk) since
syslog itself is write-only.

Requires: pip install picard[syslog]  (aiohttp for read-back API)
"""

from __future__ import annotations

import asyncio
import base64
import json
import socket
import uuid
from datetime import datetime, timezone
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class SyslogAdapter(Adapter):
    """Scatter chunks into syslog messages with API-based read-back."""

    def __init__(
        self,
        host: str,
        *,
        port: int = 514,
        protocol: str = "udp",
        app_name: str = "picard",
        read_api_url: str | None = None,
        max_chunk_bytes: int = 2048,
    ) -> None:
        self._host = host
        self._port = port
        self._protocol = protocol.lower()
        self._app_name = app_name
        self._read_api_url = read_api_url
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="syslog",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=1000,
        )

    def _make_rfc5424(self, msg_id: str, data: str) -> str:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        hostname = socket.gethostname()
        sd = f'[picard@0 msgid="{msg_id}" data="{data}"]'
        return (
            f"<134>1 {ts} {hostname} {self._app_name} "
            f"- {msg_id} {sd} chunk"
        )

    def _send_syslog(self, message: str) -> None:
        encoded = message.encode("utf-8")
        if self._protocol == "tcp":
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(10)
                s.connect((self._host, self._port))
                s.sendall(encoded + b"\n")
        else:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(10)
                s.sendto(encoded, (self._host, self._port))

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        from picard.chunker import Chunk

        try:
            chunk = Chunk.decode(encoded_chunk)
            msg_id = f"{chunk.manifest_id}_{chunk.block_index}_{chunk.chunk_index}"
        except ValueError:
            msg_id = uuid.uuid4().hex

        b64_data = base64.b64encode(encoded_chunk.encode()).decode()
        message = self._make_rfc5424(msg_id, b64_data)

        await asyncio.to_thread(self._send_syslog, message)
        self._cache[msg_id] = encoded_chunk
        location = f"syslog://{self._host}/{msg_id}"
        return StoredChunkRef(adapter_name="syslog", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        msg_id = ref.location.rsplit("/", 1)[-1]

        if msg_id in self._cache:
            return self._cache[msg_id]

        if not self._read_api_url:
            raise LookupError(
                "syslog read-back requires read_api_url (Elasticsearch/Loki)"
            )

        import aiohttp

        query = {"query": {"match": {"syslog_msgid": msg_id}}}
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self._read_api_url}/_search",
                json=query,
                headers={"Content-Type": "application/json"},
            ) as resp:
                resp.raise_for_status()
                result = await resp.json()
                hits = result.get("hits", {}).get("hits", [])
                if not hits:
                    raise KeyError(f"syslog message not found: {msg_id}")
                sd = hits[0]["_source"].get("syslog_sd", "")
                data = _extract_sd_field(sd, "data")
                return base64.b64decode(data).decode()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        msg_id = ref.location.rsplit("/", 1)[-1]
        return self._cache.pop(msg_id, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        prefix = f"{manifest_id}_"
        return [
            StoredChunkRef(
                adapter_name="syslog",
                location=f"syslog://{self._host}/{mid}",
            )
            for mid in self._cache
            if mid.startswith(prefix)
        ]

    async def health_check(self) -> bool:
        try:
            msg = self._make_rfc5424("health", "cGluZw==")
            await asyncio.to_thread(self._send_syslog, msg)
            return True
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        used = sum(len(v.encode()) for v in self._cache.values())
        return {
            "used_bytes": used,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": len(self._cache),
        }


def _extract_sd_field(sd_str: str, field: str) -> str:
    marker = f'{field}="'
    start = sd_str.find(marker)
    if start < 0:
        raise ValueError(f"field {field!r} not found in structured data")
    start += len(marker)
    end = sd_str.index('"', start)
    return sd_str[start:end]
