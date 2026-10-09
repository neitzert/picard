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

    def test_custom_max_chunk_bytes(self):
        ra = RemoteAdapter("http://peer:8471", "dns", max_chunk_bytes=4096)
        assert ra.profile().max_chunk_bytes == 4096

    def test_default_max_chunk_bytes(self):
        ra = RemoteAdapter("http://peer:8471", "memory")
        assert ra.profile().max_chunk_bytes == 64 * 1024


class TestRemoteAdapterHMAC:
    def test_sign_empty_without_secret(self):
        ra = RemoteAdapter("http://peer:8471", "memory")
        assert ra._sign(b"test", "/transport/v1/health") == {}

    def test_sign_returns_header_with_secret(self):
        ra = RemoteAdapter("http://peer:8471", "memory", secret="s3cr3t")
        headers = ra._sign(b"test body", "/transport/v1/write_chunk")
        assert "x-signature" in headers
        assert "x-timestamp" in headers
        assert len(headers["x-signature"]) == 64

    def test_sign_includes_path_and_timestamp(self):
        ra = RemoteAdapter("http://peer:8471", "memory", secret="key")
        h = ra._sign(b"data", "/some/path")
        assert "x-signature" in h
        assert "x-timestamp" in h
        assert float(h["x-timestamp"]) > 0

    def test_sign_differs_for_different_data(self):
        ra = RemoteAdapter("http://peer:8471", "memory", secret="key")
        a = ra._sign(b"data1", "/path")
        b = ra._sign(b"data2", "/path")
        assert a["x-signature"] != b["x-signature"]

    def test_sign_differs_for_different_paths(self):
        ra = RemoteAdapter("http://peer:8471", "memory", secret="key")
        a = ra._sign(b"data", "/path1")
        b = ra._sign(b"data", "/path2")
        assert a["x-signature"] != b["x-signature"]

    def test_ssrf_rejects_non_http(self):
        import pytest
        with pytest.raises(ValueError, match="http or https"):
            RemoteAdapter("file:///etc/passwd", "memory")
        with pytest.raises(ValueError, match="http or https"):
            RemoteAdapter("gopher://evil.com", "memory")
