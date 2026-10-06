"""SSH/SFTP adapter -- stores chunks on remote hosts via SSH.

Each chunk becomes a file on the remote system:
    {remote_dir}/{manifest_id}/{block}_{chunk}.pchk

Requires: pip install picard[ssh]  (asyncssh)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class SshAdapter(Adapter):
    """Scatter chunks via SSH/SFTP to remote hosts."""

    def __init__(
        self,
        host: str,
        username: str,
        *,
        port: int = 22,
        password: str | None = None,
        key_filename: str | None = None,
        remote_dir: str = "/tmp/.picard",
        max_chunk_bytes: int = 1_048_576,
    ) -> None:
        self._host = host
        self._username = username
        self._port = port
        self._password = password
        self._key_filename = key_filename
        self._remote_dir = remote_dir.rstrip("/")
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="ssh",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=300,
        )

    def _remote_path(self, manifest_id: str, fname: str) -> str:
        return f"{self._remote_dir}/{manifest_id}/{fname}"

    async def _connect(self):
        import asyncssh

        kwargs: dict = {
            "host": self._host,
            "port": self._port,
            "username": self._username,
            "known_hosts": None,
        }
        if self._password:
            kwargs["password"] = self._password
        if self._key_filename:
            kwargs["client_keys"] = [self._key_filename]
        return await asyncssh.connect(**kwargs)

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        try:
            chunk = Chunk.decode(encoded_chunk)
            manifest_id = chunk.manifest_id
            fname = f"{chunk.block_index}_{chunk.chunk_index}.pchk"
        except ValueError:
            manifest_id = uuid.uuid4().hex
            fname = f"{uuid.uuid4().hex}.pchk"

        remote = self._remote_path(manifest_id, fname)
        loc = f"ssh://{self._host}{self._remote_dir}/{manifest_id}/{fname}"

        async with await self._connect() as conn:
            async with conn.start_sftp_client() as sftp:
                parent = f"{self._remote_dir}/{manifest_id}"
                try:
                    await sftp.mkdir(self._remote_dir)
                except (OSError, Exception):
                    pass
                try:
                    await sftp.mkdir(parent)
                except (OSError, Exception):
                    pass
                async with sftp.open(remote, "w") as f:
                    await f.write(encoded_chunk)

        return StoredChunkRef(adapter_name="ssh", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        remote = ref.location.removeprefix(f"ssh://{self._host}")

        async with await self._connect() as conn:
            async with conn.start_sftp_client() as sftp:
                async with sftp.open(remote, "r") as f:
                    return await f.read()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        remote = ref.location.removeprefix(f"ssh://{self._host}")

        try:
            async with await self._connect() as conn:
                async with conn.start_sftp_client() as sftp:
                    await sftp.remove(remote)
                    return True
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        remote_dir = f"{self._remote_dir}/{manifest_id}"
        refs: list[StoredChunkRef] = []

        try:
            async with await self._connect() as conn:
                async with conn.start_sftp_client() as sftp:
                    entries = await sftp.listdir(remote_dir)
                    for name in entries:
                        if name.endswith(".pchk"):
                            loc = f"ssh://{self._host}{remote_dir}/{name}"
                            refs.append(
                                StoredChunkRef(adapter_name="ssh", location=loc)
                            )
        except Exception:
            pass

        return refs

    async def health_check(self) -> bool:
        try:
            async with await self._connect() as conn:
                result = await conn.run("echo ok", check=True)
                return result.stdout.strip() == "ok"
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        try:
            async with await self._connect() as conn:
                async with conn.start_sftp_client() as sftp:
                    stat = await sftp.statvfs(self._remote_dir)
                    total = stat.f_frsize * stat.f_blocks
                    free = stat.f_frsize * stat.f_bavail
                    return {
                        "used_bytes": total - free,
                        "free_bytes": free,
                        "total_bytes": total,
                        "chunk_count": None,
                    }
        except Exception:
            return {
                "used_bytes": None,
                "free_bytes": None,
                "total_bytes": None,
                "chunk_count": None,
            }
