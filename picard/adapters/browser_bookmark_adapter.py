"""Browser bookmark adapter -- stores chunks as bookmark entries.

Stores chunk data in browser bookmark URL fragments.  Uses a
Chrome-style JSON bookmarks file.

Requires: nothing beyond stdlib (JSON file I/O)
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


def _default_bookmarks_path() -> str:
    """Best-effort Chrome bookmarks path for the current platform."""
    if os.name == "nt":
        appdata = os.environ.get("LOCALAPPDATA", "")
        return os.path.join(appdata, "Google", "Chrome", "User Data", "Default", "Bookmarks")
    home = os.path.expanduser("~")
    if os.path.exists("/Applications"):  # macOS heuristic
        return os.path.join(home, "Library", "Application Support", "Google", "Chrome", "Default", "Bookmarks")
    return os.path.join(home, ".config", "google-chrome", "Default", "Bookmarks")


class BrowserBookmarkAdapter(Adapter):
    """Scatter chunks into browser bookmark URLs."""

    def __init__(
        self,
        *,
        bookmarks_path: str | None = None,
        folder_name: str = "PICARD",
        max_chunk_bytes: int = 8192,
    ) -> None:
        self._bookmarks_path = bookmarks_path or _default_bookmarks_path()
        self._folder = folder_name
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}

    # -- ABC ----------------------------------------------------------

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="browser-bookmarks",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=120,
        )

    def _build_url(self, chunk_id: str, encoded_chunk: str) -> str:
        return f"https://picard.local/#{chunk_id}:{encoded_chunk}"

    def _load_bookmarks(self) -> dict[str, Any]:
        try:
            with open(self._bookmarks_path) as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return {
                "roots": {
                    "bookmark_bar": {
                        "children": [],
                        "name": "Bookmarks bar",
                        "type": "folder",
                    }
                },
                "version": 1,
            }

    def _save_bookmarks(self, data: dict[str, Any]) -> None:
        try:
            os.makedirs(os.path.dirname(self._bookmarks_path), exist_ok=True)
            with open(self._bookmarks_path, "w") as fh:
                json.dump(data, fh, indent=2)
        except OSError:
            pass

    def _get_picard_folder(self, data: dict[str, Any], *, create: bool = False) -> list | None:
        bar = data.get("roots", {}).get("bookmark_bar", {})
        children = bar.get("children", [])

        for child in children:
            if child.get("type") == "folder" and child.get("name") == self._folder:
                return child.get("children", [])

        if create:
            folder: dict[str, Any] = {
                "children": [],
                "name": self._folder,
                "type": "folder",
            }
            children.append(folder)
            return folder["children"]

        return None

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        chunk_id = uuid.uuid4().hex[:12]
        url = self._build_url(chunk_id, encoded_chunk)

        bookmark_entry = {
            "name": f"picard-{chunk_id}",
            "type": "url",
            "url": url,
        }

        try:
            data = self._load_bookmarks()
            folder = self._get_picard_folder(data, create=True)
            if folder is not None:
                folder.append(bookmark_entry)
            self._save_bookmarks(data)
        except Exception:
            pass

        location = f"bookmark://{self._folder}/{chunk_id}"
        self._cache[location] = encoded_chunk
        return StoredChunkRef(adapter_name="browser-bookmarks", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location in self._cache:
            return self._cache[ref.location]

        # Try parsing the bookmarks file
        chunk_id = ref.location.rsplit("/", 1)[-1]
        marker = f"#{chunk_id}:"

        try:
            data = self._load_bookmarks()
            folder = self._get_picard_folder(data)
            if folder:
                for entry in folder:
                    url = entry.get("url", "")
                    if marker in url:
                        return url.split(marker, 1)[1]
        except Exception:
            pass

        raise KeyError(f"chunk not found: {ref.location}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        existed = ref.location in self._cache
        self._cache.pop(ref.location, None)

        chunk_id = ref.location.rsplit("/", 1)[-1]
        marker = f"picard-{chunk_id}"

        try:
            data = self._load_bookmarks()
            folder = self._get_picard_folder(data)
            if folder:
                data_folder = [e for e in folder if e.get("name") != marker]
                # Replace in-place
                folder.clear()
                folder.extend(data_folder)
                self._save_bookmarks(data)
        except Exception:
            pass

        return existed

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs: list[StoredChunkRef] = []
        for loc, data in self._cache.items():
            try:
                chunk = Chunk.decode(data)
                if chunk.manifest_id == manifest_id:
                    refs.append(
                        StoredChunkRef(adapter_name="browser-bookmarks", location=loc)
                    )
            except Exception:
                continue
        return refs

    async def health_check(self) -> bool:
        try:
            parent = os.path.dirname(self._bookmarks_path)
            return os.path.isdir(parent)
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        used = sum(len(v.encode()) for v in self._cache.values())
        return {
            "used_bytes": used,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": len(self._cache),
        }
