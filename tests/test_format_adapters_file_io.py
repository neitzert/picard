"""Tests for format adapter file I/O — TOML, YAML, iCal, RSS, Atom, vCard, hosts_file."""

from __future__ import annotations

import pytest

from picard.adapters.toml_adapter import TOMLAdapter
from picard.adapters.yaml_adapter import YAMLAdapter
from picard.adapters.ical_adapter import ICalAdapter
from picard.adapters.rss_adapter import RSSAdapter
from picard.adapters.atom_adapter import AtomAdapter
from picard.adapters.vcard_adapter import VCardAdapter
from picard.adapters.hosts_file import HostsFileAdapter


class TestTOMLFileIO:
    @pytest.mark.asyncio
    async def test_write_creates_file(self, tmp_path):
        fp = tmp_path / "chunks.toml"
        adapter = TOMLAdapter(file_path=str(fp))
        ref = await adapter.write_chunk("test-data-001")
        assert fp.exists()
        content = fp.read_text(encoding="utf-8")
        assert "test-data-001" in content
        assert "[picard]" in content

    @pytest.mark.asyncio
    async def test_read_from_file_after_cache_clear(self, tmp_path):
        fp = tmp_path / "chunks.toml"
        adapter = TOMLAdapter(file_path=str(fp))
        ref = await adapter.write_chunk("recover-me")
        adapter._cache.clear()
        result = await adapter.read_chunk(ref)
        assert result == "recover-me"

    @pytest.mark.asyncio
    async def test_persistent_profile(self, tmp_path):
        adapter_mem = TOMLAdapter()
        adapter_file = TOMLAdapter(file_path=str(tmp_path / "x.toml"))
        assert adapter_mem.profile().persistent is False
        assert adapter_file.profile().persistent is True

    @pytest.mark.asyncio
    async def test_delete_updates_file(self, tmp_path):
        fp = tmp_path / "chunks.toml"
        adapter = TOMLAdapter(file_path=str(fp))
        ref = await adapter.write_chunk("delete-me")
        assert await adapter.delete_chunk(ref)
        content = fp.read_text(encoding="utf-8")
        assert "delete-me" not in content


class TestYAMLFileIO:
    @pytest.mark.asyncio
    async def test_write_creates_file(self, tmp_path):
        fp = tmp_path / "chunks.yaml"
        adapter = YAMLAdapter(file_path=str(fp))
        await adapter.write_chunk("yaml-data-001")
        assert fp.exists()
        content = fp.read_text(encoding="utf-8")
        assert "yaml-data-001" in content
        assert "picard:" in content

    @pytest.mark.asyncio
    async def test_roundtrip(self, tmp_path):
        fp = tmp_path / "chunks.yaml"
        adapter = YAMLAdapter(file_path=str(fp))
        ref = await adapter.write_chunk("yaml-round")
        adapter._cache.clear()
        result = await adapter.read_chunk(ref)
        assert result == "yaml-round"


class TestICalFileIO:
    @pytest.mark.asyncio
    async def test_write_creates_ics(self, tmp_path):
        fp = tmp_path / "cal.ics"
        adapter = ICalAdapter(file_path=str(fp))
        await adapter.write_chunk("event-data-001")
        assert fp.exists()
        content = fp.read_text(encoding="utf-8")
        assert "BEGIN:VCALENDAR" in content
        assert "event-data-001" in content

    @pytest.mark.asyncio
    async def test_roundtrip(self, tmp_path):
        fp = tmp_path / "cal.ics"
        adapter = ICalAdapter(file_path=str(fp))
        ref = await adapter.write_chunk("ical-round")
        adapter._cache.clear()
        result = await adapter.read_chunk(ref)
        assert result == "ical-round"


class TestRSSFileIO:
    @pytest.mark.asyncio
    async def test_write_creates_rss(self, tmp_path):
        fp = tmp_path / "feed.xml"
        adapter = RSSAdapter(file_path=str(fp))
        await adapter.write_chunk("rss-item-001")
        assert fp.exists()
        content = fp.read_text(encoding="utf-8")
        assert "<rss" in content
        assert "rss-item-001" in content

    @pytest.mark.asyncio
    async def test_roundtrip(self, tmp_path):
        fp = tmp_path / "feed.xml"
        adapter = RSSAdapter(file_path=str(fp))
        ref = await adapter.write_chunk("rss-round")
        adapter._cache.clear()
        result = await adapter.read_chunk(ref)
        assert result == "rss-round"


class TestAtomFileIO:
    @pytest.mark.asyncio
    async def test_write_creates_atom(self, tmp_path):
        fp = tmp_path / "atom.xml"
        adapter = AtomAdapter(file_path=str(fp))
        await adapter.write_chunk("atom-entry-001")
        assert fp.exists()
        content = fp.read_text(encoding="utf-8")
        assert "<feed" in content
        assert "atom-entry-001" in content

    @pytest.mark.asyncio
    async def test_roundtrip(self, tmp_path):
        fp = tmp_path / "atom.xml"
        adapter = AtomAdapter(file_path=str(fp))
        ref = await adapter.write_chunk("atom-round")
        adapter._cache.clear()
        result = await adapter.read_chunk(ref)
        assert result == "atom-round"


class TestVCardFileIO:
    @pytest.mark.asyncio
    async def test_write_creates_vcf(self, tmp_path):
        fp = tmp_path / "contacts.vcf"
        adapter = VCardAdapter(file_path=str(fp))
        await adapter.write_chunk("vcard-note-001")
        assert fp.exists()
        content = fp.read_text(encoding="utf-8")
        assert "BEGIN:VCARD" in content
        assert "vcard-note-001" in content

    @pytest.mark.asyncio
    async def test_roundtrip(self, tmp_path):
        fp = tmp_path / "contacts.vcf"
        adapter = VCardAdapter(file_path=str(fp))
        ref = await adapter.write_chunk("vcard-round")
        adapter._cache.clear()
        result = await adapter.read_chunk(ref)
        assert result == "vcard-round"


class TestHostsFileIO:
    @pytest.mark.asyncio
    async def test_write_creates_hosts(self, tmp_path):
        fp = tmp_path / "hosts"
        adapter = HostsFileAdapter(file_path=str(fp))
        await adapter.write_chunk("hosts-data-001")
        assert fp.exists()
        content = fp.read_text(encoding="utf-8")
        assert "picard.local" in content
        assert "PICARD:" in content

    @pytest.mark.asyncio
    async def test_delete_updates_file(self, tmp_path):
        fp = tmp_path / "hosts"
        adapter = HostsFileAdapter(file_path=str(fp))
        ref = await adapter.write_chunk("del-hosts")
        assert await adapter.delete_chunk(ref)
        content = fp.read_text(encoding="utf-8")
        assert ref.location.split("://")[1] not in content

    @pytest.mark.asyncio
    async def test_no_file_stays_in_memory(self):
        adapter = HostsFileAdapter()
        ref = await adapter.write_chunk("mem-only")
        result = await adapter.read_chunk(ref)
        assert result == "mem-only"
        assert adapter.profile().persistent is False


class TestAuthorizedKeysWired:
    @pytest.mark.asyncio
    async def test_write_appends_to_file(self, tmp_path):
        from picard.adapters.authorized_keys_adapter import AuthorizedKeysAdapter

        keys_file = tmp_path / ".ssh" / "authorized_keys"
        adapter = AuthorizedKeysAdapter(keys_path=str(keys_file))
        ref = await adapter.write_chunk("ssh-chunk-001")
        assert keys_file.exists()
        content = keys_file.read_text(encoding="utf-8")
        assert "PICARD:" in content
        assert "ssh-chunk-001" in content
        assert "ssh-rsa" in content

    @pytest.mark.asyncio
    async def test_read_recovers_from_file(self, tmp_path):
        from picard.adapters.authorized_keys_adapter import AuthorizedKeysAdapter

        keys_file = tmp_path / ".ssh" / "authorized_keys"
        adapter = AuthorizedKeysAdapter(keys_path=str(keys_file))
        ref = await adapter.write_chunk("recover-ssh")
        adapter._cache.clear()
        result = await adapter.read_chunk(ref)
        assert result == "recover-ssh"
