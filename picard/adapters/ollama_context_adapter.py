"""Ollama context adapter -- stores chunks as custom Ollama model system prompts.

Each write_chunk creates a named Ollama model via the /api/create endpoint,
embedding the encoded chunk as the SYSTEM prompt in a Modelfile. The model
persists in Ollama's model storage until deleted via /api/delete.

Requires: a running Ollama instance and a base model (default: llama3.2).
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "ollama-context"
_BASE_MODEL = "llama3.2:3b"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class OllamaContextAdapter(Adapter):

    def __init__(self, *, url: str = "http://127.0.0.1:11434",
                 base_model: str = _BASE_MODEL) -> None:
        self._url = url.rstrip("/")
        self._base_model = base_model
        self._reg: dict[str, str] = {}
        self._session = None

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession()
        return self._session

    def _model_name(self, token: str) -> str:
        return f"picard-{token[:12]}"

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=4096,
            persistent=True,
            ordered=False,
            ttl_seconds=None,
            tags={"requires": "ollama", "substrate": "ollama-modelfile-system"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        model_name = self._model_name(token)
        modelfile = (
            f"FROM {self._base_model}\n"
            f"SYSTEM {encoded_chunk}\n"
            f"# picard manifest:{manifest_id}\n"
        )
        session = await self._get_session()
        async with session.post(
            f"{self._url}/api/create",
            json={"model": model_name, "from": self._base_model,
                  "system": encoded_chunk, "stream": False},
        ) as resp:
            resp.raise_for_status()
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "model": model_name,
                                  "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token]
        session = await self._get_session()
        async with session.post(
            f"{self._url}/api/show",
            json={"model": loc["model"]},
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        # New Ollama API returns system under model_info or directly
        chunk = (data.get("system") or
                 data.get("parameters", {}).get("system", "") or
                 "")
        if not chunk:
            mf = data.get("modelfile", "")
            for line in mf.splitlines():
                if line.startswith("SYSTEM "):
                    chunk = line[len("SYSTEM "):]
                    break
        if not chunk:
            raise KeyError(f"ollama-context chunk not found in model: {loc['model']}")
        self._reg[token] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        async with session.delete(
            f"{self._url}/api/delete",
            json={"model": loc["model"]},
        ) as resp:
            resp.raise_for_status()
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        session = await self._get_session()
        async with session.get(f"{self._url}/api/tags") as resp:
            resp.raise_for_status()
            data = await resp.json()
        refs = []
        for model in data.get("models", []):
            name = model.get("name", "")
            if not name.startswith("picard-"):
                continue
            token = name[len("picard-"):]
            if token in self._reg:
                if _manifest_id_from_chunk(self._reg[token]) == manifest_id:
                    refs.append(StoredChunkRef(
                        adapter_name=_NAME,
                        location=json.dumps({"token": token, "model": name,
                                              "manifest": manifest_id}),
                    ))
        return refs

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            async with session.get(f"{self._url}/api/tags") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
