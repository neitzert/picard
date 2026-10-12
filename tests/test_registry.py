"""Tests for the adapter registry — auto-discovery and instantiation."""

from __future__ import annotations

import inspect

import pytest

from picard.adapters.base import Adapter
from picard.adapters.memory import MemoryAdapter
from picard.registry import (
    _REGISTRY,
    get_adapter_class,
    instantiate,
    list_adapter_types,
    list_no_arg_adapters,
    register,
)


class TestAutoDiscovery:
    def test_registry_populated(self):
        assert len(_REGISTRY) > 200

    def test_all_types_sorted(self):
        types = list_adapter_types()
        assert types == sorted(types)

    def test_known_types_present(self):
        types = list_adapter_types()
        for expected in ["memory", "filesystem", "dns", "mqtt", "redis", "s3"]:
            assert expected in types

    def test_all_values_are_adapter_subclasses(self):
        for type_name, cls in _REGISTRY.items():
            assert issubclass(cls, Adapter), f"{type_name} -> {cls} is not an Adapter"

    def test_count_matches_list(self):
        assert len(list_adapter_types()) == len(_REGISTRY)


class TestGetAdapterClass:
    def test_known(self):
        cls = get_adapter_class("memory")
        assert cls is MemoryAdapter

    def test_unknown_raises(self):
        with pytest.raises(KeyError):
            get_adapter_class("nonexistent-adapter-xyz")

    def test_returns_class(self):
        cls = get_adapter_class("filesystem")
        assert inspect.isclass(cls)
        assert issubclass(cls, Adapter)


class TestInstantiate:
    def test_memory(self):
        adapter = instantiate("memory")
        assert isinstance(adapter, MemoryAdapter)
        assert adapter.profile().name == "memory"

    def test_memory_with_name(self):
        adapter = instantiate("memory", name="test-proto")
        assert adapter.profile().name == "test-proto"

    def test_unknown_raises(self):
        with pytest.raises(KeyError):
            instantiate("nonexistent-adapter-xyz")


class TestListNoArgAdapters:
    def test_returns_list(self):
        result = list_no_arg_adapters()
        assert isinstance(result, list)
        assert len(result) > 0

    def test_all_are_registered(self):
        types = set(list_adapter_types())
        for t in list_no_arg_adapters():
            assert t in types

    def test_no_arg_adapters_instantiate(self):
        for type_name in list_no_arg_adapters()[:5]:
            cls = get_adapter_class(type_name)
            instance = cls()
            assert isinstance(instance, Adapter)

    def test_subset_of_all(self):
        no_arg = list_no_arg_adapters()
        all_types = list_adapter_types()
        assert len(no_arg) < len(all_types)


class TestRegister:
    def test_register_custom(self):
        class FakeAdapter(Adapter):
            def profile(self):
                pass
            async def write_chunk(self, ec):
                pass
            async def read_chunk(self, ref):
                pass
            async def delete_chunk(self, ref):
                pass
            async def list_chunks(self, mid):
                pass
            async def health_check(self):
                pass

        register("__test-fake__", FakeAdapter)
        assert get_adapter_class("__test-fake__") is FakeAdapter
        # cleanup
        _REGISTRY.pop("__test-fake__", None)

    def test_duplicate_raises(self):
        original = get_adapter_class("memory")
        with pytest.raises(ValueError, match="already registered"):
            register("memory", original)
