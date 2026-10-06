"""Tests for the encrypted portable bundle module."""

from __future__ import annotations

import pytest
from cryptography.exceptions import InvalidTag

from picard.bundle import (
    BUNDLE_VERSION,
    export_bundle,
    import_bundle,
    peek_bundle_salt,
)
from picard.keys import KeyRing, load_master_key
from picard.manifest import (
    BlockRecord,
    ChunkRecord,
    ChunkStatus,
    Manifest,
)


def _sample_manifest() -> Manifest:
    return Manifest(
        manifest_id="abc123",
        path="/docs/secret.txt",
        original_size=1024,
        block_size=512,
        total_blocks=2,
        created_at="2026-10-06T12:00:00Z",
        plaintext_hash="deadbeef" * 8,
        tags={"env": "test"},
        blocks=[
            BlockRecord(
                block_index=0,
                digest="aa" * 32,
                size_bytes=540,
                chunks=[
                    ChunkRecord(
                        chunk_index=0,
                        adapter_name="redis",
                        location="picard:abc123:b0:c0",
                        status=ChunkStatus.STORED,
                        size_bytes=300,
                        digest="bb" * 32,
                    ),
                    ChunkRecord(
                        chunk_index=1,
                        adapter_name="s3",
                        location="picard/abc123/b0_c1.pchk",
                        status=ChunkStatus.STORED,
                        size_bytes=240,
                        digest="cc" * 32,
                    ),
                ],
            ),
            BlockRecord(
                block_index=1,
                digest="dd" * 32,
                size_bytes=520,
                chunks=[
                    ChunkRecord(
                        chunk_index=0,
                        adapter_name="redis",
                        location="picard:abc123:b1:c0",
                        status=ChunkStatus.STORED,
                        size_bytes=520,
                        digest="ee" * 32,
                    ),
                ],
            ),
        ],
    )


def _master(salt=b"\x00" * 16):
    return load_master_key(passphrase="test-pass", salt=salt)


class TestExportBundle:
    def test_produces_bundle_string(self):
        bundle = export_bundle(_sample_manifest(), _master())
        assert bundle.startswith("PICARD-BUNDLE:1:")

    def test_has_four_colon_parts(self):
        bundle = export_bundle(_sample_manifest(), _master())
        parts = bundle.split(":")
        assert len(parts) == 4
        assert parts[0] == "PICARD-BUNDLE"
        assert parts[1] == "1"


class TestImportBundle:
    def test_round_trip(self):
        mk = _master()
        original = _sample_manifest()
        bundle = export_bundle(original, mk)
        manifest, hints, versions = import_bundle(bundle, passphrase="test-pass")
        assert manifest.manifest_id == original.manifest_id
        assert manifest.path == original.path
        assert manifest.original_size == original.original_size
        assert manifest.block_size == original.block_size
        assert manifest.total_blocks == original.total_blocks
        assert manifest.created_at == original.created_at
        assert len(manifest.blocks) == 2
        assert len(manifest.blocks[0].chunks) == 2
        assert len(manifest.blocks[1].chunks) == 1

    def test_preserves_plaintext_hash(self):
        mk = _master()
        original = _sample_manifest()
        bundle = export_bundle(original, mk)
        manifest, _, _ = import_bundle(bundle, passphrase="test-pass")
        assert manifest.plaintext_hash == original.plaintext_hash

    def test_preserves_chunk_details(self):
        mk = _master()
        original = _sample_manifest()
        bundle = export_bundle(original, mk)
        manifest, _, _ = import_bundle(bundle, passphrase="test-pass")
        c = manifest.blocks[0].chunks[0]
        assert c.adapter_name == "redis"
        assert c.location == "picard:abc123:b0:c0"
        assert c.status == ChunkStatus.STORED
        assert c.size_bytes == 300
        assert c.digest == "bb" * 32

    def test_preserves_tags(self):
        mk = _master()
        original = _sample_manifest()
        bundle = export_bundle(original, mk)
        manifest, _, _ = import_bundle(bundle, passphrase="test-pass")
        assert manifest.tags == {"env": "test"}

    def test_wrong_passphrase_raises(self):
        mk = _master()
        bundle = export_bundle(_sample_manifest(), mk)
        with pytest.raises(InvalidTag):
            import_bundle(bundle, passphrase="wrong-pass")

    def test_invalid_format_raises(self):
        with pytest.raises(ValueError, match="invalid bundle format"):
            import_bundle("NOT-A-BUNDLE:garbage")

    def test_wrong_version_raises(self):
        with pytest.raises(ValueError, match="unsupported bundle version"):
            import_bundle("PICARD-BUNDLE:99:AAAA:BBBB", passphrase="x")


class TestPeekBundleSalt:
    def test_extracts_salt(self):
        salt = b"\xab\xcd" * 8
        mk = load_master_key(passphrase="pw", salt=salt)
        bundle = export_bundle(_sample_manifest(), mk)
        extracted = peek_bundle_salt(bundle)
        assert extracted == salt

    def test_matches_master(self):
        mk = _master()
        bundle = export_bundle(_sample_manifest(), mk)
        assert peek_bundle_salt(bundle) == mk.salt

    def test_invalid_format_raises(self):
        with pytest.raises(ValueError, match="invalid bundle format"):
            peek_bundle_salt("garbage")


class TestBundleAdapterHints:
    def test_adapter_hints(self):
        mk = _master()
        bundle = export_bundle(_sample_manifest(), mk)
        _, hints, _ = import_bundle(bundle, passphrase="test-pass")
        assert hints == {"redis": 2, "s3": 1}


class TestBundleKeyRing:
    def test_without_key_ring(self):
        mk = _master()
        bundle = export_bundle(_sample_manifest(), mk)
        _, _, versions = import_bundle(bundle, passphrase="test-pass")
        assert versions == {}

    def test_with_key_ring_versions(self):
        mk = _master()
        ring = KeyRing(mk)
        ring.rotate("redis")
        ring.rotate("redis")
        ring.rotate("s3")
        bundle = export_bundle(_sample_manifest(), mk, key_ring=ring)
        _, _, versions = import_bundle(bundle, passphrase="test-pass")
        assert versions == {"redis": 3, "s3": 2}


class TestBundleKeyfile:
    def test_round_trip_with_keyfile(self, tmp_path):
        kf = tmp_path / "master.key"
        kf.write_bytes(b"deterministic-key-material-32bytes!")
        mk = load_master_key(keyfile=kf)
        bundle = export_bundle(_sample_manifest(), mk)
        manifest, _, _ = import_bundle(bundle, keyfile=kf)
        assert manifest.manifest_id == "abc123"
        assert manifest.path == "/docs/secret.txt"


class TestCrossMachineScenario:
    def test_different_master_key_instances_same_passphrase(self):
        """Simulate: machine A exports, machine B imports with same passphrase."""
        mk_a = load_master_key(passphrase="shared-secret")
        original = _sample_manifest()
        bundle = export_bundle(original, mk_a)

        # Machine B only has the passphrase — import_bundle extracts
        # the salt from the bundle and re-derives the same master key.
        manifest, hints, _ = import_bundle(bundle, passphrase="shared-secret")
        assert manifest.manifest_id == original.manifest_id
        assert manifest.original_size == original.original_size
        assert len(manifest.blocks) == len(original.blocks)
        assert hints == {"redis": 2, "s3": 1}
