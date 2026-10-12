"""SMB/CIFS adapter -- stores chunks on Windows file shares.

Each chunk becomes a file on a remote SMB share:
    \\{server}\{share}\{manifest_id}\{block}_{chunk}.pchk

Optionally stores data in NTFS alternate data streams (ADS).

Requires: pip install picard-store[smb]  (smbprotocol)
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class SmbAdapter(Adapter):
    """Scatter chunks onto SMB/CIFS network shares."""

    def __init__(
        self,
        server: str,
        share: str,
        username: str = "",
        password: str = "",
        *,
        port: int = 445,
        domain: str = "",
        use_ads: bool = False,
        max_chunk_bytes: int = 65536,
    ) -> None:
        self._server = server
        self._share = share
        self._username = username
        self._password = password
        self._port = port
        self._domain = domain
        self._use_ads = use_ads
        self._max = max_chunk_bytes
        self._registered = False

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="smb",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=1000,
        )

    def _share_path(self, *parts: str) -> str:
        joined = "\\".join(parts)
        return f"\\\\{self._server}\\{self._share}\\{joined}"

    def _register(self):
        if self._registered:
            return
        import smbclient

        smbclient.register_session(
            self._server,
            username=self._username,
            password=self._password,
            port=self._port,
        )
        self._registered = True

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import smbclient

        try:
            chunk = Chunk.decode(encoded_chunk)
            manifest_id = chunk.manifest_id
            fname = f"{chunk.block_index}_{chunk.chunk_index}.pchk"
        except ValueError:
            manifest_id = uuid.uuid4().hex
            fname = f"{uuid.uuid4().hex}.pchk"

        dir_path = self._share_path(manifest_id)
        file_path = self._share_path(manifest_id, fname)
        if self._use_ads:
            file_path += ":picard"

        loc = f"smb://{self._server}/{self._share}/{manifest_id}/{fname}"
        if self._use_ads:
            loc += ":picard"

        def _write():
            self._register()
            try:
                smbclient.mkdir(dir_path)
            except OSError:
                pass
            with smbclient.open_file(file_path, mode="w") as f:
                f.write(encoded_chunk)

        await asyncio.to_thread(_write)
        return StoredChunkRef(adapter_name="smb", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        import smbclient

        parts = ref.location.removeprefix("smb://").split("/", 3)
        server, share = parts[0], parts[1]
        rel_path = parts[2] + "\\" + parts[3].replace("/", "\\")
        unc = f"\\\\{server}\\{share}\\{rel_path}"

        def _read():
            self._register()
            with smbclient.open_file(unc, mode="r") as f:
                return f.read()

        return await asyncio.to_thread(_read)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        import smbclient

        parts = ref.location.removeprefix("smb://").split("/", 3)
        server, share = parts[0], parts[1]
        rel_path = parts[2] + "\\" + parts[3].replace("/", "\\")
        unc = f"\\\\{server}\\{share}\\{rel_path}"

        def _delete():
            self._register()
            try:
                smbclient.remove(unc)
                return True
            except OSError:
                return False

        return await asyncio.to_thread(_delete)

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        import smbclient

        dir_path = self._share_path(manifest_id)

        def _list():
            self._register()
            refs: list[StoredChunkRef] = []
            try:
                for entry in smbclient.scandir(dir_path):
                    if entry.name.endswith(".pchk"):
                        loc = f"smb://{self._server}/{self._share}/{manifest_id}/{entry.name}"
                        refs.append(StoredChunkRef(adapter_name="smb", location=loc))
            except OSError:
                pass
            return refs

        return await asyncio.to_thread(_list)

    async def health_check(self) -> bool:
        import smbclient

        def _check():
            self._register()
            list(smbclient.scandir(self._share_path()))
            return True

        try:
            return await asyncio.to_thread(_check)
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        import smbclient

        def _capacity():
            self._register()
            chunk_count = 0
            used = 0
            try:
                for entry in smbclient.walk(self._share_path()):
                    for fname in entry[2]:
                        if fname.endswith(".pchk"):
                            chunk_count += 1
            except OSError:
                pass
            return {"used_bytes": used, "chunk_count": chunk_count}

        try:
            info = await asyncio.to_thread(_capacity)
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
