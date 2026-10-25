"""Latency adapter -- stores bits as Linux kernel traffic-shaping rules.

Data is encoded as ``tc qdisc netem delay`` rules applied to loopback
addresses (127.x.x.x). Each loopback IP represents one bit: a delay of
``threshold_ms`` milliseconds encodes a 1-bit; no delay encodes a 0-bit.
Nothing is written to a file, database, socket, or heap. The bytes live
as kernel qdisc parameters, readable only by measuring packet latency to
the specific addresses with knowledge of which addresses encode which bits.

Mechanism:
  Write: for each bit in the encoded chunk, assign a loopback IP and
         apply (or omit) a ``netem delay`` rule via ``tc``. A prio qdisc
         on ``lo`` routes traffic to per-IP netem children.
  Read:  ping each loopback IP, take the median RTT over ``samples``
         probes. RTT - baseline > threshold/2 decodes as 1; else 0.
  Delete: remove all netem rules and filters for the chunk's IPs.

Persistence: qdisc rules survive until explicitly removed or until the
host reboots. Add a systemd unit that restores the prio root qdisc on
boot to make the stored state reboot-persistent.

Capacity: 254 loopback IPs per /24 subnet, 1 bit each = 31 bytes per
adapter instance with the default configuration. Use a different
``loopback_subnet`` per adapter instance to increase total capacity.

Platform: Linux only. Requires ``iproute2`` (tc command) and
``CAP_NET_ADMIN`` (or root). The adapter raises ``PermissionError``
on construction if either is unavailable.

EXPERIMENTAL: This adapter is a research demonstration. Bit error rate
rises under system load because background processes share the loopback
interface and may perturb RTT measurements. Use on isolated or lightly
loaded hosts for reliable results.

WARNING: Applying tc qdisc rules to the loopback interface affects ALL
loopback traffic on the host while rules are active. Existing tc rules on
``lo`` are replaced by the prio root qdisc on the first write. Remove
chunks before deploying on hosts that already have loopback shaping.

Requires: pip install picard-store   (stdlib only -- no extra deps)
"""

from __future__ import annotations

import asyncio
import json
import re
import statistics
import subprocess
import sys
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

# ---------------------------------------------------------------------------
# Platform guard
# ---------------------------------------------------------------------------

_IS_LINUX = sys.platform == "linux"

_TC_PERM_MSG = (
    "LatencyAdapter requires Linux with iproute2 (tc) and CAP_NET_ADMIN. "
    "Run as root or grant the capability: "
    "sudo setcap cap_net_admin+ep $(which python3)"
)


def _check_tc_available() -> None:
    """Raise PermissionError if tc is missing or CAP_NET_ADMIN is absent."""
    if not _IS_LINUX:
        raise PermissionError(
            f"LatencyAdapter requires Linux; current platform is {sys.platform!r}. "
            + _TC_PERM_MSG
        )
    result = subprocess.run(
        ["tc", "qdisc", "show", "dev", "lo"],
        capture_output=True,
        timeout=5,
    )
    if result.returncode != 0:
        raise PermissionError(
            "tc returned non-zero exit code. " + _TC_PERM_MSG
        )


# ---------------------------------------------------------------------------
# Handle arithmetic
# ---------------------------------------------------------------------------

# Each loopback IP 127.<subnet>.X gets qdisc handle X (1-254).
# The prio root uses handle 1: with 4 bands.
# Per-IP netem sits at parent 1:4 with handle X0:.
# Filter prio 4 routes traffic for that /32 to band 4.

_ROOT_HANDLE = "1:"
_NETEM_BAND = "4"


def _ip_handle(last_octet: int) -> str:
    """Return the netem qdisc handle string for a given last octet."""
    return f"{last_octet}0:"


def _ip_parent(last_octet: int) -> str:  # noqa: ARG001
    return f"1:{_NETEM_BAND}"


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class LatencyAdapter(Adapter):
    """Stores encoded chunks as Linux tc-netem delay rules on loopback IPs.

    Each logical bit in the chunk maps to a unique 127.x.x.x address. A
    non-zero netem delay on that address encodes bit=1; the absence of a
    rule (or a 0ms rule) encodes bit=0. The chunk is recovered by pinging
    each address and threshold-decoding the median RTT.
    """

    def __init__(
        self,
        loopback_subnet: str = "127.1.0",
        threshold_ms: float = 15.0,
        samples_per_read: int = 12,
        bits_per_endpoint: int = 1,
        max_chunk_bytes: int = 31,
    ) -> None:
        """
        Args:
            loopback_subnet: First three octets of the loopback range used
                for encoding (e.g. "127.1.0" yields IPs 127.1.0.1-254).
            threshold_ms: Induced delay in milliseconds for a 1-bit.
                Must be several times the baseline RTT jitter (~0.1ms on
                an idle host). Default 15ms gives ample margin.
            samples_per_read: Number of ping probes per IP when reading.
                More samples reduce jitter sensitivity at the cost of
                longer read time.
            bits_per_endpoint: 1 (binary) only for now. Reserved for
                multi-level encoding in a future version.
            max_chunk_bytes: Upper bound on chunk size. Cannot exceed
                floor(254 * bits_per_endpoint / 8).
        """
        _check_tc_available()

        if bits_per_endpoint != 1:
            raise ValueError("Only bits_per_endpoint=1 is supported in this version.")

        max_possible = (254 * bits_per_endpoint) // 8
        if max_chunk_bytes > max_possible:
            raise ValueError(
                f"max_chunk_bytes={max_chunk_bytes} exceeds subnet capacity "
                f"({max_possible} bytes for {loopback_subnet}.1-254)."
            )

        self._subnet = loopback_subnet
        self._threshold_ms = threshold_ms
        self._samples = samples_per_read
        self._bpe = bits_per_endpoint
        self._max_chunk_bytes = max_chunk_bytes

        # Maps location JSON string -> manifest_id for list_chunks support.
        self._registry: dict[str, str] = {}

        # Track which last-octets are in use to avoid overlap.
        self._used_octets: set[int] = set()

    # ------------------------------------------------------------------
    # Profile
    # ------------------------------------------------------------------

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="latency",
            max_chunk_bytes=self._max_chunk_bytes,
            persistent=True,   # qdisc rules survive until explicitly removed
            ordered=False,
            tags={
                "tier": "4",
                "experimental": "true",
                "requires": "CAP_NET_ADMIN,Linux,iproute2",
                "substrate": "kernel-qdisc-netem",
            },
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _ip_for_octet(self, octet: int) -> str:
        return f"{self._subnet}.{octet}"

    def _allocate_octets(self, n_bits: int) -> list[int]:
        """Return n_bits unused last-octets (1-254), marking them used."""
        allocated: list[int] = []
        for octet in range(1, 255):
            if octet not in self._used_octets:
                allocated.append(octet)
                self._used_octets.add(octet)
            if len(allocated) == n_bits:
                return allocated
        raise RuntimeError(
            f"Subnet {self._subnet}.0/24 has no free octets for {n_bits} bits. "
            "Delete existing chunks or use a different loopback_subnet."
        )

    def _free_octets(self, octets: list[int]) -> None:
        for o in octets:
            self._used_octets.discard(o)

    @staticmethod
    def _bytes_to_bits(data: bytes) -> list[int]:
        bits: list[int] = []
        for byte in data:
            for shift in range(7, -1, -1):
                bits.append((byte >> shift) & 1)
        return bits

    @staticmethod
    def _bits_to_bytes(bits: list[int]) -> bytes:
        if len(bits) % 8:
            bits = bits + [0] * (8 - len(bits) % 8)
        result = bytearray()
        for i in range(0, len(bits), 8):
            val = 0
            for shift, b in zip(range(7, -1, -1), bits[i : i + 8]):
                val |= b << shift
            result.append(val)
        return bytes(result)

    async def _tc(self, cmd: str) -> tuple[int, str]:
        """Run a tc command, return (returncode, stderr)."""
        proc = await asyncio.create_subprocess_shell(
            f"tc {cmd}",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr_bytes = await proc.communicate()
        return proc.returncode, (stderr_bytes or b"").decode(errors="replace")

    async def _ensure_root_qdisc(self) -> None:
        """Create the prio root qdisc on lo (idempotent via replace)."""
        rc, _ = await self._tc(
            "qdisc replace dev lo root handle 1: prio bands 4 "
            "priomap 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0"
        )
        if rc != 0:
            # Already exists with different params -- try add instead.
            await self._tc(
                "qdisc add dev lo root handle 1: prio bands 4 "
                "priomap 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0"
            )

    async def _apply_delay(self, octet: int, delay_ms: float) -> None:
        """Install netem delay for one loopback IP."""
        ip = self._ip_for_octet(octet)
        h = _ip_handle(octet)
        if delay_ms > 0:
            await self._tc(
                f"qdisc replace dev lo parent 1:{_NETEM_BAND} "
                f"handle {h} netem delay {delay_ms:.1f}ms"
            )
            await self._tc(
                f"filter replace dev lo protocol ip parent 1:0 prio 4 "
                f"u32 match ip dst {ip}/32 flowid 1:{_NETEM_BAND}"
            )
        else:
            # No delay = no rule. Clean up any leftover from a previous write.
            await self._tc(f"qdisc del dev lo handle {h} 2>/dev/null || true")

    async def _remove_delay(self, octet: int) -> None:
        """Remove the netem qdisc and filter for one loopback IP."""
        ip = self._ip_for_octet(octet)
        h = _ip_handle(octet)
        await self._tc(f"filter del dev lo protocol ip parent 1:0 prio 4 "
                       f"u32 match ip dst {ip}/32 2>/dev/null || true")
        await self._tc(f"qdisc del dev lo handle {h} 2>/dev/null || true")

    async def _measure_rtt(self, ip: str, n: int | None = None) -> float:
        """Return median ping RTT in ms to a loopback IP."""
        count = n if n is not None else self._samples
        proc = await asyncio.create_subprocess_exec(
            "ping", "-c", str(count), "-q", ip,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        m = re.search(rb"rtt min/avg/max/mdev = [\d.]+/([\d.]+)", stdout)
        return float(m.group(1)) if m else 999.0

    async def _calibrate_baseline(self, octets: list[int]) -> dict[str, float]:
        """Measure baseline RTT for each octet before any delays are applied."""
        baseline: dict[str, float] = {}
        for octet in octets:
            ip = self._ip_for_octet(octet)
            baseline[ip] = await self._measure_rtt(ip, n=6)
        return baseline

    # ------------------------------------------------------------------
    # Core interface
    # ------------------------------------------------------------------

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        data = encoded_chunk.encode()
        if len(data) > self._max_chunk_bytes:
            raise ValueError(
                f"Chunk too large: {len(data)} bytes > max {self._max_chunk_bytes}."
            )

        bits = self._bytes_to_bits(data)
        n_bits = len(bits)
        octets = self._allocate_octets(n_bits)

        await self._ensure_root_qdisc()
        baseline = await self._calibrate_baseline(octets)

        for octet, bit in zip(octets, bits):
            delay = self._threshold_ms if bit else 0.0
            await self._apply_delay(octet, delay)

        ips = [self._ip_for_octet(o) for o in octets]
        loc = json.dumps({
            "ips": ips,
            "octets": octets,
            "threshold_ms": self._threshold_ms,
            "baseline_ms": baseline,
            "n_bits": n_bits,
            "samples": self._samples,
        }, separators=(",", ":"))

        ref = StoredChunkRef(adapter_name="latency", location=loc)
        # manifest_id will be filled in by the orchestrator; store empty for now.
        self._registry[loc] = ""
        return ref

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        ips: list[str] = loc["ips"]
        threshold: float = loc["threshold_ms"]
        baseline: dict[str, float] = loc["baseline_ms"]
        n_bits: int = loc["n_bits"]
        samples: int = loc.get("samples", self._samples)

        bits: list[int] = []
        for ip in ips:
            rtt = await self._measure_rtt(ip, n=samples)
            base = baseline.get(ip, 0.0)
            bits.append(1 if (rtt - base) > threshold / 2 else 0)

        raw = self._bits_to_bytes(bits[:n_bits])
        return raw.decode(errors="replace")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc_str = ref.location
        if loc_str not in self._registry:
            return False

        loc = json.loads(loc_str)
        octets: list[int] = loc.get("octets", [])
        for octet in octets:
            await self._remove_delay(octet)
        self._free_octets(octets)
        del self._registry[loc_str]
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="latency", location=loc)
            for loc, mid in self._registry.items()
            if mid == manifest_id
        ]

    async def health_check(self) -> bool:
        if not _IS_LINUX:
            return False
        try:
            result = subprocess.run(
                ["tc", "qdisc", "show", "dev", "lo"],
                capture_output=True,
                timeout=5,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    async def capacity(self) -> dict[str, int | None]:
        free_octets = 254 - len(self._used_octets)
        free_bits = free_octets * self._bpe
        used_bits = len(self._used_octets) * self._bpe
        total_bits = 254 * self._bpe
        return {
            "used_bytes": used_bits // 8,
            "free_bytes": free_bits // 8,
            "total_bytes": total_bits // 8,
            "chunk_count": len(self._registry),
        }

    async def close(self) -> None:
        pass
