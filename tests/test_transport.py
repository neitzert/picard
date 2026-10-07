"""Tests for the transport API."""

import pytest
from fastapi.testclient import TestClient

from picard.adapters.memory import MemoryAdapter
from picard.transport import configure, transport_app


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

    def test_health_works_without_signature(self):
        adapter = MemoryAdapter()
        configure({"memory": adapter}, secret="my-secret")
        client = TestClient(transport_app)

        resp = client.get("/transport/v1/health")
        assert resp.status_code == 200

        configure({})
