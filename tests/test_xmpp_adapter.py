"""Tests for the XMPP adapter — mocked, no network calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.xmpp import XmppAdapter
from picard.chunker import Chunk


def _sample_encoded(manifest_id: str = "m1", block: int = 0, idx: int = 0) -> str:
    return Chunk(
        version=1,
        manifest_id=manifest_id,
        block_index=block,
        chunk_index=idx,
        total_chunks=1,
        payload=b"test-payload",
    ).encode()


def _make_adapter(**kwargs) -> XmppAdapter:
    defaults = dict(
        jid="user@jabber.org",
        password="secret",
        recipient_jid="room@conference.jabber.org",
    )
    defaults.update(kwargs)
    return XmppAdapter(**defaults)


def _mock_mam_results(messages: list[dict]) -> dict:
    results = []
    for msg in messages:
        stanza = MagicMock()
        stanza.__getitem__ = lambda self, k, m=msg: m.get("body", "") if k == "body" else m.get("id", "")
        stanza.get = lambda k, d="", m=msg: m.get("id", d) if k == "id" else d

        body_val = msg.get("body", "")
        stanza_mock = {"body": body_val, "id": msg.get("id", "")}
        stanza_obj = MagicMock()
        stanza_obj.__getitem__ = lambda self, k, s=stanza_mock: s[k]
        stanza_obj.get = lambda k, d="", s=stanza_mock: s.get(k, d)

        fwd = MagicMock()
        fwd.__getitem__ = lambda self, k, so=stanza_obj: so if k == "stanza" else None

        mam_result = MagicMock()
        mam_result.__getitem__ = lambda self, k, f=fwd: f if k == "forwarded" else None

        result_entry = MagicMock()
        result_entry.__getitem__ = lambda self, k, mr=mam_result: mr if k == "mam_result" else None

        results.append(result_entry)

    mam = MagicMock()
    mam.__getitem__ = lambda self, k: results if k == "results" else None

    resp = MagicMock()
    resp.__getitem__ = lambda self, k: mam if k == "mam" else None

    return resp


class TestXmppAdapter:
    def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "xmpp"
        assert p.max_chunk_bytes == 60_000
        assert p.persistent is False
        assert p.ordered is True
        assert p.rate_limit_per_min == 60

    @pytest.mark.asyncio
    async def test_write_chunk(self):
        a = _make_adapter()
        client = MagicMock()
        a._client = client

        enc = _sample_encoded()
        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "xmpp"
        assert len(ref.location) == 32  # uuid4 hex
        client.send_message.assert_called_once()
        call_kwargs = client.send_message.call_args.kwargs
        assert call_kwargs["mbody"] == enc
        assert call_kwargs["mto"] == "room@conference.jabber.org"
        assert call_kwargs["mtype"] == "chat"

    @pytest.mark.asyncio
    async def test_write_chunk_muc(self):
        a = _make_adapter(use_muc=True)
        client = MagicMock()
        a._client = client

        enc = _sample_encoded()
        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "xmpp"
        call_kwargs = client.send_message.call_args.kwargs
        assert call_kwargs["mtype"] == "groupchat"

    @pytest.mark.asyncio
    async def test_read_chunk(self):
        a = _make_adapter()
        client = MagicMock()
        a._client = client

        msg_id = "abc123def456"
        enc = _sample_encoded()

        mam_plugin = AsyncMock()
        mam_plugin.retrieve = AsyncMock(return_value=_mock_mam_results([
            {"id": msg_id, "body": enc},
            {"id": "other", "body": "not this one"},
        ]))
        client.plugin = {"xep_0313": mam_plugin}

        ref = StoredChunkRef(adapter_name="xmpp", location=msg_id)
        result = await a.read_chunk(ref)
        assert result == enc

    @pytest.mark.asyncio
    async def test_read_chunk_not_found(self):
        a = _make_adapter()
        client = MagicMock()
        a._client = client

        mam_plugin = AsyncMock()
        mam_plugin.retrieve = AsyncMock(return_value=_mock_mam_results([]))
        client.plugin = {"xep_0313": mam_plugin}

        ref = StoredChunkRef(adapter_name="xmpp", location="nonexistent")
        with pytest.raises(ValueError, match="chunk not found"):
            await a.read_chunk(ref)

    @pytest.mark.asyncio
    async def test_delete_chunk(self):
        a = _make_adapter()
        client = MagicMock()
        a._client = client

        ref = StoredChunkRef(adapter_name="xmpp", location="msg123")
        assert await a.delete_chunk(ref) is True

        call_kwargs = client.send_message.call_args.kwargs
        assert call_kwargs["mbody"] == "PCHK-DEL:msg123"

    @pytest.mark.asyncio
    async def test_delete_chunk_failure(self):
        a = _make_adapter()
        client = MagicMock()
        client.send_message.side_effect = Exception("disconnected")
        a._client = client

        ref = StoredChunkRef(adapter_name="xmpp", location="msg123")
        assert await a.delete_chunk(ref) is False

    @pytest.mark.asyncio
    async def test_list_chunks(self):
        a = _make_adapter()
        client = MagicMock()
        a._client = client

        enc1 = _sample_encoded("m1", 0, 0)
        enc2 = _sample_encoded("m1", 0, 1)
        enc_other = _sample_encoded("m2", 0, 0)

        mam_plugin = AsyncMock()
        mam_plugin.retrieve = AsyncMock(return_value=_mock_mam_results([
            {"id": "id1", "body": enc1},
            {"id": "id2", "body": enc2},
            {"id": "id3", "body": enc_other},
        ]))
        client.plugin = {"xep_0313": mam_plugin}

        refs = await a.list_chunks("m1")
        assert len(refs) == 2
        assert all(r.adapter_name == "xmpp" for r in refs)
        locations = [r.location for r in refs]
        assert "id1" in locations
        assert "id2" in locations

    @pytest.mark.asyncio
    async def test_list_chunks_empty(self):
        a = _make_adapter()
        client = MagicMock()
        a._client = client

        mam_plugin = AsyncMock()
        mam_plugin.retrieve = AsyncMock(return_value=_mock_mam_results([]))
        client.plugin = {"xep_0313": mam_plugin}

        refs = await a.list_chunks("m1")
        assert refs == []

    @pytest.mark.asyncio
    async def test_health_check_connected(self):
        a = _make_adapter()
        client = MagicMock()
        client.is_connected.return_value = True
        a._client = client

        assert await a.health_check() is True

    @pytest.mark.asyncio
    async def test_health_check_disconnected(self):
        a = _make_adapter()
        client = MagicMock()
        client.is_connected.return_value = False
        a._client = client

        assert await a.health_check() is False

    @pytest.mark.asyncio
    async def test_health_check_exception(self):
        a = _make_adapter()
        a._client = None
        with patch(
            "picard.adapters.xmpp.XmppAdapter._ensure_client",
            side_effect=Exception("connection refused"),
        ):
            assert await a.health_check() is False
