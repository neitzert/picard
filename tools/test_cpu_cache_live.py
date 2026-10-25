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
    CACHE_LINE_BYTES,
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
        clflush, rdtsc, just_probe, preload_probe, flush_probe, buf = _get_primitives()
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
    """Calibrate using JIT probes -- no Python overhead in the timing window.

    preload_probe: warms the line then times the second (L1) access in JIT.
    flush_probe:   evicts the line then times the cold (DRAM) access in JIT.
    Stride matches BYTES_PER_BIT so row-buffer behavior mirrors the real channel.
    """
    import random
    print(f"\n{BOLD}2. L1 hit vs DRAM miss calibration (JIT probes){RESET}")
    n_lines = 64
    region = n_lines * BYTES_PER_BIT
    base, buf = _open_shared_mmap(shared_path, region)

    clflush, rdtsc, just_probe, preload_probe, flush_probe, _ = _get_primitives()

    indices = list(range(n_lines))
    random.shuffle(indices)
    half = n_lines // 2

    hit_samples = []
    for i in indices[:half]:
        addr = base + i * BYTES_PER_BIT
        hit_samples.append(preload_probe(ctypes.c_void_p(addr)))

    miss_samples = []
    random.shuffle(indices)
    for i in indices[:half]:
        addr = base + i * BYTES_PER_BIT
        miss_samples.append(flush_probe(ctypes.c_void_p(addr)))

    # Trim outliers (top/bottom 10%)
    trim = max(1, len(hit_samples) // 10)
    hit_sorted  = sorted(hit_samples)
    miss_sorted = sorted(miss_samples)
    avg_hit  = sum(hit_sorted [trim:-trim]) // (len(hit_sorted)  - 2 * trim)
    avg_miss = sum(miss_sorted[trim:-trim]) // (len(miss_sorted) - 2 * trim)
    threshold = (avg_hit + avg_miss) // 2
    separation = avg_miss - avg_hit

    info(f"L1 hit avg:    {avg_hit:5} cycles  (min {hit_sorted[0]}, max {hit_sorted[-1]})")
    info(f"DRAM miss avg: {avg_miss:5} cycles  (min {miss_sorted[0]}, max {miss_sorted[-1]})")
    info(f"Separation:    {separation:5} cycles")
    info(f"Threshold:     {threshold:5} cycles")

    buf.close()

    if separation < 30:
        return fail(
            f"Hit/miss separation only {separation} cycles -- too noisy for reliable "
            "encoding. Try on idle bare-metal hardware or check CPUID for CLFLUSH support."
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
import sys, json, asyncio, os, ctypes
sys.path.insert(0, sys.argv[1])
from picard.adapters.cpu_cache_adapter import (
    CpuCacheAdapter, _get_primitives, BYTES_PER_BIT,
)

async def main():
    project_root    = sys.argv[1]
    mmap_path       = sys.argv[2]
    threshold       = int(sys.argv[3])
    max_chunk_bytes = int(sys.argv[4])
    import random

    # Open the adapter and force _ensure_mmap so self._base is set and the
    # mmap file handle stays open for the full duration.
    a = CpuCacheAdapter(mmap_path=mmap_path, threshold=threshold,
                        max_chunk_bytes=max_chunk_bytes)
    base, buf = a._ensure_mmap()
    region_size = a._region_size
    total_slots = region_size // BYTES_PER_BIT

    # TLB + L3 pre-warm: load every slot page with just_probe BEFORE the writer
    # encodes any bits.  just_probe executes a real CPU load (mov rax,[rdi]) so
    # it fills the TLB entry for each page and brings the line into L3 cache.
    # max_chunk_bytes is chosen so that total_slots < STLB capacity (2048
    # entries on Alder Lake), keeping all TLB entries live until the probe.
    # After this sweep, the writer's flush passes (passes 2-5) will evict the
    # 0-bit pages to DRAM.  The 1-bit pages remain in L3 (the writer's preload
    # pass re-loads them).  The reader's TLB entries survive undisturbed.
    _, _, just_probe, *_ = _get_primitives()
    slot_order = list(range(total_slots))
    random.shuffle(slot_order)
    for s in slot_order:
        just_probe(ctypes.c_void_p(base + s * BYTES_PER_BIT))

    # Signal ready: writer may now encode bits into cache state.
    sys.stdout.write("READY\\n")
    sys.stdout.flush()

    # Receive the chunk ref from the writer via stdin.
    loc_json = sys.stdin.readline().strip()
    ref_dict = json.loads(loc_json)

    from picard.adapters.base import StoredChunkRef
    ref = StoredChunkRef(adapter_name="cpu-cache", location=json.dumps(ref_dict))

    result = await a.read_chunk(ref)
    print(result)
    sys.stdout.flush()

asyncio.run(main())
"""

async def test_cross_process(shared_path: str, threshold: int):
    print(f"\n{BOLD}4. Cross-process covert channel{RESET}")
    info("Starting reader subprocess for TLB + L3 pre-warm (reader-first protocol) ...")

    import tempfile as tf
    import json as _json

    # 16 bytes keeps total_slots = 16*8*5 = 640, safely below STLB capacity
    # (2048 entries on Alder Lake / Ice Lake), so all TLB entries survive the
    # warmup sweep intact when the reader begins probing.
    max_chunk_bytes = 16
    secret = "throwhammer"

    script_file = tf.NamedTemporaryFile(mode="w", suffix=".py", delete=False)
    script_file.write(_READER_SCRIPT)
    script_file.close()

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # Start the reader first.  It opens the mmap, pre-warms TLB + L3 for all
    # slot pages, then signals READY.  Only after that does the writer encode
    # bits into cache state, so the reader's TLB entries pre-exist the write.
    #
    # Pin the reader to CPU 1 so the OS scheduler cannot migrate it to a
    # different core between the TLB-warmup sweep and the probe loop.  TLBs
    # are per-core; migration resets them to empty.
    reader_cmd = [sys.executable, script_file.name,
                  project_root, shared_path, str(threshold), str(max_chunk_bytes)]
    import shutil as _shutil
    if _shutil.which("taskset"):
        reader_cmd = ["taskset", "-c", "1"] + reader_cmd
    proc = subprocess.Popen(
        reader_cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    # Block until the reader's warmup sweep is complete.
    ready_line = proc.stdout.readline()
    if ready_line.strip() != "READY":
        proc.kill()
        os.unlink(script_file.name)
        fail(f"Reader did not signal READY (got: {ready_line.strip()!r})")
        return False

    info("Reader TLB warm -- writer now encodes 'throwhammer' into L3 ...")

    writer = CpuCacheAdapter(
        mmap_path=shared_path,
        threshold=threshold,
        max_chunk_bytes=max_chunk_bytes,
    )
    ref = await writer.write_chunk(secret)
    await writer.close()

    # Send the chunk ref to the reader.
    ref_payload = _json.loads(ref.location)
    proc.stdin.write(_json.dumps(ref_payload) + "\n")
    proc.stdin.flush()
    proc.stdin.close()

    got = proc.stdout.readline().strip()
    stderr = proc.stderr.read()
    proc.wait(timeout=10)
    os.unlink(script_file.name)

    if got == secret:
        ok(f"Reader subprocess decoded '{got}' from cache residency alone")
        return True
    else:
        fail(f"Expected '{secret}', got '{got}' (stderr: {stderr.strip()[:200]})")
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
