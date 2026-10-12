"""Tests for the transport API."""

import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from picard.adapters.memory import MemoryAdapter
from picard.transport import _HMAC_MAX_AGE, _RateLimiter, configure, transport_app


@pytest.fixture
def client():
    adapter = MemoryAdapter()
    configure({"memory": adapter})
    yield TestClient(transport_app)
    configure({})


class TestTransportAPI:
    def test_health(self, client):
        resp = client.get("/transport/v1/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["adapters"] == 1

    def test_list_adapters(self, client):
        resp = client.get("/transport/v1/adapters")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["adapters"]) == 1
        assert data["adapters"][0]["name"] == "memory"

    def test_write_read_roundtrip(self, client):
        resp = client.post("/transport/v1/write_chunk", json={
            "adapter": "memory",
            "data": "PCHK:1:abc:0:0:1:dGVzdA==",
        })
        assert resp.status_code == 200
        ref = resp.json()
        assert ref["adapter"] == "memory"

        resp = client.post("/transport/v1/read_chunk", json={
            "adapter": "memory",
            "ref": ref["location"],
        })
        assert resp.status_code == 200
        assert resp.json()["data"] == "PCHK:1:abc:0:0:1:dGVzdA=="

    def test_delete(self, client):
        resp = client.post("/transport/v1/write_chunk", json={
            "adapter": "memory",
            "data": "PCHK:1:def:0:0:1:ZGVs",
        })
        ref = resp.json()
        resp = client.post("/transport/v1/delete_chunk", json={
            "adapter": "memory",
            "ref": ref["location"],
        })
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

    def test_list_chunks(self, client):
        client.post("/transport/v1/write_chunk", json={
            "adapter": "memory",
            "data": "PCHK:1:manifest99:0:0:1:YQ==",
        })
        resp = client.post("/transport/v1/list_chunks", json={
            "adapter": "memory",
            "manifest_id": "manifest99",
        })
        assert resp.status_code == 200
        assert len(resp.json()["refs"]) == 1

    def test_unknown_adapter_404(self, client):
        resp = client.post("/transport/v1/read_chunk", json={
            "adapter": "nonexistent",
            "ref": "nope",
        })
        assert resp.status_code == 404


class TestTransportHMAC:
    def test_hmac_rejects_unsigned_write(self):
        adapter = MemoryAdapter()
        configure({"memory": adapter}, secret="my-secret")
        client = TestClient(transport_app)

        resp = client.post("/transport/v1/write_chunk", json={
            "adapter": "memory",
            "data": "PCHK:1:x:0:0:1:YQ==",
        })
        assert resp.status_code == 403

        configure({})

    def test_health_requires_signature_when_secret_set(self):
        adapter = MemoryAdapter()
        configure({"memory": adapter}, secret="my-secret")
        client = TestClient(transport_app)

        resp = client.get("/transport/v1/health")
        assert resp.status_code == 403

        configure({})


class TestRateLimiter:
    def test_allows_within_limit(self):
        rl = _RateLimiter(max_requests=5, window_seconds=60.0)
        for _ in range(5):
            assert rl.allow("1.2.3.4")

    def test_blocks_over_limit(self):
        rl = _RateLimiter(max_requests=3, window_seconds=60.0)
        for _ in range(3):
            assert rl.allow("1.2.3.4")
        assert not rl.allow("1.2.3.4")

    def test_separate_clients_independent(self):
        rl = _RateLimiter(max_requests=2, window_seconds=60.0)
        assert rl.allow("1.1.1.1")
        assert rl.allow("1.1.1.1")
        assert not rl.allow("1.1.1.1")
        assert rl.allow("2.2.2.2")

    def test_window_expiry(self):
        rl = _RateLimiter(max_requests=1, window_seconds=0.01)
        assert rl.allow("1.2.3.4")
        assert not rl.allow("1.2.3.4")
        import time
        time.sleep(0.05)
        assert rl.allow("1.2.3.4")


class TestTransportHMACTimestampExpiry:
    """The transport rejects requests whose HMAC timestamp is expired.

    Note: the transport uses timestamp-based HMAC validation but has no
    nonce-tracking replay protection.  A validly signed request can be
    replayed within the ``_HMAC_MAX_AGE`` window.  These tests verify
    the expiry boundary.
    """

    def _sign(self, secret: str, path: str, body: bytes, timestamp: str) -> str:
        msg = path.encode() + b"\n" + timestamp.encode() + b"\n" + body
        return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()

    def test_valid_timestamp_accepted(self):
        secret = "test-hmac-secret"
        adapter = MemoryAdapter()
        configure({"memory": adapter}, secret=secret)
        client = TestClient(transport_app)

        path = "/transport/v1/health"
        ts = str(time.time())
        sig = self._sign(secret, path, b"", ts)

        resp = client.get(path, headers={
            "x-signature": sig,
            "x-timestamp": ts,
        })
        assert resp.status_code == 200
        configure({})

    def test_expired_timestamp_rejected(self):
        secret = "test-hmac-secret"
        adapter = MemoryAdapter()
        configure({"memory": adapter}, secret=secret)
        client = TestClient(transport_app)

        path = "/transport/v1/health"
        # Timestamp well beyond the max-age window.
        expired_ts = str(time.time() - _HMAC_MAX_AGE - 60)
        sig = self._sign(secret, path, b"", expired_ts)

        resp = client.get(path, headers={
            "x-signature": sig,
            "x-timestamp": expired_ts,
        })
        assert resp.status_code == 403
        configure({})

    def test_signed_write_with_valid_timestamp(self):
        secret = "write-secret"
        adapter = MemoryAdapter()
        configure({"memory": adapter}, secret=secret)
        client = TestClient(transport_app)

        path = "/transport/v1/write_chunk"
        body = json.dumps({
            "adapter": "memory",
            "data": "PCHK:1:hmactest:0:0:1:dGVzdA==",
        }).encode()
        ts = str(time.time())
        sig = self._sign(secret, path, body, ts)

        resp = client.post(path, content=body, headers={
            "content-type": "application/json",
            "x-signature": sig,
            "x-timestamp": ts,
        })
        assert resp.status_code == 200
        assert resp.json()["adapter"] == "memory"
        configure({})
