"""Wi-Fi beacon vendor IE adapter -- encodes chunks as 802.11 vendor IEs.

Data is encoded as a vendor-specific Information Element (IE type 0xDD) in
802.11 beacon frames. The adapter stores chunks in-process and generates
hostapd.conf snippets with the vendor_elements field populated.

WARNING: Deploying via hostapd broadcasts chunk data to every 802.11 client
in radio range. Use only on isolated lab networks.

Requires: hostapd (apt install hostapd) and a wireless interface in AP mode.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

_VENDOR_OUI = "f09fc2"
_VENDOR_TYPE = "01"


def _build_vendor_ie(payload_hex: str) -> str:
    total_len = (len(payload_hex) // 2) + len(_VENDOR_OUI) // 2 + len(_VENDOR_TYPE) // 2
    return f"dd{total_len:02x}{_VENDOR_OUI}{_VENDOR_TYPE}{payload_hex}"


class WifiBeaconAdapter(Adapter):
    """Store chunks as 802.11 vendor IEs (hostapd vendor_elements)."""

    def __init__(
        self,
        *,
        interface: str = "wlan0",
        channel: int = 6,
        ssid: str = "PICARD-TEST",
        max_chunk_bytes: int = 220,
    ) -> None:
        self._interface = interface
        self._channel = channel
        self._ssid = ssid
        self._max = max_chunk_bytes
        self._registry: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="wifi_beacon",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=10,
            tags={
                "requires": "Linux,root,hostapd,wifi-interface",
                "substrate": "wifi-beacon-vendor-ie",
                "WARNING": "broadcasts-to-all-wifi-clients",
            },
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        if len(encoded_chunk.encode()) > self._max:
            raise ValueError(
                f"Chunk too large: {len(encoded_chunk.encode())} bytes > {self._max}"
            )
        try:
            manifest_id = Chunk.decode(encoded_chunk).manifest_id
        except Exception:
            manifest_id = ""

        token = uuid.uuid4().hex[:16]
        payload_hex = encoded_chunk.encode().hex()
        vendor_ie = _build_vendor_ie(payload_hex)

        location = json.dumps({
            "token": token,
            "vendor_ie": vendor_ie,
            "iface": self._interface,
            "manifest_id": manifest_id,
        }, separators=(",", ":"))

        self._registry[token] = location
        return StoredChunkRef(adapter_name="wifi_beacon", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        try:
            loc = json.loads(ref.location)
            token = loc["token"]
        except Exception:
            raise KeyError(f"wifi_beacon: malformed location: {ref.location[:60]}")

        if token not in self._registry:
            raise KeyError(f"wifi_beacon chunk not found: {token}")

        loc = json.loads(ref.location)
        vendor_ie = loc["vendor_ie"]
        # Header is always: "dd" + 2-char length + 6-char OUI + 2-char type = 12 chars
        header_len = 2 + 2 + len(_VENDOR_OUI) + len(_VENDOR_TYPE)
        payload_hex = vendor_ie[header_len:]
        return bytes.fromhex(payload_hex).decode()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        try:
            loc = json.loads(ref.location)
            token = loc["token"]
        except Exception:
            return False
        return self._registry.pop(token, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        for _, loc_str in self._registry.items():
            try:
                loc = json.loads(loc_str)
                if loc.get("manifest_id") == manifest_id:
                    refs.append(StoredChunkRef(adapter_name="wifi_beacon", location=loc_str))
            except Exception:
                continue
        return refs

    async def health_check(self) -> bool:
        if shutil.which("hostapd") is None:
            return False
        iface_path = f"/sys/class/net/{self._interface}"
        return os.path.exists(iface_path)

    def generate_hostapd_conf(self, ref: StoredChunkRef) -> str:
        try:
            loc = json.loads(ref.location)
            vendor_ie = loc["vendor_ie"]
        except Exception:
            raise ValueError("cannot parse vendor_ie from ref.location")

        return (
            f"interface={self._interface}\n"
            f"driver=nl80211\n"
            f"ssid={self._ssid}\n"
            f"hw_mode=g\n"
            f"channel={self._channel}\n"
            f"vendor_elements={vendor_ie}\n"
        )
