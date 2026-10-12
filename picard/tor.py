"""Tor integration — hidden services and SOCKS proxy for PICARD.

Manages Tor process lifecycle, ephemeral hidden service creation,
and SOCKS5 proxy configuration for routing traffic through Tor.

Requires: stem (Tor controller library)
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

try:
    from stem.control import Controller
    from stem.process import launch_tor_with_config
    HAS_STEM = True
except ImportError:
    HAS_STEM = False

try:
    import httpx_socks
    HAS_SOCKS = True
except ImportError:
    HAS_SOCKS = False


def require_tor():
    if not HAS_STEM:
        raise RuntimeError(
            "Tor support requires 'stem'. Install with: pip install picard-store[tor]"
        )


def require_socks():
    if not HAS_SOCKS:
        raise RuntimeError(
            "Tor SOCKS proxy requires 'httpx-socks'. Install with: pip install picard-store[tor]"
        )


@dataclass
class TorConfig:
    socks_port: int = 9050
    control_port: int = 9051
    data_dir: Path = field(default_factory=lambda: Path.home() / ".picard" / "tor")
    hidden_service_dir: Path = field(default_factory=lambda: Path.home() / ".picard" / "tor" / "hidden_service")
    tor_binary: str = "tor"
    existing_tor: bool = False


@dataclass
class HiddenService:
    onion_address: str
    port: int
    hostname_file: Path | None = None


class TorManager:
    """Manage Tor process and hidden services for PICARD."""

    def __init__(self, config: TorConfig | None = None) -> None:
        require_tor()
        self._config = config or TorConfig()
        self._process = None
        self._controller: Controller | None = None
        self._hidden_services: list[HiddenService] = []

    @property
    def socks_proxy(self) -> str:
        return f"socks5://127.0.0.1:{self._config.socks_port}"

    @property
    def onion_address(self) -> str | None:
        if self._hidden_services:
            return self._hidden_services[0].onion_address
        return None

    def start(self) -> None:
        self._config.data_dir.mkdir(parents=True, exist_ok=True)

        if self._config.existing_tor:
            self._controller = Controller.from_port(port=self._config.control_port)
            self._controller.authenticate()
            return

        tor_binary = shutil.which(self._config.tor_binary)
        if tor_binary is None:
            raise RuntimeError(
                f"Tor binary not found: {self._config.tor_binary}. "
                "Install Tor: https://www.torproject.org/download/"
            )

        self._process = launch_tor_with_config(
            config={
                "SocksPort": str(self._config.socks_port),
                "ControlPort": str(self._config.control_port),
                "DataDirectory": str(self._config.data_dir),
                "CookieAuthentication": "1",
            },
            tor_cmd=tor_binary,
            take_ownership=True,
        )
        self._controller = Controller.from_port(port=self._config.control_port)
        self._controller.authenticate()

    def create_hidden_service(self, local_port: int, remote_port: int = 80) -> HiddenService:
        if self._controller is None:
            raise RuntimeError("Tor not started. Call start() first.")

        self._config.hidden_service_dir.mkdir(parents=True, exist_ok=True)

        response = self._controller.create_ephemeral_hidden_service(
            {remote_port: f"127.0.0.1:{local_port}"},
            await_publication=True,
            key_type="NEW",
            key_content="ED25519-V3",
        )
        onion = response.service_id + ".onion"
        hs = HiddenService(onion_address=onion, port=local_port)
        self._hidden_services.append(hs)
        return hs

    def create_persistent_hidden_service(self, local_port: int, remote_port: int = 80) -> HiddenService:
        if self._controller is None:
            raise RuntimeError("Tor not started. Call start() first.")

        hs_dir = self._config.hidden_service_dir
        hs_dir.mkdir(parents=True, exist_ok=True)

        key_file = hs_dir / "hs_ed25519_secret_key"
        if key_file.exists():
            response = self._controller.create_ephemeral_hidden_service(
                {remote_port: f"127.0.0.1:{local_port}"},
                key_type="ED25519-V3",
                key_content=key_file.read_text().strip(),
                await_publication=True,
            )
        else:
            response = self._controller.create_ephemeral_hidden_service(
                {remote_port: f"127.0.0.1:{local_port}"},
                key_type="NEW",
                key_content="ED25519-V3",
                await_publication=True,
            )
            if hasattr(response, "private_key"):
                key_file.write_text(response.private_key)
                try:
                    os.chmod(str(key_file), 0o600)
                except OSError:
                    pass

        onion = response.service_id + ".onion"
        hostname_file = hs_dir / "hostname"
        hostname_file.write_text(onion + "\n")

        hs = HiddenService(
            onion_address=onion,
            port=local_port,
            hostname_file=hostname_file,
        )
        self._hidden_services.append(hs)
        return hs

    def stop(self) -> None:
        for hs in self._hidden_services:
            try:
                service_id = hs.onion_address.replace(".onion", "")
                if self._controller:
                    self._controller.remove_ephemeral_hidden_service(service_id)
            except Exception:
                pass
        self._hidden_services.clear()

        if self._controller:
            try:
                self._controller.close()
            except Exception:
                pass
            self._controller = None

        if self._process:
            try:
                self._process.kill()
            except Exception:
                pass
            self._process = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()


def make_tor_transport(socks_proxy: str):
    require_socks()
    return httpx_socks.AsyncProxyTransport.from_url(socks_proxy)
