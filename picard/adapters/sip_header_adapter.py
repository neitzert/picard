"""SIP header adapter -- stores chunks in SIP message custom headers.

Each write_chunk encodes the chunk as an X-Picard-Chunk header in a
synthetic SIP MESSAGE packet stored in-process. Cold reads parse the SIP
headers with a regex. No network required. Stdlib only.
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "sip_header"

_HEADER_RE = re.compile(r"^X-Picard-Chunk: ([^\r\n]+)", re.MULTILINE)


def _make_sip_message(token: str, encoded_chunk: str) -> str:
    call_id = f"{token}@picard.local"
    return (
        f"MESSAGE sip:picard@127.0.0.1 SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bK{token[:8]}\r\n"
        f"From: <sip:picard@127.0.0.1>;tag={token[:8]}\r\n"
        f"To: <sip:picard@127.0.0.1>\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: 1 MESSAGE\r\n"
        f"Content-Length: 0\r\n"
        f"X-Picard-Chunk: {encoded_chunk}\r\n"
        f"\r\n"
    )


def _parse_sip(msg: str) -> str:
    m = _HEADER_RE.search(msg)
    if not m:
        raise KeyError("X-Picard-Chunk header not found in SIP message")
    return m.group(1).strip()


class SipHeaderAdapter(Adapter):

    def __init__(self) -> None:
        self._reg: dict[str, tuple[str, str]] = {}  # token -> (sip_msg, encoded_chunk)

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=False,
            ordered=False,
            tags={"requires": "stdlib", "substrate": "sip-custom-header"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        def _write():
            token = uuid.uuid4().hex[:16]
            msg = _make_sip_message(token, encoded_chunk)
            return token, msg

        token, msg = await asyncio.to_thread(_write)
        self._reg[token] = (msg, encoded_chunk)
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        token = json.loads(ref.location)["token"]
        if token not in self._reg:
            raise KeyError(f"sip_header chunk not in registry: {token}")
        msg, _ = self._reg[token]
        return _parse_sip(msg)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        token = json.loads(ref.location)["token"]
        self._reg.pop(token, None)
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        from picard.chunker import Chunk
        refs = []
        for token, (_, data) in self._reg.items():
            try:
                if Chunk.decode(data).manifest_id == manifest_id:
                    refs.append(StoredChunkRef(adapter_name=_NAME,
                                               location=json.dumps({"token": token})))
            except Exception:
                continue
        return refs

    async def health_check(self) -> bool:
        try:
            msg = _make_sip_message("deadbeef01234567", "test")
            return _parse_sip(msg) == "test"
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
