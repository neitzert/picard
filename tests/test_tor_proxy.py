"""Tests for Tor proxy configuration."""

from picard.tor_proxy import TorProxyConfig


class TestTorProxyConfig:
    def test_defaults(self):
        cfg = TorProxyConfig()
        assert cfg.socks_port == 9050
        assert cfg.socks_host == "127.0.0.1"
        assert cfg.proxy_url == "socks5://127.0.0.1:9050"

    def test_custom_port(self):
        cfg = TorProxyConfig(socks_port=9150)
        assert cfg.proxy_url == "socks5://127.0.0.1:9150"

    def test_from_env(self, monkeypatch):
        monkeypatch.setenv("PICARD_TOR_SOCKS_PORT", "9150")
        monkeypatch.setenv("PICARD_TOR_SOCKS_HOST", "10.0.0.1")
        cfg = TorProxyConfig.from_env()
        assert cfg.socks_port == 9150
        assert cfg.socks_host == "10.0.0.1"

    def test_from_env_defaults(self, monkeypatch):
        monkeypatch.delenv("PICARD_TOR_SOCKS_PORT", raising=False)
        monkeypatch.delenv("PICARD_TOR_SOCKS_HOST", raising=False)
        cfg = TorProxyConfig.from_env()
        assert cfg.socks_port == 9050
