"""Tests for the key hierarchy module."""

from __future__ import annotations

import pytest

from picard.keys import (
    KeyRing,
    MasterKey,
    derive_correlation_tag,
    derive_sub_key,
    load_master_key,
)


class TestLoadMasterKey:
    def test_from_passphrase(self):
        mk = load_master_key(passphrase="test-pass")
        assert len(mk.key) == 32
        assert len(mk.salt) == 16
        assert mk.source == "passphrase"

    def test_from_keyfile(self, tmp_path):
        kf = tmp_path / "master.key"
        kf.write_bytes(b"secret-key-material-1234567890ab")
        mk = load_master_key(keyfile=kf)
        assert len(mk.key) == 32
        assert len(mk.salt) == 16
        assert mk.source == "keyfile"

    def test_neither_raises(self):
        with pytest.raises(ValueError, match="provide passphrase or keyfile"):
            load_master_key()

    def test_both_raises(self, tmp_path):
        kf = tmp_path / "master.key"
        kf.write_bytes(b"x")
        with pytest.raises(ValueError, match="not both"):
            load_master_key(passphrase="pass", keyfile=kf)

    def test_passphrase_with_salt_reproduces(self):
        mk1 = load_master_key(passphrase="test-pass")
        mk2 = load_master_key(passphrase="test-pass", salt=mk1.salt)
        assert mk1.key == mk2.key

    def test_keyfile_with_salt_reproduces(self, tmp_path):
        kf = tmp_path / "master.key"
        kf.write_bytes(b"deterministic-content")
        mk1 = load_master_key(keyfile=kf)
        mk2 = load_master_key(keyfile=kf, salt=mk1.salt)
        assert mk1.key == mk2.key


class TestDeriveSubKey:
    def _master(self) -> MasterKey:
        return load_master_key(passphrase="test-pass", salt=b"\x00" * 16)

    def test_deterministic(self):
        m = self._master()
        sk1 = derive_sub_key(m, "redis")
        sk2 = derive_sub_key(m, "redis")
        assert sk1.key == sk2.key

    def test_different_adapters_different_keys(self):
        m = self._master()
        sk1 = derive_sub_key(m, "redis")
        sk2 = derive_sub_key(m, "s3")
        assert sk1.key != sk2.key

    def test_different_versions_different_keys(self):
        m = self._master()
        sk1 = derive_sub_key(m, "redis", version=1)
        sk2 = derive_sub_key(m, "redis", version=2)
        assert sk1.key != sk2.key

    def test_sub_key_length(self):
        sk = derive_sub_key(self._master(), "test")
        assert len(sk.key) == 32

    def test_sub_key_fields(self):
        sk = derive_sub_key(self._master(), "mqtt", version=3)
        assert sk.adapter_name == "mqtt"
        assert sk.version == 3


class TestCorrelationTag:
    def _sub_key(self, adapter: str = "test") -> "SubKey":
        m = load_master_key(passphrase="test-pass", salt=b"\x00" * 16)
        return derive_sub_key(m, adapter)

    def test_deterministic(self):
        sk = self._sub_key()
        t1 = derive_correlation_tag(sk, "manifest-abc")
        t2 = derive_correlation_tag(sk, "manifest-abc")
        assert t1 == t2

    def test_length(self):
        tag = derive_correlation_tag(self._sub_key(), "m1")
        assert len(tag) == 8

    def test_different_manifests_different_tags(self):
        sk = self._sub_key()
        t1 = derive_correlation_tag(sk, "m1")
        t2 = derive_correlation_tag(sk, "m2")
        assert t1 != t2


class TestKeyRing:
    def _ring(self) -> KeyRing:
        mk = load_master_key(passphrase="test-pass", salt=b"\x00" * 16)
        return KeyRing(mk)

    def test_caching(self):
        ring = self._ring()
        sk1 = ring.get_sub_key("redis")
        sk2 = ring.get_sub_key("redis")
        assert sk1 is sk2

    def test_rotation_increments_version(self):
        ring = self._ring()
        sk = ring.rotate("redis")
        assert sk.version == 2
        assert ring.versions["redis"] == 2

    def test_rotation_produces_different_key(self):
        ring = self._ring()
        sk1 = ring.get_sub_key("redis", version=1)
        sk2 = ring.rotate("redis")
        assert sk1.key != sk2.key

    def test_double_rotation(self):
        ring = self._ring()
        ring.rotate("s3")
        sk = ring.rotate("s3")
        assert sk.version == 3
