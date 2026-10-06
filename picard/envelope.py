"""Sealed envelope — encrypted chunk wire format.

Replaces the plaintext PCHK header with an opaque binary blob
indistinguishable from random data. Only the correlation tag
is visible for grouping.

Wire format:
    [version 1B][correlation_tag 8B][nonce 12B][ciphertext+GCM_tag...]

The ciphertext contains the original PCHK-encoded chunk, encrypted
with the adapter's sub-key. Total overhead: 1 + 8 + 12 + 16(GCM tag) = 37 bytes.
"""

from __future__ import annotations

import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from picard.keys import SubKey, derive_correlation_tag

_VERSION = 1
_NONCE_LEN = 12
ENVELOPE_OVERHEAD = 1 + 8 + _NONCE_LEN + 16  # 37 bytes


def seal(encoded_chunk: str, sub_key: SubKey, manifest_id: str) -> bytes:
    correlation_tag = derive_correlation_tag(sub_key, manifest_id)
    nonce = os.urandom(_NONCE_LEN)
    ciphertext = AESGCM(sub_key.key).encrypt(
        nonce, encoded_chunk.encode(), associated_data=correlation_tag
    )
    return bytes([_VERSION]) + correlation_tag + nonce + ciphertext


def unseal(envelope: bytes, sub_key: SubKey) -> str:
    if len(envelope) < ENVELOPE_OVERHEAD:
        raise ValueError("envelope too short")
    version = envelope[0]
    if version != _VERSION:
        raise ValueError(f"unsupported envelope version: {version}")
    correlation_tag = envelope[1:9]
    nonce = envelope[9:21]
    ciphertext = envelope[21:]
    plaintext = AESGCM(sub_key.key).decrypt(
        nonce, ciphertext, associated_data=correlation_tag
    )
    return plaintext.decode()


def extract_correlation_tag(envelope: bytes) -> bytes:
    if len(envelope) < 9:
        raise ValueError("envelope too short for correlation tag")
    return envelope[1:9]
