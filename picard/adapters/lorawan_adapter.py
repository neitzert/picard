"""LoRaWAN adapter — stores chunks as downlink queue payloads.

Sends chunk data to LoRaWAN devices via the application server API
(e.g. ChirpStack, The Things Network).  Range: kilometers.
Bandwidth: bytes.

Requires: pip install picard[lorawan]  (aiohttp)
"""

from __future__ import annotations

import base64
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class LorawanAdapter(Adapter):
    """Scatter chunks into LoRaWAN downlink queues."""

    def __init__(
        self,
        api_url: str,
        api_key: str,
        application_id: str,
        device_eui: str,
        *,
        f_port: int = 10,
        max_chunk_bytes: int = 242,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._api_key = api_key
        self._app_id = application_id
        self._dev_eui = device_eui
        self._f_port = f_port
        self._max = max_chunk_bytes
        self._written: dict[str, str] = {}
        self._f_cnt = 0
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
            name="lorawan",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=True,
            rate_limit_per_min=6,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        try:
            chunk = Chunk.decode(encoded_chunk)
            key = f"{chunk.manifest_id}/{chunk.block_index}/{chunk.chunk_index}"
        except ValueError:
            key = uuid.uuid4().hex

        payload_b64 = base64.b64encode(encoded_chunk.encode()).decode()
        self._f_cnt += 1

        session = await self._get_session()
        url = f"{self._api_url}/api/devices/{self._dev_eui}/queue"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "deviceQueueItem": {
                "data": payload_b64,
                "fPort": self._f_port,
                "confirmed": False,
            }
        }
        async with session.post(url, json=body, headers=headers) as resp:
            resp.raise_for_status()
            data = await resp.json()

        loc = f"lora://{self._app_id}/{self._dev_eui}/{self._f_cnt}"
        self._written[loc] = encoded_chunk
        return StoredChunkRef(adapter_name="lorawan", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location in self._written:
            return self._written[ref.location]

        session = await self._get_session()
        url = f"{self._api_url}/api/devices/{self._dev_eui}/queue"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        async with session.get(url, headers=headers) as resp:
            resp.raise_for_status()
            data = await resp.json()

        for item in data.get("deviceQueueItems", []):
            decoded = base64.b64decode(item["data"]).decode()
            if decoded == self._written.get(ref.location, ""):
                return decoded

        raise KeyError(f"chunk not found: {ref.location}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        if ref.location not in self._written:
            return False
        session = await self._get_session()
        url = f"{self._api_url}/api/devices/{self._dev_eui}/queue"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        async with session.delete(url, headers=headers) as resp:
            pass

        self._written.pop(ref.location, None)
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="lorawan", location=loc)
            for loc in self._written
            if manifest_id in loc
        ]

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            url = f"{self._api_url}/api/devices/{self._dev_eui}"
            headers = {"Authorization": f"Bearer {self._api_key}"}
            async with session.get(url, headers=headers) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        used = sum(len(v.encode()) for v in self._written.values())
        return {
            "used_bytes": used,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": len(self._written),
        }
