"""Rowhammer adapter -- stores chunks as induced DRAM bit flips.

Rowhammer (Kim et al., ISCA 2014, "Flipping Bits in Memory Without
Accessing Them") exploits electrical coupling between DRAM rows. Rapidly
and repeatedly reading two aggressor rows causes charge leakage in
adjacent victim rows, flipping bits that were never directly addressed.
Because the flips are reproducible on a given chip, they constitute a
write primitive: hammer to flip, read to verify.

Threat model: this adapter demonstrates that DRAM itself, a component
present in every computing device, can function as a covert, encrypted
storage substrate entirely outside the operating system's visibility. No
filesystem entry, no syscall, no block device -- chunks live as induced
electrical states in silicon.

This is a HAL-pattern adapter. The hammering algorithm, profiler, physical
address resolution, and chunk encoding are fully implemented. Subclass and
override _dram_row() and _same_bank_candidates() for your memory controller,
or call empirical_profile() to discover flippable bits without knowledge of
the bank addressing function.

Platform: Linux x86-64.
  - /proc/self/pagemap for virtual-to-physical address resolution.
  - CLFLUSH instruction (JIT-compiled via ctypes + mmap) to bypass cache.
  - MAP_HUGETLB (2 MB transparent huge pages) for contiguous physical memory.

Requirements:
  - Non-ECC DRAM. ECC corrects single-bit errors and will undo writes.
  - DDR3 or vulnerable DDR4. DDR4 Target Row Refresh (TRR) reduces but
    does not eliminate flippability; empirical_profile() finds what survives.
  - Root or CAP_SYS_PTRACE (for /proc/pagemap on kernels with paranoid
    perf settings). Huge pages require vm.nr_hugepages > 0 or THP=always.

Persistence: NONE. Data lives in DRAM charge state. Power cycle, OS page
reclaim, or a competing workload with large memory footprint will lose it.
persistent=False is set in the adapter profile.

EXPERIMENTAL: Run at your own risk. This adapter has not been tested against
every DRAM vendor and chipset combination. Results vary by hardware. Physical
bit flips are irreversible without a rewrite and can corrupt adjacent memory
if the target region overlaps live process state.

WARNING: use only on hardware you own. Rowhammer on shared infrastructure
(cloud VMs, co-located servers, shared RDMA fabric) constitutes unauthorized
access and is illegal in most jurisdictions.

Requires: pip install picard-store[rowhammer]   (no third-party deps;
          uses stdlib ctypes and mmap only)
"""

from __future__ import annotations

import ctypes
import json
import mmap as _mmap
import os
import struct
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

# ---------------------------------------------------------------------------
# Platform guard
# ---------------------------------------------------------------------------

_LINUX_X86 = sys.platform == "linux" and os.uname().machine in ("x86_64", "amd64")


# ---------------------------------------------------------------------------
# x86-64 hardware primitives (JIT-compiled machine code)
# ---------------------------------------------------------------------------

# clflushopt [rdi]  -- flush cache line at address in rdi (write-back + evict)
# mfence            -- serialise memory operations
# ret
_CODE_CLFLUSH = bytes([
    0x66, 0x0f, 0xae, 0x3f,   # clflushopt [rdi]
    0x0f, 0xae, 0xf0,          # mfence
    0xc3,                       # ret
])

# rdtsc             -- read timestamp counter into edx:eax
# shl rdx, 32      -- shift edx to upper half of rdx
# or  rax, rdx    -- combine into rax
# ret
_CODE_RDTSC = bytes([
    0x0f, 0x31,                 # rdtsc
    0x48, 0xc1, 0xe2, 0x20,    # shl rdx, 32
    0x48, 0x09, 0xd0,           # or rax, rdx
    0xc3,                       # ret
])


class _X86Primitives:
    """JIT-compiled x86-64 CLFLUSH and RDTSC callables.

    Creates a single RWX mmap page holding both routines. The mmap object
    is kept alive as long as this instance exists.
    """

    def __init__(self) -> None:
        if not _LINUX_X86:
            raise RuntimeError("RowhammerAdapter requires Linux x86-64")
        code = _CODE_CLFLUSH + _CODE_RDTSC
        self._buf = _mmap.mmap(
            -1, max(len(code), 4096),
            prot=_mmap.PROT_READ | _mmap.PROT_WRITE | _mmap.PROT_EXEC,
        )
        self._buf.write(code)
        base = ctypes.addressof(ctypes.c_char.from_buffer(self._buf))
        self.clflush: ctypes.CFUNCTYPE = ctypes.CFUNCTYPE(None, ctypes.c_void_p)(base)
        self.rdtsc: ctypes.CFUNCTYPE = ctypes.CFUNCTYPE(ctypes.c_uint64)(
            base + len(_CODE_CLFLUSH)
        )

    def timed_read(self, addr: int) -> tuple[int, int]:
        """Read one byte at addr and return (value, cycles)."""
        t0 = self.rdtsc()
        val = ctypes.c_uint8.from_address(addr).value
        t1 = self.rdtsc()
        self.clflush(ctypes.c_void_p(addr))
        return val, t1 - t0


# ---------------------------------------------------------------------------
# Physical memory helpers
# ---------------------------------------------------------------------------

_PAGE_SIZE = 4096
_HUGE_SIZE = 2 * 1024 * 1024   # 2 MB huge page


def _virt_to_phys(virt: int) -> int:
    """Translate a virtual address to physical via /proc/self/pagemap.

    Requires the page to be present (not swapped out). Huge pages work
    correctly: the pfn returned covers the entire 2 MB region.
    """
    page = virt >> 12
    with open("/proc/self/pagemap", "rb") as f:
        f.seek(page * 8)
        entry = struct.unpack("<Q", f.read(8))[0]
    if not (entry >> 63):
        raise RuntimeError(f"page not resident: virt={virt:#x}")
    pfn = entry & ((1 << 55) - 1)
    return (pfn << 12) | (virt & 0xFFF)


def _alloc_huge_pages(size: int) -> tuple[int, "_mmap.mmap"]:
    """Allocate size bytes of huge-page-backed memory.

    Returns (base_virt_addr, mmap_obj). The mmap keeps pages resident.
    Requires vm.nr_hugepages > 0 or transparent_hugepage=always.
    Falls back to regular pages if huge pages are unavailable.
    """
    aligned = (size + _HUGE_SIZE - 1) & ~(_HUGE_SIZE - 1)
    try:
        buf = _mmap.mmap(
            -1, aligned,
            flags=_mmap.MAP_PRIVATE | _mmap.MAP_ANONYMOUS | 0x40000,  # MAP_HUGETLB
            prot=_mmap.PROT_READ | _mmap.PROT_WRITE,
        )
    except OSError:
        # Fall back to standard pages (profiling may yield fewer flips)
        buf = _mmap.mmap(
            -1, aligned,
            flags=_mmap.MAP_PRIVATE | _mmap.MAP_ANONYMOUS,
            prot=_mmap.PROT_READ | _mmap.PROT_WRITE,
        )
    # Touch every page to force allocation (ensure pages are in RAM)
    buf.write(b"\x00" * aligned)
    buf.seek(0)
    base = ctypes.addressof(ctypes.c_char.from_buffer(buf))
    return base, buf


# ---------------------------------------------------------------------------
# DRAM geometry (HAL)
# ---------------------------------------------------------------------------

DRAM_ROW_BYTES = 8192   # typical DDR3/DDR4 row size; override in subclass


class DramGeometry:
    """Abstract DRAM address decoder.

    Maps physical addresses to (bank, rank, row, column) coordinates.
    The bank function is chipset-specific and must be implemented for
    each memory controller variant.

    The Intel Sandy Bridge / Ivy Bridge XOR function is provided as the
    default implementation; it is correct for many dual-channel setups
    with that generation of chipset.
    """

    ROW_BYTES: int = DRAM_ROW_BYTES

    def bank(self, phys: int) -> int:
        """Return the DRAM bank index for a physical address."""
        raise NotImplementedError

    def row(self, phys: int) -> int:
        """Return the DRAM row number for a physical address."""
        return phys >> int.bit_length(self.ROW_BYTES - 1)

    def same_bank(self, a: int, b: int) -> bool:
        return self.bank(a) == self.bank(b)

    def adjacent_rows(self, a: int, b: int) -> bool:
        return self.same_bank(a, b) and abs(self.row(a) - self.row(b)) == 1


class IntelSandyBridgeGeometry(DramGeometry):
    """Intel Sandy Bridge / Ivy Bridge dual-channel DRAM geometry.

    Bank XOR function derived from reverse-engineering by Seaborn (2015)
    and later verified by multiple academic groups. Correct for many
    consumer DDR3 setups on that chipset generation.

    Physical address bits used for bank selection:
      bank[0] = PA[14] ^ PA[18]
      bank[1] = PA[15] ^ PA[19]
      bank[2] = PA[16] ^ PA[20]  (three-bank interleaving)
    """

    def bank(self, phys: int) -> int:
        b0 = ((phys >> 14) ^ (phys >> 18)) & 1
        b1 = ((phys >> 15) ^ (phys >> 19)) & 1
        b2 = ((phys >> 16) ^ (phys >> 20)) & 1
        return (b2 << 2) | (b1 << 1) | b0


# ---------------------------------------------------------------------------
# Profile: known-flippable bit positions
# ---------------------------------------------------------------------------

@dataclass
class FlippableBit:
    """A DRAM bit position that can be reliably flipped by rowhammer.

    aggressor_a_virt and aggressor_b_virt are the two rows to hammer
    (double-sided rowhammer). victim_virt is the row containing the bit.
    bit_offset is the bit index within the victim page (0 = LSB of byte 0).
    natural_value is the bit's value WITHOUT hammering (0 or 1).
    """
    aggressor_a_virt: int
    aggressor_b_virt: int
    victim_virt: int
    bit_offset: int   # bit index within the victim page
    natural_value: int  # 0 or 1


@dataclass
class RowhammerProfile:
    """A set of FlippableBit positions discovered by profiling.

    Encodes how chunk bytes map to physical bit positions. Serialises to
    JSON for storage in the PICARD index.
    """
    bits: list[FlippableBit] = field(default_factory=list)
    geometry: str = "IntelSandyBridgeGeometry"
    profiled_at: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps({
            "geometry": self.geometry,
            "profiled_at": self.profiled_at,
            "bits": [asdict(b) for b in self.bits],
        })

    @classmethod
    def from_json(cls, s: str) -> "RowhammerProfile":
        d = json.loads(s)
        bits = [FlippableBit(**b) for b in d["bits"]]
        return cls(bits=bits, geometry=d["geometry"], profiled_at=d["profiled_at"])

    def capacity_bytes(self) -> int:
        return len(self.bits) // 8


# ---------------------------------------------------------------------------
# Main adapter
# ---------------------------------------------------------------------------

class RowhammerAdapter(Adapter):
    """Scatter chunks into DRAM victim rows via rowhammer-induced bit flips.

    Write path:
      1. Ensure a profile exists (or call profile() to generate one).
      2. Map chunk bytes to flippable bit positions from the profile.
      3. For each bit that must be 1 (and whose natural value is 0):
         hammer the aggressor pair until the victim bit flips.
      4. For each bit that must be 0 (and whose natural value is 1):
         hammer the aggressor pair for the opposite effect (rare but possible).
      5. Return a StoredChunkRef encoding the bit positions used.

    Read path:
      1. Deserialise the bit position list from the location ref.
      2. Read the virtual address of each victim bit.
      3. Reconstruct the chunk bytes.

    Subclass this adapter and implement _dram_row() / _same_bank_candidates()
    for your memory controller, or call empirical_profile() to let the
    profiler discover flippable pairs without geometry knowledge.
    """

    #: Hammer iterations per attempt. Tune down for profiling, up for reliability.
    HAMMER_ITERATIONS: int = 1_000_000
    #: Profile memory region size (bytes). More = more flippable bits found.
    PROFILE_REGION_BYTES: int = 256 * 1024 * 1024   # 256 MB
    #: Minimum flippable bits required to write a chunk.
    MIN_PROFILE_BITS: int = 512

    def __init__(
        self,
        *,
        hammer_iterations: int = HAMMER_ITERATIONS,
        region_bytes: int = PROFILE_REGION_BYTES,
        geometry: DramGeometry | None = None,
        max_chunk_bytes: int = 512,
    ) -> None:
        self._iterations = hammer_iterations
        self._region_bytes = region_bytes
        self._geometry: DramGeometry = geometry or IntelSandyBridgeGeometry()
        self._max = max_chunk_bytes
        self._profile: RowhammerProfile | None = None
        self._region_base: int | None = None
        self._region_mmap: _mmap.mmap | None = None
        self._hw: _X86Primitives | None = None

    def _primitives(self) -> _X86Primitives:
        if self._hw is None:
            self._hw = _X86Primitives()
        return self._hw

    def _ensure_region(self) -> tuple[int, _mmap.mmap]:
        if self._region_base is None:
            self._region_base, self._region_mmap = _alloc_huge_pages(
                self._region_bytes
            )
        return self._region_base, self._region_mmap  # type: ignore[return-value]

    async def close(self) -> None:
        if self._region_mmap is not None:
            self._region_mmap.close()
            self._region_base = None
            self._region_mmap = None

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="rowhammer",
            max_chunk_bytes=self._max,
            persistent=False,   # ephemeral: power cycle loses everything
            ordered=False,
            rate_limit_per_min=2,
        )

    # ------------------------------------------------------------------
    # HAL: subclass to override for different memory controllers
    # ------------------------------------------------------------------

    def _dram_row(self, virt: int) -> int:
        """Return the DRAM row number for a virtual address."""
        try:
            phys = _virt_to_phys(virt)
            return self._geometry.row(phys)
        except Exception:
            # Fallback: use virtual row (less accurate but avoids pagemap issues)
            return virt // DRAM_ROW_BYTES

    def _same_bank_candidates(
        self, base_virt: int, size: int
    ) -> list[tuple[int, int, int]]:
        """Return (aggressor_a, aggressor_b, victim) virtual address triples.

        Default implementation uses the configured DramGeometry to identify
        same-bank addresses. Override for hardware-specific bank functions.

        Returns triples where aggressor_a and aggressor_b flank the victim
        row (double-sided rowhammer pattern).
        """
        candidates = []
        row_size = DRAM_ROW_BYTES
        n_rows = size // row_size

        try:
            # Physical address based (accurate)
            phys_rows: list[tuple[int, int]] = []  # (row_num, virt_addr)
            for i in range(n_rows):
                virt = base_virt + i * row_size
                try:
                    phys = _virt_to_phys(virt)
                    phys_rows.append((self._geometry.row(phys), virt))
                except Exception:
                    continue

            # Group by bank
            from collections import defaultdict
            bank_rows: dict[int, list[tuple[int, int]]] = defaultdict(list)
            for i, (row_num, virt) in enumerate(phys_rows):
                try:
                    phys = _virt_to_phys(virt)
                    bank = self._geometry.bank(phys)
                    bank_rows[bank].append((row_num, virt))
                except Exception:
                    continue

            for bank, rows in bank_rows.items():
                rows.sort(key=lambda x: x[0])
                for j in range(1, len(rows) - 1):
                    row_a = rows[j - 1][1]
                    victim = rows[j][1]
                    row_b = rows[j + 1][1]
                    if (rows[j][0] - rows[j-1][0] == 1 and
                            rows[j+1][0] - rows[j][0] == 1):
                        candidates.append((row_a, row_b, victim))

        except Exception:
            # Fallback: assume rows are physically contiguous (less reliable)
            for i in range(1, n_rows - 1):
                row_a = base_virt + (i - 1) * row_size
                victim = base_virt + i * row_size
                row_b = base_virt + (i + 1) * row_size
                candidates.append((row_a, row_b, victim))

        return candidates

    # ------------------------------------------------------------------
    # Hammer primitive
    # ------------------------------------------------------------------

    def _hammer(self, aggressor_a: int, aggressor_b: int) -> None:
        """Double-sided rowhammer: hammer two aggressor rows flanking victim.

        Both aggressors must be in the same DRAM bank as the victim and on
        opposite sides. CLFLUSH before each read ensures accesses hit DRAM,
        not the L1/L2/L3 cache.
        """
        hw = self._primitives()
        ptr_a = ctypes.c_void_p(aggressor_a)
        ptr_b = ctypes.c_void_p(aggressor_b)
        for _ in range(self._iterations):
            # Read both aggressor rows -- this is what causes the disturbance
            ctypes.c_uint8.from_address(aggressor_a)
            ctypes.c_uint8.from_address(aggressor_b)
            # Flush from all cache levels so the next iteration hits DRAM
            hw.clflush(ptr_a)
            hw.clflush(ptr_b)

    # ------------------------------------------------------------------
    # Profiling: discover which bits flip on this hardware
    # ------------------------------------------------------------------

    def empirical_profile(self, max_bits: int = 4096) -> RowhammerProfile:
        """Profile DRAM to find reliably flippable bit positions.

        Hammers candidate aggressor pairs and records which victim bits flip.
        This is the primary way to use the adapter without knowledge of the
        DRAM bank addressing function.

        Takes several minutes on a typical system. Call once and cache.
        """
        base, _ = self._ensure_region()
        candidates = self._same_bank_candidates(base, self._region_bytes)
        flippable: list[FlippableBit] = []

        for agg_a, agg_b, victim in candidates:
            if len(flippable) >= max_bits:
                break

            # Snapshot victim row before hammering
            row_size = DRAM_ROW_BYTES
            before = (ctypes.c_uint8 * row_size).from_address(victim)[:]

            self._hammer(agg_a, agg_b)

            # Compare with after
            after = (ctypes.c_uint8 * row_size).from_address(victim)[:]
            for byte_idx, (b0, b1) in enumerate(zip(before, after)):
                diff = b0 ^ b1
                for bit_idx in range(8):
                    if diff & (1 << bit_idx):
                        nat = (b0 >> bit_idx) & 1
                        flippable.append(FlippableBit(
                            aggressor_a_virt=agg_a,
                            aggressor_b_virt=agg_b,
                            victim_virt=victim,
                            bit_offset=byte_idx * 8 + bit_idx,
                            natural_value=nat,
                        ))

            # Restore victim row to natural state
            for i, b in enumerate(before):
                ctypes.c_uint8.from_address(victim + i).value = b

        profile = RowhammerProfile(
            bits=flippable,
            geometry=type(self._geometry).__name__,
        )
        self._profile = profile
        return profile

    def load_profile(self, profile: RowhammerProfile) -> None:
        self._profile = profile

    # ------------------------------------------------------------------
    # Chunk encoding
    # ------------------------------------------------------------------

    def _encode(self, data: bytes, profile: RowhammerProfile) -> list[FlippableBit]:
        """Select which profile bits to flip to encode data."""
        bits_needed = len(data) * 8
        if len(profile.bits) < bits_needed:
            raise RuntimeError(
                f"profile has {len(profile.bits)} flippable bits, "
                f"need {bits_needed} for {len(data)} bytes"
            )
        selected = profile.bits[:bits_needed]
        result = []
        for i, fb in enumerate(selected):
            byte_idx = i // 8
            bit_idx = i % 8
            target_value = (data[byte_idx] >> bit_idx) & 1
            if target_value != fb.natural_value:
                result.append(fb)  # needs to be flipped
        return result

    def _decode(self, bit_positions: list[dict], n_bytes: int) -> bytes:
        """Read victim DRAM addresses and reconstruct chunk bytes."""
        result = bytearray(n_bytes)
        for i, bp in enumerate(bit_positions):
            byte_idx = i // 8
            bit_idx = i % 8
            victim_virt = bp["victim_virt"]
            bit_offset = bp["bit_offset"]
            byte_addr = victim_virt + bit_offset // 8
            byte_val = ctypes.c_uint8.from_address(byte_addr).value
            bit_val = (byte_val >> (bit_offset % 8)) & 1
            if bit_val:
                result[byte_idx] |= (1 << bit_idx)
        return bytes(result)

    # ------------------------------------------------------------------
    # write_chunk / read_chunk
    # ------------------------------------------------------------------

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        if not _LINUX_X86:
            raise RuntimeError("RowhammerAdapter requires Linux x86-64")

        data = encoded_chunk.encode()
        if len(data) > self._max:
            raise ValueError(f"chunk too large: {len(data)} > {self._max}")

        if self._profile is None:
            self._profile = self.empirical_profile()

        if self._profile.capacity_bytes() < len(data):
            raise RuntimeError(
                f"insufficient flippable bits for {len(data)} bytes; "
                f"re-run empirical_profile() on this hardware"
            )

        to_flip = self._encode(data, self._profile)

        # Flip each bit that needs to differ from its natural state
        for fb in to_flip:
            self._hammer(fb.aggressor_a_virt, fb.aggressor_b_virt)

        # Build location: full list of (victim, bit_offset, natural_value) for
        # all bits used, in order. Reader reconstructs from current DRAM state.
        bits_needed = len(data) * 8
        location_bits = [
            {
                "victim_virt": b.victim_virt,
                "bit_offset": b.bit_offset,
                "natural_value": b.natural_value,
            }
            for b in self._profile.bits[:bits_needed]
        ]
        location = "hammer://" + json.dumps({
            "n_bytes": len(data),
            "bits": location_bits,
        })
        return StoredChunkRef(adapter_name="rowhammer", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if not _LINUX_X86:
            raise RuntimeError("RowhammerAdapter requires Linux x86-64")

        payload = json.loads(ref.location.removeprefix("hammer://"))
        n_bytes = payload["n_bytes"]
        bits = payload["bits"]
        return self._decode(bits, n_bytes).decode()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        # Restore victim bits to natural values
        try:
            payload = json.loads(ref.location.removeprefix("hammer://"))
            for bp in payload["bits"]:
                byte_addr = bp["victim_virt"] + bp["bit_offset"] // 8
                bit_pos = bp["bit_offset"] % 8
                current = ctypes.c_uint8.from_address(byte_addr).value
                if bp["natural_value"] == 0:
                    current &= ~(1 << bit_pos)
                else:
                    current |= (1 << bit_pos)
                ctypes.c_uint8.from_address(byte_addr).value = current
            return True
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return []

    async def health_check(self) -> bool:
        try:
            _X86Primitives()
            return _LINUX_X86 and os.path.exists("/proc/self/pagemap")
        except Exception:
            return False
