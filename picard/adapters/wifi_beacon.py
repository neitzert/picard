"""WiFi beacon adapter — stores chunks in 802.11 beacon frame Information Elements.

Broadcasts chunk data as vendor-specific Information Elements in WiFi
beacon frames.  Every WiFi-capable device within radio range receives
the data — a broadcast storage channel.

Note: Transmitting beacon frames requires an RF-capable interface in
monitor/injection mode.  Comply with local radio regulations (FCC Part 15,
ETSI EN 300 328, etc.) for your jurisdiction.

Requires: pip install picard-store[wifi]  (scapy), monitor-mode wireless interface
"""

from __future__ import annotations

import asyncio
import subprocess
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

try:
    from scapy.all import (
        Dot11,
        Dot11Beacon,
        Dot11Elt,
        RadioTap,
        sendp,
        sniff,
    )
except ImportError:
    Dot11 = Dot11Beacon = Dot11Elt = RadioTap = sendp = sniff = None


_PICARD_OUI = b"\x02\x50\x43"  # locally administered OUI (bit 1 of first octet set)


class WifiBeaconAdapter(Adapter):
    """Scatter chunks into WiFi beacon frame IEs."""

    def __init__(
        self,
        interface: str,
        *,
        channel: int = 6,
        bssid: str = "02:50:43:41:52:44",
        max_chunk_bytes: int = 255,
    ) -> None:
        self._interface = interface
        self._channel = channel
        self._bssid = bssid
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="wifi-beacon",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=600,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:

        try:
            from picard.chunker import Chunk
            chunk = Chunk.decode(encoded_chunk)
            chunk_id = f"{chunk.manifest_id}/{chunk.block_index}/{chunk.chunk_index}"
        except (ValueError, Exception):
            chunk_id = uuid.uuid4().hex

        data_bytes = encoded_chunk.encode("utf-8")[: self._max]

        def _broadcast():
            dot11 = Dot11(
                type=0,
                subtype=8,
                addr1="ff:ff:ff:ff:ff:ff",
                addr2=self._bssid,
                addr3=self._bssid,
            )
            beacon = Dot11Beacon(cap="ESS")
            ssid = Dot11Elt(ID=0, info=f"PICARD-{chunk_id[:24]}")
            vendor_ie = Dot11Elt(
                ID=221,
                info=_PICARD_OUI + data_bytes,
            )
            frame = RadioTap() / dot11 / beacon / ssid / vendor_ie
            sendp(frame, iface=self._interface, count=3, inter=0.1, verbose=False)

        await asyncio.to_thread(_broadcast)
        location = f"wifi://{self._interface}/{self._bssid}/{chunk_id}"
        self._cache[location] = encoded_chunk
        return StoredChunkRef(adapter_name="wifi-beacon", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        val = self._cache.get(ref.location)
        if val is not None:
            return val

        target_id = ref.location.split("/")[-1]

        def _sniff():
            def _filter(pkt):
                if pkt.haslayer(Dot11Elt):
                    elt = pkt.getlayer(Dot11Elt)
                    while elt:
                        if elt.ID == 221 and elt.info[:3] == _PICARD_OUI:
                            return True
                        elt = elt.payload.getlayer(Dot11Elt)
                return False

            packets = sniff(
                iface=self._interface,
                lfilter=_filter,
                count=1,
                timeout=5,
            )
            if not packets:
                raise KeyError(f"beacon not found: {ref.location}")
            elt = packets[0].getlayer(Dot11Elt)
            while elt:
                if elt.ID == 221 and elt.info[:3] == _PICARD_OUI:
                    return elt.info[3:].decode("utf-8")
                elt = elt.payload.getlayer(Dot11Elt)
            raise KeyError(f"beacon not found: {ref.location}")

        return await asyncio.to_thread(_sniff)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return self._cache.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="wifi-beacon", location=loc)
            for loc in self._cache
            if manifest_id in loc
        ]

    async def health_check(self) -> bool:
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["iwconfig", self._interface],
                capture_output=True,
                text=True,
            )
            return "Monitor" in result.stdout or "Mode:" in result.stdout
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
