"""Modbus adapter — stores chunks in PLC/RTU holding registers.

Each chunk is packed into 16-bit holding registers starting at a
configurable base address.  The first two registers store the data
length (big-endian uint32), followed by the raw chunk bytes padded
to register boundaries.

Requires: pip install picard[modbus]  (pymodbus)
"""

from __future__ import annotations

import asyncio
import struct
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class ModbusAdapter(Adapter):
    """Scatter chunks into Modbus holding registers on PLCs/RTUs."""

    def __init__(
        self,
        host: str,
        *,
        port: int = 502,
        unit_id: int = 1,
        start_register: int = 1000,
        max_chunk_bytes: int = 250,
    ) -> None:
        self._host = host
        self._port = port
        self._unit = unit_id
        self._start = start_register
        self._max = max_chunk_bytes
        self._alloc: dict[str, tuple[int, int]] = {}
        self._next_reg = start_register

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="modbus",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=60,
        )

    def _registers_needed(self, data_len: int) -> int:
        return 2 + (data_len + 1) // 2

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        from pymodbus.client import AsyncModbusTcpClient

        try:
            chunk = Chunk.decode(encoded_chunk)
            key = f"{chunk.manifest_id}/{chunk.block_index}/{chunk.chunk_index}"
        except ValueError:
            key = uuid.uuid4().hex

        raw = encoded_chunk.encode("utf-8")
        if len(raw) > self._max:
            raise ValueError(f"chunk too large: {len(raw)} > {self._max}")

        n_regs = self._registers_needed(len(raw))
        base_reg = self._next_reg
        self._next_reg += n_regs

        length_regs = list(struct.unpack(">HH", struct.pack(">I", len(raw))))
        data_regs = []
        padded = raw + b"\x00" * (len(raw) % 2)
        for i in range(0, len(padded), 2):
            data_regs.append(struct.unpack(">H", padded[i : i + 2])[0])

        values = length_regs + data_regs

        async def _write():
            client = AsyncModbusTcpClient(self._host, port=self._port)
            await client.connect()
            try:
                await client.write_registers(base_reg, values, slave=self._unit)
            finally:
                client.close()

        await _write()

        loc = f"modbus://{self._host}:{self._port}/{self._unit}/hr/{base_reg}-{base_reg + n_regs - 1}"
        self._alloc[loc] = (base_reg, n_regs)
        return StoredChunkRef(adapter_name="modbus", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        from pymodbus.client import AsyncModbusTcpClient

        base_reg, n_regs = self._alloc[ref.location]

        async def _read():
            client = AsyncModbusTcpClient(self._host, port=self._port)
            await client.connect()
            try:
                result = await client.read_holding_registers(
                    base_reg, n_regs, slave=self._unit
                )
                return result.registers
            finally:
                client.close()

        registers = await _read()
        length = struct.unpack(">I", struct.pack(">HH", registers[0], registers[1]))[0]
        data_bytes = b""
        for reg in registers[2:]:
            data_bytes += struct.pack(">H", reg)
        return data_bytes[:length].decode("utf-8")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        if ref.location not in self._alloc:
            return False
        from pymodbus.client import AsyncModbusTcpClient

        base_reg, n_regs = self._alloc.pop(ref.location)

        async def _zero():
            client = AsyncModbusTcpClient(self._host, port=self._port)
            await client.connect()
            try:
                await client.write_registers(
                    base_reg, [0] * n_regs, slave=self._unit
                )
            finally:
                client.close()

        try:
            await _zero()
        except Exception:
            pass
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="modbus", location=loc)
            for loc in self._alloc
            if manifest_id in loc
        ]

    async def health_check(self) -> bool:
        from pymodbus.client import AsyncModbusTcpClient

        try:
            client = AsyncModbusTcpClient(self._host, port=self._port)
            await client.connect()
            result = await client.read_holding_registers(
                self._start, 1, slave=self._unit
            )
            client.close()
            return not result.isError()
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        return {
            "used_bytes": sum(n * 2 for _, n in self._alloc.values()),
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": len(self._alloc),
        }
