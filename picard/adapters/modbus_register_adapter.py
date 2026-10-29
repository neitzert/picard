"""Modbus register adapter -- stores chunks in Modbus holding register values.

Each write_chunk encodes the chunk as 2-byte big-endian words stored in a
synthetic Modbus holding register map. Each register holds one uint16. The
register map is held in-process; cold reads reconstruct from the map.
No network required. Stdlib only.

Modbus holding registers are addressed 40001-49999 (0-based: 0-9998).
"""
from __future__ import annotations

import asyncio
import json
import struct
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "modbus_register"

_HOLDING_REG_BASE = 40001


def _pad(data: bytes, block: int) -> bytes:
    rem = len(data) % block
    return data + b"\x00" * (block - rem if rem else 0)


class ModbusRegisterAdapter(Adapter):

    def __init__(self) -> None:
        # registers: {token: {reg_addr: uint16_value}}
        self._reg: dict[str, tuple[dict[int, int], int, str]] = {}
        # token -> (register_map, orig_len, encoded_chunk)

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=False,
            ordered=True,
            tags={"requires": "stdlib", "substrate": "modbus-holding-registers"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        def _write():
            token = uuid.uuid4().hex[:16]
            raw = encoded_chunk.encode("utf-8")
            padded = _pad(raw, 2)
            reg_map: dict[int, int] = {}
            for i in range(0, len(padded), 2):
                reg_addr = _HOLDING_REG_BASE + (i // 2)
                value = struct.unpack(">H", padded[i:i+2])[0]
                reg_map[reg_addr] = value
            return token, reg_map, len(raw)

        token, reg_map, orig_len = await asyncio.to_thread(_write)
        self._reg[token] = (reg_map, orig_len, encoded_chunk)
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token, "orig_len": orig_len,
                               "n_regs": len(reg_map)}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token not in self._reg:
            raise KeyError(f"modbus_register chunk not in registry: {token}")
        reg_map, orig_len, _ = self._reg[token]
        n_regs = loc["n_regs"]
        raw = b""
        for i in range(n_regs):
            reg_addr = _HOLDING_REG_BASE + i
            value = reg_map.get(reg_addr, 0)
            raw += struct.pack(">H", value)
        return raw[:orig_len].decode("utf-8")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        token = json.loads(ref.location)["token"]
        self._reg.pop(token, None)
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        from picard.chunker import Chunk
        refs = []
        for token, (reg_map, orig_len, data) in self._reg.items():
            try:
                if Chunk.decode(data).manifest_id == manifest_id:
                    refs.append(StoredChunkRef(adapter_name=_NAME,
                                               location=json.dumps({"token": token,
                                                "orig_len": orig_len,
                                                "n_regs": len(reg_map)})))
            except Exception:
                continue
        return refs

    async def health_check(self) -> bool:
        try:
            raw = b"testtest"
            padded = _pad(raw, 2)
            reg_map: dict[int, int] = {}
            for i in range(0, len(padded), 2):
                reg_map[_HOLDING_REG_BASE + (i // 2)] = struct.unpack(">H", padded[i:i+2])[0]
            reconstructed = b"".join(struct.pack(">H", reg_map[_HOLDING_REG_BASE + i])
                                     for i in range(len(reg_map)))
            return reconstructed[:len(raw)] == raw
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
