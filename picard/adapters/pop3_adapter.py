"""POP3 adapter — stores chunks as self-addressed emails.

Write via SMTP, retrieve via POP3 scanning for X-Picard-Chunk headers.
    pop3://{host}/{message_id}

Requires: standard library (poplib, smtplib)
"""

from __future__ import annotations

import asyncio
import email.mime.text
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class Pop3Adapter(Adapter):
    """Scatter chunks into an email inbox via SMTP, retrieve via POP3."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        *,
        port: int = 995,
        use_ssl: bool = True,
        smtp_host: str | None = None,
        smtp_port: int = 587,
        max_chunk_bytes: int = 50_000,
    ) -> None:
        self._host = host
        self._username = username
        self._password = password
        self._port = port
        self._use_ssl = use_ssl
        self._smtp_host = smtp_host or host
        self._smtp_port = smtp_port
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="pop3",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=30,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import smtplib

        chunk_id = uuid.uuid4().hex

        msg = email.mime.text.MIMEText(encoded_chunk)
        msg["Subject"] = f"PICARD-{chunk_id}"
        msg["From"] = self._username
        msg["To"] = self._username
        msg["X-Picard-Chunk"] = chunk_id

        def _send():
            with smtplib.SMTP(self._smtp_host, self._smtp_port) as smtp:
                smtp.starttls()
                smtp.login(self._username, self._password)
                smtp.send_message(msg)

        await asyncio.to_thread(_send)
        location = f"pop3://{self._host}/{chunk_id}"
        return StoredChunkRef(adapter_name="pop3", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        import poplib

        chunk_id = ref.location.rsplit("/", 1)[1]

        def _read():
            if self._use_ssl:
                pop = poplib.POP3_SSL(self._host, self._port)
            else:
                pop = poplib.POP3(self._host, self._port)
            try:
                pop.user(self._username)
                pop.pass_(self._password)
                count, _ = pop.stat()
                for i in range(count, 0, -1):
                    _, lines, _ = pop.retr(i)
                    raw = b"\r\n".join(lines)
                    parsed = email.message_from_bytes(raw)
                    if parsed.get("X-Picard-Chunk") == chunk_id:
                        payload = parsed.get_payload(decode=True)
                        if payload:
                            return payload.decode()
                        return parsed.get_payload()
            finally:
                pop.quit()
            raise KeyError(f"chunk not found: {chunk_id}")

        return await asyncio.to_thread(_read)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        import poplib

        chunk_id = ref.location.rsplit("/", 1)[1]

        def _delete():
            if self._use_ssl:
                pop = poplib.POP3_SSL(self._host, self._port)
            else:
                pop = poplib.POP3(self._host, self._port)
            try:
                pop.user(self._username)
                pop.pass_(self._password)
                count, _ = pop.stat()
                for i in range(1, count + 1):
                    _, lines, _ = pop.retr(i)
                    raw = b"\r\n".join(lines)
                    parsed = email.message_from_bytes(raw)
                    if parsed.get("X-Picard-Chunk") == chunk_id:
                        pop.dele(i)
                        return True
            finally:
                pop.quit()
            return False

        return await asyncio.to_thread(_delete)

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return []

    async def health_check(self) -> bool:
        import poplib

        def _check():
            if self._use_ssl:
                pop = poplib.POP3_SSL(self._host, self._port)
            else:
                pop = poplib.POP3(self._host, self._port)
            try:
                pop.user(self._username)
                pop.pass_(self._password)
                pop.stat()
                return True
            finally:
                pop.quit()

        try:
            return await asyncio.to_thread(_check)
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        import poplib

        def _cap():
            if self._use_ssl:
                pop = poplib.POP3_SSL(self._host, self._port)
            else:
                pop = poplib.POP3(self._host, self._port)
            try:
                pop.user(self._username)
                pop.pass_(self._password)
                count, size = pop.stat()
                return {"used_bytes": size, "chunk_count": count}
            finally:
                pop.quit()

        try:
            info = await asyncio.to_thread(_cap)
            return {
                "used_bytes": info["used_bytes"],
                "free_bytes": None,
                "total_bytes": None,
                "chunk_count": info["chunk_count"],
            }
        except Exception:
            return {
                "used_bytes": None,
                "free_bytes": None,
                "total_bytes": None,
                "chunk_count": None,
            }
