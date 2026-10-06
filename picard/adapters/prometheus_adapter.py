"""Prometheus adapter -- stores chunks as metrics via Pushgateway.

Each chunk becomes a gauge metric with the data base64-encoded in a
label value.  Read-back queries the Prometheus server's HTTP API.

Requires: pip install picard[prometheus]  (aiohttp)
"""

from __future__ import annotations

import base64
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class PrometheusAdapter(Adapter):
    """Scatter chunks as Prometheus metrics via Pushgateway."""

    def __init__(
        self,
        pushgateway_url: str,
        *,
        job_name: str = "picard",
        query_url: str | None = None,
        max_chunk_bytes: int = 4096,
    ) -> None:
        self._pushgateway_url = pushgateway_url.rstrip("/")
        self._job_name = job_name
        self._query_url = query_url.rstrip("/") if query_url else None
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="prometheus",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=300,
        )

    def _metric_body(self, group_key: str, b64_data: str) -> str:
        return (
            f"# TYPE picard_chunk gauge\n"
            f'picard_chunk{{group="{group_key}",data="{b64_data}"}} 1\n'
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import aiohttp
        from picard.chunker import Chunk

        try:
            chunk = Chunk.decode(encoded_chunk)
            group_key = (
                f"{chunk.manifest_id}/{chunk.block_index}_{chunk.chunk_index}"
            )
        except ValueError:
            group_key = uuid.uuid4().hex

        b64_data = base64.b64encode(encoded_chunk.encode()).decode()
        body = self._metric_body(group_key, b64_data)
        url = (
            f"{self._pushgateway_url}/metrics/job/{self._job_name}"
            f"/group/{group_key}"
        )

        async with aiohttp.ClientSession() as session:
            async with session.put(
                url,
                data=body,
                headers={"Content-Type": "text/plain"},
            ) as resp:
                resp.raise_for_status()

        location = f"prom://{self._job_name}/{group_key}"
        return StoredChunkRef(adapter_name="prometheus", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if not self._query_url:
            raise LookupError(
                "prometheus read requires query_url (Prometheus server)"
            )

        import aiohttp

        parts = ref.location.split("/", 2)
        group_key = parts[2] if len(parts) > 2 else parts[-1]

        query = f'picard_chunk{{group="{group_key}"}}'
        url = f"{self._query_url}/api/v1/query"

        async with aiohttp.ClientSession() as session:
            async with session.get(url, params={"query": query}) as resp:
                resp.raise_for_status()
                result = await resp.json()
                data = result["data"]["result"]
                if not data:
                    raise KeyError(f"metric not found: {group_key}")
                b64 = data[0]["metric"]["data"]
                return base64.b64decode(b64).decode()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        import aiohttp

        parts = ref.location.split("/", 2)
        group_key = parts[2] if len(parts) > 2 else parts[-1]
        url = (
            f"{self._pushgateway_url}/metrics/job/{self._job_name}"
            f"/group/{group_key}"
        )

        try:
            async with aiohttp.ClientSession() as session:
                async with session.delete(url) as resp:
                    return resp.status in (200, 202)
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        if not self._query_url:
            return []

        import aiohttp

        query = f'picard_chunk{{group=~"{manifest_id}/.*"}}'
        url = f"{self._query_url}/api/v1/query"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params={"query": query}) as resp:
                    resp.raise_for_status()
                    result = await resp.json()
                    return [
                        StoredChunkRef(
                            adapter_name="prometheus",
                            location=f"prom://{self._job_name}/{r['metric']['group']}",
                        )
                        for r in result["data"]["result"]
                    ]
        except Exception:
            return []

    async def health_check(self) -> bool:
        import aiohttp

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self._pushgateway_url}/-/healthy"
                ) as resp:
                    return resp.status == 200
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        return {
            "used_bytes": None,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": None,
        }
