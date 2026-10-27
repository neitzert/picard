"""CPU frequency adapter -- encodes bits via CPU P-state selection.

On hardware with cpufreq sysfs support, each CPU core's current frequency
is set to one of two available frequencies to encode a single bit. The
total capacity is (num_cores // 8) bytes.

On VMs and containers where cpufreq sysfs is absent, the adapter falls back
to embedding the chunk directly in the location JSON. The fallback is fully
functional and passes all ABC contract tests; it just has no novel substrate.

Requires: Linux. Real hardware mode additionally requires the
``userspace`` scaling governor and write access to
``/sys/devices/system/cpu/cpuN/cpufreq/scaling_setspeed`` (root).
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

_CPUFREQ_BASE = Path("/sys/devices/system/cpu/cpu0/cpufreq")
_AVAIL_FREQS = _CPUFREQ_BASE / "scaling_available_frequencies"


def _cpu_count() -> int:
    return os.cpu_count() or 1


def _available_freqs() -> list[int]:
    return sorted(int(f) for f in _AVAIL_FREQS.read_text().split())


def _set_freq(cpu: int, freq: int) -> None:
    path = Path(f"/sys/devices/system/cpu/cpu{cpu}/cpufreq/scaling_setspeed")
    path.write_text(str(freq))


def _get_freq(cpu: int) -> int:
    path = Path(f"/sys/devices/system/cpu/cpu{cpu}/cpufreq/scaling_cur_freq")
    return int(path.read_text().strip())


class CpuFreqAdapter(Adapter):
    """Scatter bits via CPU frequency P-state selection, with VM fallback."""

    def __init__(
        self,
        *,
        max_chunk_bytes: int = 256,
    ) -> None:
        if sys.platform != "linux":
            self._mode = "fallback"
        elif _AVAIL_FREQS.exists():
            self._mode = "cpufreq"
        else:
            self._mode = "fallback"

        self._fallback_max = max_chunk_bytes
        self._registry: dict[str, str] = {}

        if self._mode == "cpufreq":
            freqs = _available_freqs()
            self._freq_low = freqs[0]
            self._freq_high = freqs[-1]
            self._ncpus = _cpu_count()
            self._capacity_bytes = max(1, self._ncpus // 8)
        else:
            self._capacity_bytes = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        tags: dict[str, str] = {"requires": "Linux"}
        if self._mode == "cpufreq":
            tags["substrate"] = "cpu-freq-pstate"
            tags["requires"] = "Linux,root,userspace-governor"
        else:
            tags["substrate"] = "cpu-freq-vm-fallback"
            tags["fallback"] = "true"

        return AdapterProfile(
            name="cpu-freq",
            max_chunk_bytes=self._capacity_bytes,
            persistent=self._mode == "cpufreq",
            ordered=False,
            rate_limit_per_min=60 if self._mode == "cpufreq" else 1000,
            tags=tags,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        try:
            manifest_id = Chunk.decode(encoded_chunk).manifest_id
        except Exception:
            manifest_id = ""

        if self._mode == "fallback":
            token = uuid.uuid4().hex[:16]
            location = json.dumps(
                {"mode": "fallback", "token": token, "data": encoded_chunk},
                separators=(",", ":"),
            )
            self._registry[location] = manifest_id
            return StoredChunkRef(adapter_name="cpu-freq", location=location)

        data = encoded_chunk.encode()
        if len(data) > self._capacity_bytes:
            raise ValueError(
                f"chunk too large: {len(data)} bytes > {self._capacity_bytes} capacity"
            )

        bits: list[int] = []
        for byte in data:
            for shift in range(7, -1, -1):
                bits.append((byte >> shift) & 1)

        bits = bits[: self._ncpus]
        bits.extend([0] * (self._ncpus - len(bits)))

        try:
            for cpu, bit in enumerate(bits):
                freq = self._freq_high if bit else self._freq_low
                _set_freq(cpu, freq)
        except OSError as exc:
            raise PermissionError(f"cannot set CPU frequency: {exc}") from exc

        location = json.dumps(
            {"mode": "cpufreq", "n_bits": len(data) * 8, "ncpus": self._ncpus},
            separators=(",", ":"),
        )
        self._registry[location] = manifest_id
        return StoredChunkRef(adapter_name="cpu-freq", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location not in self._registry:
            raise KeyError(f"chunk not found: {ref.location[:80]}")

        loc = json.loads(ref.location)

        if loc.get("mode") == "fallback":
            return loc["data"]

        n_bits: int = loc["n_bits"]
        ncpus: int = loc["ncpus"]
        try:
            raw_bits = [
                1 if _get_freq(cpu) >= self._freq_high else 0
                for cpu in range(ncpus)
            ]
        except OSError as exc:
            raise KeyError(f"cannot read CPU frequencies: {exc}") from exc

        raw_bits = raw_bits[:n_bits]
        if len(raw_bits) % 8:
            raw_bits.extend([0] * (8 - len(raw_bits) % 8))

        result = bytearray()
        for i in range(0, len(raw_bits), 8):
            val = 0
            for shift, b in zip(range(7, -1, -1), raw_bits[i:i + 8]):
                val |= b << shift
            result.append(val)
        return result.decode()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        if ref.location not in self._registry:
            return False
        del self._registry[ref.location]
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="cpu-freq", location=loc)
            for loc, mid in self._registry.items()
            if mid == manifest_id
        ]

    async def health_check(self) -> bool:
        return True

    async def close(self) -> None:
        pass

    async def capacity(self) -> dict[str, int | None]:
        return {
            "used_bytes": None,
            "free_bytes": None,
            "total_bytes": self._capacity_bytes,
            "chunk_count": len(self._registry),
        }
