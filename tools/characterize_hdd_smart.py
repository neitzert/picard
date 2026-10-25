"""Characterize a spinning HDD's SMART response to controlled seek patterns.

Run this script before using HddSmartAdapter to determine whether your specific
drive model responds to the seek-pattern encoding hypothesis. Different drive
models, firmware versions, and SMART implementations vary significantly.

Usage:
    python tools/characterize_hdd_smart.py /dev/sda

Requirements:
    - Linux
    - smartmontools: apt-get install smartmontools
    - Root or disk group membership
    - A genuine spinning HDD (not SSD, NVMe, or USB flash)

The script issues controlled seek patterns to odd and even LBA bands,
measures SMART attribute deltas across 5 trial pairs, and reports which
attributes (if any) respond measurably and reproducibly.

Output: go/no-go verdict plus per-attribute statistics. If the drive responds,
the output includes a calibration dict you can paste into picard.toml or pass
to HddSmartAdapter(calibration=...).
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import statistics
import subprocess
import sys
import time


GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

N_TRIALS    = 5
SEEKS_EACH  = 10_000
MIN_SEPARATION = 5.0  # raw SMART units; heuristic


def die(msg: str) -> None:
    print(f"{RED}ERROR:{RESET} {msg}", file=sys.stderr)
    sys.exit(1)


def read_smart(device: str) -> dict[int, int]:
    r = subprocess.run(
        ["smartctl", "-A", device],
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


def get_device_model(device: str) -> str:
    try:
        r = subprocess.run(
            ["smartctl", "-i", device],
            capture_output=True, text=True, timeout=15,
        )
        for line in r.stdout.splitlines():
            if "Device Model" in line or "Product" in line:
                return line.split(":", 1)[-1].strip()
    except Exception:
        pass
    return "unknown"


def get_device_sectors(device: str) -> int:
    try:
        base = os.path.basename(device)
        with open(f"/sys/block/{base}/size") as f:
            return int(f.read().strip())
    except OSError:
        return 1_000_000


def seek_to_band(device: str, lba_lo: int, lba_hi: int, count: int) -> None:
    fd = os.open(device, os.O_RDONLY)
    try:
        for _ in range(count):
            lba = random.randint(lba_lo, lba_hi)
            os.lseek(fd, lba * 512, os.SEEK_SET)
            try:
                os.read(fd, 512)
            except OSError:
                pass
    finally:
        os.close(fd)


def main() -> None:
    if sys.platform != "linux":
        die(f"This script requires Linux. Platform: {sys.platform}")

    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} /dev/sdX")
        sys.exit(1)

    device = sys.argv[1]

    if shutil.which("smartctl") is None:
        die("smartctl not found. Install smartmontools: apt-get install smartmontools")

    if not os.path.exists(device):
        die(f"Device {device!r} not found.")

    if not os.access(device, os.R_OK):
        die(
            f"Cannot read {device!r}. Run as root or: sudo usermod -aG disk $USER && newgrp disk"
        )

    print(f"\n{BOLD}{CYAN}HDD SMART characterization{RESET}")
    print(f"  Device:  {device}")
    print(f"  Model:   {get_device_model(device)}")
    print(f"  Trials:  {N_TRIALS} pairs ({SEEKS_EACH:,} seeks each side)")

    total_sectors = get_device_sectors(device)
    band_size = max(total_sectors // 20, 1000)

    print(f"  Sectors: {total_sectors:,}  (band size {band_size:,})")
    print()

    print("Reading baseline SMART attributes...")
    baseline = read_smart(device)
    if not baseline:
        die(
            "No SMART attributes found. Is SMART enabled? Try: smartctl -s on " + device
        )
    print(f"  Found {len(baseline)} attributes: {sorted(baseline.keys())}")
    print()

    # Run N_TRIALS pairs: odd band vs even band
    odd_deltas:  list[dict[int, int]] = []
    even_deltas: list[dict[int, int]] = []

    for trial in range(1, N_TRIALS + 1):
        odd_lo  = band_size
        odd_hi  = 2 * band_size
        even_lo = 2 * band_size
        even_hi = 3 * band_size

        odd_lo  = min(odd_lo,  total_sectors - 2)
        odd_hi  = min(odd_hi,  total_sectors - 1)
        even_lo = min(even_lo, total_sectors - 2)
        even_hi = min(even_hi, total_sectors - 1)

        print(f"Trial {trial}/{N_TRIALS}: odd band seeks...", end=" ", flush=True)
        before = read_smart(device)
        seek_to_band(device, odd_lo, odd_hi, SEEKS_EACH)
        after = read_smart(device)
        delta = {k: after.get(k, 0) - before.get(k, 0) for k in baseline}
        odd_deltas.append(delta)
        print("done")

        print(f"Trial {trial}/{N_TRIALS}: even band seeks...", end=" ", flush=True)
        before = read_smart(device)
        seek_to_band(device, even_lo, even_hi, SEEKS_EACH)
        after = read_smart(device)
        delta = {k: after.get(k, 0) - before.get(k, 0) for k in baseline}
        even_deltas.append(delta)
        print("done")

    print()
    print(f"{'Attr':>5}  {'Odd mean':>10}  {'Even mean':>10}  {'Separation':>12}  {'Std (odd)':>10}  {'Reliable?':>10}")
    print("-" * 70)

    results: list[dict] = []
    for attr_id in sorted(baseline.keys()):
        odd_vals  = [d.get(attr_id, 0) for d in odd_deltas]
        even_vals = [d.get(attr_id, 0) for d in even_deltas]
        odd_mean  = statistics.mean(odd_vals)
        even_mean = statistics.mean(even_vals)
        separation = abs(odd_mean - even_mean)
        odd_std = statistics.stdev(odd_vals) if len(odd_vals) > 1 else 0.0
        reliable = separation > MIN_SEPARATION and odd_std < separation / 2

        marker = f"{GREEN}YES{RESET}" if reliable else f"{'  -':>10}"
        print(
            f"{attr_id:5d}  {odd_mean:>10.1f}  {even_mean:>10.1f}  "
            f"{separation:>12.1f}  {odd_std:>10.1f}  {marker}"
        )
        results.append({
            "attr_id": attr_id,
            "odd_mean": odd_mean,
            "even_mean": even_mean,
            "separation": separation,
            "std": odd_std,
            "reliable": reliable,
        })

    print()

    reliable_attrs = [r for r in results if r["reliable"]]
    best = max(results, key=lambda r: r["separation"]) if results else None

    if reliable_attrs:
        best_attr = max(reliable_attrs, key=lambda r: r["separation"])
        print(
            f"{GREEN}{BOLD}VERDICT: Drive responds reliably to seek patterns.{RESET}\n"
            f"Best attribute: {best_attr['attr_id']} "
            f"(separation {best_attr['separation']:.1f})\n"
            f"The hdd-smart adapter should work on this drive."
        )

        calibration = {
            "device": device,
            "model": get_device_model(device),
            "attribute_id": best_attr["attr_id"],
            "band_size_sectors": band_size,
            "best_separation": best_attr["separation"],
            "reliable": True,
            "calibration_ts": time.time(),
        }
        print(f"\n{BOLD}Calibration dict (save to picard.toml or pass to HddSmartAdapter):{RESET}")
        print(json.dumps(calibration, indent=2))

    else:
        best_sep = best["separation"] if best else 0.0
        print(
            f"{RED}{BOLD}VERDICT: No reliable SMART response detected.{RESET}\n"
            f"Best separation found: {best_sep:.1f} raw units "
            f"(threshold: {MIN_SEPARATION}).\n"
            f"The hdd-smart adapter will not work reliably on this drive.\n\n"
            f"Possible causes:\n"
            f"  - SSD, NVMe, or USB flash (no mechanical seek calibration)\n"
            f"  - Drive firmware does not expose adaptive seek state via SMART\n"
            f"  - SMART attributes are vendor-specific and not responding to seeks\n"
            f"  - Insufficient seeks ({SEEKS_EACH:,}) to produce a measurable delta\n"
            f"    (try re-running with a higher SEEKS_EACH value in the script)"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
