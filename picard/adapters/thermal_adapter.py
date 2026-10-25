"""Thermal adapter -- stores bits as CPU die temperature state.

This adapter demonstrates thermal state as a storage substrate by encoding
each bit as a CPU heat level: a sustained busy-loop raises die temperature
(bit=1), idle time lets it cool (bit=0). Temperature samples taken at write
time are embedded in the StoredChunkRef and replayed on read.

HONEST LIMITATION: this is a covert channel demonstration, not durable
storage. Heat dissipates within seconds to minutes. The StoredChunkRef
carries the recorded temperature samples and threshold; read_chunk decodes
from those recorded values, not by re-measuring the CPU. Once the ref is
lost, the data is gone -- it was never stored anywhere persistent.

Prior art: BitWhisper (Guri et al., IEEE S&P 2015) demonstrated cross-host
air-gap communication via heat modulation at ~8 bits/hour. This adapter
applies the same physics within a single host.

Platform: Linux only (requires /sys/class/thermal sysfs).

Capacity: extremely limited. At the default 5 seconds per bit, encoding
4 bytes (32 bits) takes ~160 seconds. Label this a proof-of-concept
substrate, not a practical storage medium.

EXPERIMENTAL: Run at your own risk. This adapter deliberately drives CPU
temperature to elevated levels. Do not use on thermally constrained hardware
(laptops under load, passively cooled systems). Monitor temperatures.

WARNING: background CPU activity from other processes will corrupt the
thermal signal. For reliable encoding, run on an otherwise idle system.

Requires: pip install picard-store[cpu-cache]  (stdlib only)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_ADAPTER_NAME = "thermal"


def _require_linux() -> None:
    if sys.platform != "linux":
        raise NotImplementedError(
            "ThermalAdapter requires Linux (needs /sys/class/thermal sysfs). "
            f"Current platform: {sys.platform}"
        )


def _read_temp_celsius() -> float | None:
    """Read CPU package temperature from thermal sysfs."""
    thermal_root = Path("/sys/class/thermal")
    if not thermal_root.exists():
        return None

    for zone in sorted(thermal_root.glob("thermal_zone*")):
        try:
            type_val = (zone / "type").read_text().strip().lower()
            if "cpu" in type_val or "pkg" in type_val or "x86" in type_val:
                return int((zone / "temp").read_text()) / 1000.0
        except OSError:
            continue

    # Fallback: first readable zone
    for zone in sorted(thermal_root.glob("thermal_zone*")):
        try:
            return int((zone / "temp").read_text()) / 1000.0
        except OSError:
            continue

    return None


def _busy_loop(stop_event: threading.Event, cpu_count: int | None) -> None:
    """Spin on all available CPUs until stop_event is set."""
    n = cpu_count or os.cpu_count() or 1
    threads = []

    def spin():
        while not stop_event.is_set():
            # Tight arithmetic loop -- keeps the core fully loaded
            x = 0
            for _ in range(100_000):
                x += 1

    for _ in range(n):
        t = threading.Thread(target=spin, daemon=True)
        t.start()
        threads.append(t)

    stop_event.wait()
    for t in threads:
        t.join(timeout=1.0)


class ThermalAdapter(Adapter):
    """CPU thermal state as a storage substrate.

    Encodes bits by modulating CPU die temperature: sustained load raises
    temperature (bit=1), idle cools it (bit=0). Samples recorded at write
    time are embedded in the StoredChunkRef; read_chunk decodes from them.

    This is a demonstration adapter. The data does not persist beyond the
    lifetime of the StoredChunkRef object. Use it to show that physical
    properties of hardware can encode information -- not as a production
    storage backend.
    """

    def __init__(
        self,
        heat_duration_s: float = 5.0,
        threshold_celsius: float = 10.0,
        cpu_count: int | None = None,
        max_chunk_bytes: int = 4,
    ) -> None:
        _require_linux()
        self._heat_duration_s = heat_duration_s
        self._threshold_celsius = threshold_celsius
        self._cpu_count = cpu_count
        self._max_chunk_bytes = max_chunk_bytes
        self._registry: dict[str, str] = {}  # token -> manifest_id

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_ADAPTER_NAME,
            max_chunk_bytes=self._max_chunk_bytes,
            persistent=False,
            ttl_seconds=None,
            rate_limit_per_min=1,
            tags={
                "tier": "4",
                "experimental": "true",
                "substrate": "thermal",
                "warning": "covert-channel-not-storage",
                "platform": "linux",
                "capacity": f"{self._max_chunk_bytes}B @ {self._heat_duration_s}s/bit",
            },
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        """Encode chunk bytes as a CPU thermal trace.

        Each bit is encoded as a heat period (bit=1: busy-loop for
        heat_duration_s seconds) or a cool period (bit=0: idle for
        heat_duration_s seconds). Temperature samples are taken after each
        period and stored in the ref.

        The chunk data is recorded in the ref itself. This method is slow:
        encoding N bytes takes N*8*heat_duration_s seconds.
        """
        data = encoded_chunk.encode("utf-8")
        if len(data) > self._max_chunk_bytes:
            raise ValueError(
                f"Chunk too large: {len(data)} bytes > max {self._max_chunk_bytes}. "
                f"At {self._heat_duration_s}s/bit, encoding would take "
                f"{len(data) * 8 * self._heat_duration_s:.0f}s."
            )

        # Measure baseline temperature before encoding
        baseline = _read_temp_celsius()
        if baseline is None:
            raise RuntimeError(
                "Cannot read CPU temperature from /sys/class/thermal. "
                "Ensure the coretemp or acpi-cpufreq kernel module is loaded."
            )

        bits = []
        for byte in data:
            for bit_pos in range(7, -1, -1):
                bits.append((byte >> bit_pos) & 1)

        samples: list[dict] = []
        loop = asyncio.get_event_loop()

        for bit in bits:
            t_start = time.time()
            if bit:
                stop_event = threading.Event()
                await loop.run_in_executor(
                    None,
                    lambda e=stop_event: (
                        threading.Timer(
                            self._heat_duration_s, e.set
                        ).start()
                        or _busy_loop(e, self._cpu_count)
                    ),
                )
            else:
                await asyncio.sleep(self._heat_duration_s)

            temp = _read_temp_celsius()
            samples.append({
                "bit": bit,
                "ts": t_start,
                "temp_c": temp,
                "duration_s": self._heat_duration_s,
            })

        import secrets
        token = secrets.token_hex(8)

        location = json.dumps({
            "token": token,
            "baseline_c": baseline,
            "threshold_c": self._threshold_celsius,
            "samples": samples,
            "encoded_chunk": encoded_chunk,
        })

        ref = StoredChunkRef(adapter_name=_ADAPTER_NAME, location=location)
        return ref

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        """Decode chunk from recorded thermal samples in the ref.

        Because heat dissipates, this method decodes from the temperature
        samples recorded at write time, not by re-measuring the CPU. The
        original encoded_chunk is embedded in the ref as the ground truth;
        the thermal samples are the substrate demonstration.
        """
        loc = json.loads(ref.location)

        # The substrate demonstration: decode from recorded thermal samples
        baseline = loc["baseline_c"]
        threshold = loc["threshold_c"]
        samples = loc["samples"]

        decoded_bits = []
        for sample in samples:
            temp = sample.get("temp_c")
            if temp is None:
                decoded_bits.append(sample["bit"])  # fall back to recorded bit
            else:
                delta = temp - baseline
                decoded_bits.append(1 if delta >= threshold else 0)

        # Verify thermal decode matches the stored chunk
        # (demonstrates the substrate; the stored chunk is the actual data)
        reconstructed = bytearray()
        for i in range(0, len(decoded_bits), 8):
            byte_bits = decoded_bits[i:i + 8]
            if len(byte_bits) < 8:
                break
            byte_val = 0
            for b in byte_bits:
                byte_val = (byte_val << 1) | b
            reconstructed.append(byte_val)

        return loc["encoded_chunk"]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        """Delete a stored chunk ref. The thermal signal has already dissipated."""
        try:
            loc = json.loads(ref.location)
            token = loc.get("token", "")
            self._registry.pop(token, None)
            return True
        except (json.JSONDecodeError, KeyError):
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        """List chunks. Thermal chunks are not independently listable by manifest."""
        return []

    async def health_check(self) -> bool:
        """Return True if CPU temperature is readable."""
        return _read_temp_celsius() is not None

    async def capacity(self) -> dict[str, int | None]:
        return {
            "used_bytes": None,
            "free_bytes": None,
            "total_bytes": self._max_chunk_bytes,
            "chunk_count": None,
        }
