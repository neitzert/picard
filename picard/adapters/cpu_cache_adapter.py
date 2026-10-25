"""CPU cache adapter -- stores chunks in L3 SRAM via Flush+Reload side channel.

The CPU L1/L2/L3 cache is SRAM, a different physical medium from DRAM. It is
faster, more stable (no charge decay, no refresh cycles), and -- critically --
shared across cores and, in some microarchitectures, across process boundaries.
The Flush+Reload technique (Yarom and Falkner, USENIX Security 2014) exploits
this shared SRAM to create a reliable covert storage channel between two
processes running on the same physical CPU, even across isolation boundaries
such as containers or same-host VMs sharing a hypervisor.

Encoding principle:
  - Each bit of the chunk maps to one 64-byte cache line in a shared memory
    region (a file mapped by both writer and reader).
  - Bit = 1: writer accesses (loads) that cache line into L3 cache.
  - Bit = 0: writer issues CLFLUSH to evict that cache line from all caches.
  - Reader uses RDTSC to time its access to each line:
      - Fast access (< threshold cycles) = cache hit = bit 1.
      - Slow access (> threshold cycles) = DRAM miss = bit 0.

Why L3 specifically: L1/L2 are per-core and not shared between processes. L3
is last-level cache, shared by all cores on a physical socket, and its lines
persist much longer than L1/L2. On a lightly loaded system, a cache line
loaded into L3 can remain resident for seconds to minutes.

Cross-VM applicability: this technique has been demonstrated across VMs on the
same hypervisor when the hypervisor uses shared library page deduplication
(KSM/THP), which maps identical physical pages into multiple VM page tables.
An attacker VM and victim VM sharing a physical page (e.g., both running the
same libc) can communicate via L3 cache line residency of that page. No shared
IPC, no shared filesystem, no inter-VM network -- only the shared L3 cache.

PICARD angle: the CPU's own SRAM is used as a storage substrate. From a
forensic perspective, nothing is written to disk, no network packets are sent,
no syscall for IPC is made. The chunk exists only in cache residency state.

Persistence: extremely volatile. Other processes with large memory footprints
will evict cache lines. Typical window: 10ms to several seconds in L3.

Platform: Linux or macOS, x86-64 (uses CLFLUSH + RDTSC via JIT-compiled code).
          On Windows, uses a fallback that is less reliable (no CLFLUSH).

Shared memory: two processes on the same host must both map the same file to
communicate. The writer and reader must use the same mmap_path.

EXPERIMENTAL: Run at your own risk. Cache residency timing thresholds
differ across CPU generations, NUMA topologies, and background workload
levels. Auto-calibration helps but a busy system may produce false reads.
Cross-VM applicability depends on hypervisor KSM configuration.

Requires: pip install picard-store[cpu-cache]   (stdlib only; ctypes, mmap)

Reference: "FLUSH+RELOAD: A High Resolution, Low Noise, L3 Cache Side-Channel
           Attack", Yarom & Falkner, USENIX Security 2014.
"""

from __future__ import annotations

import ctypes
import json
import mmap as _mmap
import os
import sys
import time
from pathlib import Path
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_LINUX_X86 = sys.platform == "linux" and os.uname().machine in ("x86_64", "amd64")
_DARWIN_X86 = sys.platform == "darwin" and os.uname().machine == "x86_64"
_X86 = _LINUX_X86 or _DARWIN_X86

# Cache line size: 64 bytes on all modern x86-64 processors.
CACHE_LINE_BYTES: int = 64
# Timing threshold in CPU cycles: below = cache hit (L3), above = DRAM miss.
# Calibrate with _calibrate_threshold() for your hardware.
# 100-120 cycles: typical L3 hit on modern Intel/AMD.
# 180-250 cycles: typical DRAM miss.
DEFAULT_THRESHOLD: int = 150
# Bytes of shared memory per bit (one cache line per bit).
BYTES_PER_BIT: int = CACHE_LINE_BYTES


# ---------------------------------------------------------------------------
# x86-64 JIT primitives: CLFLUSH and RDTSC
# ---------------------------------------------------------------------------

# clflushopt [rdi]: flush + evict cache line at address in rdi
# mfence: serialise
# ret
_CLFLUSH_CODE = bytes([
    0x66, 0x0f, 0xae, 0x3f,   # clflushopt [rdi]
    0x0f, 0xae, 0xf0,          # mfence
    0xc3,
])

# rdtsc; shl rdx,32; or rax,rdx; ret  -- returns TSC in rax
_RDTSC_CODE = bytes([
    0x0f, 0x31,
    0x48, 0xc1, 0xe2, 0x20,
    0x48, 0x09, 0xd0,
    0xc3,
])

_primitives_cache: dict = {}


def _get_primitives():
    """Return (clflush_fn, rdtsc_fn, _buf) with JIT-compiled x86 code.
    Cached globally so we only allocate the RWX page once.
    """
    if "primitives" in _primitives_cache:
        return _primitives_cache["primitives"]

    if not _X86:
        raise RuntimeError("CpuCacheAdapter requires x86-64 (Linux or macOS)")

    code = _CLFLUSH_CODE + _RDTSC_CODE
    buf = _mmap.mmap(
        -1, max(len(code), 4096),
        prot=_mmap.PROT_READ | _mmap.PROT_WRITE | _mmap.PROT_EXEC,
    )
    buf.write(code)
    base = ctypes.addressof(ctypes.c_char.from_buffer(buf))
    clflush = ctypes.CFUNCTYPE(None, ctypes.c_void_p)(base)
    rdtsc = ctypes.CFUNCTYPE(ctypes.c_uint64)(base + len(_CLFLUSH_CODE))
    result = (clflush, rdtsc, buf)
    _primitives_cache["primitives"] = result
    return result


# ---------------------------------------------------------------------------
# Shared memory management
# ---------------------------------------------------------------------------

def _open_shared_mmap(path: str, size: int) -> tuple[int, _mmap.mmap]:
    """Open or create a file-backed shared mmap.

    Both writer and reader must use the same path and size. The mmap is
    shared (MAP_SHARED) so both processes see each other's cache line state
    via the underlying physical page -- the precondition for Flush+Reload.
    """
    p = Path(path)
    if not p.exists():
        p.write_bytes(b"\x00" * size)
    elif p.stat().st_size < size:
        with open(path, "ab") as f:
            f.write(b"\x00" * (size - p.stat().st_size))

    fh = open(path, "r+b")
    buf = _mmap.mmap(fh.fileno(), size, access=_mmap.ACCESS_WRITE)
    base = ctypes.addressof(ctypes.c_char.from_buffer(buf))
    return base, buf


# ---------------------------------------------------------------------------
# Timing oracle: measure cache line residency
# ---------------------------------------------------------------------------

def _probe_cache_line(addr: int, rdtsc_fn, clflush_fn) -> tuple[bool, int]:
    """Time access to a cache line and return (is_cached, cycles).

    Issues a volatile load followed by CLFLUSH to restore the line to
    evicted state for the next probe. The CLFLUSH is needed so repeated
    reads do not stay in cache and produce false positives.
    """
    t0 = rdtsc_fn()
    ctypes.c_uint8.from_address(addr)    # the probe load
    t1 = rdtsc_fn()
    clflush_fn(ctypes.c_void_p(addr))    # evict so next probe is fresh
    cycles = t1 - t0
    return cycles < DEFAULT_THRESHOLD, cycles


def _calibrate_threshold(shared_base: int, n_lines: int = 128) -> int:
    """Estimate the cycle threshold between L3 hit and DRAM miss.

    Loads cache lines, measures hit latency, flushes, measures miss latency,
    returns the midpoint as the threshold.
    """
    if not _X86:
        return DEFAULT_THRESHOLD

    clflush, rdtsc, _ = _get_primitives()

    # Measure L3 hit latency: load then time immediately
    hit_cycles = []
    for i in range(min(n_lines, 64)):
        addr = shared_base + i * CACHE_LINE_BYTES
        ctypes.c_uint8.from_address(addr)   # bring into cache
        t0 = rdtsc()
        ctypes.c_uint8.from_address(addr)   # this hits L3 (or L1/L2)
        t1 = rdtsc()
        hit_cycles.append(t1 - t0)
        clflush(ctypes.c_void_p(addr))

    # Measure DRAM miss latency: flush then time
    miss_cycles = []
    for i in range(min(n_lines, 64)):
        addr = shared_base + i * CACHE_LINE_BYTES
        clflush(ctypes.c_void_p(addr))
        t0 = rdtsc()
        ctypes.c_uint8.from_address(addr)   # cold DRAM access
        t1 = rdtsc()
        miss_cycles.append(t1 - t0)
        clflush(ctypes.c_void_p(addr))

    avg_hit = sum(hit_cycles) // len(hit_cycles)
    avg_miss = sum(miss_cycles) // len(miss_cycles)
    return (avg_hit + avg_miss) // 2


# ---------------------------------------------------------------------------
# Main adapter
# ---------------------------------------------------------------------------

class CpuCacheAdapter(Adapter):
    """Store chunks in CPU L3 SRAM cache via Flush+Reload encoding.

    Write: for each bit of the chunk, either load (bit=1) or flush (bit=0)
    the corresponding cache line in the shared memory region.

    Read: for each cache line, time the access with RDTSC. A fast access
    (cache hit) decodes as 1; a slow access (DRAM miss) decodes as 0.

    Both writer and reader must map the same shared file (mmap_path). On the
    same physical host, both processes will share the L3 cache state of the
    underlying physical page. This is the covert channel.

    Persistence window: 10ms to several seconds in L3, depending on system
    load and LLC size. Workloads with large memory footprints (ML training,
    video transcoding) will evict lines more quickly.

    For maximum persistence: use a large mmap (fills more of the LLC),
    read immediately after write, and avoid background workloads.
    """

    def __init__(
        self,
        mmap_path: str = "/tmp/picard-cache-channel",
        *,
        region_size: int = 0,   # 0 = auto-size to max_chunk_bytes
        threshold: int = 0,     # 0 = auto-calibrate
        max_chunk_bytes: int = 4096,
    ) -> None:
        self._mmap_path = mmap_path
        self._max = max_chunk_bytes
        self._threshold = threshold
        bits_needed = max_chunk_bytes * 8
        self._region_size = region_size or (bits_needed * BYTES_PER_BIT)
        self._base: int | None = None
        self._buf: _mmap.mmap | None = None

    def _ensure_mmap(self) -> tuple[int, _mmap.mmap]:
        if self._base is None:
            self._base, self._buf = _open_shared_mmap(
                self._mmap_path, self._region_size
            )
            if self._threshold == 0 and _X86:
                self._threshold = _calibrate_threshold(self._base)
                if self._threshold == 0:
                    self._threshold = DEFAULT_THRESHOLD
        return self._base, self._buf  # type: ignore[return-value]

    async def close(self) -> None:
        if self._buf is not None:
            self._buf.close()
            self._base = None
            self._buf = None

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="cpu-cache",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=120,
        )

    # ------------------------------------------------------------------
    # Cache line load / flush primitives
    # ------------------------------------------------------------------

    def _write_bit(self, bit_index: int, value: int, base: int) -> None:
        """Encode one bit by loading or flushing a cache line."""
        addr = base + bit_index * BYTES_PER_BIT
        if _X86:
            clflush, rdtsc, _ = _get_primitives()
            if value:
                # Bit = 1: bring cache line into L3
                ctypes.c_uint8.from_address(addr)
            else:
                # Bit = 0: evict cache line from all caches
                clflush(ctypes.c_void_p(addr))
        else:
            # Non-x86 fallback: write a sentinel byte (less stealthy but functional)
            ctypes.c_uint8.from_address(addr).value = 0xFF if value else 0x00

    def _read_bit(self, bit_index: int, base: int) -> int:
        """Decode one bit by timing access to its cache line."""
        addr = base + bit_index * BYTES_PER_BIT
        if _X86:
            clflush, rdtsc, _ = _get_primitives()
            threshold = self._threshold or DEFAULT_THRESHOLD
            t0 = rdtsc()
            ctypes.c_uint8.from_address(addr)
            t1 = rdtsc()
            clflush(ctypes.c_void_p(addr))
            return 1 if (t1 - t0) < threshold else 0
        else:
            # Non-x86 fallback: read the sentinel byte
            val = ctypes.c_uint8.from_address(addr).value
            return 1 if val == 0xFF else 0

    # ------------------------------------------------------------------
    # write_chunk / read_chunk
    # ------------------------------------------------------------------

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        data = encoded_chunk.encode()
        if len(data) > self._max:
            raise ValueError(f"chunk too large: {len(data)} > {self._max}")

        base, _ = self._ensure_mmap()
        bits_needed = len(data) * 8

        if bits_needed * BYTES_PER_BIT > self._region_size:
            raise RuntimeError(
                f"shared region too small: need {bits_needed * BYTES_PER_BIT} bytes"
            )

        for byte_idx, byte_val in enumerate(data):
            for bit_idx in range(8):
                linear_bit = byte_idx * 8 + bit_idx
                bit_val = (byte_val >> bit_idx) & 1
                self._write_bit(linear_bit, bit_val, base)

        location = json.dumps({
            "mmap_path": self._mmap_path,
            "n_bytes": len(data),
            "threshold": self._threshold,
            "written_at": time.time(),
        })
        return StoredChunkRef(adapter_name="cpu-cache", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        payload = json.loads(ref.location)
        n_bytes = payload["n_bytes"]
        mmap_path = payload.get("mmap_path", self._mmap_path)

        if mmap_path != self._mmap_path or self._base is None:
            self._mmap_path = mmap_path
            self._base, self._buf = _open_shared_mmap(mmap_path, self._region_size)

        base, _ = self._ensure_mmap()
        result = bytearray(n_bytes)

        for byte_idx in range(n_bytes):
            for bit_idx in range(8):
                linear_bit = byte_idx * 8 + bit_idx
                bit_val = self._read_bit(linear_bit, base)
                if bit_val:
                    result[byte_idx] |= (1 << bit_idx)

        return result.decode()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        try:
            payload = json.loads(ref.location)
            n_bytes = payload["n_bytes"]
            base, _ = self._ensure_mmap()
            if _X86:
                clflush, _, _ = _get_primitives()
                for i in range(n_bytes * 8):
                    addr = base + i * BYTES_PER_BIT
                    clflush(ctypes.c_void_p(addr))
            return True
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return []

    async def health_check(self) -> bool:
        try:
            base, _ = self._ensure_mmap()
            if _X86:
                clflush, rdtsc, _ = _get_primitives()
            return True
        except Exception:
            return False

    def mmap_path(self) -> str:
        return self._mmap_path

    def calibrated_threshold(self) -> int:
        self._ensure_mmap()
        return self._threshold or DEFAULT_THRESHOLD
