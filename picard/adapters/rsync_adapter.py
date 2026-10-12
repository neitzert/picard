"""Rsync adapter -- stores chunks via rsync protocol or SSH.

Each chunk is staged locally, then pushed to a remote rsync module:
    rsync://{host}/{module}/{manifest_id}/{block}_{chunk}.pchk

Requires: rsync CLI on PATH.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class RsyncAdapter(Adapter):
    """Scatter chunks via rsync to a remote module or SSH host."""

    def __init__(
        self,
        host: str,
        module: str,
        *,
        username: str | None = None,
        password: str | None = None,
        port: int = 873,
        local_staging: str | Path = "/tmp/.picard-rsync-staging",
        max_chunk_bytes: int = 1_048_576,
    ) -> None:
        self._host = host
        self._module = module
        self._username = username
        self._password = password
        self._port = port
        self._staging = Path(local_staging)
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="rsync",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=60,
        )

    def _remote_url(self, *parts: str) -> str:
        prefix = f"{self._username}@" if self._username else ""
        base = f"rsync://{prefix}{self._host}:{self._port}/{self._module}"
        if parts:
            return base + "/" + "/".join(parts)
        return base

    def _env(self) -> dict[str, str] | None:
        if self._password:
            import os

            env = dict(os.environ)
            env["RSYNC_PASSWORD"] = self._password
            return env
        return None

    async def _run(self, *args: str) -> asyncio.subprocess.Process:
        proc = await asyncio.create_subprocess_exec(
            "rsync",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env(),
        )
        await proc.wait()
        return proc

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        try:
            chunk = Chunk.decode(encoded_chunk)
            manifest_id = chunk.manifest_id
            fname = f"{chunk.block_index}_{chunk.chunk_index}.pchk"
        except ValueError:
            manifest_id = uuid.uuid4().hex
            fname = f"{uuid.uuid4().hex}.pchk"

        local_dir = self._staging / manifest_id
        local_dir.mkdir(parents=True, exist_ok=True)
        local_file = local_dir / fname
        local_file.write_text(encoded_chunk, encoding="utf-8")

        remote = self._remote_url(manifest_id) + "/"
        proc = await self._run(
            "--mkpath", str(local_file), remote,
            "--port", str(self._port),
        )
        if proc.returncode != 0:
            stderr = (await proc.stderr.read()).decode() if proc.stderr else ""
            raise RuntimeError(f"rsync push failed: {stderr}")

        local_file.unlink(missing_ok=True)
        loc = f"rsync://{self._host}/{self._module}/{manifest_id}/{fname}"
        return StoredChunkRef(adapter_name="rsync", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        parts = ref.location.removeprefix("rsync://").split("/", 2)
        host, module = parts[0], parts[1]
        rel_path = parts[2]
        manifest_id = rel_path.split("/")[0]
        fname = rel_path.split("/")[1]

        local_dir = self._staging / manifest_id
        local_dir.mkdir(parents=True, exist_ok=True)
        local_file = local_dir / fname

        remote = self._remote_url(rel_path)
        proc = await self._run(
            remote, str(local_file),
            "--port", str(self._port),
        )
        if proc.returncode != 0:
            stderr = (await proc.stderr.read()).decode() if proc.stderr else ""
            raise RuntimeError(f"rsync pull failed: {stderr}")

        data = local_file.read_text(encoding="utf-8")
        local_file.unlink(missing_ok=True)
        return data

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        parts = ref.location.removeprefix("rsync://").split("/", 2)
        rel_path = parts[2]
        manifest_id = rel_path.split("/")[0]
        fname = rel_path.split("/")[1]

        empty_dir = self._staging / "_empty"
        empty_dir.mkdir(parents=True, exist_ok=True)

        remote_dir = self._remote_url(manifest_id) + "/"
        try:
            proc = await self._run(
                "--delete", "--include", fname, "--exclude", "*",
                str(empty_dir) + "/", remote_dir,
                "--port", str(self._port),
            )
            return proc.returncode == 0
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        remote = self._remote_url(manifest_id) + "/"

        try:
            proc = await self._run("--list-only", remote, "--port", str(self._port))
            if proc.returncode != 0 or proc.stdout is None:
                return []
            stdout = (await proc.stdout.read()).decode()
        except Exception:
            return []

        refs: list[StoredChunkRef] = []
        for line in stdout.splitlines():
            parts = line.split()
            if parts and parts[-1].endswith(".pchk"):
                fname = parts[-1]
                loc = f"rsync://{self._host}/{self._module}/{manifest_id}/{fname}"
                refs.append(StoredChunkRef(adapter_name="rsync", location=loc))
        return refs

    async def health_check(self) -> bool:
        try:
            proc = await self._run(
                "--list-only", self._remote_url(),
                "--port", str(self._port),
            )
            return proc.returncode == 0
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        return {
            "used_bytes": None,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": None,
        }
