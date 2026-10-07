"""Adapter registry — auto-discovers and maps type names to adapter classes.

Scans ``picard/adapters/`` on import, registers every Adapter subclass
it finds, and provides instantiation from configuration.
"""

from __future__ import annotations

import importlib
import inspect
import os
from pathlib import Path

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


def instantiate(type_name: str, **params) -> Adapter:
    """Create an adapter instance from the registry with the given params."""
    cls = get_adapter_class(type_name)
    return cls(**params)


def list_no_arg_adapters() -> list[str]:
    """Return type names whose constructors need no required positional args."""
    result = []
    for type_name, cls in sorted(_REGISTRY.items()):
        sig = inspect.signature(cls.__init__)
        required = [
            p for p in sig.parameters.values()
            if p.name != "self"
            and p.default is inspect.Parameter.empty
            and p.kind not in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            )
        ]
        if not required:
            result.append(type_name)
    return result


def _module_to_type_name(filename: str) -> str:
    """Derive a type name from a module filename.

    ``netflow_adapter.py`` → ``netflow``
    ``backblaze_b2.py``    → ``backblaze-b2``
    ``audio_stego.py``     → ``audio-stego``
    """
    stem = filename.removesuffix(".py")
    if stem.endswith("_adapter"):
        stem = stem[: -len("_adapter")]
    return stem.replace("_", "-")


def _auto_discover() -> None:
    """Scan picard/adapters/ and register every Adapter subclass found."""
    adapters_pkg = Path(__file__).parent / "adapters"
    for fname in sorted(os.listdir(adapters_pkg)):
        if not fname.endswith(".py") or fname.startswith("__") or fname in ("base.py", "browser_base.py"):
            continue
        modname = f"picard.adapters.{fname[:-3]}"
        type_name = _module_to_type_name(fname)
        try:
            mod = importlib.import_module(modname)
        except Exception:
            continue
        for _name, obj in inspect.getmembers(mod, inspect.isclass):
            if (
                issubclass(obj, Adapter)
                and obj is not Adapter
                and obj.__module__ == modname
            ):
                if type_name not in _REGISTRY:
                    _REGISTRY[type_name] = obj
                break


_auto_discover()
