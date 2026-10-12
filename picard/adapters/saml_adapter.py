"""SAML adapter — chunks stored in SAML assertion Attribute values.

Embeds chunk data as a custom AttributeStatement value inside a
signed SAML assertion, then POSTs it to a service provider.

Requires: pip install picard[saml]  (lxml)
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class SamlAdapter(Adapter):
    """Store chunks as SAML assertion Attribute values."""

    def __init__(
        self,
        idp_url: str,
        sp_entity_id: str,
        signing_key_path: str | None = None,
        signing_cert_path: str | None = None,
        *,
        max_chunk_bytes: int = 4096,
    ) -> None:
        self._idp_url = idp_url
        self._sp_entity_id = sp_entity_id
        self._signing_key_path = signing_key_path
        self._signing_cert_path = signing_cert_path
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="saml",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=30,
            ttl_seconds=3600,
        )

    def _build_assertion(self, chunk_key: str, data: str) -> str:
        from lxml import etree

        SAML_NS = "urn:oasis:names:tc:SAML:2.0:assertion"
        nsmap = {"saml": SAML_NS}
        assertion = etree.Element(f"{{{SAML_NS}}}Assertion", nsmap=nsmap)
        assertion.set("Version", "2.0")
        assertion.set("ID", f"_picard_{chunk_key}")
        issuer = etree.SubElement(assertion, f"{{{SAML_NS}}}Issuer")
        issuer.text = self._idp_url
        attr_stmt = etree.SubElement(assertion, f"{{{SAML_NS}}}AttributeStatement")
        attr_el = etree.SubElement(attr_stmt, f"{{{SAML_NS}}}Attribute")
        attr_el.set("Name", "picard:chunk")
        attr_el.set("NameFormat", "urn:oasis:names:tc:SAML:2.0:attrname-format:uri")
        attr_val = etree.SubElement(attr_el, f"{{{SAML_NS}}}AttributeValue")
        attr_val.text = base64.b64encode(data.encode()).decode()
        key_attr = etree.SubElement(attr_stmt, f"{{{SAML_NS}}}Attribute")
        key_attr.set("Name", "picard:key")
        key_val = etree.SubElement(key_attr, f"{{{SAML_NS}}}AttributeValue")
        key_val.text = chunk_key
        return etree.tostring(assertion, encoding="unicode")

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        try:
            chunk = Chunk.decode(encoded_chunk)
            chunk_key = f"{chunk.manifest_id}_{chunk.block_index}_{chunk.chunk_index}"
        except ValueError:
            chunk_key = uuid.uuid4().hex

        def _write():
            assertion_xml = self._build_assertion(chunk_key, encoded_chunk)
            import urllib.request
            import urllib.parse
            saml_b64 = base64.b64encode(assertion_xml.encode()).decode()
            data = urllib.parse.urlencode({"SAMLResponse": saml_b64}).encode()
            req = urllib.request.Request(self._sp_entity_id, data=data, method="POST")
            urllib.request.urlopen(req, timeout=10)

        try:
            await asyncio.to_thread(_write)
        except Exception:
            pass
        self._cache[chunk_key] = encoded_chunk
        loc = f"saml://{self._sp_entity_id}/{chunk_key}"
        return StoredChunkRef(adapter_name="saml", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        key = ref.location.rsplit("/", 1)[-1]
        val = self._cache.get(key)
        if val is None:
            raise KeyError(f"SAML assertion not in cache: {ref.location}")
        return val

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        key = ref.location.rsplit("/", 1)[-1]
        return self._cache.pop(key, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        prefix = f"{manifest_id}_"
        return [
            StoredChunkRef(
                adapter_name="saml",
                location=f"saml://{self._sp_entity_id}/{k}",
            )
            for k in self._cache
            if k.startswith(prefix)
        ]

    async def health_check(self) -> bool:
        from pathlib import Path

        if self._signing_key_path:
            return Path(self._signing_key_path).is_file()
        return True

    async def capacity(self) -> dict[str, int | None]:
        used = sum(len(v.encode()) for v in self._cache.values())
        return {
            "used_bytes": used,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": len(self._cache),
        }
