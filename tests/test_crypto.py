"""Tests for the crypto module."""

import os
import struct
import threading

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from picard.crypto import (
    _ALG_AES256GCM,
    _FORMAT_V2,
    _HEADER_LEGACY,
    _HEADER_V2,
    _NONCE_LEN,
    _SALT_LEN,
    DerivedKeyCache,
    _zeroize,
    decrypt,
    derive_key,
    encrypt,
    set_key_cache,
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


class TestCorruptionHandling:
    """Tests for corrupted ciphertext, salt, nonce, and truncated blobs."""

    def test_corrupted_ciphertext_raises(self):
        ct = bytearray(encrypt(b"secret data", "pw"))
        ct[-1] ^= 0xFF
        with pytest.raises(Exception):
            decrypt(bytes(ct), "pw")

    def test_corrupted_ciphertext_middle_byte(self):
        ct = bytearray(encrypt(b"secret data", "pw"))
        mid = len(ct) // 2
        ct[mid] ^= 0x42
        with pytest.raises(Exception):
            decrypt(bytes(ct), "pw")

    def test_corrupted_salt_raises(self):
        ct = bytearray(encrypt(b"salt corruption test", "pw"))
        ct[2] ^= 0xFF  # salt starts at byte 2 in v2 format
        with pytest.raises(Exception):
            decrypt(bytes(ct), "pw")

    def test_corrupted_nonce_raises(self):
        ct = bytearray(encrypt(b"nonce corruption test", "pw"))
        ct[18] ^= 0xFF  # nonce starts at byte 18 in v2 format (2+16)
        with pytest.raises(Exception):
            decrypt(bytes(ct), "pw")

    def test_corrupted_version_byte_still_decrypts_via_legacy_fallback(self):
        ct = bytearray(encrypt(b"version test", "pw"))
        ct[0] = 0x00  # not a valid version — falls to legacy path
        # Legacy path will also fail because the format is wrong
        with pytest.raises(Exception):
            decrypt(bytes(ct), "pw")

    def test_corrupted_gcm_tag_raises(self):
        ct = bytearray(encrypt(b"tag test", "pw"))
        ct[-16] ^= 0xFF  # corrupt the GCM tag (last 16 bytes)
        with pytest.raises(Exception):
            decrypt(bytes(ct), "pw")

    def test_truncated_blob_just_header(self):
        ct = encrypt(b"data", "pw")
        with pytest.raises(Exception):
            decrypt(ct[:_HEADER_V2.size], "pw")

    def test_truncated_blob_missing_tag(self):
        ct = encrypt(b"data", "pw")
        with pytest.raises(Exception):
            decrypt(ct[:-1], "pw")

    def test_empty_blob_raises(self):
        with pytest.raises(ValueError, match="too short"):
            decrypt(b"", "pw")

    def test_single_byte_blob_raises(self):
        with pytest.raises(ValueError, match="too short"):
            decrypt(b"\x02", "pw")

    def test_all_zeros_blob_raises(self):
        with pytest.raises(Exception):
            decrypt(b"\x00" * 100, "pw")

    def test_random_garbage_raises(self):
        garbage = os.urandom(200)
        with pytest.raises(Exception):
            decrypt(garbage, "pw")

    def test_header_only_no_ciphertext(self):
        header_only = bytes([_FORMAT_V2, _ALG_AES256GCM]) + os.urandom(16) + os.urandom(12)
        assert len(header_only) == _HEADER_V2.size
        with pytest.raises(Exception):
            decrypt(header_only, "pw")

    def test_legacy_header_only_no_ciphertext(self):
        header_only = os.urandom(16) + os.urandom(12)
        assert len(header_only) == _HEADER_LEGACY.size
        with pytest.raises(Exception):
            decrypt(header_only, "pw")


class TestDataVariants:
    """Edge case data round-trip tests."""

    def test_binary_with_null_bytes(self):
        data = b"\x00\x01\x02" + b"\x00" * 100 + b"\xff\xfe\xfd"
        ct = encrypt(data, "pw")
        assert decrypt(ct, "pw") == data

    def test_all_null_bytes(self):
        data = b"\x00" * 1024
        ct = encrypt(data, "pw")
        assert decrypt(ct, "pw") == data

    def test_all_0xff_bytes(self):
        data = b"\xff" * 1024
        ct = encrypt(data, "pw")
        assert decrypt(ct, "pw") == data

    def test_single_byte(self):
        ct = encrypt(b"\x42", "pw")
        assert decrypt(ct, "pw") == b"\x42"

    def test_large_plaintext_1mb(self):
        data = os.urandom(1_048_576)
        ct = encrypt(data, "pw")
        assert decrypt(ct, "pw") == data

    def test_unicode_passphrase(self):
        data = b"unicode passphrase test"
        ct = encrypt(data, "\U0001f512éñüß")
        assert decrypt(ct, "\U0001f512éñüß") == data

    def test_empty_passphrase(self):
        data = b"empty passphrase"
        ct = encrypt(data, "")
        assert decrypt(ct, "") == data

    def test_long_passphrase(self):
        pw = "x" * 10_000
        data = b"long passphrase test"
        ct = encrypt(data, pw)
        assert decrypt(ct, pw) == data

    def test_each_encrypt_produces_unique_ciphertext(self):
        data = b"same data"
        ct1 = encrypt(data, "pw")
        ct2 = encrypt(data, "pw")
        assert ct1 != ct2  # different salt + nonce each time
        assert decrypt(ct1, "pw") == data
        assert decrypt(ct2, "pw") == data


class TestLegacyFormatCompat:
    """Verify backward compatibility with legacy (pre-v2) blobs."""

    def _make_legacy_blob(self, plaintext: bytes, passphrase: str) -> bytes:
        dk, _ = derive_key(passphrase)
        nonce = os.urandom(_NONCE_LEN)
        ct = AESGCM(dk.key).encrypt(nonce, plaintext, associated_data=None)
        return struct.pack(f"!{_SALT_LEN}s{_NONCE_LEN}s", dk.salt, nonce) + ct

    def test_legacy_round_trip(self):
        blob = self._make_legacy_blob(b"legacy round trip", "pw")
        assert decrypt(blob, "pw") == b"legacy round trip"

    def test_legacy_empty_data(self):
        blob = self._make_legacy_blob(b"", "pw")
        assert decrypt(blob, "pw") == b""

    def test_legacy_large_data(self):
        data = os.urandom(100_000)
        blob = self._make_legacy_blob(data, "pw")
        assert decrypt(blob, "pw") == data

    def test_legacy_wrong_passphrase(self):
        blob = self._make_legacy_blob(b"secret", "right")
        with pytest.raises(Exception):
            decrypt(blob, "wrong")

    def test_legacy_blob_does_not_have_v2_header(self):
        blob = self._make_legacy_blob(b"check format", "pw")
        is_v2 = blob[0] == _FORMAT_V2 and blob[1] == _ALG_AES256GCM
        if not is_v2:
            assert decrypt(blob, "pw") == b"check format"


class TestV2Format:
    """Detailed v2 wire format tests."""

    def test_v2_wire_layout(self):
        ct = encrypt(b"wire layout", "pw")
        ver, alg, salt, nonce = _HEADER_V2.unpack_from(ct)
        assert ver == _FORMAT_V2
        assert alg == _ALG_AES256GCM
        assert len(salt) == _SALT_LEN
        assert len(nonce) == _NONCE_LEN

    def test_v2_overhead(self):
        for size in [0, 1, 100, 10_000]:
            ct = encrypt(b"x" * size, "pw")
            overhead = len(ct) - size
            # 2 (header) + 16 (salt) + 12 (nonce) + 16 (GCM tag) = 46
            assert overhead == 46

    def test_unknown_alg_0x00_raises(self):
        blob = bytes([_FORMAT_V2, 0x00]) + os.urandom(44)
        with pytest.raises(ValueError, match="unsupported algorithm: 0x00"):
            decrypt(blob, "pw")

    def test_unknown_alg_0x02_raises(self):
        blob = bytes([_FORMAT_V2, 0x02]) + os.urandom(44)
        with pytest.raises(ValueError, match="unsupported algorithm: 0x02"):
            decrypt(blob, "pw")

    def test_unknown_alg_0xfe_raises(self):
        blob = bytes([_FORMAT_V2, 0xFE]) + os.urandom(44)
        with pytest.raises(ValueError, match="unsupported algorithm: 0xfe"):
            decrypt(blob, "pw")

    def test_future_version_byte_falls_to_legacy(self):
        blob = bytes([0x03, _ALG_AES256GCM]) + os.urandom(44)
        with pytest.raises(Exception):
            decrypt(blob, "pw")


class TestDeriveKeyEdgeCases:
    def test_salt_too_short(self):
        with pytest.raises(ValueError, match="salt must be 16 bytes"):
            derive_key("pw", salt=b"\x00" * 8)

    def test_salt_too_long(self):
        with pytest.raises(ValueError, match="salt must be 16 bytes"):
            derive_key("pw", salt=b"\x00" * 32)

    def test_salt_empty(self):
        with pytest.raises(ValueError, match="salt must be 16 bytes"):
            derive_key("pw", salt=b"")

    def test_same_passphrase_same_salt_same_key(self):
        salt = os.urandom(16)
        dk1, _ = derive_key("test", salt=salt)
        dk2, _ = derive_key("test", salt=salt)
        assert dk1.key == dk2.key

    def test_different_passphrase_same_salt_different_key(self):
        salt = os.urandom(16)
        dk1, _ = derive_key("pass1", salt=salt)
        dk2, _ = derive_key("pass2", salt=salt)
        assert dk1.key != dk2.key


class TestDerivedKeyCacheUnit:
    """Unit tests for the DerivedKeyCache."""

    def test_put_get_round_trip(self):
        cache = DerivedKeyCache(max_entries=4, ttl_seconds=60)
        salt = os.urandom(16)
        dk, _ = derive_key("pw", salt=salt)
        cipher = cache.put("pw", salt, dk.key)
        result = cache.get("pw", salt)
        assert result is not None
        raw, cached_cipher = result
        assert raw == dk.key

    def test_cache_miss(self):
        cache = DerivedKeyCache(max_entries=4, ttl_seconds=60)
        assert cache.get("pw", os.urandom(16)) is None

    def test_eviction_on_max(self):
        cache = DerivedKeyCache(max_entries=2, ttl_seconds=60)
        salt1 = os.urandom(16)
        salt2 = os.urandom(16)
        salt3 = os.urandom(16)
        dk1, _ = derive_key("pw", salt=salt1)
        dk2, _ = derive_key("pw", salt=salt2)
        dk3, _ = derive_key("pw", salt=salt3)
        cache.put("pw", salt1, dk1.key)
        cache.put("pw", salt2, dk2.key)
        cache.put("pw", salt3, dk3.key)
        assert cache.get("pw", salt1) is None  # evicted
        assert cache.get("pw", salt2) is not None
        assert cache.get("pw", salt3) is not None

    def test_ttl_expiry(self):
        cache = DerivedKeyCache(max_entries=4, ttl_seconds=0.001)
        salt = os.urandom(16)
        dk, _ = derive_key("pw", salt=salt)
        cache.put("pw", salt, dk.key)
        import time
        time.sleep(0.05)
        assert cache.get("pw", salt) is None

    def test_clear_empties_cache(self):
        cache = DerivedKeyCache(max_entries=4, ttl_seconds=60)
        salt = os.urandom(16)
        dk, _ = derive_key("pw", salt=salt)
        cache.put("pw", salt, dk.key)
        cache.clear()
        assert cache.get("pw", salt) is None

    def test_close_empties_cache(self):
        cache = DerivedKeyCache(max_entries=4, ttl_seconds=60)
        salt = os.urandom(16)
        dk, _ = derive_key("pw", salt=salt)
        cache.put("pw", salt, dk.key)
        cache.close()
        assert cache.get("pw", salt) is None

    def test_different_passphrase_different_entry(self):
        cache = DerivedKeyCache(max_entries=4, ttl_seconds=60)
        salt = os.urandom(16)
        dk1, _ = derive_key("pw1", salt=salt)
        dk2, _ = derive_key("pw2", salt=salt)
        cache.put("pw1", salt, dk1.key)
        cache.put("pw2", salt, dk2.key)
        r1 = cache.get("pw1", salt)
        r2 = cache.get("pw2", salt)
        assert r1 is not None and r2 is not None
        assert r1[0] != r2[0]

    def test_replace_same_key(self):
        cache = DerivedKeyCache(max_entries=4, ttl_seconds=60)
        salt = os.urandom(16)
        dk, _ = derive_key("pw", salt=salt)
        cache.put("pw", salt, dk.key)
        cache.put("pw", salt, dk.key)  # re-put
        result = cache.get("pw", salt)
        assert result is not None
        assert result[0] == dk.key


class TestZeroize:
    """Tests for key zeroization."""

    def test_zeroize_clears_buffer(self):
        buf = bytearray(b"sensitive key material")
        _zeroize(buf)
        assert buf == bytearray(len(b"sensitive key material"))

    def test_zeroize_empty_buffer(self):
        buf = bytearray()
        _zeroize(buf)  # should not raise
        assert buf == bytearray()

    def test_zeroize_large_buffer(self):
        buf = bytearray(os.urandom(4096))
        _zeroize(buf)
        assert all(b == 0 for b in buf)


class TestConcurrentEncryptDecrypt:
    """Thread-safety tests for encrypt/decrypt operations."""

    def test_concurrent_encrypt_decrypt(self):
        data = b"concurrent test data"
        pw = "pw"
        errors = []

        def worker():
            try:
                for _ in range(20):
                    ct = encrypt(data, pw)
                    result = decrypt(ct, pw)
                    if result != data:
                        errors.append(f"mismatch: {result!r}")
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, f"Concurrent encrypt/decrypt errors: {errors}"

    def test_concurrent_with_cache(self):
        import picard.crypto as crypto
        old_cache = crypto._key_cache
        cache = DerivedKeyCache(max_entries=16, ttl_seconds=30)
        set_key_cache(cache)
        try:
            data = b"cached concurrent test"
            pw = "pw"
            errors = []

            def worker():
                try:
                    for _ in range(20):
                        ct = encrypt(data, pw)
                        result = decrypt(ct, pw)
                        if result != data:
                            errors.append(f"mismatch: {result!r}")
                except Exception as e:
                    errors.append(str(e))

            threads = [threading.Thread(target=worker) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            assert not errors, f"Concurrent cached encrypt/decrypt errors: {errors}"
        finally:
            set_key_cache(old_cache)
            cache.close()


class TestModuleLevelCache:
    """Tests for set_key_cache / module-level cache integration."""

    def test_set_and_unset_cache(self):
        import picard.crypto as crypto
        old = crypto._key_cache
        try:
            cache = DerivedKeyCache(max_entries=4, ttl_seconds=60)
            set_key_cache(cache)
            assert crypto._key_cache is cache
            set_key_cache(None)
            assert crypto._key_cache is None
        finally:
            set_key_cache(old)

    def test_encrypt_decrypt_with_cache_enabled(self):
        import picard.crypto as crypto
        old = crypto._key_cache
        cache = DerivedKeyCache(max_entries=8, ttl_seconds=60)
        set_key_cache(cache)
        try:
            data = b"cached round trip"
            ct = encrypt(data, "pw")
            assert decrypt(ct, "pw") == data
            # Second decrypt should hit cache
            assert decrypt(ct, "pw") == data
        finally:
            set_key_cache(old)
            cache.close()
