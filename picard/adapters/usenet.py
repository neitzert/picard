"""Usenet/NNTP adapter — stores chunks as newsgroup articles.

Each chunk becomes a Usenet article posted to a configured newsgroup.
Articles propagate across NNTP servers and persist according to server
retention policy.

Requires: no extra dependencies (stdlib nntplib)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from email.utils import formatdate
from typing import Sequence
from uuid import uuid4

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class UsenetAdapter(Adapter):
    """Scatter chunks into Usenet articles via NNTP."""

    def __init__(
        self,
        host: str,
        *,
        port: int = 119,
        username: str | None = None,
        password: str | None = None,
        newsgroup: str = "alt.binaries.picard",
        use_ssl: bool = False,
        max_chunk_bytes: int = 500_000,
        from_address: str = "",
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._newsgroup = newsgroup
        self._use_ssl = use_ssl
        self._max = max_chunk_bytes
        self._from_address = from_address
        self._client: object | None = None

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        import nntplib

        if self._use_ssl:
            client = nntplib.NNTP_SSL(self._host, port=self._port)
        else:
            client = nntplib.NNTP(self._host, port=self._port)

        if self._username and self._password:
            client.login(self._username, self._password)

        client.group(self._newsgroup)
        self._client = client
        return client

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="usenet",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=30,
        )

    def _build_article(self, encoded_chunk: str, message_id: str) -> list[bytes]:
        chunk = Chunk.decode(encoded_chunk)
        subject = (
            f"PCHK {chunk.manifest_id} "
            f"b{chunk.block_index} c{chunk.chunk_index}"
        )
        date_str = formatdate(usegmt=True)
        if self._from_address:
            from_header = self._from_address
        elif self._username:
            from_header = f"{self._username}@{self._host}"
        else:
            from_header = f"picard@{self._host}"
        lines = [
            f"From: {from_header}",
            f"Newsgroups: {self._newsgroup}",
            f"Subject: {subject}",
            f"Message-ID: {message_id}",
            f"Date: {date_str}",
            "",
            encoded_chunk,
        ]
        return [line.encode() for line in lines]

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        client = self._ensure_client()
        msg_id = f"<{uuid4().hex}@picard>"
        article = self._build_article(encoded_chunk, msg_id)

        def _post():
            client.post(article)

        await asyncio.to_thread(_post)
        return StoredChunkRef(adapter_name="usenet", location=msg_id)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        client = self._ensure_client()

        def _read():
            resp, info = client.article(ref.location)
            lines = [
                line.decode(errors="replace")
                if isinstance(line, bytes)
                else line
                for line in info.lines
            ]
            body_start = None
            for i, line in enumerate(lines):
                if line == "":
                    body_start = i + 1
                    break
            if body_start is None:
                raise ValueError("malformed article: no body separator")
            return "\n".join(lines[body_start:])

        return await asyncio.to_thread(_read)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        client = self._ensure_client()
        cancel_id = f"<{uuid4().hex}@picard>"

        def _cancel():
            try:
                if self._from_address:
                    from_header = self._from_address
                elif self._username:
                    from_header = f"{self._username}@{self._host}"
                else:
                    from_header = f"picard@{self._host}"
                article = [
                    f"From: {from_header}".encode(),
                    f"Newsgroups: {self._newsgroup}".encode(),
                    f"Subject: cancel".encode(),
                    f"Message-ID: {cancel_id}".encode(),
                    f"Control: cancel {ref.location}".encode(),
                    b"",
                    b"Chunk removed by PICARD.",
                ]
                client.post(article)
                return True
            except Exception:
                return False

        return await asyncio.to_thread(_cancel)

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        client = self._ensure_client()
        prefix = f"PCHK {manifest_id} "

        def _list():
            refs: list[StoredChunkRef] = []
            try:
                resp, subjects = client.xhdr("subject", "1-")
                for art_num, subject in subjects:
                    if isinstance(subject, bytes):
                        subject = subject.decode(errors="replace")
                    if subject.startswith(prefix):
                        resp2, info = client.article(str(art_num))
                        msg_id = None
                        for line in info.lines:
                            s = line.decode(errors="replace") if isinstance(line, bytes) else line
                            if s.lower().startswith("message-id:"):
                                msg_id = s.split(":", 1)[1].strip()
                                break
                        if msg_id:
                            refs.append(
                                StoredChunkRef(
                                    adapter_name="usenet",
                                    location=msg_id,
                                )
                            )
            except Exception:
                pass
            return refs

        return await asyncio.to_thread(_list)

    async def health_check(self) -> bool:
        try:
            self._ensure_client()
            return True
        except Exception:
            return False
