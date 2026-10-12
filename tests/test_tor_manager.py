"""Tests for TorConfig, TorManager, and HiddenService dataclasses."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from picard.tor import HiddenService, TorConfig


class TestTorConfig:
    def test_defaults(self):
        cfg = TorConfig()
        assert cfg.socks_port == 9050
        assert cfg.control_port == 9051
        assert cfg.tor_binary == "tor"
        assert cfg.existing_tor is False

    def test_data_dir(self):
        cfg = TorConfig()
        assert str(cfg.data_dir).endswith(".picard/tor") or str(cfg.data_dir).endswith(".picard\\tor")

    def test_custom_ports(self):
        cfg = TorConfig(socks_port=9150, control_port=9151)
        assert cfg.socks_port == 9150
        assert cfg.control_port == 9151


class TestHiddenService:
    def test_fields(self):
        hs = HiddenService(onion_address="abc.onion", port=8471)
        assert hs.onion_address == "abc.onion"
        assert hs.port == 8471
        assert hs.hostname_file is None

    def test_with_hostname_file(self, tmp_path):
        f = tmp_path / "hostname"
        hs = HiddenService(onion_address="abc.onion", port=80, hostname_file=f)
        assert hs.hostname_file == f


class TestTorManager:
    def _make_manager(self, config=None):
        with patch("picard.tor.require_tor"):
            from picard.tor import TorManager
            return TorManager(config)

    def test_socks_proxy(self):
        mgr = self._make_manager(TorConfig(socks_port=9150))
        assert mgr.socks_proxy == "socks5://127.0.0.1:9150"

    def test_onion_address_none_initially(self):
        mgr = self._make_manager()
        assert mgr.onion_address is None

    def test_onion_address_after_service(self):
        mgr = self._make_manager()
        mgr._hidden_services.append(
            HiddenService(onion_address="xyz.onion", port=80)
        )
        assert mgr.onion_address == "xyz.onion"

    def test_start_existing_tor(self):
        mock_ctrl = MagicMock()
        mock_ctrl_cls = MagicMock(return_value=mock_ctrl)
        mock_ctrl_cls.from_port.return_value = mock_ctrl

        cfg = TorConfig(existing_tor=True)
        mgr = self._make_manager(cfg)

        with patch.object(type(mgr), '_controller', create=True):
            pass

        with patch("picard.tor.Controller", mock_ctrl_cls, create=True):
            mgr.start()

        mock_ctrl_cls.from_port.assert_called_once_with(port=cfg.control_port)
        mock_ctrl.authenticate.assert_called_once()

    def test_stop_clears_services(self):
        mgr = self._make_manager()
        mock_ctrl = MagicMock()
        mgr._controller = mock_ctrl
        mgr._hidden_services.append(
            HiddenService(onion_address="xyz.onion", port=80)
        )

        mgr.stop()

        assert mgr._hidden_services == []
        assert mgr._controller is None
        mock_ctrl.remove_ephemeral_hidden_service.assert_called_once_with("xyz")
        mock_ctrl.close.assert_called_once()

    def test_stop_handles_missing_controller(self):
        mgr = self._make_manager()
        mgr.stop()

    def test_create_hidden_service_requires_start(self):
        mgr = self._make_manager()
        with pytest.raises(RuntimeError, match="not started"):
            mgr.create_hidden_service(8471)

    def test_create_hidden_service(self):
        mgr = self._make_manager()
        mock_ctrl = MagicMock()
        mock_response = MagicMock()
        mock_response.service_id = "abcdef1234567890"
        mock_ctrl.create_ephemeral_hidden_service.return_value = mock_response
        mgr._controller = mock_ctrl

        hs = mgr.create_hidden_service(8471)
        assert hs.onion_address == "abcdef1234567890.onion"
        assert hs.port == 8471
        assert len(mgr._hidden_services) == 1

    def test_create_persistent_hidden_service(self, tmp_path):
        mgr = self._make_manager(TorConfig(hidden_service_dir=tmp_path / "hs"))
        mock_ctrl = MagicMock()
        mock_response = MagicMock()
        mock_response.service_id = "persistent1234567"
        mock_response.private_key = "FAKE_KEY"
        mock_ctrl.create_ephemeral_hidden_service.return_value = mock_response
        mgr._controller = mock_ctrl

        hs = mgr.create_persistent_hidden_service(8471)
        assert hs.onion_address == "persistent1234567.onion"
        assert hs.hostname_file is not None
        assert hs.hostname_file.read_text().strip() == "persistent1234567.onion"

        key_file = tmp_path / "hs" / "hs_ed25519_secret_key"
        assert key_file.exists()
        assert key_file.read_text() == "FAKE_KEY"

    def test_context_manager(self):
        mock_ctrl = MagicMock()
        mock_ctrl_cls = MagicMock()
        mock_ctrl_cls.from_port.return_value = mock_ctrl

        cfg = TorConfig(existing_tor=True)
        mgr = self._make_manager(cfg)

        with patch("picard.tor.Controller", mock_ctrl_cls, create=True):
            mgr.start()
            assert mgr._controller is mock_ctrl

        mgr.stop()
        assert mgr._controller is None

    def test_start_without_tor_binary(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda _: None)
        cfg = TorConfig(existing_tor=False)
        mgr = self._make_manager(cfg)
        with pytest.raises(RuntimeError, match="not found"):
            mgr.start()


class TestRequireFunctions:
    def test_require_tor_without_stem(self):
        with patch("picard.tor.HAS_STEM", False):
            from picard.tor import require_tor
            with pytest.raises(RuntimeError, match="stem"):
                require_tor()

    def test_require_socks_without_httpx_socks(self):
        with patch("picard.tor.HAS_SOCKS", False):
            from picard.tor import require_socks
            with pytest.raises(RuntimeError, match="httpx-socks"):
                require_socks()
