"""Tor proxy — route adapter HTTP traffic through Tor for anonymity.

Wraps outbound connections through Tor's SOCKS5 proxy so the user's
real IP is never exposed to public chunk storage services (social media,
paste sites, DNS servers, etc.).

Usage:
    from picard.tor_proxy import tor_session, TorProxyConfig

    config = TorProxyConfig(socks_port=9050)
    async with tor_session(config) as client:
        resp = await client.get("https://check.torproject.org")
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx


@dataclass
class TorProxyConfig:
    socks_port: int = 9050
    socks_host: str = "127.0.0.1"
    timeout: float = 120.0
    verify_ssl: bool = True

    @property
    def proxy_url(self) -> str:
        return f"socks5://{self.socks_host}:{self.socks_port}"

    @classmethod
    def from_env(cls) -> TorProxyConfig:
        return cls(
            socks_port=int(os.environ.get("PICARD_TOR_SOCKS_PORT", "9050")),
            socks_host=os.environ.get("PICARD_TOR_SOCKS_HOST", "127.0.0.1"),
        )


@asynccontextmanager
async def tor_session(config: TorProxyConfig | None = None):
    """Async context manager that yields a Tor-proxied httpx.AsyncClient."""
    cfg = config or TorProxyConfig.from_env()
    try:
        from httpx_socks import AsyncProxyTransport
    except ImportError:
        raise RuntimeError(
            "Tor proxy requires 'httpx-socks'. Install with: pip install picard-store[tor]"
        )

    transport = AsyncProxyTransport.from_url(cfg.proxy_url)
    async with httpx.AsyncClient(
        transport=transport,
        timeout=cfg.timeout,
        verify=cfg.verify_ssl,
    ) as client:
        yield client


async def check_tor(config: TorProxyConfig | None = None) -> dict:
    """Check if Tor is working by hitting the Tor Project's check page."""
    cfg = config or TorProxyConfig.from_env()
    try:
        async with tor_session(cfg) as client:
            resp = await client.get("https://check.torproject.org/api/ip")
            data = resp.json()
            return {
                "tor": data.get("IsTor", False),
                "ip": data.get("IP", "unknown"),
                "socks_proxy": cfg.proxy_url,
            }
    except Exception as e:
        return {
            "tor": False,
            "ip": "unreachable",
            "error": str(e),
            "socks_proxy": cfg.proxy_url,
        }
