"""SoundCloud adapter — stores chunks in track descriptions.

Uploads a silent audio stub and stores chunk data in the track
description field.

Notice: This adapter must only be used with SoundCloud accounts you
own and control.  Use in a manner that violates SoundCloud's Terms of
Service is outside the scope of this project and the responsibility
of the user.  No endorsement by or affiliation with SoundCloud Ltd.
is implied.

Requires: pip install picard-store[soundcloud]   (uses aiohttp)
"""

from __future__ import annotations

import io
import struct
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
import aiohttp

_API_BASE = "https://api.soundcloud.com"


def _silent_wav() -> bytes:
    """Generate a minimal silent WAV (44 bytes header + 2 bytes silence)."""
    num_channels = 1
    sample_rate = 8000
    bits_per_sample = 16
    data_size = 2
    byte_rate = sample_rate * num_channels * bits_per_sample // 8
    block_align = num_channels * bits_per_sample // 8

    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_size,
        b"WAVE",
        b"fmt ",
        16,
        1,
        num_channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
        b"data",
        data_size,
    )
    return header + b"\x00\x00"


class SoundCloudAdapter(Adapter):
    """Scatter chunks into SoundCloud track descriptions."""

    def __init__(
        self,
        client_id: str,
        oauth_token: str,
        *,
        max_chunk_bytes: int = 8000,
    ) -> None:
        self._client_id = client_id
        self._oauth_token = oauth_token
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
            name="soundcloud",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=15,
        )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"OAuth {self._oauth_token}"}

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        wav_data = _silent_wav()
        form = aiohttp.FormData()
        form.add_field("track[title]", f"pchk-{hash(encoded_chunk) & 0xFFFF:04x}")
        form.add_field("track[description]", encoded_chunk)
        form.add_field("track[sharing]", "private")
        form.add_field(
            "track[asset_data]",
            io.BytesIO(wav_data),
            filename="silence.wav",
            content_type="audio/wav",
        )

        session = await self._get_session()
        async with session.post(
            f"{_API_BASE}/tracks",
            headers=self._headers(),
            data=form,
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        loc = f"soundcloud://{data.get('user_id', '0')}/{data['id']}"
        return StoredChunkRef(adapter_name="soundcloud", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        track_id = ref.location.rsplit("/", 1)[-1]
        session = await self._get_session()
        async with session.get(
            f"{_API_BASE}/tracks/{track_id}",
            headers=self._headers(),
            params={"client_id": self._client_id},
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        return data.get("description", "")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        track_id = ref.location.rsplit("/", 1)[-1]
        try:
            session = await self._get_session()
            async with session.delete(
                f"{_API_BASE}/tracks/{track_id}",
                headers=self._headers(),
            ) as resp:
                return resp.status in (200, 204)
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs: list[StoredChunkRef] = []
        session = await self._get_session()
        async with session.get(
            f"{_API_BASE}/me/tracks",
            headers=self._headers(),
            params={"client_id": self._client_id, "limit": 200},
        ) as resp:
            resp.raise_for_status()
            tracks = await resp.json()
        for track in tracks:
            desc = track.get("description", "")
            if manifest_id in desc:
                refs.append(
                    StoredChunkRef(
                        adapter_name="soundcloud",
                        location=f"soundcloud://{track.get('user_id', '0')}/{track['id']}",
                    )
                )
        return refs

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            async with session.get(
                f"{_API_BASE}/me",
                headers=self._headers(),
            ) as resp:
                return resp.status == 200
        except Exception:
            return False
