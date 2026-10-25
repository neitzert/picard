"""HalfDouble adapter -- two-hop rowhammer, bypasses Target Row Refresh.

HalfDouble (Google Project Zero, 2021) is a rowhammer variant that achieves
bit flips two DRAM rows away from the hammered aggressor row, rather than the
adjacent row targeted by classic double-sided rowhammer. The technique
exploits the fact that DRAM electromagnetic coupling is not strictly limited
to adjacent rows: charge disturbance propagates with diminishing intensity
across multiple rows, and DDR4 Target Row Refresh (TRR) mitigations that
protect directly adjacent rows may leave two-hop victims unguarded.

Pattern: given rows N-2, N-1, N (victim), N+1, N+2 in the same bank:
  - Classic double-sided: hammer N-1 and N+1 (adjacent), flips N.
  - HalfDouble: hammer N-1 (near, high rate) and N-2 (far, lower rate)
    simultaneously. The far aggressor's disturbance propagates through N-1
    and compounds with the near aggressor's direct coupling at N, producing
    flips in N even though N-2 and N are not adjacent.

Why TRR does not stop this: TRR tracks aggressor rows and refreshes their
immediate neighbours when an aggressor threshold is reached. Hammering N-2
may not trigger TRR for N (two hops away), and the non-uniform access
pattern (near/far rate split) can stay below TRR per-row thresholds while
still accumulating enough disturbance to flip N.

Implication for PICARD: this extends the rowhammer storage substrate to
hardware where classic rowhammer is fully mitigated by TRR. HalfDouble
recovers write capability on DDR4 devices that were considered safe.

Inheritance: this adapter inherits all profile, encoding, and chunk I/O
logic from RowhammerAdapter. Only the hammer method is overridden.

EXPERIMENTAL: Run at your own risk. Half-double flippability is highly
chip-specific. Many DDR4 modules are not vulnerable. Profiling on a
non-vulnerable machine will find zero flippable bits; write_chunk will
raise RuntimeError rather than silently corrupt memory.

Platform: Linux x86-64 (same requirements as RowhammerAdapter).
Volatile: all data lost on power cycle or page reclaim.

Reference: "Half-Double: Hammering From the Next Row Over"
           Project Zero blog, June 2021.
           https://googleprojectzero.blogspot.com/2021/06/half-double.html
"""

from __future__ import annotations

import ctypes

from picard.adapters.rowhammer_adapter import (
    RowhammerAdapter,
    DramGeometry,
    DRAM_ROW_BYTES,
    _LINUX_X86,
    FlippableBit,
    RowhammerProfile,
    _alloc_huge_pages,
    _virt_to_phys,
)
from picard.adapters.base import AdapterProfile


# Ratio of near-aggressor to far-aggressor accesses per hammer iteration.
# Near aggressor gets hammered NEAR_FAR_RATIO times more than the far aggressor.
# Values between 3 and 8 produce the strongest half-double effect empirically.
_DEFAULT_NEAR_FAR_RATIO: int = 5


class HalfDoubleAdapter(RowhammerAdapter):
    """HalfDouble two-hop rowhammer for DDR4 with TRR mitigation.

    Overrides the single hammer loop with a near/far split pattern. The near
    aggressor (one row from victim) is accessed at high frequency; the far
    aggressor (two rows from victim) is interspersed at lower frequency.
    This compounds disturbance at the victim without saturating either
    aggressor's TRR counter alone.

    All other adapter behaviour (profiling, chunk encoding, read/write) is
    inherited from RowhammerAdapter. The only change is which physical rows
    are hammered and at what ratio.
    """

    def __init__(
        self,
        *,
        near_far_ratio: int = _DEFAULT_NEAR_FAR_RATIO,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._nf_ratio = near_far_ratio

    def profile(self) -> AdapterProfile:
        p = super().profile()
        return AdapterProfile(
            name="halfdouble",
            max_chunk_bytes=p.max_chunk_bytes,
            persistent=False,
            ordered=False,
            rate_limit_per_min=p.rate_limit_per_min,
        )

    # ------------------------------------------------------------------
    # HAL: find near/far aggressor triples (two hops)
    # ------------------------------------------------------------------

    def _halfdouble_candidates(
        self, base_virt: int, size: int
    ) -> list[tuple[int, int, int]]:
        """Return (near_aggressor, far_aggressor, victim) virtual address triples.

        Near aggressor: one row above the victim in the same bank.
        Far aggressor: two rows above the victim in the same bank.
        Victim: the row to be flipped.

        The victim is flanked asymmetrically: N-1 (near) and N-2 (far) are
        the aggressors; N is the victim. We do not need a row below the victim
        for the half-double pattern, unlike classic double-sided rowhammer.
        """
        candidates = []
        row_size = DRAM_ROW_BYTES
        n_rows = size // row_size

        try:
            from collections import defaultdict
            bank_rows: dict[int, list[tuple[int, int]]] = defaultdict(list)

            for i in range(n_rows):
                virt = base_virt + i * row_size
                try:
                    phys = _virt_to_phys(virt)
                    row_num = self._geometry.row(phys)
                    bank = self._geometry.bank(phys)
                    bank_rows[bank].append((row_num, virt))
                except Exception:
                    continue

            for bank, rows in bank_rows.items():
                rows.sort(key=lambda x: x[0])
                for j in range(2, len(rows)):
                    # rows[j-2] = far aggressor (row N-2)
                    # rows[j-1] = near aggressor (row N-1)
                    # rows[j]   = victim (row N)
                    if (rows[j][0] - rows[j-1][0] == 1 and
                            rows[j-1][0] - rows[j-2][0] == 1):
                        far_agg = rows[j-2][1]
                        near_agg = rows[j-1][1]
                        victim = rows[j][1]
                        candidates.append((near_agg, far_agg, victim))

        except Exception:
            # Fallback: assume rows are physically contiguous
            for i in range(2, n_rows):
                near_agg = base_virt + (i - 1) * row_size
                far_agg = base_virt + (i - 2) * row_size
                victim = base_virt + i * row_size
                candidates.append((near_agg, far_agg, victim))

        return candidates

    # ------------------------------------------------------------------
    # Hammer override: non-uniform near/far access pattern
    # ------------------------------------------------------------------

    def _hammer(self, aggressor_a: int, aggressor_b: int) -> None:
        """HalfDouble non-uniform hammer.

        aggressor_a is treated as the NEAR aggressor (accessed every iteration).
        aggressor_b is treated as the FAR aggressor (accessed every 1/ratio iterations).

        The asymmetric access pattern is what distinguishes half-double from
        classic rowhammer and allows it to bypass per-row TRR thresholds.
        """
        hw = self._primitives()
        ptr_near = ctypes.c_void_p(aggressor_a)
        ptr_far = ctypes.c_void_p(aggressor_b)

        for i in range(self._iterations):
            # Always access near aggressor
            ctypes.c_uint8.from_address(aggressor_a)
            hw.clflush(ptr_near)
            # Access far aggressor less frequently
            if i % self._nf_ratio == 0:
                ctypes.c_uint8.from_address(aggressor_b)
                hw.clflush(ptr_far)

    # ------------------------------------------------------------------
    # Override empirical profiler to use half-double candidate search
    # ------------------------------------------------------------------

    def empirical_profile(self, max_bits: int = 4096) -> RowhammerProfile:
        """Profile DRAM for half-double flippable bits.

        Uses the two-hop aggressor pattern instead of double-sided. May
        find different (or additional) flippable bits compared to classic
        rowhammer on the same hardware.
        """
        base, _ = self._ensure_region()
        candidates = self._halfdouble_candidates(base, self._region_bytes)
        flippable: list[FlippableBit] = []

        for near_agg, far_agg, victim in candidates:
            if len(flippable) >= max_bits:
                break

            row_size = DRAM_ROW_BYTES
            before = (ctypes.c_uint8 * row_size).from_address(victim)[:]

            # Use the overridden _hammer with the half-double pattern
            self._hammer(near_agg, far_agg)

            after = (ctypes.c_uint8 * row_size).from_address(victim)[:]
            for byte_idx, (b0, b1) in enumerate(zip(before, after)):
                diff = b0 ^ b1
                for bit_idx in range(8):
                    if diff & (1 << bit_idx):
                        nat = (b0 >> bit_idx) & 1
                        flippable.append(FlippableBit(
                            aggressor_a_virt=near_agg,
                            aggressor_b_virt=far_agg,
                            victim_virt=victim,
                            bit_offset=byte_idx * 8 + bit_idx,
                            natural_value=nat,
                        ))

            # Restore victim row
            for i, b in enumerate(before):
                ctypes.c_uint8.from_address(victim + i).value = b

        profile = RowhammerProfile(
            bits=flippable,
            geometry=f"HalfDouble[{type(self._geometry).__name__}]",
        )
        self._profile = profile
        return profile
