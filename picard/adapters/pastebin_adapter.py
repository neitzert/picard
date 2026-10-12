"""Pastebin adapter — stores chunks as unlisted pastes.

Each chunk becomes a paste:
    paste://{paste_key}

Requires: pip install picard[pastebin]
"""

from __future__ import annotations

from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_API = "https://pastebin.com/api"


class PastebinAdapter(Adapter):
    """Scatter chunks into Pastebin pastes."""

    def __init__(
        self,
        api_key: str,
        *,
        api_user_key: str | None = None,
        expire: str = "1M",
        visibility: int = 1,
        max_chunk_bytes: int = 512_000,
    ) -> None:
        self._api_key = api_key
        self._user_key = api_user_key
        self._expire = expire
        self._visibility = visibility
        self._max = max_chunk_bytes
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
            name="pastebin",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=10,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        data = {
            "api_dev_key": self._api_key,
            "api_option": "paste",
            "api_paste_code": encoded_chunk,
            "api_paste_private": str(self._visibility),
            "api_paste_expire_date": self._expire,
            "api_paste_name": "pchk",
        }
        if self._user_key:
            data["api_user_key"] = self._user_key

        session = await self._get_session()
        async with session.post(f"{_API}/api_post.php", data=data) as resp:
            resp.raise_for_status()
            url = await resp.text()

        paste_key = url.rstrip("/").rsplit("/", 1)[-1]
        location = f"paste://{paste_key}"
        return StoredChunkRef(adapter_name="pastebin", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        paste_key = ref.location.split("://", 1)[1]

        if self._user_key:
            data = {
                "api_dev_key": self._api_key,
                "api_user_key": self._user_key,
                "api_option": "show_paste",
                "api_paste_key": paste_key,
            }
            session = await self._get_session()
            async with session.post(
                f"{_API}/api_raw.php", data=data
            ) as resp:
                resp.raise_for_status()
                return await resp.text()
        else:
            session = await self._get_session()
            async with session.get(
                f"https://pastebin.com/raw/{paste_key}"
            ) as resp:
                resp.raise_for_status()
                return await resp.text()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        paste_key = ref.location.split("://", 1)[1]

        if not self._user_key:
            return False

        data = {
            "api_dev_key": self._api_key,
            "api_user_key": self._user_key,
            "api_option": "delete",
            "api_paste_key": paste_key,
        }

        try:
            session = await self._get_session()
            async with session.post(
                f"{_API}/api_post.php", data=data
            ) as resp:
                text = await resp.text()
                return "removed" in text.lower()
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return []

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            async with session.get("https://pastebin.com") as resp:
                return resp.status == 200
        except Exception:
            return False
