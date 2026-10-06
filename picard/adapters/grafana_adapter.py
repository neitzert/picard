"""Grafana adapter — stores chunks as dashboard annotations.

Annotations are a legitimate Grafana feature for marking events on
dashboards.  Chunk data hides in the annotation text field, tagged
for retrieval.  Who audits annotation content?

Requires: pip install picard[grafana]  (aiohttp)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class GrafanaAdapter(Adapter):
    """Scatter chunks into Grafana annotations."""

    def __init__(
        self,
        url: str,
        api_key: str,
        *,
        dashboard_id: int | None = None,
        max_chunk_bytes: int = 65_536,
    ) -> None:
        self._url = url.rstrip("/")
        self._api_key = api_key
        self._dashboard_id = dashboard_id
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="grafana",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=100,
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _tags(self, manifest_id: str, chunk_key: str) -> list[str]:
        return ["picard", f"mid:{manifest_id}", f"ckey:{chunk_key}"]

    def _chunk_key(self, encoded_chunk: str) -> tuple[str, str]:
        try:
            chunk = Chunk.decode(encoded_chunk)
            key = f"b{chunk.block_index}_c{chunk.chunk_index}"
            return chunk.manifest_id, key
        except ValueError:
            uid = uuid.uuid4().hex
            return uid, uid

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import aiohttp

        manifest_id, chunk_key = self._chunk_key(encoded_chunk)
        body: dict = {
            "text": encoded_chunk,
            "tags": self._tags(manifest_id, chunk_key),
            "time": 0,
            "timeEnd": 0,
        }
        if self._dashboard_id is not None:
            body["dashboardId"] = self._dashboard_id

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self._url}/api/annotations",
                json=body,
                headers=self._headers(),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
                ann_id = data.get("id", chunk_key)

        return StoredChunkRef(
            adapter_name="grafana",
            location=f"grafana://{self._url}/annotations/{ann_id}",
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        import aiohttp

        ann_id = ref.location.rsplit("/", 1)[-1]
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self._url}/api/annotations/{ann_id}",
                headers=self._headers(),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
                text = data.get("text")
                if text is None:
                    raise KeyError(f"annotation not found: {ann_id}")
                return text

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        import aiohttp

        ann_id = ref.location.rsplit("/", 1)[-1]
        try:
            async with aiohttp.ClientSession() as session:
                async with session.delete(
                    f"{self._url}/api/annotations/{ann_id}",
                    headers=self._headers(),
                ) as resp:
                    return resp.status < 400
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        import aiohttp

        params = {"tags": ["picard", f"mid:{manifest_id}"], "limit": 5000}
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self._url}/api/annotations",
                params=params,
                headers=self._headers(),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
        return [
            StoredChunkRef(
                adapter_name="grafana",
                location=f"grafana://{self._url}/annotations/{ann['id']}",
            )
            for ann in data
        ]

    async def health_check(self) -> bool:
        import aiohttp

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self._url}/api/health",
                    headers=self._headers(),
                ) as resp:
                    return resp.status == 200
        except Exception:
            return False
