"""FTP adapter — stores chunks as files on an FTP server.

Each chunk becomes a file in a dedicated directory on the FTP server.
Filenames encode the chunk identity for recovery.

Requires: Python stdlib (ftplib)
"""

from __future__ import annotations

import asyncio
import ftplib
from io import BytesIO
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class FtpAdapter(Adapter):
    """Scatter chunks into files on an FTP server."""

    def __init__(
        self,
        host: str,
        port: int = 21,
        username: str = "anonymous",
        password: str = "",
        base_dir: str = "/picard",
        *,
        max_chunk_bytes: int = 500_000,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._base_dir = base_dir
        self._max = max_chunk_bytes
        self._ftp: ftplib.FTP | None = None

    def _ensure_client(self) -> ftplib.FTP:
        if self._ftp is not None:
            return self._ftp
        ftp = ftplib.FTP()
        ftp.connect(self._host, self._port)
        ftp.login(self._username, self._password)
        try:
            ftp.cwd(self._base_dir)
        except ftplib.error_perm:
            ftp.mkd(self._base_dir)
            ftp.cwd(self._base_dir)
        self._ftp = ftp
        return ftp

    @staticmethod
    def _filename(manifest_id: str, block: int, chunk: int) -> str:
        return f"pchk_{manifest_id}_b{block}_c{chunk}.txt"

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="ftp",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=60,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        chunk = Chunk.decode(encoded_chunk)
        filename = self._filename(
            chunk.manifest_id, chunk.block_index, chunk.chunk_index,
        )

        def _store():
            ftp = self._ensure_client()
            ftp.storbinary(f"STOR {filename}", BytesIO(encoded_chunk.encode()))

        await asyncio.to_thread(_store)
        return StoredChunkRef(adapter_name="ftp", location=filename)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        def _read():
            ftp = self._ensure_client()
            buf = BytesIO()
            ftp.retrbinary(f"RETR {ref.location}", buf.write)
            return buf.getvalue().decode()

        return await asyncio.to_thread(_read)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        def _delete():
            try:
                ftp = self._ensure_client()
                ftp.delete(ref.location)
                return True
            except Exception:
                return False

        return await asyncio.to_thread(_delete)

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        prefix = f"pchk_{manifest_id}_"

        def _list():
            ftp = self._ensure_client()
            names = ftp.nlst()
            return [
                StoredChunkRef(adapter_name="ftp", location=name)
                for name in names
                if name.startswith(prefix)
            ]

        return await asyncio.to_thread(_list)

    async def health_check(self) -> bool:
        def _check():
            try:
                ftp = self._ensure_client()
                ftp.voidcmd("NOOP")
                return True
            except Exception:
                return False

        return await asyncio.to_thread(_check)
