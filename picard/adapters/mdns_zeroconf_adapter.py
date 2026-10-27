"""mDNS TXT record adapter (zeroconf) -- stores chunks as _picard._tcp.local. services.

Each chunk is registered as an mDNS service with TXT records on the local network.
LAN-visible: any host on the segment running a zeroconf browser will see these services.

Requires: pip install zeroconf>=0.38

See also: picard/adapters/mdns.py (raw UDP, no library dependency).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

_SERVICE_TYPE = "_picard._tcp.local."


class MdnsZeroconfAdapter(Adapter):
    """Scatter chunks as mDNS service TXT records via the zeroconf library."""

    def __init__(
        self,
        *,
        max_chunk_bytes: int = 1024,
    ) -> None:
        self._max = max_chunk_bytes
        self._zc = None
        self._registry: dict[str, tuple[object, str]] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="mdns_zeroconf",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=60,
            tags={
                "requires": "zeroconf>=0.38",
                "substrate": "mdns-txt-record",
                "visibility": "LAN",
            },
        )

    def _ensure_zc(self):
        if self._zc is not None:
            return self._zc
        try:
            from zeroconf import Zeroconf
        except ImportError:
            raise ImportError(
                "zeroconf is required: pip install 'zeroconf>=0.38'"
            )
        self._zc = Zeroconf()
        return self._zc

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        try:
            manifest_id = Chunk.decode(encoded_chunk).manifest_id
        except Exception:
            manifest_id = ""

        token = uuid.uuid4().hex[:16]
        service_name = f"picard-{token}.{_SERVICE_TYPE}"

        try:
            from zeroconf import ServiceInfo
            zc = self._ensure_zc()
            info = ServiceInfo(
                type_=_SERVICE_TYPE,
                name=service_name,
                port=0,
                properties={
                    b"data": encoded_chunk.encode(),
                    b"manifest_id": manifest_id.encode(),
                },
                addresses=[b"\x7f\x00\x00\x01"],
            )
            await asyncio.to_thread(zc.register_service, info)
            self._registry[token] = (info, encoded_chunk)
        except ImportError:
            self._registry[token] = (None, encoded_chunk)

        return StoredChunkRef(adapter_name="mdns_zeroconf", location=f"mdns://{token}")

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        token = ref.location.removeprefix("mdns://")
        entry = self._registry.get(token)
        if entry is not None:
            return entry[1]

        try:
            from zeroconf import ServiceBrowser, Zeroconf
            zc = self._ensure_zc()
            service_name = f"picard-{token}.{_SERVICE_TYPE}"
            info = await asyncio.to_thread(
                zc.get_service_info, _SERVICE_TYPE, service_name, timeout=3000
            )
            if info and info.properties:
                data = info.properties.get(b"data")
                if data:
                    return data.decode()
        except Exception:
            pass

        raise KeyError(f"mdns_zeroconf chunk not found: {ref.location}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        token = ref.location.removeprefix("mdns://")
        entry = self._registry.pop(token, None)
        if entry is None:
            return False
        info, _ = entry
        if info is not None:
            try:
                zc = self._ensure_zc()
                await asyncio.to_thread(zc.unregister_service, info)
            except Exception:
                pass
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        for token, (_, data) in self._registry.items():
            try:
                if Chunk.decode(data).manifest_id == manifest_id:
                    refs.append(StoredChunkRef(adapter_name="mdns_zeroconf", location=f"mdns://{token}"))
            except Exception:
                continue
        return refs

    async def health_check(self) -> bool:
        try:
            from zeroconf import Zeroconf
            zc = Zeroconf()
            await asyncio.to_thread(zc.close)
            return True
        except ImportError:
            return False
        except Exception:
            return False

    async def close(self) -> None:
        if self._zc is not None:
            try:
                await asyncio.to_thread(self._zc.close)
            except Exception:
                pass
            self._zc = None
