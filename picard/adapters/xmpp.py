"""XMPP adapter — stores chunks as XMPP messages via slixmpp.

Each chunk is sent as a message to a recipient JID (user or MUC room).
Retrieval uses XEP-0313 (MAM) to query the message archive.
Deletion is best-effort via a control message since XMPP lacks native
message deletion.

Requires: pip install picard[xmpp]
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class XmppAdapter(Adapter):
    """Scatter chunks into XMPP messages."""

    def __init__(
        self,
        jid: str,
        password: str,
        recipient_jid: str,
        *,
        use_muc: bool = False,
        max_chunk_bytes: int = 60_000,
    ) -> None:
        self._jid = jid
        self._password = password
        self._recipient_jid = recipient_jid
        self._use_muc = use_muc
        self._max = max_chunk_bytes
        self._client: object | None = None

    async def _ensure_client(self):
        if self._client is not None:
            return self._client
        from slixmpp import ClientXMPP

        client = ClientXMPP(self._jid, self._password)
        client.register_plugin("xep_0313")
        if self._use_muc:
            client.register_plugin("xep_0045")

        await asyncio.to_thread(client.connect)
        client.process(forever=False)

        if self._use_muc:
            await client.plugin["xep_0045"].join_muc(
                self._recipient_jid, self._jid.split("@")[0]
            )

        self._client = client
        return client

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="xmpp",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=True,
            rate_limit_per_min=60,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        client = await self._ensure_client()
        msg_id = uuid.uuid4().hex
        mtype = "groupchat" if self._use_muc else "chat"
        client.send_message(
            mto=self._recipient_jid,
            mbody=encoded_chunk,
            mtype=mtype,
            mnick=self._jid.split("@")[0],
            mfrom=self._jid,
            mhtml=None,
        )
        return StoredChunkRef(adapter_name="xmpp", location=msg_id)

    async def _query_archive(self, msg_id: str | None = None) -> list[dict]:
        client = await self._ensure_client()
        results = await client.plugin["xep_0313"].retrieve(
            with_jid=self._recipient_jid,
        )
        messages = []
        for msg in results["mam"]["results"]:
            fwd = msg["mam_result"]["forwarded"]
            body = fwd["stanza"]["body"]
            stanza_id = fwd["stanza"].get("id", "")
            if body:
                messages.append({"id": stanza_id, "body": str(body)})
        return messages

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        messages = await self._query_archive(ref.location)
        for msg in messages:
            if msg["id"] == ref.location:
                return msg["body"]
        raise ValueError(f"chunk not found in archive: {ref.location}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        try:
            client = await self._ensure_client()
            mtype = "groupchat" if self._use_muc else "chat"
            client.send_message(
                mto=self._recipient_jid,
                mbody=f"PCHK-DEL:{ref.location}",
                mtype=mtype,
                mnick=self._jid.split("@")[0],
                mfrom=self._jid,
                mhtml=None,
            )
            return True
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        prefix = f"PCHK:1:{manifest_id}:"
        messages = await self._query_archive()
        refs: list[StoredChunkRef] = []
        for msg in messages:
            if msg["body"].startswith(prefix):
                refs.append(
                    StoredChunkRef(adapter_name="xmpp", location=msg["id"])
                )
        return refs

    async def health_check(self) -> bool:
        try:
            client = await self._ensure_client()
            return client.is_connected()
        except Exception:
            return False
