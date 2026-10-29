"""SMTP header adapter -- stores chunks in outgoing email X-Picard-Data headers.

Each write_chunk sends an email with an X-Picard-Data header containing the
encoded chunk plus a unique token. A companion IMAP host is required for
cold reads; warm reads are served from the in-process registry.

Requires: pip install aiosmtplib aioimaplib
"""
from __future__ import annotations

import email.message
import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "smtp-header"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class SmtpHeaderAdapter(Adapter):

    def __init__(
        self,
        *,
        smtp_host: str = "127.0.0.1",
        smtp_port: int = 587,
        imap_host: str = "127.0.0.1",
        imap_port: int = 993,
        username: str = "",
        password: str = "",
        from_addr: str = "",
        to_addr: str = "",
        use_tls: bool = True,
    ) -> None:
        self._smtp_host = smtp_host
        self._smtp_port = smtp_port
        self._imap_host = imap_host
        self._imap_port = imap_port
        self._username = username
        self._password = password
        self._from_addr = from_addr or username
        self._to_addr = to_addr or username
        self._use_tls = use_tls
        self._reg: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=4096,
            persistent=True,
            ordered=False,
            rate_limit_per_min=10,
            tags={"requires": "aiosmtplib aioimaplib", "substrate": "smtp-email-header"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import aiosmtplib

        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)

        msg = email.message.EmailMessage()
        msg["From"] = self._from_addr
        msg["To"] = self._to_addr
        msg["Subject"] = f"picard:{token}"
        msg["X-Picard-Token"] = token
        msg["X-Picard-Data"] = encoded_chunk
        msg.set_content(f"picard chunk {token}")

        await aiosmtplib.send(
            msg,
            hostname=self._smtp_host,
            port=self._smtp_port,
            username=self._username or None,
            password=self._password or None,
            use_tls=self._use_tls,
        )

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

        import aioimaplib

        imap = aioimaplib.IMAP4_SSL(host=self._imap_host, port=self._imap_port)
        await imap.wait_hello_from_server()
        await imap.login(self._username, self._password)
        await imap.select("INBOX")
        _, data = await imap.search(f'HEADER Subject "picard:{token}"')
        msg_ids = data[0].split()
        if not msg_ids:
            raise KeyError(f"smtp-header chunk not found: {token}")
        _, msg_data = await imap.fetch(msg_ids[-1].decode(), "(RFC822)")
        raw = msg_data[1]
        import email as emaillib
        parsed = emaillib.message_from_bytes(raw)
        chunk = parsed.get("X-Picard-Data", "")
        await imap.logout()
        if not chunk:
            raise KeyError(f"smtp-header chunk missing X-Picard-Data: {token}")
        self._reg[token] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        token = loc["token"]
        self._reg.pop(token, None)

        try:
            import aioimaplib
            imap = aioimaplib.IMAP4_SSL(host=self._imap_host, port=self._imap_port)
            await imap.wait_hello_from_server()
            await imap.login(self._username, self._password)
            await imap.select("INBOX")
            _, data = await imap.search(f'HEADER Subject "picard:{token}"')
            for mid in data[0].split():
                await imap.store(mid.decode(), "+FLAGS", "\\Deleted")
            await imap.expunge()
            await imap.logout()
        except Exception:
            pass
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
        if not self._username:
            return False
        try:
            import aiosmtplib
            smtp = aiosmtplib.SMTP(
                hostname=self._smtp_host,
                port=self._smtp_port,
                use_tls=self._use_tls,
            )
            await smtp.connect()
            await smtp.quit()
            return True
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
