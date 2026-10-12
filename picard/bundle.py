"""Bundle — encrypted portable manifest for cross-machine file access.

A bundle contains everything a remote PICARD instance needs to
retrieve and decrypt a scattered file:
  - Full manifest (blocks, chunks, locations)
  - Master key salt (for HKDF sub-key re-derivation)
  - Adapter hints (which adapters hold which chunks)
  - KeyRing versions

The bundle itself is encrypted with the master key, so it can
safely travel over insecure channels.  The recipient needs the
same passphrase or keyfile to open it.

Wire format (text-safe):

    PICARD-BUNDLE:1:<base64_key_salt>:<base64(nonce ‖ ciphertext)>

The key salt sits outside the encrypted blob so the recipient can
re-derive the master key before decrypting the rest.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from picard.keys import KeyRing, MasterKey

BUNDLE_VERSION = 1
_NONCE_LEN = 12
_AAD = b"picard-bundle-v1"


def export_bundle(
    manifest,
    master: MasterKey,
    key_ring: KeyRing | None = None,
) -> str:
    payload = {
        "bundle_version": BUNDLE_VERSION,
        "manifest": _manifest_to_dict(manifest),
        "key_source": master.source,
        "adapter_hints": _extract_adapter_hints(manifest),
    }
    if key_ring:
        payload["key_versions"] = dict(key_ring.versions)

    plaintext = json.dumps(payload, separators=(",", ":")).encode()
    nonce = os.urandom(_NONCE_LEN)
    ct = AESGCM(master.key).encrypt(nonce, plaintext, associated_data=_AAD)

    salt_b64 = base64.b64encode(master.salt).decode()
    data_b64 = base64.b64encode(nonce + ct).decode()
    return f"PICARD-BUNDLE:1:{salt_b64}:{data_b64}"


def import_bundle(
    bundle_str: str,
    passphrase: str | None = None,
    keyfile: Path | None = None,
) -> tuple:
    """Decrypt a bundle and return (Manifest, adapter_hints, key_versions)."""
    parts = bundle_str.strip().split(":")
    if len(parts) != 4 or parts[0] != "PICARD-BUNDLE":
        raise ValueError("invalid bundle format")
    version = int(parts[1])
    if version != BUNDLE_VERSION:
        raise ValueError(f"unsupported bundle version: {version}")

    salt = base64.b64decode(parts[2])
    raw = base64.b64decode(parts[3])

    from picard.keys import load_master_key

    master = load_master_key(passphrase=passphrase, keyfile=keyfile, salt=salt)

    nonce = raw[:_NONCE_LEN]
    ct = raw[_NONCE_LEN:]
    plaintext = AESGCM(master.key).decrypt(nonce, ct, associated_data=_AAD)

    payload = json.loads(plaintext)
    manifest = _dict_to_manifest(payload["manifest"])
    adapter_hints = payload.get("adapter_hints", {})
    key_versions = payload.get("key_versions", {})

    return manifest, adapter_hints, key_versions


def peek_bundle_salt(bundle_str: str) -> bytes:
    """Extract the key salt without decrypting."""
    parts = bundle_str.strip().split(":")
    if len(parts) != 4 or parts[0] != "PICARD-BUNDLE":
        raise ValueError("invalid bundle format")
    return base64.b64decode(parts[2])


# --- serialization helpers --------------------------------------------------


def _manifest_to_dict(m) -> dict:
    return {
        "manifest_id": m.manifest_id,
        "path": m.path,
        "original_size": m.original_size,
        "block_size": m.block_size,
        "total_blocks": m.total_blocks,
        "created_at": m.created_at,
        "plaintext_hash": getattr(m, "plaintext_hash", ""),
        "tags": m.tags,
        "blocks": [
            {
                "block_index": b.block_index,
                "digest": b.digest,
                "size_bytes": b.size_bytes,
                "chunks": [
                    {
                        "chunk_index": c.chunk_index,
                        "adapter_name": c.adapter_name,
                        "location": c.location,
                        "status": c.status.value
                        if hasattr(c.status, "value")
                        else c.status,
                        "size_bytes": c.size_bytes,
                        "digest": getattr(c, "digest", ""),
                    }
                    for c in b.chunks
                ],
            }
            for b in m.blocks
        ],
    }


def _dict_to_manifest(d: dict):
    from picard.manifest import BlockRecord, ChunkRecord, ChunkStatus, Manifest

    blocks = []
    for bd in d["blocks"]:
        chunks = [
            ChunkRecord(
                chunk_index=cd["chunk_index"],
                adapter_name=cd["adapter_name"],
                location=cd["location"],
                status=ChunkStatus(cd["status"]),
                size_bytes=cd.get("size_bytes", 0),
                digest=cd.get("digest", ""),
            )
            for cd in bd["chunks"]
        ]
        blocks.append(
            BlockRecord(
                block_index=bd["block_index"],
                digest=bd["digest"],
                size_bytes=bd.get("size_bytes", 0),
                chunks=chunks,
            )
        )
    m = Manifest(
        manifest_id=d["manifest_id"],
        path=d["path"],
        original_size=d["original_size"],
        block_size=d["block_size"],
        total_blocks=d["total_blocks"],
        created_at=d["created_at"],
        tags=d.get("tags", {}),
        plaintext_hash=d.get("plaintext_hash", ""),
    )
    m.blocks = blocks
    return m


def _extract_adapter_hints(manifest) -> dict[str, int]:
    counts: dict[str, int] = {}
    for b in manifest.blocks:
        for c in b.chunks:
            counts[c.adapter_name] = counts.get(c.adapter_name, 0) + 1
    return counts
