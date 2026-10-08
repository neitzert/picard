"""Tests for the crypto module."""

import pytest

from picard.crypto import decrypt, derive_key, encrypt


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
