"""Encryption layer — AES-256-GCM with Argon2id key derivation.

Every block is encrypted before chunking.  The caller never touches
plaintext after this module returns ciphertext, and never touches
ciphertext after this module returns plaintext.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac_mod
import os
import struct
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from argon2.low_level import Type, hash_secret_raw
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def _zeroize(b: bytearray) -> None:
    """Overwrite a mutable buffer with zeros."""
    for i in range(len(b)):
        b[i] = 0

# --- Key derivation --------------------------------------------------------

# Argon2id parameters — tuned for interactive use, not archival.
_KDF_TIME_COST = 3
_KDF_MEMORY_COST = 65536  # 64 MiB
_KDF_PARALLELISM = 4
_KDF_HASH_LEN = 32  # 256-bit key
_SALT_LEN = 16


@dataclass(frozen=True, slots=True)
class DerivedKey:
    """A derived 256-bit key plus the salt used to produce it."""

    key: bytes  # 32 bytes
    salt: bytes  # 16 bytes


class DerivedKeyCache:
    """LRU cache for Argon2id-derived keys, keyed by (passphrase_hmac, salt).

    Security hardening:
    - Keys stored as mutable bytearrays and zeroized on eviction/clear.
    - Cache lookup key uses HMAC-SHA256 with a per-boot random secret,
      so passphrase hashes can't be brute-forced from a core dump.
    - Caches AESGCM cipher objects alongside derived keys to avoid
      re-instantiation on every encrypt/decrypt call.
    """

    def __init__(
        self, max_entries: int = 16, ttl_seconds: float = 300.0
    ) -> None:
        self._max = max_entries
        self._ttl = ttl_seconds
        self._store: OrderedDict[
            tuple[bytes, bytes], tuple[bytearray, AESGCM, float]
        ] = OrderedDict()
        self._lock = threading.Lock()
        self._hmac_key = os.urandom(32)

    def get(
        self, passphrase: str, salt: bytes
    ) -> tuple[bytes, AESGCM] | None:
        key = (self._hash_pp(passphrase), salt)
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            raw, cipher, expires = entry
            if time.monotonic() > expires:
                _zeroize(raw)
                self._store.pop(key)
                return None
            self._store.move_to_end(key)
            return bytes(raw), cipher

    def put(self, passphrase: str, salt: bytes, raw: bytes) -> AESGCM:
        key = (self._hash_pp(passphrase), salt)
        cipher = AESGCM(raw)
        stored = bytearray(raw)
        with self._lock:
            old = self._store.pop(key, None)
            if old is not None:
                _zeroize(old[0])
            while len(self._store) >= self._max:
                _, evicted = self._store.popitem(last=False)
                _zeroize(evicted[0])
            self._store[key] = (stored, cipher, time.monotonic() + self._ttl)
        return cipher

    def clear(self) -> None:
        with self._lock:
            for raw, _cipher, _exp in self._store.values():
                _zeroize(raw)
            self._store.clear()

    def close(self) -> None:
        """Zeroize all cached keys and the HMAC secret."""
        self.clear()
        secret = bytearray(self._hmac_key)
        _zeroize(secret)
        self._hmac_key = bytes(secret)

    def _hash_pp(self, passphrase: str) -> bytes:
        return _hmac_mod.new(
            self._hmac_key, passphrase.encode("utf-8"), hashlib.sha256
        ).digest()


_key_cache: DerivedKeyCache | None = None


def set_key_cache(cache: DerivedKeyCache | None) -> None:
    """Install a module-level DerivedKeyCache (or None to disable)."""
    global _key_cache
    _key_cache = cache


def derive_key(
    passphrase: str, salt: bytes | None = None
) -> tuple[DerivedKey, AESGCM]:
    """Derive a 256-bit key from *passphrase* via Argon2id.

    Returns (DerivedKey, AESGCM) — the cipher object is cached alongside
    the key material so callers don't recreate it per call.

    If *salt* is ``None`` a fresh random salt is generated.  To re-derive
    the same key later (for decryption) pass the original salt back in.
    """
    if salt is None:
        salt = os.urandom(_SALT_LEN)
    if len(salt) != _SALT_LEN:
        raise ValueError(f"salt must be {_SALT_LEN} bytes, got {len(salt)}")

    if _key_cache is not None:
        cached = _key_cache.get(passphrase, salt)
        if cached is not None:
            raw, cipher = cached
            return DerivedKey(key=raw, salt=salt), cipher

    raw = hash_secret_raw(
        secret=passphrase.encode("utf-8"),
        salt=salt,
        time_cost=_KDF_TIME_COST,
        memory_cost=_KDF_MEMORY_COST,
        parallelism=_KDF_PARALLELISM,
        hash_len=_KDF_HASH_LEN,
        type=Type.ID,
    )

    if _key_cache is not None:
        cipher = _key_cache.put(passphrase, salt, raw)
    else:
        cipher = AESGCM(raw)

    return DerivedKey(key=raw, salt=salt), cipher


# --- Authenticated encryption -----------------------------------------------

_NONCE_LEN = 12  # 96-bit nonce for AES-GCM

# Format v2 wire layout:
#   [version 1B][alg 1B][salt 16B][nonce 12B][ciphertext+GCM_tag]
_FORMAT_V2 = 2
_ALG_AES256GCM = 0x01
_KNOWN_ALGS = frozenset({_ALG_AES256GCM})

_HEADER_V2 = struct.Struct(f"!BB{_SALT_LEN}s{_NONCE_LEN}s")  # 30 bytes

# Legacy format (pre-v2): [salt 16B][nonce 12B][ciphertext+GCM_tag]
_HEADER_LEGACY = struct.Struct(f"!{_SALT_LEN}s{_NONCE_LEN}s")  # 28 bytes


def encrypt(plaintext: bytes, passphrase: str) -> bytes:
    """Encrypt *plaintext* under *passphrase*.  Returns v2 format blob."""
    dk, cipher = derive_key(passphrase)
    nonce = os.urandom(_NONCE_LEN)
    ct = cipher.encrypt(nonce, plaintext, associated_data=None)
    return _HEADER_V2.pack(_FORMAT_V2, _ALG_AES256GCM, dk.salt, nonce) + ct


def _decrypt_v2(blob: bytes, passphrase: str) -> bytes:
    if len(blob) < _HEADER_V2.size:
        raise ValueError("blob too short to contain v2 header")
    _ver, alg, salt, nonce = _HEADER_V2.unpack_from(blob)
    if alg not in _KNOWN_ALGS:
        raise ValueError(f"unsupported algorithm: 0x{alg:02x}")
    ct = blob[_HEADER_V2.size:]
    dk, cipher = derive_key(passphrase, salt=salt)
    return cipher.decrypt(nonce, ct, associated_data=None)


def _decrypt_legacy(blob: bytes, passphrase: str) -> bytes:
    if len(blob) < _HEADER_LEGACY.size:
        raise ValueError("blob too short to contain header")
    salt, nonce = _HEADER_LEGACY.unpack_from(blob)
    ct = blob[_HEADER_LEGACY.size:]
    dk, cipher = derive_key(passphrase, salt=salt)
    return cipher.decrypt(nonce, ct, associated_data=None)


def decrypt(blob: bytes, passphrase: str) -> bytes:
    """Decrypt a blob produced by :func:`encrypt`.

    Handles both v2 (version+alg header) and legacy (salt+nonce) formats.
    """
    if len(blob) < _HEADER_LEGACY.size:
        raise ValueError("blob too short to contain header")
    if len(blob) >= _HEADER_V2.size and blob[0] == _FORMAT_V2:
        if blob[1] not in _KNOWN_ALGS:
            raise ValueError(f"unsupported algorithm: 0x{blob[1]:02x}")
        try:
            return _decrypt_v2(blob, passphrase)
        except Exception:
            # 1/65536 chance a legacy blob's salt starts with 0x02 0x01;
            # GCM auth fails, fall back to legacy parse.
            return _decrypt_legacy(blob, passphrase)
    return _decrypt_legacy(blob, passphrase)
