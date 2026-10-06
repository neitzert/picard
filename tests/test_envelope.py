"""Tests for the sealed envelope module."""

from __future__ import annotations

import pytest
from cryptography.exceptions import InvalidTag

from picard.chunker import Chunk
from picard.envelope import ENVELOPE_OVERHEAD, extract_correlation_tag, seal, unseal
from picard.keys import derive_correlation_tag, derive_sub_key, load_master_key


def _master():
    return load_master_key(passphrase="test-pass", salt=b"\x00" * 16)


def _sub_key(adapter: str = "test"):
    return derive_sub_key(_master(), adapter)


def _sample_encoded(manifest_id: str = "m1") -> str:
    return Chunk(
        version=1,
        manifest_id=manifest_id,
        block_index=0,
        chunk_index=0,
        total_chunks=1,
        payload=b"test-payload",
    ).encode()


class TestSeal:
    def test_version_byte(self):
        env = seal(_sample_encoded(), _sub_key(), "m1")
        assert env[0] == 1

    def test_output_length(self):
        encoded = _sample_encoded()
        env = seal(encoded, _sub_key(), "m1")
        assert len(env) == ENVELOPE_OVERHEAD + len(encoded.encode())

    def test_no_pchk_visible(self):
        env = seal(_sample_encoded(), _sub_key(), "m1")
        assert b"PCHK" not in env

    def test_different_keys_different_output(self):
        encoded = _sample_encoded()
        env1 = seal(encoded, _sub_key("redis"), "m1")
        env2 = seal(encoded, _sub_key("s3"), "m1")
        assert env1[21:] != env2[21:]


class TestUnseal:
    def test_round_trip(self):
        sk = _sub_key()
        encoded = _sample_encoded()
        env = seal(encoded, sk, "m1")
        recovered = unseal(env, sk)
        assert recovered == encoded

    def test_wrong_key_raises(self):
        env = seal(_sample_encoded(), _sub_key("redis"), "m1")
        with pytest.raises(InvalidTag):
            unseal(env, _sub_key("s3"))

    def test_tampered_correlation_tag_raises(self):
        env = bytearray(seal(_sample_encoded(), _sub_key(), "m1"))
        env[1] ^= 0xFF
        with pytest.raises(InvalidTag):
            unseal(bytes(env), _sub_key())

    def test_tampered_ciphertext_raises(self):
        env = bytearray(seal(_sample_encoded(), _sub_key(), "m1"))
        env[-1] ^= 0xFF
        with pytest.raises(InvalidTag):
            unseal(bytes(env), _sub_key())

    def test_wrong_version_raises(self):
        env = bytearray(seal(_sample_encoded(), _sub_key(), "m1"))
        env[0] = 99
        with pytest.raises(ValueError, match="unsupported envelope version"):
            unseal(bytes(env), _sub_key())

    def test_too_short_raises(self):
        with pytest.raises(ValueError, match="too short"):
            unseal(b"\x01" * 10, _sub_key())


class TestExtractCorrelationTag:
    def test_matches_embedded(self):
        sk = _sub_key()
        env = seal(_sample_encoded(), sk, "m1")
        tag = extract_correlation_tag(env)
        expected = derive_correlation_tag(sk, "m1")
        assert tag == expected

    def test_too_short_raises(self):
        with pytest.raises(ValueError, match="too short"):
            extract_correlation_tag(b"\x01\x02")
