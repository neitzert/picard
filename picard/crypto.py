"""Encryption layer — AES-256-GCM with Argon2id key derivation.

Every block is encrypted before chunking.  The caller never touches
plaintext after this module returns ciphertext, and never touches
ciphertext after this module returns plaintext.
"""

from __future__ import annotations

import hashlib
import os
import struct
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from argon2.low_level import Type, hash_secret_raw
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

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
    """LRU cache for Argon2id-derived keys, keyed by (passphrase_hash, salt)."""

    def __init__(
        self, max_entries: int = 16, ttl_seconds: float = 300.0
    ) -> None:
        self._max = max_entries
        self._ttl = ttl_seconds
        self._store: OrderedDict[tuple[bytes, bytes], tuple[bytes, float]] = (
            OrderedDict()
        )
        self._lock = threading.Lock()

    def get(self, passphrase: str, salt: bytes) -> bytes | None:
        key = (self._hash_pp(passphrase), salt)
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            raw, expires = entry
            if time.monotonic() > expires:
                self._store.pop(key)
                return None
            self._store.move_to_end(key)
            return raw

    def put(self, passphrase: str, salt: bytes, raw: bytes) -> None:
        key = (self._hash_pp(passphrase), salt)
        with self._lock:
            if key in self._store:
                self._store.pop(key)
            while len(self._store) >= self._max:
                self._store.popitem(last=False)
            self._store[key] = (raw, time.monotonic() + self._ttl)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    @staticmethod
    def _hash_pp(passphrase: str) -> bytes:
        return hashlib.sha256(passphrase.encode("utf-8")).digest()


_key_cache: DerivedKeyCache | None = None


def set_key_cache(cache: DerivedKeyCache | None) -> None:
    """Install a module-level DerivedKeyCache (or None to disable)."""
    global _key_cache
    _key_cache = cache


def derive_key(passphrase: str, salt: bytes | None = None) -> DerivedKey:
    """Derive a 256-bit key from *passphrase* via Argon2id.

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
            return DerivedKey(key=cached, salt=salt)

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
        _key_cache.put(passphrase, salt, raw)

    return DerivedKey(key=raw, salt=salt)


# --- Authenticated encryption -----------------------------------------------

_NONCE_LEN = 12  # 96-bit nonce for AES-GCM

# Wire format of an encrypted blob:
#   [salt 16B][nonce 12B][ciphertext…]
# The ciphertext includes the 16-byte GCM tag appended by AESGCM.

_HEADER = struct.Struct(f"!{_SALT_LEN}s{_NONCE_LEN}s")


def encrypt(plaintext: bytes, passphrase: str) -> bytes:
    """Encrypt *plaintext* under *passphrase*.  Returns salt‖nonce‖ciphertext."""
    dk = derive_key(passphrase)
    nonce = os.urandom(_NONCE_LEN)
    ct = AESGCM(dk.key).encrypt(nonce, plaintext, associated_data=None)
    return _HEADER.pack(dk.salt, nonce) + ct


def decrypt(blob: bytes, passphrase: str) -> bytes:
    """Decrypt a blob produced by :func:`encrypt`."""
    if len(blob) < _HEADER.size:
        raise ValueError("blob too short to contain header")
    salt, nonce = _HEADER.unpack_from(blob)
    ct = blob[_HEADER.size :]
    dk = derive_key(passphrase, salt=salt)
    return AESGCM(dk.key).decrypt(nonce, ct, associated_data=None)
