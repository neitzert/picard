"""Shared pytest configuration."""

# pytest-asyncio: auto mode so @pytest.mark.asyncio is applied to all
# async test functions without decorating each one individually.
# (The decorator in the test files is harmless but explicit.)
import pytest


def pytest_collection_modifyitems(config, items):
    """Ensure async tests run with asyncio."""
    for item in items:
        if item.get_closest_marker("asyncio") is None and "async" in str(
            item.obj.__class__.__dict__.get(item.originalname, "")
        ):
            item.add_marker(pytest.mark.asyncio)
