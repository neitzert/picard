"""Homebrew tap adapter -- stores chunks as formula descriptions.

Each chunk becomes a Homebrew formula file in a local tap repo
with the data in the desc/caveats field.

No external deps -- uses pathlib + git subprocess.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from pathlib import Path
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class HomebrewAdapter(Adapter):
    """Scatter chunks into Homebrew formula descriptions."""

    def __init__(
        self,
        tap_repo_path: str | Path,
        *,
        remote_url: str | None = None,
        max_chunk_bytes: int = 8192,
    ) -> None:
        self._tap = Path(tap_repo_path)
        self._remote_url = remote_url
        self._max = max_chunk_bytes
        self._formula_dir = self._tap / "Formula"

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="homebrew",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=30,
        )

    def _formula_name(self, manifest_id: str, block: int, chunk: int) -> str:
        slug = hashlib.sha256(f"{manifest_id}/{block}/{chunk}".encode()).hexdigest()[:12]
        return f"pchk-{slug}"

    def _formula_path(self, name: str) -> Path:
        return self._formula_dir / f"{name}.rb"

    def _render_formula(self, name: str, data: str) -> str:
        safe = data.replace("\\", "\\\\").replace('"', '\\"')
        return f'''class {name.replace("-", "_").title()} < Formula
  desc "{safe}"
  homepage "https://github.com/picard/chunks"
  url "https://example.com/{name}-0.1.tar.gz"
  sha256 "0" * 64
  version "0.1"

  def install
    # chunk carrier
  end
end
'''

    async def _git(self, *args: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=str(self._tap),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return stdout.decode()

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        try:
            chunk = Chunk.decode(encoded_chunk)
            name = self._formula_name(chunk.manifest_id, chunk.block_index, chunk.chunk_index)
        except ValueError:
            name = f"pchk-{uuid.uuid4().hex[:12]}"

        self._formula_dir.mkdir(parents=True, exist_ok=True)
        path = self._formula_path(name)

        content = self._render_formula(name, encoded_chunk)
        await asyncio.to_thread(path.write_text, content, "utf-8")

        await self._git("add", str(path))
        await self._git("commit", "-m", f"Add {name}")

        tap_name = self._tap.name
        loc = f"brew://{tap_name}/{name}"
        return StoredChunkRef(adapter_name="homebrew", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        parts = ref.location.removeprefix("brew://").split("/")
        name = parts[-1]
        path = self._formula_path(name)

        if not path.exists():
            raise KeyError(f"chunk not found: {ref.location}")

        content = await asyncio.to_thread(path.read_text, "utf-8")

        for line in content.splitlines():
            line = line.strip()
            if line.startswith('desc "') and line.endswith('"'):
                raw = line[5:].strip().strip('"')
                return raw.replace('\\"', '"').replace("\\\\", "\\")

        raise KeyError(f"no desc found in formula: {ref.location}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        parts = ref.location.removeprefix("brew://").split("/")
        name = parts[-1]
        path = self._formula_path(name)

        if not path.exists():
            return False

        try:
            await asyncio.to_thread(path.unlink)
            await self._git("add", str(path))
            await self._git("commit", "-m", f"Remove {name}")
            return True
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        if not self._formula_dir.exists():
            return []

        tap_name = self._tap.name
        refs = []
        for path in self._formula_dir.iterdir():
            if path.suffix == ".rb" and path.stem.startswith("pchk-"):
                refs.append(StoredChunkRef(
                    adapter_name="homebrew",
                    location=f"brew://{tap_name}/{path.stem}",
                ))
        return refs

    async def health_check(self) -> bool:
        return self._tap.is_dir() and (self._tap / ".git").is_dir()
