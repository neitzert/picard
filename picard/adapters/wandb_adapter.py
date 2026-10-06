"""Weights & Biases adapter — stores chunks as W&B artifacts.

Chunks are uploaded as files within W&B artifact versions.
Blends in perfectly with ML experiment tracking workflows.

Requires: pip install picard[wandb]  (aiohttp)
"""

from __future__ import annotations

import uuid
from typing import Sequence

import aiohttp

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

_WANDB_API = "https://api.wandb.ai"


class WandbAdapter(Adapter):
    """Scatter chunks into Weights & Biases artifacts."""

    def __init__(
        self,
        api_key: str,
        *,
        entity: str = "default",
        project: str = "picard-data",
        artifact_type: str = "dataset",
        max_chunk_bytes: int = 10485760,
    ) -> None:
        self._api_key = api_key
        self._entity = entity
        self._project = project
        self._artifact_type = artifact_type
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="wandb",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=30,
        )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        try:
            chunk = Chunk.decode(encoded_chunk)
            key = f"{chunk.manifest_id}/{chunk.block_index}_{chunk.chunk_index}"
        except ValueError:
            key = uuid.uuid4().hex

        artifact_name = f"chunk-{key.replace('/', '-')}"

        async with aiohttp.ClientSession() as session:
            resp = await session.post(
                f"{_WANDB_API}/graphql",
                headers=self._headers(),
                json={
                    "query": """
                        mutation CreateArtifact($entityName: String!, $projectName: String!,
                                               $artifactTypeName: String!, $artifactCollectionName: String!,
                                               $description: String) {
                            createArtifact(input: {
                                entityName: $entityName,
                                projectName: $projectName,
                                artifactTypeName: $artifactTypeName,
                                artifactCollectionName: $artifactCollectionName,
                                description: $description
                            }) { artifact { id } }
                        }
                    """,
                    "variables": {
                        "entityName": self._entity,
                        "projectName": self._project,
                        "artifactTypeName": self._artifact_type,
                        "artifactCollectionName": artifact_name,
                        "description": encoded_chunk[:4096],
                    },
                },
            )
            resp.raise_for_status()
            data = await resp.json()

        artifact_id = data.get("data", {}).get("createArtifact", {}).get("artifact", {}).get("id", key)
        loc = f"wandb://{self._entity}/{self._project}/{artifact_id}"
        self._cache[loc] = encoded_chunk
        return StoredChunkRef(adapter_name="wandb", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location in self._cache:
            return self._cache[ref.location]

        parts = ref.location.replace("wandb://", "").split("/")
        artifact_id = parts[-1] if parts else ref.location

        async with aiohttp.ClientSession() as session:
            resp = await session.post(
                f"{_WANDB_API}/graphql",
                headers=self._headers(),
                json={
                    "query": """
                        query GetArtifact($id: ID!) {
                            artifact(id: $id) { description }
                        }
                    """,
                    "variables": {"id": artifact_id},
                },
            )
            resp.raise_for_status()
            data = await resp.json()

        return data["data"]["artifact"]["description"]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        self._cache.pop(ref.location, None)
        parts = ref.location.replace("wandb://", "").split("/")
        artifact_id = parts[-1] if parts else ref.location

        try:
            async with aiohttp.ClientSession() as session:
                resp = await session.post(
                    f"{_WANDB_API}/graphql",
                    headers=self._headers(),
                    json={
                        "query": """
                            mutation DeleteArtifact($id: ID!) {
                                deleteArtifact(input: {artifactID: $id}) { success }
                            }
                        """,
                        "variables": {"id": artifact_id},
                    },
                )
                resp.raise_for_status()
            return True
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="wandb", location=loc)
            for loc in self._cache
            if manifest_id in loc
        ]

    async def health_check(self) -> bool:
        try:
            async with aiohttp.ClientSession() as session:
                resp = await session.get(
                    f"{_WANDB_API}/healthz",
                    headers=self._headers(),
                )
                return resp.status == 200
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        return {
            "used_bytes": sum(len(v.encode()) for v in self._cache.values()),
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": len(self._cache),
        }
