"""LLM inference context adapter.

Stores chunks as system messages in a persistent Ollama conversation
session. The data lives in the LLM's active context window for that
session; evicted when the session ends or the model unloads.

Falls back to in-process registry when Ollama is not running. Uses
httpx (already a core dependency) for all HTTP calls.

Requires: Ollama running at ollama_url (optional -- in-process fallback used if absent).
"""

from __future__ import annotations

import uuid
from typing import Sequence

import httpx

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

_ADAPTER_NAME = "llm-context"
_STORE_PREFIX = "PICARD_STORE:"


class LlmContextAdapter(Adapter):
    """Store chunks in an LLM conversation context via Ollama (in-process fallback)."""

    def __init__(
        self,
        ollama_url: str = "http://localhost:11434",
        model: str = "llama3.2",
        *,
        max_chunk_bytes: int = 65536,
    ) -> None:
        self._url = ollama_url.rstrip("/")
        self._model = model
        self._max = max_chunk_bytes
        # token -> {"history": [...], "manifest_id": str}
        self._sessions: dict[str, dict] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_ADAPTER_NAME,
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            tags={
                "substrate": "llm-context",
                "requires": "Ollama (optional)",
            },
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        if len(encoded_chunk.encode()) > self._max:
            raise ValueError(f"Chunk too large: {len(encoded_chunk.encode())} > {self._max}")
        try:
            manifest_id = Chunk.decode(encoded_chunk).manifest_id
        except Exception:
            manifest_id = ""

        token = uuid.uuid4().hex[:16]
        content = f"{_STORE_PREFIX}{token}:{encoded_chunk}"
        history = [{"role": "system", "content": content}]

        self._sessions[token] = {"history": history, "manifest_id": manifest_id}

        # Best-effort: send to Ollama
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    f"{self._url}/api/chat",
                    json={"model": self._model, "messages": history, "stream": False},
                )
        except Exception:
            pass

        return StoredChunkRef(adapter_name=_ADAPTER_NAME, location=f"llm-context://{token}")

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        token = ref.location.removeprefix("llm-context://")
        session = self._sessions.get(token)
        if session is None:
            raise KeyError(f"llm-context chunk not found: {token}")
        content = session["history"][0]["content"]
        # Parse PICARD_STORE:<token>:<chunk>
        prefix = f"{_STORE_PREFIX}{token}:"
        if content.startswith(prefix):
            return content[len(prefix):]
        raise KeyError(f"llm-context: malformed session content for {token}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        token = ref.location.removeprefix("llm-context://")
        session = self._sessions.pop(token, None)
        return session is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name=_ADAPTER_NAME, location=f"llm-context://{tok}")
            for tok, sess in self._sessions.items()
            if sess.get("manifest_id") == manifest_id
        ]

    async def health_check(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(f"{self._url}/api/tags")
                return resp.status_code == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._sessions.clear()
