"""Adapter registry — maps type names to adapter classes.

Provides discovery of available adapter types and instantiation
from configuration.
"""

from __future__ import annotations

from picard.adapters.base import Adapter

_REGISTRY: dict[str, type[Adapter]] = {}


def register(type_name: str, cls: type[Adapter]) -> None:
    """Register an adapter class under *type_name*."""
    if type_name in _REGISTRY:
        raise ValueError(f"adapter type already registered: {type_name!r}")
    _REGISTRY[type_name] = cls


def get_adapter_class(type_name: str) -> type[Adapter]:
    """Return the adapter class for *type_name*, or raise KeyError."""
    try:
        return _REGISTRY[type_name]
    except KeyError:
        raise KeyError(f"unknown adapter type: {type_name!r}") from None


def list_adapter_types() -> list[str]:
    """Return sorted list of registered adapter type names."""
    return sorted(_REGISTRY)


def _register_builtins() -> None:
    """Register all built-in adapters."""
    from picard.adapters.memory import MemoryAdapter
    from picard.adapters.filesystem import FilesystemAdapter

    register("memory", MemoryAdapter)
    register("filesystem", FilesystemAdapter)

    _register_optional("bluesky", "picard.adapters.bluesky", "BlueskyAdapter")
    _register_optional("reddit", "picard.adapters.reddit", "RedditAdapter")
    _register_optional("dns", "picard.adapters.dns", "DnsAdapter")
    _register_optional(
        "cloudflare-dns", "picard.adapters.cloudflare_dns", "CloudflareDnsAdapter"
    )
    _register_optional("mqtt", "picard.adapters.mqtt", "MqttAdapter")
    _register_optional("icmp", "picard.adapters.icmp", "IcmpAdapter")


def _register_optional(type_name: str, module: str, class_name: str) -> None:
    """Register an adapter only if its module can be imported."""
    try:
        import importlib

        mod = importlib.import_module(module)
        cls = getattr(mod, class_name)
        register(type_name, cls)
    except (ImportError, Exception):
        pass


_register_builtins()
