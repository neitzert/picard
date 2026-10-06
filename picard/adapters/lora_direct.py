"""Direct LoRa radio adapter — stores chunks via raw LoRa modulation.

Not LoRaWAN (the network protocol) — raw LoRa PHY.  A $5 SX1276 module
on a serial port gives you 10+ km range at low data rates.  No network
infrastructure, no registration, no logs.  ISM band (915 MHz US, 868 MHz EU).

Requires: pip install pyserial + a LoRa module (SX1276/SX1262) on UART
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class LoRaDirectAdapter(Adapter):
    """Scatter chunks via raw LoRa radio packets."""

    def __init__(
        self,
        *,
        serial_port: str = "/dev/ttyUSB0",
        baudrate: int = 115200,
        frequency: int = 915_000_000,
        spreading_factor: int = 7,
        bandwidth: int = 125_000,
        tx_power: int = 14,
        max_chunk_bytes: int = 255,
    ) -> None:
        self._port = serial_port
        self._baudrate = baudrate
        self._freq = frequency
        self._sf = spreading_factor
        self._bw = bandwidth
        self._tx_power = tx_power
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}
        self._pkt_counter = 0

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="lora-direct",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=10,
        )

    def _configure_cmd(self) -> list[str]:
        return [
            f"AT+FREQ={self._freq}",
            f"AT+SF={self._sf}",
            f"AT+BW={self._bw}",
            f"AT+TXPW={self._tx_power}",
        ]

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        from picard.chunker import Chunk

        try:
            chunk = Chunk.decode(encoded_chunk)
            key = f"{chunk.manifest_id}/{chunk.block_index}/{chunk.chunk_index}"
        except ValueError:
            key = uuid.uuid4().hex

        self._pkt_counter += 1
        pkt_id = self._pkt_counter

        def _write():
            import serial

            ser = serial.Serial(self._port, self._baudrate, timeout=2.0)
            try:
                for cmd in self._configure_cmd():
                    ser.write(f"{cmd}\r\n".encode())
                    ser.readline()

                data = encoded_chunk.encode()
                header = f"PICARD:{pkt_id}:{len(data)}:"
                payload = header.encode() + data
                ser.write(f"AT+SEND={len(payload)}\r\n".encode())
                ser.readline()
                ser.write(payload)
                ser.readline()
            finally:
                ser.close()

        await asyncio.to_thread(_write)
        loc = f"lora://{self._freq}/{self._sf}/{pkt_id}"
        self._cache[loc] = encoded_chunk
        return StoredChunkRef(adapter_name="lora-direct", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        cached = self._cache.get(ref.location)
        if cached is not None:
            return cached

        def _read():
            import serial

            ser = serial.Serial(self._port, self._baudrate, timeout=5.0)
            try:
                for cmd in self._configure_cmd():
                    ser.write(f"{cmd}\r\n".encode())
                    ser.readline()

                ser.write(b"AT+RECV\r\n")
                while True:
                    line = ser.readline()
                    if not line:
                        break
                    text = line.decode(errors="replace").strip()
                    if text.startswith("PICARD:"):
                        parts = text.split(":", 3)
                        return parts[3] if len(parts) > 3 else ""
                raise KeyError(f"no packet received: {ref.location}")
            finally:
                ser.close()

        return await asyncio.to_thread(_read)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return self._cache.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="lora-direct", location=loc)
            for loc in self._cache
            if manifest_id in loc
        ]

    async def health_check(self) -> bool:
        def _check():
            import serial

            try:
                ser = serial.Serial(self._port, self._baudrate, timeout=2.0)
                ser.write(b"AT\r\n")
                resp = ser.readline()
                ser.close()
                return b"OK" in resp
            except Exception:
                return False

        try:
            return await asyncio.to_thread(_check)
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
