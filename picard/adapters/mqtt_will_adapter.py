"""MQTT LWT adapter -- stores chunks in MQTT Last Will and Testament payloads.

Each write_chunk encodes the chunk as the LWT payload in a synthetic MQTT
CONNECT packet (MQTT 3.1.1, RFC 8320). The packet bytes are the substrate;
data is also held in-process registry. No broker required. Stdlib only.

MQTT CONNECT fixed header: 0x10 + remaining_length
Variable header: protocol name + level + flags + keepalive
Payload: client_id + will_topic + will_message
"""
from __future__ import annotations

import asyncio
import json
import struct
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "mqtt_will"


def _mqtt_str(s: str) -> bytes:
    enc = s.encode("utf-8")
    return struct.pack(">H", len(enc)) + enc


def _encode_remaining_length(n: int) -> bytes:
    result = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        if n:
            byte |= 0x80
        result.append(byte)
        if not n:
            break
    return bytes(result)


def _make_mqtt_connect(client_id: str, will_topic: str, will_payload: bytes) -> bytes:
    # Connect flags: CleanSession=1, WillFlag=1, WillQoS=0, WillRetain=0
    flags = 0b00000110  # CleanSession + WillFlag
    var_header = (
        _mqtt_str("MQTT")    # protocol name
        + b"\x04"            # protocol level 4 = MQTT 3.1.1
        + bytes([flags])     # connect flags
        + struct.pack(">H", 60)  # keepalive = 60s
    )
    payload = (
        _mqtt_str(client_id)
        + _mqtt_str(will_topic)
        + struct.pack(">H", len(will_payload)) + will_payload
    )
    remaining = var_header + payload
    return bytes([0x10]) + _encode_remaining_length(len(remaining)) + remaining


def _extract_will_payload(pkt: bytes) -> bytes:
    """Extract will payload from MQTT CONNECT packet."""
    # Skip fixed header (1 + variable length encoding)
    pos = 1
    multiplier = 1
    remaining_len = 0
    while True:
        byte = pkt[pos]
        pos += 1
        remaining_len += (byte & 0x7F) * multiplier
        multiplier *= 128
        if not (byte & 0x80):
            break
    # Variable header: protocol name (2+4), level (1), flags (1), keepalive (2) = 10 bytes
    pos += 10
    # Payload: client_id (2 + N), will_topic (2 + M), will_payload (2 + L)
    cid_len = struct.unpack_from(">H", pkt, pos)[0]; pos += 2 + cid_len
    topic_len = struct.unpack_from(">H", pkt, pos)[0]; pos += 2 + topic_len
    payload_len = struct.unpack_from(">H", pkt, pos)[0]; pos += 2
    return pkt[pos:pos + payload_len]


class MqttWillAdapter(Adapter):

    def __init__(self) -> None:
        self._reg: dict[str, tuple[bytes, str]] = {}  # token -> (pkt, chunk)

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=False,
            ordered=False,
            tags={"requires": "stdlib", "substrate": "mqtt-lwt-payload"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        def _write():
            token = uuid.uuid4().hex[:16]
            client_id = f"picard-{token[:8]}"
            will_topic = f"picard/chunks/{token}"
            will_payload = encoded_chunk.encode("utf-8")
            pkt = _make_mqtt_connect(client_id, will_topic, will_payload)
            return token, pkt

        token, pkt = await asyncio.to_thread(_write)
        self._reg[token] = (pkt, encoded_chunk)
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        token = json.loads(ref.location)["token"]
        if token not in self._reg:
            raise KeyError(f"mqtt_will chunk not in registry: {token}")
        pkt, _ = self._reg[token]
        return _extract_will_payload(pkt).decode("utf-8")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        token = json.loads(ref.location)["token"]
        self._reg.pop(token, None)
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        from picard.chunker import Chunk
        refs = []
        for token, (_, data) in self._reg.items():
            try:
                if Chunk.decode(data).manifest_id == manifest_id:
                    refs.append(StoredChunkRef(adapter_name=_NAME,
                                               location=json.dumps({"token": token})))
            except Exception:
                continue
        return refs

    async def health_check(self) -> bool:
        try:
            pkt = _make_mqtt_connect("picard-hc", "picard/hc", b"test")
            return _extract_will_payload(pkt) == b"test"
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
