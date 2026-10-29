"""Spotify playlist adapter -- stores chunks as Spotify playlist descriptions.

Each write_chunk creates a private playlist with the encoded chunk as its
description. Cold reads fetch via GET /v1/playlists/{id}.

Requires: aiohttp, a Spotify OAuth access token and user ID.
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "spotify-playlist"
_BASE_URL = "https://api.spotify.com/v1"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class SpotifyPlaylistAdapter(Adapter):

    def __init__(self, *, access_token: str = "", user_id: str = "") -> None:
        self._access_token = access_token
        self._user_id = user_id
        self._reg: dict[str, str] = {}
        self._session = None

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            headers = {}
            if self._access_token:
                headers["Authorization"] = f"Bearer {self._access_token}"
            self._session = aiohttp.ClientSession(headers=headers)
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=300,
            persistent=True,
            ordered=False,
            rate_limit_per_min=30,
            tags={"requires": "aiohttp,spotify-access-token", "substrate": "spotify-playlist-description"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        payload = {
            "name": f"picard-{manifest_id[:16]}-{token}",
            "description": encoded_chunk,
            "public": False,
        }
        async with session.post(
                f"{_BASE_URL}/users/{self._user_id}/playlists",
                json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()
        playlist_id = data["id"]
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "playlist_id": playlist_id,
                                  "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        if loc["token"] in self._reg:
            return self._reg[loc["token"]]
        session = await self._get_session()
        async with session.get(
                f"{_BASE_URL}/playlists/{loc['playlist_id']}") as resp:
            resp.raise_for_status()
            data = await resp.json()
        chunk = data["description"]
        self._reg[loc["token"]] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        async with session.delete(
                f"{_BASE_URL}/playlists/{loc['playlist_id']}/followers") as resp:
            resp.raise_for_status()
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        if not self._access_token or not self._user_id:
            return []
        session = await self._get_session()
        prefix = f"picard-{manifest_id[:16]}-"
        refs = []
        url = f"{_BASE_URL}/users/{self._user_id}/playlists?limit=50"
        while url:
            async with session.get(url) as resp:
                if resp.status != 200:
                    break
                data = await resp.json()
            for pl in data.get("items", []):
                name = pl.get("name", "")
                if name.startswith(prefix):
                    token = name[len(prefix):]
                    refs.append(StoredChunkRef(
                        adapter_name=_NAME,
                        location=json.dumps({"token": token,
                                              "playlist_id": pl["id"],
                                              "manifest": manifest_id}),
                    ))
            url = (data.get("next") or "")
        return refs

    async def health_check(self) -> bool:
        if not self._access_token:
            return False
        try:
            session = await self._get_session()
            async with session.get(f"{_BASE_URL}/me") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
