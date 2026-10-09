"""Flickr adapter — stores chunks in photo descriptions.

Uploads a 1x1 pixel PNG placeholder and hides chunk data in the
photo's description field.  Reading extracts the description.

Requires: pip install picard[flickr]   (uses aiohttp)
"""

from __future__ import annotations

import io
import struct
import zlib
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
import aiohttp

_API_BASE = "https://www.flickr.com/services"
_REST_URL = "https://api.flickr.com/services/rest/"


def _tiny_png() -> bytes:
    """Generate a minimal 1x1 white PNG."""

    def _chunk(chunk_type: bytes, data: bytes) -> bytes:
        raw = chunk_type + data
        return struct.pack(">I", len(data)) + raw + struct.pack(">I", zlib.crc32(raw) & 0xFFFFFFFF)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    raw_data = zlib.compress(b"\x00\xff\xff\xff")
    idat = _chunk(b"IDAT", raw_data)
    iend = _chunk(b"IEND", b"")
    return sig + ihdr + idat + iend


class FlickrAdapter(Adapter):
    """Scatter chunks into Flickr photo descriptions."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        oauth_token: str,
        oauth_secret: str,
        *,
        max_chunk_bytes: int = 32768,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._oauth_token = oauth_token
        self._oauth_secret = oauth_secret
        self._max = max_chunk_bytes
        self._session = None


    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="flickr",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=30,
        )

    def _auth_params(self) -> dict[str, str]:
        return {
            "api_key": self._api_key,
            "auth_token": self._oauth_token,
            "format": "json",
            "nojsoncallback": "1",
        }

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        png_data = _tiny_png()
        form = aiohttp.FormData()
        form.add_field("api_key", self._api_key)
        form.add_field("auth_token", self._oauth_token)
        form.add_field("description", encoded_chunk)
        form.add_field("title", "pchk")
        form.add_field("is_public", "0")
        form.add_field("hidden", "2")
        form.add_field(
            "photo",
            io.BytesIO(png_data),
            filename="chunk.png",
            content_type="image/png",
        )

        session = await self._get_session()
        async with session.post(
            f"{_API_BASE}/upload/", data=form
        ) as resp:
            resp.raise_for_status()
            text = await resp.text()
            import xml.etree.ElementTree as ET
            root = ET.fromstring(text)
            photo_id = root.find("photoid").text
        loc = f"flickr://{photo_id}"
        return StoredChunkRef(adapter_name="flickr", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        photo_id = ref.location.rsplit("/", 1)[-1]
        params = self._auth_params()
        params["method"] = "flickr.photos.getInfo"
        params["photo_id"] = photo_id

        session = await self._get_session()
        async with session.get(_REST_URL, params=params) as resp:
            resp.raise_for_status()
            data = await resp.json()
        return data["photo"]["description"]["_content"]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        photo_id = ref.location.rsplit("/", 1)[-1]
        params = self._auth_params()
        params["method"] = "flickr.photos.delete"
        params["photo_id"] = photo_id

        try:
            session = await self._get_session()
            async with session.post(_REST_URL, params=params) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        params = self._auth_params()
        params["method"] = "flickr.photos.search"
        params["user_id"] = "me"
        params["text"] = manifest_id
        params["extras"] = "description"

        refs: list[StoredChunkRef] = []
        session = await self._get_session()
        async with session.get(_REST_URL, params=params) as resp:
            resp.raise_for_status()
            data = await resp.json()
        for photo in data.get("photos", {}).get("photo", []):
            desc = photo.get("description", {}).get("_content", "")
            if manifest_id in desc:
                refs.append(
                    StoredChunkRef(
                        adapter_name="flickr",
                        location=f"flickr://{photo['id']}",
                    )
                )
        return refs

    async def health_check(self) -> bool:
        params = self._auth_params()
        params["method"] = "flickr.test.login"
        try:
            session = await self._get_session()
            async with session.get(_REST_URL, params=params) as resp:
                return resp.status == 200
        except Exception:
            return False
