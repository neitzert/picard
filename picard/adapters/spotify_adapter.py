"""Spotify adapter — stores chunks in playlist descriptions.

Each chunk becomes a new playlist with chunk data in the description
field (up to 300 characters).  Reading extracts the description.
Playlists are created as private/unlisted.

Notice: This adapter must only be used with Spotify accounts you own
and control.  Use in a manner that violates Spotify's Terms of Service
or Developer Policy is outside the scope of this project and the
responsibility of the user.  No endorsement by or affiliation with
Spotify AB is implied.

Requires: pip install picard[spotify]   (uses aiohttp)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_API_BASE = "https://api.spotify.com/v1"


class SpotifyAdapter(Adapter):
    """Scatter chunks into Spotify playlist descriptions."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        user_id: str,
        *,
        max_chunk_bytes: int = 300,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._user_id = user_id
        self._max = max_chunk_bytes
        self._access_token: str | None = None
        self._session = None


    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _get_session(self):
        import aiohttp
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="spotify",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=30,
        )

    async def _ensure_token(self, session) -> str:
        if self._access_token:
            return self._access_token
        async with session.post(
            "https://accounts.spotify.com/api/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            self._access_token = data["access_token"]
        return self._access_token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token}"}

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        session = await self._get_session()
        await self._ensure_token(session)
        async with session.post(
            f"{_API_BASE}/users/{self._user_id}/playlists",
            headers=self._headers(),
            json={
                "name": f"pchk-{uuid.uuid4().hex[:8]}",
                "description": encoded_chunk,
                "public": False,
            },
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        return StoredChunkRef(
            adapter_name="spotify",
            location=f"spotify://{self._user_id}/{data['id']}",
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        playlist_id = ref.location.rsplit("/", 1)[-1]
        session = await self._get_session()
        await self._ensure_token(session)
        async with session.get(
            f"{_API_BASE}/playlists/{playlist_id}",
            headers=self._headers(),
            params={"fields": "description"},
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        return data["description"]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        playlist_id = ref.location.rsplit("/", 1)[-1]
        try:
            session = await self._get_session()
            await self._ensure_token(session)
            async with session.delete(
                f"{_API_BASE}/playlists/{playlist_id}/followers",
                headers=self._headers(),
            ) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs: list[StoredChunkRef] = []
        session = await self._get_session()
        await self._ensure_token(session)
        url = f"{_API_BASE}/users/{self._user_id}/playlists"
        params: dict[str, int] = {"limit": 50, "offset": 0}
        while True:
            async with session.get(
                url, headers=self._headers(), params=params
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
            for item in data.get("items", []):
                desc = item.get("description", "")
                if manifest_id in desc:
                    refs.append(
                        StoredChunkRef(
                            adapter_name="spotify",
                            location=f"spotify://{self._user_id}/{item['id']}",
                        )
                    )
            if not data.get("next"):
                break
            params["offset"] += 50
        return refs

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            await self._ensure_token(session)
            async with session.get(
                f"{_API_BASE}/me", headers=self._headers()
            ) as resp:
                return resp.status == 200
        except Exception:
            return False
