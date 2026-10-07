"""Tests for the remote adapter."""

import pytest

from picard.adapters.remote import RemoteAdapter


class TestRemoteAdapterProfile:
    def test_profile_name(self):
        ra = RemoteAdapter("http://peer:8471", "memory")
        prof = ra.profile()
        assert "remote:" in prof.name
        assert "memory" in prof.name
        assert prof.tags["remote"] == "true"

    def test_profile_peer_url(self):
        ra = RemoteAdapter("http://peer:8471", "fs")
        prof = ra.profile()
        assert prof.tags["peer"] == "http://peer:8471"

    def test_trailing_slash_stripped(self):
        ra = RemoteAdapter("http://peer:8471/", "memory")
        assert ra._peer_url == "http://peer:8471"


class TestRemoteAdapterHMAC:
    def test_sign_empty_without_secret(self):
        ra = RemoteAdapter("http://peer:8471", "memory")
        assert ra._sign(b"test") == {}

    def test_sign_returns_header_with_secret(self):
        ra = RemoteAdapter("http://peer:8471", "memory", secret="s3cr3t")
        headers = ra._sign(b"test body")
        assert "x-signature" in headers
        assert len(headers["x-signature"]) == 64

    def test_sign_deterministic(self):
        ra = RemoteAdapter("http://peer:8471", "memory", secret="key")
        a = ra._sign(b"data")
        b = ra._sign(b"data")
        assert a == b

    def test_sign_differs_for_different_data(self):
        ra = RemoteAdapter("http://peer:8471", "memory", secret="key")
        a = ra._sign(b"data1")
        b = ra._sign(b"data2")
        assert a != b
