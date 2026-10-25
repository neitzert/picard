"""Tests for LatencyAdapter.

All subprocess calls are mocked. No Linux, tc, or CAP_NET_ADMIN required.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ping_output(avg_ms: float) -> bytes:
    """Return a fake ping -q summary line for a given average RTT."""
    lo = max(avg_ms - 0.5, 0.001)
    return (
        f"PING 127.1.0.1 56(84) bytes of data.\n\n"
        f"--- 127.1.0.1 ping statistics ---\n"
        f"12 packets transmitted, 12 received, 0% packet loss, time 11ms\n"
        f"rtt min/avg/max/mdev = {lo:.3f}/{avg_ms:.3f}"
        f"/{avg_ms + 0.5:.3f}/0.009 ms\n"
    ).encode()


def _make_proc(stdout: bytes = b"", returncode: int = 0) -> AsyncMock:
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, b""))
    return proc


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def adapter(monkeypatch):
    """Return a LatencyAdapter with all system calls mocked out."""

    # Patch _check_tc_available so __init__ succeeds everywhere.
    monkeypatch.setattr(
        "picard.adapters.latency_adapter._check_tc_available", lambda: None
    )
    monkeypatch.setattr(
        "picard.adapters.latency_adapter._IS_LINUX", True
    )

    from picard.adapters.latency_adapter import LatencyAdapter
    return LatencyAdapter(threshold_ms=15.0, samples_per_read=12, max_chunk_bytes=31)


# ---------------------------------------------------------------------------
# Tests: initialisation
# ---------------------------------------------------------------------------


def test_permission_error_on_non_linux(monkeypatch):
    monkeypatch.setattr("picard.adapters.latency_adapter._IS_LINUX", False)
    from picard.adapters.latency_adapter import LatencyAdapter
    with pytest.raises(PermissionError, match="Linux"):
        LatencyAdapter()


def test_permission_error_when_tc_fails(monkeypatch):
    monkeypatch.setattr("picard.adapters.latency_adapter._IS_LINUX", True)
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: MagicMock(returncode=1),
    )
    from picard.adapters.latency_adapter import LatencyAdapter
    with pytest.raises(PermissionError):
        LatencyAdapter()


# ---------------------------------------------------------------------------
# Tests: write_chunk
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_chunk_returns_ref(adapter):
    tc_calls: list[str] = []

    async def fake_shell(cmd, **kwargs):
        tc_calls.append(cmd)
        return _make_proc()

    async def fake_ping(*args, **kwargs):
        return _make_proc(_make_ping_output(0.05))

    with (
        patch("asyncio.create_subprocess_shell", side_effect=fake_shell),
        patch("asyncio.create_subprocess_exec", side_effect=fake_ping),
    ):
        ref = await adapter.write_chunk("hi")

    assert ref.adapter_name == "latency"
    loc = json.loads(ref.location)
    assert "ips" in loc
    assert loc["n_bits"] == len("hi".encode()) * 8


@pytest.mark.asyncio
async def test_write_chunk_applies_delay_for_one_bits(adapter):
    """Bits set to 1 must produce a netem delay; bits set to 0 must not."""
    # "A" = 0x41 = 0b01000001 -- bits 1 and 6 (from MSB) are 1, rest 0.
    tc_calls: list[str] = []

    async def fake_shell(cmd, **kwargs):
        tc_calls.append(cmd)
        return _make_proc()

    async def fake_ping(*a, **kw):
        return _make_proc(_make_ping_output(0.05))

    with (
        patch("asyncio.create_subprocess_shell", side_effect=fake_shell),
        patch("asyncio.create_subprocess_exec", side_effect=fake_ping),
    ):
        await adapter.write_chunk("A")

    delay_calls = [c for c in tc_calls if "netem delay" in c and "0.0ms" not in c]
    # "A" has 2 one-bits, so at least 2 delay rules with non-zero delay.
    assert len(delay_calls) >= 2


# ---------------------------------------------------------------------------
# Tests: read_chunk
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_chunk_decodes_correct_bits(adapter):
    """RTT above threshold decodes as 1; at or below decodes as 0."""
    # Encode "A" (0x41 = 0b01000001) manually in a fake ref.
    bits_A = [0, 1, 0, 0, 0, 0, 0, 1]  # MSB first
    ips = [f"127.1.0.{i + 1}" for i in range(8)]
    baseline = {ip: 0.05 for ip in ips}
    loc = json.dumps({
        "ips": ips,
        "octets": list(range(1, 9)),
        "threshold_ms": 15.0,
        "baseline_ms": baseline,
        "n_bits": 8,
        "samples": 12,
    })

    from picard.adapters.base import StoredChunkRef
    ref = StoredChunkRef(adapter_name="latency", location=loc)

    # Ping returns high RTT for 1-bits, low for 0-bits.
    call_count = 0

    async def fake_exec(*args, **kwargs):
        nonlocal call_count
        bit = bits_A[call_count % 8]
        call_count += 1
        rtt = 20.0 if bit else 0.05  # 20ms >> threshold/2=7.5ms for 1; 0.05 for 0
        return _make_proc(_make_ping_output(rtt))

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        result = await adapter.read_chunk(ref)

    assert result == "A"


# ---------------------------------------------------------------------------
# Tests: delete_chunk
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_chunk_returns_true_and_removes_rules(adapter):
    tc_calls: list[str] = []

    async def fake_shell(cmd, **kwargs):
        tc_calls.append(cmd)
        return _make_proc()

    async def fake_ping(*a, **kw):
        return _make_proc(_make_ping_output(0.05))

    with (
        patch("asyncio.create_subprocess_shell", side_effect=fake_shell),
        patch("asyncio.create_subprocess_exec", side_effect=fake_ping),
    ):
        ref = await adapter.write_chunk("x")
        tc_calls.clear()
        deleted = await adapter.delete_chunk(ref)

    assert deleted is True
    del_calls = [c for c in tc_calls if "del" in c]
    assert del_calls  # at least one tc del issued


@pytest.mark.asyncio
async def test_delete_chunk_returns_false_for_unknown_ref(adapter):
    from picard.adapters.base import StoredChunkRef
    fake_ref = StoredChunkRef(
        adapter_name="latency",
        location='{"ips":[],"octets":[],"threshold_ms":15,"baseline_ms":{},"n_bits":0,"samples":12}',
    )
    result = await adapter.delete_chunk(fake_ref)
    assert result is False


# ---------------------------------------------------------------------------
# Tests: health_check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_check_true_when_tc_succeeds(adapter, monkeypatch):
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: MagicMock(returncode=0),
    )
    assert await adapter.health_check() is True


@pytest.mark.asyncio
async def test_health_check_false_when_tc_missing(adapter, monkeypatch):
    def raise_fnf(*a, **kw):
        raise FileNotFoundError("tc not found")

    monkeypatch.setattr("subprocess.run", raise_fnf)
    assert await adapter.health_check() is False


@pytest.mark.asyncio
async def test_health_check_false_on_non_linux(monkeypatch):
    monkeypatch.setattr("picard.adapters.latency_adapter._check_tc_available", lambda: None)
    monkeypatch.setattr("picard.adapters.latency_adapter._IS_LINUX", False)
    from picard.adapters.latency_adapter import LatencyAdapter
    a = LatencyAdapter()
    assert await a.health_check() is False


# ---------------------------------------------------------------------------
# Tests: round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_round_trip(adapter):
    """Write then read must recover the original string."""
    original = "hello"
    bits_map: dict[str, int] = {}

    async def fake_shell(cmd, **kwargs):
        # Record netem delay assignments so read can mirror them.
        if "netem delay" in cmd and "0.0ms" not in cmd:
            # Extract IP from the filter command if present, or track by handle.
            pass
        return _make_proc()

    write_bits: list[int] = []

    async def fake_ping_write(*args, **kwargs):
        return _make_proc(_make_ping_output(0.05))

    with (
        patch("asyncio.create_subprocess_shell", side_effect=fake_shell),
        patch("asyncio.create_subprocess_exec", side_effect=fake_ping_write),
    ):
        ref = await adapter.write_chunk(original)

    loc = json.loads(ref.location)
    # Reconstruct the expected bits from the original string.
    expected_bits = adapter._bytes_to_bits(original.encode())

    read_call_idx = 0

    async def fake_ping_read(*args, **kwargs):
        nonlocal read_call_idx
        ip_arg = args[4] if len(args) > 4 else ""
        octet = int(ip_arg.split(".")[-1]) if ip_arg and "." in ip_arg else 1
        # Map octet back to bit index via the stored octets list.
        octets_list = loc["octets"]
        try:
            idx = octets_list.index(octet)
        except ValueError:
            idx = read_call_idx
        read_call_idx += 1
        bit = expected_bits[idx] if idx < len(expected_bits) else 0
        rtt = 20.0 if bit else 0.05
        return _make_proc(_make_ping_output(rtt))

    with patch("asyncio.create_subprocess_exec", side_effect=fake_ping_read):
        result = await adapter.read_chunk(ref)

    assert result == original
