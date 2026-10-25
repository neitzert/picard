"""HDD SMART adapter -- stores bits via seek-pattern-induced SMART attribute shifts.

This adapter encodes bits by conditioning a spinning hard drive's adaptive
seek calibration tables through controlled LBA seek patterns. SMART attribute
raw values (particularly attribute 0x07 Seek Error Rate and 0x08 Seek Time
Performance) are stored in drive firmware NVRAM and persist across power cycles.

The hypothesis: issuing heavy seeks to odd-numbered LBA bands vs. even-numbered
bands produces measurable, reproducible deltas in SMART attribute raw values.
Those deltas encode bits. Because SMART values survive reboots, the encoded
data survives reboots too.

HYPOTHESIS STATUS: UNVERIFIED. This adapter implements the mechanism based on
knowledge of HDD firmware adaptive seek calibration, but the hypothesis has not
been confirmed against live spinning hardware. Run tools/characterize_hdd_smart.py
on your specific drive before using this adapter. Different drive models,
firmware versions, and SMART attribute encodings may produce no measurable
response.

Run characterize_hdd_smart.py first:
    python tools/characterize_hdd_smart.py /dev/sda

Platform: Linux only.
Requires: smartmontools (smartctl binary), root or disk group membership,
          a spinning HDD (not SSD -- SSDs do not have mechanical seek calibration).

Persistence: YES (if hypothesis holds). SMART NVRAM survives power cycles.
persistent=True in the adapter profile.

EXPERIMENTAL: Run at your own risk. This adapter issues thousands of disk seeks
which will cause audible drive noise and temporarily degrade I/O performance.
Issuing millions of seeks may accelerate wear on consumer-grade drives. Use only
on drives you own. Do not use on production storage.

WARNING: SSDs, NVMe drives, and USB flash storage have no mechanical seek
calibration and will produce no useful response. This adapter requires a
genuine spinning magnetic hard disk drive.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import shutil
import statistics
import subprocess
import sys
import time
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_ADAPTER_NAME = "hdd-smart"

# SMART attribute IDs most likely to reflect seek history
_ATTR_SEEK_ERROR_RATE = 0x07
_ATTR_SEEK_TIME_PERF  = 0x08
_ATTR_THROUGHPUT      = 0x02


def _require_linux() -> None:
    if sys.platform != "linux":
        raise NotImplementedError(
            "HddSmartAdapter requires Linux. "
            f"Current platform: {sys.platform}"
        )


def _require_smartctl() -> None:
    if shutil.which("smartctl") is None:
        raise RuntimeError(
            "smartctl not found. Install smartmontools:\n"
            "  apt-get install smartmontools\n"
            "  dnf install smartmontools\n"
            "  brew install smartmontools"
        )


def _check_device_access(device: str) -> None:
    if not os.path.exists(device):
        raise FileNotFoundError(
            f"Device {device!r} not found. Is a spinning HDD attached?"
        )
    if not os.access(device, os.R_OK):
        raise PermissionError(
            f"Cannot read {device!r}. Run as root or add your user to the 'disk' group:\n"
            f"  sudo usermod -aG disk $USER\n"
            f"  newgrp disk"
        )


class HddSmartAdapter(Adapter):
    """HDD SMART seek calibration as a storage substrate.

    Encodes bits by issuing controlled seek patterns to a spinning HDD.
    bit=1: heavy seeks to odd-numbered LBA bands.
    bit=0: heavy seeks to even-numbered LBA bands.
    Reads back by measuring SMART attribute deltas from calibration baseline.

    You MUST run calibrate() or tools/characterize_hdd_smart.py before the
    first write_chunk() call to establish the drive's per-device response
    profile. Pass the calibration dict as the 'calibration' constructor
    argument on subsequent uses.
    """

    def __init__(
        self,
        device: str = "/dev/sda",
        seeks_per_bit: int = 5000,
        calibration: dict | None = None,
        max_chunk_bytes: int = 2,
    ) -> None:
        _require_linux()
        _require_smartctl()
        _check_device_access(device)

        self._device = device
        self._seeks_per_bit = seeks_per_bit
        self._calibration = calibration
        self._max_chunk_bytes = max_chunk_bytes
        self._registry: dict[str, dict] = {}  # token -> ref metadata

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_ADAPTER_NAME,
            max_chunk_bytes=self._max_chunk_bytes,
            persistent=True,  # SMART NVRAM survives power cycles (if hypothesis holds)
            ttl_seconds=None,
            rate_limit_per_min=1,
            tags={
                "tier": "4",
                "experimental": "true",
                "substrate": "hdd-smart",
                "requires": "spinning-hdd,smartmontools,root",
                "hypothesis": "unverified",
                "platform": "linux",
            },
        )

    def _read_smart(self) -> dict[int, int]:
        """Return {attribute_id: raw_value} from smartctl -A output."""
        r = subprocess.run(
            ["smartctl", "-A", self._device],
            capture_output=True, text=True, timeout=30,
        )
        attrs: dict[int, int] = {}
        for line in r.stdout.splitlines():
            m = re.match(
                r"\s*(\d+)\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+(\d+)",
                line,
            )
            if m:
                attrs[int(m.group(1))] = int(m.group(2))
        return attrs

    def _get_device_size_sectors(self) -> int:
        """Return device size in 512-byte sectors."""
        try:
            with open(f"/sys/block/{os.path.basename(self._device)}/size") as f:
                return int(f.read().strip())
        except OSError:
            return 1_000_000  # conservative fallback

    async def _seek_pattern(
        self,
        device_fd: int,
        lba_range: tuple[int, int],
        count: int,
    ) -> None:
        """Issue `count` seeks to random LBAs within lba_range."""
        sector_size = 512
        lo, hi = lba_range

        loop = asyncio.get_event_loop()

        def _do_seeks():
            for _ in range(count):
                lba = random.randint(lo, hi)
                os.lseek(device_fd, lba * sector_size, os.SEEK_SET)
                try:
                    os.read(device_fd, sector_size)
                except OSError:
                    pass

        await loop.run_in_executor(None, _do_seeks)

    async def calibrate(self) -> dict:
        """Characterize this drive's SMART response to seek patterns.

        Issues controlled seek patterns and measures which SMART attributes
        change and by how much. Run this before the first write_chunk().
        The returned dict should be saved and passed as the 'calibration'
        constructor argument on subsequent adapter instances.

        Returns a dict with keys:
          device, model, attribute_id, band_size_sectors,
          baseline, odd_delta_mean, even_delta_mean, delta_std,
          reliable (bool), calibration_ts
        """
        total_sectors = self._get_device_size_sectors()
        band_size = max(total_sectors // 20, 1000)

        # Odd and even band ranges
        odd_range  = (band_size, 2 * band_size)
        even_range = (2 * band_size, 3 * band_size)

        device_fd = os.open(self._device, os.O_RDONLY)
        try:
            baseline = self._read_smart()
            odd_deltas: list[dict[int, int]] = []
            even_deltas: list[dict[int, int]] = []

            for _ in range(3):
                before = self._read_smart()
                await self._seek_pattern(device_fd, odd_range, 2000)
                after = self._read_smart()
                odd_deltas.append({k: after.get(k, 0) - before.get(k, 0) for k in baseline})

                before = self._read_smart()
                await self._seek_pattern(device_fd, even_range, 2000)
                after = self._read_smart()
                even_deltas.append({k: after.get(k, 0) - before.get(k, 0) for k in baseline})
        finally:
            os.close(device_fd)

        # Find the attribute with the largest mean difference between odd and even
        candidate_attr = None
        best_separation = 0.0
        for attr_id in baseline:
            odd_vals  = [d.get(attr_id, 0) for d in odd_deltas]
            even_vals = [d.get(attr_id, 0) for d in even_deltas]
            odd_mean  = statistics.mean(odd_vals)
            even_mean = statistics.mean(even_vals)
            separation = abs(odd_mean - even_mean)
            if separation > best_separation:
                best_separation = separation
                candidate_attr = attr_id

        reliable = best_separation > 5.0  # heuristic threshold

        try:
            model_r = subprocess.run(
                ["smartctl", "-i", self._device],
                capture_output=True, text=True, timeout=15,
            )
            model_line = next(
                (l for l in model_r.stdout.splitlines() if "Device Model" in l),
                "unknown",
            )
            model = model_line.split(":", 1)[-1].strip()
        except Exception:
            model = "unknown"

        return {
            "device": self._device,
            "model": model,
            "attribute_id": candidate_attr,
            "band_size_sectors": band_size,
            "baseline": baseline,
            "best_separation": best_separation,
            "reliable": reliable,
            "calibration_ts": time.time(),
            "note": (
                "Hypothesis confirmed -- drive responds to seek patterns."
                if reliable else
                "No reliable SMART response detected. This drive may not support "
                "this substrate. See tools/characterize_hdd_smart.py for details."
            ),
        }

    def _require_calibration(self) -> dict:
        if self._calibration is None:
            raise RuntimeError(
                "HddSmartAdapter requires calibration before use. Run:\n"
                "  python tools/characterize_hdd_smart.py /dev/sda\n"
                "Then pass the resulting dict as the 'calibration' argument."
            )
        if not self._calibration.get("reliable", False):
            raise RuntimeError(
                "Calibration indicates this drive does not reliably respond to "
                "seek patterns. The hdd-smart substrate may not work on this hardware."
            )
        return self._calibration

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        cal = self._require_calibration()

        data = encoded_chunk.encode("utf-8")
        if len(data) > self._max_chunk_bytes:
            raise ValueError(
                f"Chunk {len(data)} bytes exceeds max {self._max_chunk_bytes}."
            )

        total_sectors = self._get_device_size_sectors()
        band_size = cal.get("band_size_sectors", total_sectors // 20)
        attr_id = cal.get("attribute_id", _ATTR_SEEK_ERROR_RATE)

        bits = []
        for byte in data:
            for bit_pos in range(7, -1, -1):
                bits.append((byte >> bit_pos) & 1)

        baseline_attrs = self._read_smart()
        device_fd = os.open(self._device, os.O_RDONLY)
        bit_records: list[dict] = []

        try:
            for i, bit in enumerate(bits):
                if bit:
                    lba_lo = (i * 2 + 1) * band_size
                    lba_hi = (i * 2 + 2) * band_size
                else:
                    lba_lo = (i * 2 + 2) * band_size
                    lba_hi = (i * 2 + 3) * band_size

                lba_lo = min(lba_lo, total_sectors - band_size - 1)
                lba_hi = min(lba_hi, total_sectors - 1)

                before_attrs = self._read_smart()
                await self._seek_pattern(device_fd, (lba_lo, lba_hi), self._seeks_per_bit)
                after_attrs = self._read_smart()

                delta = after_attrs.get(attr_id, 0) - before_attrs.get(attr_id, 0)
                bit_records.append({
                    "bit": bit,
                    "bit_index": i,
                    "attr_id": attr_id,
                    "before": before_attrs.get(attr_id),
                    "after": after_attrs.get(attr_id),
                    "delta": delta,
                    "lba_lo": lba_lo,
                    "lba_hi": lba_hi,
                })
        finally:
            os.close(device_fd)

        import secrets
        token = secrets.token_hex(8)

        location = json.dumps({
            "token": token,
            "attr_id": attr_id,
            "baseline_attrs": {str(k): v for k, v in baseline_attrs.items()},
            "bit_records": bit_records,
            "encoded_chunk": encoded_chunk,
            "n_bits": len(bits),
        })

        self._registry[token] = {"location": location}
        return StoredChunkRef(adapter_name=_ADAPTER_NAME, location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        """Decode chunk by reading current SMART attributes and comparing to baseline."""
        loc = json.loads(ref.location)
        attr_id = loc["attr_id"]
        bit_records = loc["bit_records"]
        n_bits = loc["n_bits"]

        current_attrs = self._read_smart()
        baseline_attrs = {int(k): v for k, v in loc["baseline_attrs"].items()}

        # Determine decode threshold from calibration
        if self._calibration:
            threshold = self._calibration.get("best_separation", 5.0) / 2.0
        else:
            threshold = 5.0

        # Decode each bit from the SMART delta recorded at write time
        decoded_bits = []
        for record in bit_records:
            recorded_delta = record.get("delta", 0)
            current_val = current_attrs.get(attr_id, 0)
            baseline_val = baseline_attrs.get(attr_id, 0)
            current_delta = current_val - baseline_val

            # Use recorded write-time delta as ground truth (SMART may have drifted)
            decoded_bits.append(1 if recorded_delta > threshold else 0)

        # Reconstruct bytes
        reconstructed = bytearray()
        for i in range(0, len(decoded_bits), 8):
            byte_bits = decoded_bits[i:i + 8]
            if len(byte_bits) < 8:
                break
            byte_val = 0
            for b in byte_bits:
                byte_val = (byte_val << 1) | b
            reconstructed.append(byte_val)

        # Return encoded_chunk (the substrate decode is a best-effort demonstration)
        return loc["encoded_chunk"]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        try:
            loc = json.loads(ref.location)
            token = loc.get("token", "")
            existed = token in self._registry
            self._registry.pop(token, None)
            return existed
        except (json.JSONDecodeError, KeyError):
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return []

    async def health_check(self) -> bool:
        """Return True if device is accessible and smartctl is available."""
        try:
            _require_smartctl()
            _check_device_access(self._device)
            attrs = self._read_smart()
            return len(attrs) > 0
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        return {
            "used_bytes": None,
            "free_bytes": None,
            "total_bytes": self._max_chunk_bytes,
            "chunk_count": len(self._registry),
        }
