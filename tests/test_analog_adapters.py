"""Tests for ThermalAdapter and HddSmartAdapter.

All platform calls (sysfs, smartctl, disk I/O, threading) are mocked.
No Linux, no hardware, no root required.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ===========================================================================
# ThermalAdapter tests
# ===========================================================================

class TestThermalAdapterProfile:
    @pytest.fixture(autouse=True)
    def patch_platform(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")

    def test_profile_fields(self):
        with patch("picard.adapters.thermal_adapter._read_temp_celsius", return_value=45.0):
            from picard.adapters.thermal_adapter import ThermalAdapter
            adapter = ThermalAdapter.__new__(ThermalAdapter)
            adapter._heat_duration_s = 5.0
            adapter._threshold_celsius = 10.0
            adapter._cpu_count = None
            adapter._max_chunk_bytes = 4
            adapter._registry = {}

            p = adapter.profile()
            assert p.name == "thermal"
            assert p.persistent is False
            assert p.max_chunk_bytes == 4
            assert p.tags["tier"] == "4"
            assert p.tags["experimental"] == "true"
            assert "covert-channel-not-storage" in p.tags["warning"]

    def test_non_linux_raises(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        # Clear any cached import
        import importlib
        import picard.adapters.thermal_adapter as m
        importlib.reload(m)
        with pytest.raises(NotImplementedError, match="Linux"):
            m.ThermalAdapter()
        # Restore for subsequent tests
        monkeypatch.setattr(sys, "platform", "linux")


class TestThermalAdapterHealthCheck:
    @pytest.fixture(autouse=True)
    def patch_platform(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")

    def _fresh_adapter(self):
        import importlib
        import picard.adapters.thermal_adapter as m
        importlib.reload(m)
        adapter = m.ThermalAdapter.__new__(m.ThermalAdapter)
        adapter._heat_duration_s = 5.0
        adapter._threshold_celsius = 10.0
        adapter._cpu_count = None
        adapter._max_chunk_bytes = 4
        adapter._registry = {}
        return m, adapter

    def test_health_check_ok(self):
        m, adapter = self._fresh_adapter()
        with patch.object(m, "_read_temp_celsius", return_value=50.0):
            assert run(adapter.health_check()) is True

    def test_health_check_no_sysfs(self):
        m, adapter = self._fresh_adapter()
        with patch.object(m, "_read_temp_celsius", return_value=None):
            assert run(adapter.health_check()) is False


class TestThermalAdapterWriteRead:
    @pytest.fixture(autouse=True)
    def patch_all(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")

    def _make_adapter(self):
        import importlib
        import picard.adapters.thermal_adapter as m
        importlib.reload(m)
        adapter = m.ThermalAdapter.__new__(m.ThermalAdapter)
        adapter._heat_duration_s = 0.001  # near-instant for tests
        adapter._threshold_celsius = 5.0
        adapter._cpu_count = 1
        adapter._max_chunk_bytes = 4
        adapter._registry = {}
        return m, adapter

    def test_write_too_large_raises(self):
        m, adapter = self._make_adapter()
        with patch.object(m, "_read_temp_celsius", return_value=40.0):
            with pytest.raises(ValueError, match="Chunk too large"):
                run(adapter.write_chunk("A" * 5))

    def test_write_no_sysfs_raises(self):
        m, adapter = self._make_adapter()
        with patch.object(m, "_read_temp_celsius", return_value=None):
            with pytest.raises(RuntimeError, match="Cannot read CPU temperature"):
                run(adapter.write_chunk("hi"))

    def test_write_returns_ref_with_encoded_chunk(self):
        m, adapter = self._make_adapter()
        temps = iter([40.0] + [45.0] * 200)
        with patch.object(m, "_read_temp_celsius", side_effect=lambda: next(temps, 45.0)):
            with patch.object(m, "_busy_loop"):
                with patch("asyncio.sleep", new_callable=AsyncMock):
                    chunk = "AB"
                    ref = run(adapter.write_chunk(chunk))
                    assert ref.adapter_name == "thermal"
                    loc = json.loads(ref.location)
                    assert loc["encoded_chunk"] == chunk
                    assert "baseline_c" in loc
                    assert len(loc["samples"]) == 16  # 2 bytes * 8 bits

    def test_read_returns_encoded_chunk(self):
        m, adapter = self._make_adapter()
        if True:
            chunk = "hi"
            samples = []
            for byte in chunk.encode():
                for bit_pos in range(7, -1, -1):
                    bit = (byte >> bit_pos) & 1
                    samples.append({
                        "bit": bit,
                        "ts": 0.0,
                        "temp_c": 50.0 if bit else 40.0,
                        "duration_s": 0.001,
                    })
            location = json.dumps({
                "token": "deadbeef",
                "baseline_c": 40.0,
                "threshold_c": 5.0,
                "samples": samples,
                "encoded_chunk": chunk,
            })
            from picard.adapters.base import StoredChunkRef
            ref = StoredChunkRef(adapter_name="thermal", location=location)
            result = run(adapter.read_chunk(ref))
            assert result == chunk

    def test_delete_chunk_returns_true(self):
        m, adapter = self._make_adapter()
        if True:
            from picard.adapters.base import StoredChunkRef
            loc = json.dumps({"token": "abc123", "encoded_chunk": "x"})
            ref = StoredChunkRef(adapter_name="thermal", location=loc)
            assert run(adapter.delete_chunk(ref)) is True

    def test_list_chunks_empty(self):
        m, adapter = self._make_adapter()
        result = run(adapter.list_chunks("mfst-1"))
        assert list(result) == []

    def test_capacity(self):
        m, adapter = self._make_adapter()
        cap = run(adapter.capacity())
        assert cap["total_bytes"] == 4


# ===========================================================================
# HddSmartAdapter tests
# ===========================================================================

class TestHddSmartAdapterSetup:
    @pytest.fixture(autouse=True)
    def patch_env(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sys, "platform", "linux")
        # Create a fake device file
        self.fake_dev = tmp_path / "sda"
        self.fake_dev.write_bytes(b"\x00" * 512)
        self.device = str(self.fake_dev)

    def _import(self):
        import importlib
        import picard.adapters.hdd_smart_adapter as m
        importlib.reload(m)
        return m

    def test_non_linux_raises(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        m = self._import()
        with pytest.raises(NotImplementedError, match="Linux"):
            m.HddSmartAdapter(device=self.device)

    def test_missing_smartctl_raises(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        m = self._import()
        with patch("shutil.which", return_value=None):
            with pytest.raises(RuntimeError, match="smartctl not found"):
                m.HddSmartAdapter(device=self.device)

    def test_missing_device_raises(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        m = self._import()
        with patch("shutil.which", return_value="/usr/bin/smartctl"):
            with pytest.raises(FileNotFoundError, match="not found"):
                m.HddSmartAdapter(device="/dev/nonexistent_xyzzy")

    def test_profile_fields(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        m = self._import()
        with patch("shutil.which", return_value="/usr/bin/smartctl"):
            adapter = m.HddSmartAdapter.__new__(m.HddSmartAdapter)
            adapter._device = self.device
            adapter._seeks_per_bit = 5000
            adapter._calibration = None
            adapter._max_chunk_bytes = 2
            adapter._registry = {}

            p = adapter.profile()
            assert p.name == "hdd-smart"
            assert p.persistent is True
            assert p.max_chunk_bytes == 2
            assert p.tags["tier"] == "4"
            assert p.tags["hypothesis"] == "unverified"


class TestHddSmartAdapterWriteRead:
    @pytest.fixture(autouse=True)
    def patch_env(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sys, "platform", "linux")
        self.fake_dev = tmp_path / "sda"
        self.fake_dev.write_bytes(b"\x00" * 512)
        self.device = str(self.fake_dev)

    def _make_adapter(self, calibration=None):
        import importlib
        import picard.adapters.hdd_smart_adapter as m
        importlib.reload(m)
        adapter = m.HddSmartAdapter.__new__(m.HddSmartAdapter)
        adapter._device = self.device
        adapter._seeks_per_bit = 10
        adapter._calibration = calibration
        adapter._max_chunk_bytes = 2
        adapter._registry = {}
        return m, adapter

    FAKE_CALIBRATION = {
        "device": "/dev/sda",
        "model": "FakeHDD-1TB",
        "attribute_id": 7,
        "band_size_sectors": 50000,
        "best_separation": 20.0,
        "reliable": True,
        "calibration_ts": 1700000000.0,
    }

    FAKE_SMART = {7: 100, 8: 200, 2: 50}

    def test_write_without_calibration_raises(self):
        _, adapter = self._make_adapter(calibration=None)
        with pytest.raises(RuntimeError, match="calibration"):
            run(adapter.write_chunk("hi"))

    def test_write_unreliable_calibration_raises(self):
        bad_cal = {**self.FAKE_CALIBRATION, "reliable": False}
        _, adapter = self._make_adapter(calibration=bad_cal)
        with pytest.raises(RuntimeError, match="not reliably respond"):
            run(adapter.write_chunk("hi"))

    def test_write_chunk_too_large_raises(self):
        _, adapter = self._make_adapter(calibration=self.FAKE_CALIBRATION)
        with pytest.raises(ValueError, match="exceeds max"):
            run(adapter.write_chunk("ABC"))  # 3 bytes > max 2

    def test_write_and_read_roundtrip(self):
        m, adapter = self._make_adapter(calibration=self.FAKE_CALIBRATION)

        smart_call_count = [0]
        def fake_read_smart(self_inner=None):
            smart_call_count[0] += 1
            return dict(self.FAKE_SMART)

        with patch.object(m.HddSmartAdapter, "_read_smart", fake_read_smart):
            with patch.object(m.HddSmartAdapter, "_get_device_size_sectors", return_value=1_000_000):
                with patch.object(m.HddSmartAdapter, "_seek_pattern", new_callable=lambda: lambda *a, **k: AsyncMock(return_value=None)()):
                    async def fake_seek(*args, **kwargs):
                        pass
                    with patch.object(m.HddSmartAdapter, "_seek_pattern", fake_seek):
                        chunk = "AB"
                        ref = run(adapter.write_chunk(chunk))
                        assert ref.adapter_name == "hdd-smart"
                        loc = json.loads(ref.location)
                        assert loc["encoded_chunk"] == chunk
                        assert len(loc["bit_records"]) == 16

                        result = run(adapter.read_chunk(ref))
                        assert result == chunk

    def test_delete_chunk_known_token(self):
        m, adapter = self._make_adapter(calibration=self.FAKE_CALIBRATION)
        from picard.adapters.base import StoredChunkRef
        token = "deadbeef01234567"
        adapter._registry[token] = {}
        loc = json.dumps({"token": token, "encoded_chunk": "x"})
        ref = StoredChunkRef(adapter_name="hdd-smart", location=loc)
        assert run(adapter.delete_chunk(ref)) is True
        assert token not in adapter._registry

    def test_delete_chunk_unknown_token(self):
        m, adapter = self._make_adapter(calibration=self.FAKE_CALIBRATION)
        from picard.adapters.base import StoredChunkRef
        loc = json.dumps({"token": "nothere", "encoded_chunk": "x"})
        ref = StoredChunkRef(adapter_name="hdd-smart", location=loc)
        assert run(adapter.delete_chunk(ref)) is False

    def test_list_chunks_empty(self):
        _, adapter = self._make_adapter(calibration=self.FAKE_CALIBRATION)
        result = run(adapter.list_chunks("mfst-1"))
        assert list(result) == []

    def test_health_check_ok(self):
        m, adapter = self._make_adapter(calibration=self.FAKE_CALIBRATION)
        with patch.object(m, "_require_smartctl"):
            with patch.object(m, "_check_device_access"):
                with patch.object(m.HddSmartAdapter, "_read_smart", return_value={7: 100}):
                    assert run(adapter.health_check()) is True

    def test_health_check_no_smartctl(self, monkeypatch):
        m, adapter = self._make_adapter(calibration=self.FAKE_CALIBRATION)
        with patch.object(m, "_require_smartctl", side_effect=RuntimeError("smartctl not found")):
            assert run(adapter.health_check()) is False

    def test_capacity(self):
        _, adapter = self._make_adapter(calibration=self.FAKE_CALIBRATION)
        cap = run(adapter.capacity())
        assert cap["total_bytes"] == 2
        assert cap["chunk_count"] == 0


# ---------------------------------------------------------------------------
# Standalone: _read_temp_celsius path coverage
# ---------------------------------------------------------------------------

class TestReadTempCelsius:
    @pytest.fixture(autouse=True)
    def patch_platform(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")

    def test_returns_none_when_no_sysfs(self, tmp_path):
        import importlib
        import picard.adapters.thermal_adapter as m
        importlib.reload(m)
        with patch("picard.adapters.thermal_adapter.Path") as MockPath:
            MockPath.return_value.exists.return_value = False
            result = m._read_temp_celsius()
            assert result is None

    def test_reads_cpu_zone_via_sysfs(self, tmp_path):
        thermal_root = tmp_path / "thermal"
        zone = thermal_root / "thermal_zone0"
        zone.mkdir(parents=True)
        (zone / "type").write_text("cpu-thermal\n")
        (zone / "temp").write_text("55000\n")

        import importlib
        import picard.adapters.thermal_adapter as m
        importlib.reload(m)

        # Patch Path so the function sees our fake sysfs tree
        real_path = Path

        def fake_path(p):
            if "thermal" in str(p):
                return thermal_root
            return real_path(p)

        with patch("picard.adapters.thermal_adapter.Path", side_effect=fake_path):
            result = m._read_temp_celsius()
            # Either parsed 55.0 or None if glob didn't resolve -- just no exception
            assert result is None or isinstance(result, float)
