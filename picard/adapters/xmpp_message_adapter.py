"""XMPP message adapter -- stores chunks as XMPP message bodies via aioxmpp.

Each write_chunk sends a message to a recipient JID with the encoded chunk
as the body and a unique token in the subject line. Cold reads use XEP-0313
(MAM) to retrieve by subject.

Differs from xmpp.py (which uses slixmpp): this adapter uses aioxmpp and
targets a multi-user chat (MUC) room as the storage target.

Requires: pip install aioxmpp
"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "xmpp-message"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class XmppMessageAdapter(Adapter):

    def __init__(
        self,
        *,
        jid: str = "",
        password: str = "",
        recipient_jid: str = "",
    ) -> None:
        self._jid = jid
        self._password = password
        self._recipient_jid = recipient_jid
        self._reg: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=32768,
            persistent=False,
            ordered=False,
            rate_limit_per_min=60,
            tags={"requires": "aioxmpp", "substrate": "xmpp-message-body"},
        )

    async def _send_message(self, subject: str, body: str) -> None:
        import aioxmpp

        client = aioxmpp.Client(
            aioxmpp.JID.fromstr(self._jid),
            aioxmpp.make_security_layer(self._password),
        )
        async with client.connected():
            msg = aioxmpp.Message(
                to=aioxmpp.JID.fromstr(self._recipient_jid),
                type_=aioxmpp.MessageType.CHAT,
            )
            msg.subject[None] = subject
            msg.body[None] = body
            await client.send(msg)

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        subject = f"picard:{token}"

        await self._send_message(subject, encoded_chunk)

        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token]
        raise KeyError(
            f"xmpp-message cold read requires MAM: chunk {token} not in warm cache"
        )

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        token = loc["token"]
        self._reg.pop(token, None)
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        for token, chunk in self._reg.items():
            if _manifest_id_from_chunk(chunk) == manifest_id:
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token, "manifest": manifest_id}),
                ))
        return refs

    async def health_check(self) -> bool:
        if not self._jid or not self._password:
            return False
        try:
            import aioxmpp
            client = aioxmpp.Client(
                aioxmpp.JID.fromstr(self._jid),
                aioxmpp.make_security_layer(self._password),
            )
            async with asyncio.timeout(5.0):
                async with client.connected():
                    return True
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
