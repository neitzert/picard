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
# Timing threshold in CPU cycles: below = cache hit (L1/L3), above = DRAM miss.
# Calibrate with _calibrate_threshold() for your hardware.
# 20-50 cycles: typical L1/L3 hit on modern Intel/AMD.
# 180-280 cycles: typical DRAM miss.
DEFAULT_THRESHOLD: int = 150
# Bytes of shared memory allocated per bit.
# Each bit occupies a 128-byte (two-cache-line) slot, aligned to a 128-byte
# boundary. This is intentional: Intel's adjacent cache line prefetcher fires
# on L2 misses and unconditionally fetches the OTHER 64-byte line in the same
# 128-byte aligned pair. With 64-byte-per-bit layout, a miss on bit N would
# also warm bit N+1 (breaking 0-bit detection). With 128-byte slots, the
# prefetched "adjacent" line is the second cache line of the SAME slot --
# harmless, since we only probe the first line of each slot.
BYTES_PER_BIT: int = 4096  # one OS page per bit -- isolates each bit in its own 4K SMS region
# Repetitions per logical bit for majority-vote noise reduction.
# Each logical bit is encoded at N_REPS independent physical slots; on read the
# majority of those N_REPS probes decides the bit value.  BER with N_REPS=5 and
# raw BER ≈ 0.7% drops to ~3.5e-6 per bit, giving >99.9% probability of a
# correct 272-bit decode.  Region size scales by N_REPS (5×).
N_REPS: int = 5


# ---------------------------------------------------------------------------
# x86-64 JIT primitives
# ---------------------------------------------------------------------------

# clflushopt [rdi]: flush + evict cache line at address in rdi; mfence; ret
_CLFLUSH_CODE = bytes([
    0x66, 0x0f, 0xae, 0x3f,   # clflushopt [rdi]
    0x0f, 0xae, 0xf0,          # mfence
    0xc3,
])

# rdtsc -> edx:eax; combine into rax; ret
_RDTSC_CODE = bytes([
    0x0f, 0x31,
    0x48, 0xc1, 0xe2, 0x20,
    0x48, 0x09, 0xd0,
    0xc3,
])

# just_probe(addr) -> cycles: time a single load with no prior flush.
# Used for reading bits (hot line = bit 1, cold line = bit 0).
# All timing happens in machine code; no Python overhead in the critical path.
_JUST_PROBE_CODE = bytes([
    0x0f, 0xae, 0xf0,          # mfence
    0x0f, 0x31,                 # rdtsc -> t0 in edx:eax
    0x48, 0xc1, 0xe2, 0x20,    # shl rdx, 32
    0x48, 0x09, 0xd0,           # or rax, rdx   -> t0 in rax
    0x49, 0x89, 0xc0,           # mov r8, rax   (save t0)
    0x48, 0x8b, 0x07,           # mov rax, [rdi] (probe load)
    0x0f, 0xae, 0xe8,           # lfence
    0x0f, 0x31,                 # rdtsc -> t1 in edx:eax
    0x48, 0xc1, 0xe2, 0x20,    # shl rdx, 32
    0x48, 0x09, 0xd0,           # or rax, rdx   -> t1 in rax
    0x4c, 0x29, 0xc0,           # sub rax, r8   (delta = t1 - t0)
    0xc3,
])

# preload_probe(addr) -> cycles: warm the line, then time the L1 hit.
# Used for calibrating the hot (cache-hit) latency baseline.
_PRELOAD_PROBE_CODE = bytes([
    0x48, 0x8b, 0x07,           # mov rax, [rdi]  (preload into L1)
    0x0f, 0xae, 0xf0,           # mfence
    0x0f, 0x31,                  # rdtsc -> t0
    0x48, 0xc1, 0xe2, 0x20,     # shl rdx, 32
    0x48, 0x09, 0xd0,            # or rax, rdx
    0x49, 0x89, 0xc0,            # mov r8, rax  (save t0)
    0x48, 0x8b, 0x07,            # mov rax, [rdi]  (timed L1 hit)
    0x0f, 0xae, 0xe8,            # lfence
    0x0f, 0x31,                  # rdtsc -> t1
    0x48, 0xc1, 0xe2, 0x20,     # shl rdx, 32
    0x48, 0x09, 0xd0,            # or rax, rdx
    0x4c, 0x29, 0xc0,            # sub rax, r8
    0xc3,
])

# flush_probe(addr) -> cycles: evict the line, then time the cold DRAM miss.
# Used for calibrating the cold (DRAM-miss) latency baseline.
# Uses plain CLFLUSH (not CLFLUSHOPT) so it works on all x86-64 CPUs.
_FLUSH_PROBE_CODE = bytes([
    0x0f, 0xae, 0x3f,           # clflush [rdi]
    0x0f, 0xae, 0xf0,           # mfence
    0x0f, 0x31,                  # rdtsc -> t0
    0x48, 0xc1, 0xe2, 0x20,     # shl rdx, 32
    0x48, 0x09, 0xd0,            # or rax, rdx
    0x49, 0x89, 0xc0,            # mov r8, rax  (save t0)
    0x48, 0x8b, 0x07,            # mov rax, [rdi]  (cold DRAM load)
    0x0f, 0xae, 0xe8,            # lfence
    0x0f, 0x31,                  # rdtsc -> t1
    0x48, 0xc1, 0xe2, 0x20,     # shl rdx, 32
    0x48, 0x09, 0xd0,            # or rax, rdx
    0x4c, 0x29, 0xc0,            # sub rax, r8
    0xc3,
])

_primitives_cache: dict = {}


def _get_primitives():
    """Return (clflush, rdtsc, just_probe, preload_probe, flush_probe, buf).

    All five are JIT-compiled x86-64 machine code functions backed by a single
    shared RWX mmap page. Cached globally so the page is allocated once.

    - clflush(addr):          evict a cache line
    - rdtsc() -> int:         read the TSC
    - just_probe(addr) -> int:        time a single load (hot or cold depending on state)
    - preload_probe(addr) -> int:     preload then time the second load (always L1 hit)
    - flush_probe(addr) -> int:       flush then time the cold load (always DRAM miss)
    """
    if "primitives" in _primitives_cache:
        return _primitives_cache["primitives"]

    if not _X86:
        raise RuntimeError("CpuCacheAdapter requires x86-64 (Linux or macOS)")

    _P = ctypes.c_void_p
    _U = ctypes.c_uint64

    code = (
        _CLFLUSH_CODE
        + _RDTSC_CODE
        + _JUST_PROBE_CODE
        + _PRELOAD_PROBE_CODE
        + _FLUSH_PROBE_CODE
    )
    buf = _mmap.mmap(
        -1, max(len(code), 4096),
        prot=_mmap.PROT_READ | _mmap.PROT_WRITE | _mmap.PROT_EXEC,
    )
    buf.write(code)
    b = ctypes.addressof(ctypes.c_char.from_buffer(buf))

    off0 = 0
    off1 = len(_CLFLUSH_CODE)
    off2 = off1 + len(_RDTSC_CODE)
    off3 = off2 + len(_JUST_PROBE_CODE)
    off4 = off3 + len(_PRELOAD_PROBE_CODE)

    clflush      = ctypes.CFUNCTYPE(None, _P)(b + off0)
    rdtsc        = ctypes.CFUNCTYPE(_U)      (b + off1)
    just_probe   = ctypes.CFUNCTYPE(_U, _P)  (b + off2)
    preload_probe = ctypes.CFUNCTYPE(_U, _P) (b + off3)
    flush_probe  = ctypes.CFUNCTYPE(_U, _P)  (b + off4)

    result = (clflush, rdtsc, just_probe, preload_probe, flush_probe, buf)
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

def _probe_cache_line(addr: int, just_probe_fn, clflush_fn) -> tuple[bool, int]:
    """Time access to a cache line and return (is_cached, cycles).

    The timing is done entirely in JIT machine code (just_probe_fn) so that
    Python interpreter overhead does not obscure the sub-100-cycle L3 signal.
    CLFLUSH after the probe restores the cold state for the next probe.
    """
    cycles = just_probe_fn(ctypes.c_void_p(addr))
    clflush_fn(ctypes.c_void_p(addr))
    return cycles < DEFAULT_THRESHOLD, cycles


def _calibrate_threshold(shared_base: int, n_lines: int = 64) -> int:
    """Estimate the cycle threshold between L3 hit and DRAM miss.

    Uses JIT preload_probe and flush_probe with no Python in the timing window.
    Calibration samples are spaced BYTES_PER_BIT apart (same as real channel)
    so the DRAM row-buffer behavior matches operational conditions -- if real
    bit accesses land on the same DRAM row (row buffer hits, fast), calibration
    sees the same row-buffer hit latency.  Access order is randomised.
    """
    import random

    if not _X86:
        return DEFAULT_THRESHOLD

    clflush, rdtsc, just_probe, preload_probe, flush_probe, _ = _get_primitives()

    indices = list(range(n_lines))
    random.shuffle(indices)
    half = n_lines // 2

    hit_cycles = []
    for i in indices[:half]:
        addr = shared_base + i * BYTES_PER_BIT
        hit_cycles.append(preload_probe(ctypes.c_void_p(addr)))

    miss_cycles = []
    random.shuffle(indices)
    for i in indices[:half]:
        addr = shared_base + i * BYTES_PER_BIT
        miss_cycles.append(flush_probe(ctypes.c_void_p(addr)))

    trim = max(1, len(hit_cycles) // 10)
    avg_hit  = sum(sorted(hit_cycles) [trim:-trim]) // (len(hit_cycles)  - 2 * trim)
    avg_miss = sum(sorted(miss_cycles)[trim:-trim]) // (len(miss_cycles) - 2 * trim)
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
        self._region_size = region_size or (bits_needed * N_REPS * BYTES_PER_BIT)
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
        """Encode one bit by loading (1) or flushing (0) a cache line."""
        addr = base + bit_index * BYTES_PER_BIT
        if _X86:
            clflush, rdtsc, just_probe, preload_probe, flush_probe, _ = _get_primitives()
            if value:
                preload_probe(ctypes.c_void_p(addr))    # Bit = 1: warm the line (actual CPU load)
            else:
                clflush(ctypes.c_void_p(addr))          # Bit = 0: evict the line
        else:
            ctypes.c_uint8.from_address(addr).value = 0xFF if value else 0x00

    def _read_bit(self, bit_index: int, base: int) -> int:
        """Decode one bit by timing access to its cache line.

        just_probe does the entire RDTSC-load-RDTSC sequence in JIT machine
        code so Python overhead does not swamp the ~40-cycle L3 signal.
        """
        addr = base + bit_index * BYTES_PER_BIT
        if _X86:
            clflush, rdtsc, just_probe, preload_probe, flush_probe, _ = _get_primitives()
            threshold = self._threshold or DEFAULT_THRESHOLD
            cycles = just_probe(ctypes.c_void_p(addr))
            clflush(ctypes.c_void_p(addr))              # restore cold state
            return 1 if cycles < threshold else 0
        else:
            val = ctypes.c_uint8.from_address(addr).value
            return 1 if val == 0xFF else 0

    # ------------------------------------------------------------------
    # write_chunk / read_chunk
    # ------------------------------------------------------------------

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import random

        data = encoded_chunk.encode()
        if len(data) > self._max:
            raise ValueError(f"chunk too large: {len(data)} > {self._max}")

        base, _ = self._ensure_mmap()
        n_bits = len(data) * 8

        if n_bits * N_REPS * BYTES_PER_BIT > self._region_size:
            raise RuntimeError(
                f"shared region too small: need {n_bits * N_REPS * BYTES_PER_BIT} bytes"
            )

        # Assign each logical bit N_REPS independent physical slots.
        # slot_map[logical_bit] = list of N_REPS physical slot indices.
        # A random permutation across all physical slots ensures that
        # no two replicas of any bit are adjacent, defeating stride prefetchers.
        total_slots = n_bits * N_REPS
        all_slots = list(range(total_slots))
        random.shuffle(all_slots)
        slot_map = [all_slots[i * N_REPS:(i + 1) * N_REPS] for i in range(n_bits)]

        # Build bit value list
        bit_vals = []
        for byte_idx, byte_val in enumerate(data):
            for bit_idx in range(8):
                bit_vals.append((byte_val >> bit_idx) & 1)

        # Collect all physical slots by value across all replicas.
        one_slots  = [s for i, v in enumerate(bit_vals) if v for s in slot_map[i]]
        zero_slots = [s for i, v in enumerate(bit_vals) if not v for s in slot_map[i]]
        random.shuffle(one_slots)
        random.shuffle(zero_slots)

        # Warmup pass: load ALL data slots (1-bit and 0-bit replicas) into L3
        # immediately before pass 1.  With all slots already in L3, pass 1's
        # preload_probe calls are L3 hits rather than DRAM misses.  L3 hits do
        # not trigger the IP prefetcher (which fires only on L2 misses), so 0-bit
        # slots are not accidentally prefetched during the 1-bit warmup.
        if _X86:
            clflush_fn, rdtsc_fn, just_probe_fn, preload_probe_fn, flush_probe_fn, _ = _get_primitives()
            all_slots = one_slots + zero_slots
            random.shuffle(all_slots)
            for slot in all_slots:
                preload_probe_fn(ctypes.c_void_p(base + slot * BYTES_PER_BIT))

        # Pass 1: load all 1-bit replicas (random physical order).
        for slot in one_slots:
            self._write_bit(slot, 1, base)

        # Passes 2-5: four flush sweeps for 0-bit replicas in changing random
        # orders.  Hardware prefetch requests from pass 1 may complete AFTER the
        # first flush; subsequent sweeps catch progressively later arrivals.
        for _ in range(4):
            random.shuffle(zero_slots)
            for slot in zero_slots:
                self._write_bit(slot, 0, base)

        location = json.dumps({
            "mmap_path": self._mmap_path,
            "n_bytes": len(data),
            "threshold": self._threshold,
            "slot_map": slot_map,
            "written_at": time.time(),
        })
        return StoredChunkRef(adapter_name="cpu-cache", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        import random

        payload = json.loads(ref.location)
        n_bytes = payload["n_bytes"]
        mmap_path = payload.get("mmap_path", self._mmap_path)

        if mmap_path != self._mmap_path or self._base is None:
            self._mmap_path = mmap_path
            self._base, self._buf = _open_shared_mmap(mmap_path, self._region_size)

        base, _ = self._ensure_mmap()
        n_bits = n_bytes * 8
        raw_map = payload.get("slot_map", list(range(n_bits)))
        # slot_map may be a list-of-lists (N_REPS replicas) or a flat list
        # (legacy single-replica format written before N_REPS was added).
        if raw_map and isinstance(raw_map[0], list):
            slot_map = raw_map  # [[slot, ...], ...]
        else:
            slot_map = [[s] for s in raw_map]  # legacy: wrap each slot

        bit_values = [0] * n_bits

        # Probe logical bits in random order using their randomised physical
        # slots.  Each logical bit has N_REPS independent slots; take the
        # majority vote across all replicas for noise immunity.
        order = list(range(n_bits))
        random.shuffle(order)
        for logical_bit in order:
            votes = sum(self._read_bit(s, base) for s in slot_map[logical_bit])
            bit_values[logical_bit] = 1 if votes * 2 >= len(slot_map[logical_bit]) else 0

        result = bytearray(n_bytes)
        for byte_idx in range(n_bytes):
            for bit_idx in range(8):
                if bit_values[byte_idx * 8 + bit_idx]:
                    result[byte_idx] |= (1 << bit_idx)

        return result.decode()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        try:
            payload = json.loads(ref.location)
            n_bytes = payload["n_bytes"]
            base, _ = self._ensure_mmap()
            if _X86:
                clflush, *_ = _get_primitives()
                for i in range(n_bytes * 8 * N_REPS):
                    addr = base + i * BYTES_PER_BIT
                    clflush(ctypes.c_void_p(addr))
            return True
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return []

    async def health_check(self) -> bool:
        try:
            self._ensure_mmap()
            if _X86:
                _get_primitives()
            return True
        except Exception:
            return False

    def mmap_path(self) -> str:
        return self._mmap_path

    def calibrated_threshold(self) -> int:
        self._ensure_mmap()
        return self._threshold or DEFAULT_THRESHOLD
