"""JWT adapter — chunks hidden in JWT claims.

Embeds chunk data as custom claims in signed JWTs, then stores them
in an HTTP endpoint (or local cache). Chunk data travels as
standard bearer tokens.

Requires: pip install picard[jwt]  (PyJWT)
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class JwtAdapter(Adapter):
    """Store chunks as custom claims inside signed JWTs."""

    def __init__(
        self,
        secret: str = "picard-default-secret",
        algorithm: str = "HS256",
        issuer: str = "picard",
        audience: str = "picard-receiver",
        endpoint: str | None = None,
        *,
        max_chunk_bytes: int = 4096,
    ) -> None:
        self._secret = secret
        self._algorithm = algorithm
        self._issuer = issuer
        self._audience = audience
        self._endpoint = endpoint
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="jwt",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=120,
            ttl_seconds=3600,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import jwt as pyjwt
        import time

        try:
            chunk = Chunk.decode(encoded_chunk)
            chunk_key = f"{chunk.manifest_id}_{chunk.block_index}_{chunk.chunk_index}"
        except ValueError:
            chunk_key = uuid.uuid4().hex

        now = int(time.time())
        payload = {
            "iss": self._issuer,
            "aud": self._audience,
            "iat": now,
            "exp": now + 3600,
            "jti": chunk_key,
            "picard_data": base64.b64encode(encoded_chunk.encode()).decode(),
        }
        token = pyjwt.encode(payload, self._secret, algorithm=self._algorithm)

        if self._endpoint:
            def _post():
                import urllib.request
                req = urllib.request.Request(
                    self._endpoint,
                    data=token.encode(),
                    headers={"Content-Type": "application/jwt"},
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=10)
            try:
                await asyncio.to_thread(_post)
            except Exception:
                pass

        self._cache[chunk_key] = token
        return StoredChunkRef(adapter_name="jwt", location=chunk_key)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        import jwt as pyjwt

        token = self._cache.get(ref.location)
        if token is None:
            raise KeyError(f"JWT not in cache: {ref.location}")
        payload = pyjwt.decode(
            token,
            self._secret,
            algorithms=[self._algorithm],
            audience=self._audience,
        )
        return base64.b64decode(payload["picard_data"]).decode()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return self._cache.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        prefix = f"{manifest_id}_"
        return [
            StoredChunkRef(adapter_name="jwt", location=k)
            for k in self._cache
            if k.startswith(prefix)
        ]

    async def health_check(self) -> bool:
        try:
            import jwt as pyjwt  # noqa: F811
            return True
        except ImportError:
            return False

    async def capacity(self) -> dict[str, int | None]:
        used = sum(len(v.encode()) for v in self._cache.values())
        return {
            "used_bytes": used,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": len(self._cache),
        }
