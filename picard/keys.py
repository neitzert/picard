"""Key hierarchy — master key loading and per-adapter sub-key derivation.

One master key (from passphrase or keyfile) derives deterministic
sub-keys for each adapter. Sub-keys are rotatable by version number.
Compromising one adapter's sub-key reveals nothing about others.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from picard.crypto import derive_key

_SALT_LEN = 16


@dataclass(frozen=True, slots=True)
class MasterKey:
    key: bytes
    salt: bytes
    source: str


@dataclass(frozen=True, slots=True)
class SubKey:
    key: bytes
    adapter_name: str
    version: int


def load_master_key(
    passphrase: str | None = None,
    keyfile: Path | None = None,
    salt: bytes | None = None,
) -> MasterKey:
    if passphrase and keyfile:
        raise ValueError("provide passphrase or keyfile, not both")
    if not passphrase and not keyfile:
        raise ValueError("provide passphrase or keyfile")

    if keyfile:
        raw = Path(keyfile).read_bytes()
        key = hashlib.sha256(raw).digest()
        if salt is None:
            salt = os.urandom(_SALT_LEN)
        return MasterKey(key=key, salt=salt, source="keyfile")

    dk = derive_key(passphrase, salt=salt)
    return MasterKey(key=dk.key, salt=dk.salt, source="passphrase")


def derive_sub_key(master: MasterKey, adapter_name: str, version: int = 1) -> SubKey:
    info = f"picard-{adapter_name}-v{version}".encode()
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=master.salt, info=info)
    sub_key_bytes = hkdf.derive(master.key)
    return SubKey(key=sub_key_bytes, adapter_name=adapter_name, version=version)


def derive_correlation_tag(sub_key: SubKey, manifest_id: str) -> bytes:
    return hmac.new(sub_key.key, manifest_id.encode(), hashlib.sha256).digest()[:8]


def export_master_key_salt(master: MasterKey) -> dict:
    """Export the non-secret parts needed to re-derive on another machine."""
    import base64

    return {"salt": base64.b64encode(master.salt).decode(), "source": master.source}


def import_master_key(
    portable: dict,
    passphrase: str | None = None,
    keyfile: Path | None = None,
) -> MasterKey:
    """Re-derive a MasterKey on another machine using exported salt."""
    import base64

    salt = base64.b64decode(portable["salt"])
    return load_master_key(passphrase=passphrase, keyfile=keyfile, salt=salt)


class KeyRing:
    def __init__(self, master: MasterKey) -> None:
        self._master = master
        self._cache: dict[tuple[str, int], SubKey] = {}
        self.versions: dict[str, int] = {}

    def get_sub_key(self, adapter_name: str, version: int = 1) -> SubKey:
        cache_key = (adapter_name, version)
        if cache_key not in self._cache:
            self._cache[cache_key] = derive_sub_key(self._master, adapter_name, version)
        return self._cache[cache_key]

    def rotate(self, adapter_name: str) -> SubKey:
        current = self.versions.get(adapter_name, 1)
        new_version = current + 1
        self.versions[adapter_name] = new_version
        return self.get_sub_key(adapter_name, new_version)
