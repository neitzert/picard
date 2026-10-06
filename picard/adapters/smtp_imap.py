"""SMTP/IMAP adapter — stores chunks as email messages.

Writes via SMTP, reads/lists/deletes via IMAP.  Each chunk becomes an
email in a dedicated IMAP folder, tagged with X-PCHK-Id for retrieval.

Requires: smtplib and imaplib (stdlib)
"""

from __future__ import annotations

import asyncio
import email.mime.text
import email.utils
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class SmtpImapAdapter(Adapter):
    """Scatter chunks into email messages via SMTP, retrieve via IMAP."""

    def __init__(
        self,
        smtp_host: str,
        imap_host: str,
        username: str,
        password: str,
        *,
        smtp_port: int = 587,
        imap_port: int = 993,
        folder: str = "PICARD",
        use_ssl: bool = True,
        max_chunk_bytes: int = 500_000,
    ) -> None:
        self._smtp_host = smtp_host
        self._smtp_port = smtp_port
        self._imap_host = imap_host
        self._imap_port = imap_port
        self._username = username
        self._password = password
        self._folder = folder
        self._use_ssl = use_ssl
        self._max = max_chunk_bytes
        self._smtp: object | None = None
        self._imap: object | None = None

    def _ensure_smtp(self):
        if self._smtp is not None:
            return self._smtp
        import smtplib

        smtp = smtplib.SMTP(self._smtp_host, self._smtp_port)
        smtp.starttls()
        smtp.login(self._username, self._password)
        self._smtp = smtp
        return smtp

    def _ensure_imap(self):
        if self._imap is not None:
            return self._imap
        import imaplib

        if self._use_ssl:
            imap = imaplib.IMAP4_SSL(self._imap_host, self._imap_port)
        else:
            imap = imaplib.IMAP4(self._imap_host, self._imap_port)
        imap.login(self._username, self._password)
        try:
            imap.select(self._folder)
        except Exception:
            imap.create(self._folder)
            imap.select(self._folder)
        self._imap = imap
        return imap

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="smtp-imap",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=30,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        chunk = Chunk.decode(encoded_chunk)
        msg_id = uuid.uuid4().hex[:16]

        subject = (
            f"PCHK {chunk.manifest_id} "
            f"b{chunk.block_index} c{chunk.chunk_index} [{msg_id}]"
        )

        msg = email.mime.text.MIMEText(encoded_chunk)
        msg["From"] = self._username
        msg["To"] = self._username
        msg["Subject"] = subject
        msg["Message-ID"] = f"<{msg_id}@picard>"
        msg["X-PCHK-Id"] = msg_id
        msg["Date"] = email.utils.formatdate(localtime=True)

        def _send():
            smtp = self._ensure_smtp()
            smtp.sendmail(self._username, [self._username], msg.as_string())

        await asyncio.to_thread(_send)
        return StoredChunkRef(adapter_name="smtp-imap", location=msg_id)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        def _read():
            imap = self._ensure_imap()
            _typ, data = imap.search(None, f'HEADER X-PCHK-Id {ref.location}')
            nums = data[0].split()
            if not nums:
                raise ValueError(f"chunk not found: {ref.location}")
            _typ, msg_data = imap.fetch(nums[0], "(RFC822)")
            raw = msg_data[0][1]
            parsed = email.message_from_bytes(raw)
            return parsed.get_payload(decode=True).decode()

        return await asyncio.to_thread(_read)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        def _delete():
            try:
                imap = self._ensure_imap()
                _typ, data = imap.search(None, f'HEADER X-PCHK-Id {ref.location}')
                nums = data[0].split()
                if not nums:
                    return False
                for num in nums:
                    imap.store(num, "+FLAGS", "\\Deleted")
                imap.expunge()
                return True
            except Exception:
                return False

        return await asyncio.to_thread(_delete)

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        def _list():
            imap = self._ensure_imap()
            _typ, data = imap.search(None, f'SUBJECT "PCHK {manifest_id}"')
            nums = data[0].split()
            refs: list[StoredChunkRef] = []
            for num in nums:
                _typ, msg_data = imap.fetch(num, "(BODY[HEADER.FIELDS (X-PCHK-ID)])")
                header = msg_data[0][1].decode()
                for line in header.splitlines():
                    if line.lower().startswith("x-pchk-id:"):
                        pchk_id = line.split(":", 1)[1].strip()
                        refs.append(
                            StoredChunkRef(adapter_name="smtp-imap", location=pchk_id)
                        )
                        break
            return refs

        return await asyncio.to_thread(_list)

    async def health_check(self) -> bool:
        def _check():
            try:
                self._ensure_imap()
                return True
            except Exception:
                return False

        return await asyncio.to_thread(_check)
