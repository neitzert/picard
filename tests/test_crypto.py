"""Tests for the crypto module."""

import os
import struct

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from picard.crypto import (
    _ALG_AES256GCM,
    _FORMAT_V2,
    _HEADER_V2,
    _NONCE_LEN,
    _SALT_LEN,
    decrypt,
    derive_key,
    encrypt,
)


class TestDeriveKey:
    def test_deterministic_with_same_salt(self):
        dk1, _ = derive_key("test-passphrase")
        dk2, _ = derive_key("test-passphrase", salt=dk1.salt)
        assert dk1.key == dk2.key

    def test_different_salt_different_key(self):
        dk1, _ = derive_key("test-passphrase")
        dk2, _ = derive_key("test-passphrase")  # new random salt
        assert dk1.key != dk2.key

    def test_key_length(self):
        dk, cipher = derive_key("test-passphrase")
        assert len(dk.key) == 32
        assert len(dk.salt) == 16
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        assert isinstance(cipher, AESGCM)


class TestEncryptDecrypt:
    def test_round_trip(self):
        plaintext = b"hello, picard"
        passphrase = "test-pass"
        ct = encrypt(plaintext, passphrase)
        assert ct != plaintext
        assert decrypt(ct, passphrase) == plaintext

    def test_empty_plaintext(self):
        ct = encrypt(b"", "pw")
        assert decrypt(ct, "pw") == b""

    def test_large_block(self):
        data = b"x" * 100_000
        ct = encrypt(data, "pw")
        assert decrypt(ct, "pw") == data

    def test_wrong_passphrase_raises(self):
        ct = encrypt(b"secret", "right")
        with pytest.raises(Exception):
            decrypt(ct, "wrong")

    def test_truncated_blob_raises(self):
        with pytest.raises(ValueError, match="too short"):
            decrypt(b"short", "pw")

    def test_v2_header_present(self):
        ct = encrypt(b"hello", "pw")
        assert ct[0] == _FORMAT_V2
        assert ct[1] == _ALG_AES256GCM

    def test_v2_blob_size(self):
        data = b"x" * 1000
        ct = encrypt(data, "pw")
        # 2 (version+alg) + 16 (salt) + 12 (nonce) + 1000 (data) + 16 (GCM tag)
        assert len(ct) == 2 + 16 + 12 + len(data) + 16

    def test_legacy_blob_still_decrypts(self):
        plaintext = b"legacy data from before v2"
        pw = "old-pass"
        dk, _ = derive_key(pw)
        nonce = os.urandom(_NONCE_LEN)
        ct = AESGCM(dk.key).encrypt(nonce, plaintext, associated_data=None)
        legacy_blob = struct.pack(f"!{_SALT_LEN}s{_NONCE_LEN}s", dk.salt, nonce) + ct
        assert legacy_blob[0] != _FORMAT_V2 or legacy_blob[1] != _ALG_AES256GCM or \
            decrypt(legacy_blob, pw) == plaintext
        result = decrypt(legacy_blob, pw)
        assert result == plaintext

    def test_unknown_alg_raises(self):
        bad_blob = bytes([_FORMAT_V2, 0xFF]) + b"\x00" * 44
        with pytest.raises(ValueError, match="unsupported algorithm: 0xff"):
            decrypt(bad_blob, "pw")

    def test_truncated_v2_header_raises(self):
        short_blob = bytes([_FORMAT_V2, _ALG_AES256GCM]) + b"\x00" * 10
        with pytest.raises((ValueError, Exception)):
            decrypt(short_blob, "pw")
