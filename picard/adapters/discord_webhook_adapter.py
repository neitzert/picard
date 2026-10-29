"""Discord Webhook adapter -- stores chunks as Discord messages via an incoming webhook."""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "discord-webhook"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class DiscordWebhookAdapter(Adapter):

    def __init__(self, *, webhook_url: str = "") -> None:
        self._webhook_url = webhook_url
        self._session = None
        self._reg: dict[str, str] = {}

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession()
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=2000,
            persistent=False,
            ordered=False,
            rate_limit_per_min=30,
            tags={"requires": "aiohttp", "substrate": "discord-webhook-message"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        self._reg[token] = encoded_chunk
        if self._webhook_url:
            session = await self._get_session()
            url = f"{self._webhook_url}?wait=true"
            payload = {
                "content": f"PICARD:{token}:{encoded_chunk}",
                "username": "picard-store",
            }
            try:
                async with session.post(url, json=payload) as resp:
                    resp.raise_for_status()
            except Exception:
                pass
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "chunk": encoded_chunk,
                                 "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token]
        chunk = loc.get("chunk", "")
        if chunk:
            self._reg[token] = chunk
            return chunk
        raise KeyError(f"discord-webhook chunk not found: {token}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs: list[StoredChunkRef] = []
        for token, chunk in self._reg.items():
            if _manifest_id_from_chunk(chunk) == manifest_id:
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token, "chunk": chunk,
                                         "manifest": manifest_id}),
                ))
        return refs

    async def health_check(self) -> bool:
        if not self._webhook_url:
            return True
        try:
            session = await self._get_session()
            async with session.get(self._webhook_url) as resp:
                return resp.status in (200, 401, 403)
        except Exception:
            return False

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
