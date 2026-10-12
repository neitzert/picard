"""Pytest configuration for browser integration tests."""

import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: marks tests as integration tests that hit real services",
    )
    config.addinivalue_line(
        "markers",
        "browser: marks tests that require a Playwright browser",
    )
