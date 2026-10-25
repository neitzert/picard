"""Live hardware test for CpuCacheAdapter -- run on Linux x86-64 bare metal.

Tests the actual Flush+Reload JIT path (CLFLUSH + RDTSC machine code),
not the fallback. Demonstrates the covert channel works as a real two-process
storage substrate on the target machine.

Usage:
    python tools/test_cpu_cache_live.py

Requires: Linux x86-64. Run as normal user (no root needed).

What this tests:
  1. JIT compilation: CLFLUSH and RDTSC machine code executes without segfault.
  2. Threshold calibration: measures actual L3 hit vs DRAM miss cycle counts.
  3. Single-process roundtrip: write chunk, read back, verify bytes.
  4. Cross-process channel: spawn a reader subprocess, write from this process,
     confirm the reader decodes the correct bytes via cache line residency only.
  5. Noise floor: write alternating 0/1 bits 100 times, measure bit error rate.
"""

import os
import subprocess
import sys
import tempfile
import time

# ------ platform guard -------------------------------------------------------

if sys.platform != "linux":
    print("SKIP: requires Linux (this machine is not Linux)")
    sys.exit(0)

import os
if os.uname().machine not in ("x86_64", "amd64"):
    print("SKIP: requires x86-64 (this machine is", os.uname().machine, ")")
    sys.exit(0)

# ------ ensure we can import from project root --------------------------------

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from picard.adapters.cpu_cache_adapter import (
    CpuCacheAdapter,
    DEFAULT_THRESHOLD,
    _get_primitives,
    _calibrate_threshold,
    _open_shared_mmap,
    BYTES_PER_BIT,
    _X86,
)
import ctypes
import asyncio

# Colors for terminal output
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def ok(msg):  print(f"  {GREEN}[PASS]{RESET} {msg}")
def fail(msg): print(f"  {RED}[FAIL]{RESET} {msg}"); return False
def info(msg): print(f"  {CYAN}     {RESET} {msg}")


# =============================================================================
# Test 1: JIT compilation
# =============================================================================

def test_jit():
    print(f"\n{BOLD}1. JIT compilation{RESET}")
    if not _X86:
        return fail("_X86 is False -- adapter will use fallback, not JIT")
    try:
        clflush, rdtsc, buf = _get_primitives()
    except Exception as e:
        return fail(f"_get_primitives() raised: {e}")

    # Verify RDTSC returns a plausible TSC value
    t0 = rdtsc()
    time.sleep(0.001)
    t1 = rdtsc()
    delta = t1 - t0
    if delta < 1_000:
        return fail(f"RDTSC delta too small ({delta}); likely not a real TSC")
    ok(f"RDTSC: delta over 1ms = {delta:,} cycles  (~{delta / 1e6:.0f}M cycles/sec)")

    # Verify CLFLUSH doesn't crash
    arr = (ctypes.c_uint8 * 64)()
    addr = ctypes.addressof(arr)
    try:
        clflush(ctypes.c_void_p(addr))
    except Exception as e:
        return fail(f"CLFLUSH raised: {e}")
    ok("CLFLUSH executed without crash")
    return True


# =============================================================================
# Test 2: Threshold calibration
# =============================================================================

def test_calibration(shared_path: str):
    print(f"\n{BOLD}2. L3 hit vs DRAM miss threshold calibration{RESET}")
    n_bits = 256
    region = n_bits * BYTES_PER_BIT
    base, buf = _open_shared_mmap(shared_path, region)

    clflush, rdtsc, _ = _get_primitives()

    # Measure L3 hits: load line, time it while still in cache
    hit_samples = []
    for i in range(n_bits):
        addr = base + i * BYTES_PER_BIT
        ctypes.c_uint8.from_address(addr)          # bring into L3
        t0 = rdtsc()
        ctypes.c_uint8.from_address(addr)          # should hit L3
        t1 = rdtsc()
        hit_samples.append(t1 - t0)
        clflush(ctypes.c_void_p(addr))

    # Measure DRAM misses: flush line, time cold access
    miss_samples = []
    for i in range(n_bits):
        addr = base + i * BYTES_PER_BIT
        clflush(ctypes.c_void_p(addr))
        t0 = rdtsc()
        ctypes.c_uint8.from_address(addr)          # cold DRAM access
        t1 = rdtsc()
        miss_samples.append(t1 - t0)
        clflush(ctypes.c_void_p(addr))

    avg_hit  = sum(hit_samples)  // len(hit_samples)
    avg_miss = sum(miss_samples) // len(miss_samples)
    threshold = (avg_hit + avg_miss) // 2
    separation = avg_miss - avg_hit

    info(f"L3 hit avg:  {avg_hit:5} cycles")
    info(f"DRAM miss avg: {avg_miss:5} cycles")
    info(f"Separation:  {separation:5} cycles")
    info(f"Threshold:   {threshold:5} cycles")

    buf.close()

    if separation < 30:
        return fail(
            f"Hit/miss separation only {separation} cycles -- too noisy for reliable encoding. "
            "Try on idle hardware or a bare-metal machine (not inside a hypervisor)."
        ), threshold

    ok(f"Clear hit/miss separation: {separation} cycles")
    return True, threshold


# =============================================================================
# Test 3: Single-process roundtrip with x86 JIT
# =============================================================================

async def test_single_process(shared_path: str, threshold: int):
    print(f"\n{BOLD}3. Single-process write/read roundtrip (x86 JIT){RESET}")
    a = CpuCacheAdapter(
        mmap_path=shared_path,
        threshold=threshold,
        max_chunk_bytes=64,
    )
    test_strings = [
        "PICARD",
        "rowhammer",
        "Hello, World!",
        "0x" + "deadbeef" * 4,
    ]
    all_ok = True
    for s in test_strings:
        ref = await a.write_chunk(s)
        got = await a.read_chunk(ref)
        if got == s:
            ok(f"'{s}'")
        else:
            fail(f"'{s}' -> got '{got}'")
            all_ok = False
    await a.close()
    return all_ok


# =============================================================================
# Test 4: Cross-process channel
# =============================================================================

_READER_SCRIPT = """\
import sys, json, asyncio, os
sys.path.insert(0, sys.argv[1])
from picard.adapters.cpu_cache_adapter import CpuCacheAdapter

async def main():
    loc_json = sys.argv[2]
    threshold = int(sys.argv[3])
    mmap_path = sys.argv[4]
    ref_dict = json.loads(loc_json)

    from picard.adapters.base import StoredChunkRef
    ref = StoredChunkRef(adapter_name="cpu-cache", location=json.dumps(ref_dict))

    a = CpuCacheAdapter(mmap_path=mmap_path, threshold=threshold, max_chunk_bytes=64)
    result = await a.read_chunk(ref)
    print(result)

asyncio.run(main())
"""

async def test_cross_process(shared_path: str, threshold: int):
    print(f"\n{BOLD}4. Cross-process covert channel{RESET}")
    info("Writer process encodes 'throwhammer' into L3 cache residency ...")

    writer = CpuCacheAdapter(
        mmap_path=shared_path,
        threshold=threshold,
        max_chunk_bytes=64,
    )
    secret = "throwhammer"
    ref = await writer.write_chunk(secret)

    # Spawn a completely separate Python process as the reader
    import tempfile as tf
    script_file = tf.NamedTemporaryFile(mode="w", suffix=".py", delete=False)
    script_file.write(_READER_SCRIPT)
    script_file.close()

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ref_payload = json.loads(ref.location)

    import json as _json
    proc = subprocess.run(
        [sys.executable, script_file.name,
         project_root,
         _json.dumps(ref_payload),
         str(threshold),
         shared_path],
        capture_output=True, text=True, timeout=10,
    )
    os.unlink(script_file.name)
    await writer.close()

    got = proc.stdout.strip()
    if got == secret:
        ok(f"Reader subprocess decoded '{got}' from cache residency alone")
        return True
    else:
        fail(f"Expected '{secret}', got '{got}' (stderr: {proc.stderr.strip()[:200]})")
        return False


# =============================================================================
# Test 5: Bit error rate
# =============================================================================

async def test_ber(shared_path: str, threshold: int):
    print(f"\n{BOLD}5. Bit error rate (100 write/read cycles){RESET}")
    a = CpuCacheAdapter(mmap_path=shared_path, threshold=threshold, max_chunk_bytes=16)
    payload = "PICARD-BER-TEST!"   # 16 bytes = 128 bits
    errors = 0
    trials = 100
    for _ in range(trials):
        ref = await a.write_chunk(payload)
        got = await a.read_chunk(ref)
        for b0, b1 in zip(payload.encode(), got.encode() if len(got) == len(payload) else b"\x00" * len(payload)):
            errors += bin(b0 ^ b1).count("1")
    total_bits = trials * len(payload) * 8
    ber = errors / total_bits
    await a.close()

    info(f"Errors: {errors} / {total_bits} bits  (BER = {ber:.2e})")
    if ber < 0.01:
        ok(f"BER {ber:.2e} -- channel reliable for storage use")
        return True
    elif ber < 0.05:
        ok(f"BER {ber:.2e} -- acceptable with ECC overhead")
        return True
    else:
        fail(f"BER {ber:.2e} -- too noisy; check threshold calibration or background load")
        return False


# =============================================================================
# Main
# =============================================================================

async def main():
    print(f"\n{BOLD}{CYAN}PICARD cpu_cache live hardware test{RESET}")
    print(f"  Platform: {sys.platform} / {os.uname().machine}")
    print(f"  Python:   {sys.version.split()[0]}")
    print(f"  CPU:      ", end="")
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if "model name" in line:
                    print(line.split(":")[1].strip())
                    break
    except Exception:
        print("unknown")

    results = {}
    with tempfile.TemporaryDirectory() as tmpdir:
        shared_path = os.path.join(tmpdir, "picard-cache-channel.mmap")

        results["jit"] = test_jit()
        if not results["jit"]:
            print(f"\n{RED}JIT failed -- remaining tests skipped.{RESET}")
            sys.exit(1)

        cal_ok, threshold = test_calibration(shared_path)
        results["calibration"] = cal_ok
        if not cal_ok:
            print(f"\n{YELLOW}Calibration poor -- continuing with default threshold {DEFAULT_THRESHOLD}.{RESET}")
            threshold = DEFAULT_THRESHOLD

        results["single_process"] = await test_single_process(shared_path, threshold)
        results["cross_process"]  = await test_cross_process(shared_path, threshold)
        results["ber"]            = await test_ber(shared_path, threshold)

    passed = sum(1 for v in results.values() if v)
    total  = len(results)
    print(f"\n{BOLD}Result: {passed}/{total} passed{RESET}")
    for name, v in results.items():
        mark = f"{GREEN}PASS{RESET}" if v else f"{RED}FAIL{RESET}"
        print(f"  [{mark}] {name}")

    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    import json
    asyncio.run(main())
