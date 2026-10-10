"""Docker Labels adapter — stores chunks as container/image labels.

Stores data in Docker metadata. Labels are arbitrary key-value pairs
attached to containers and images.

Requires: pip install picard[docker]  (aiodocker)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class DockerLabelsAdapter(Adapter):
    """Scatter chunks into Docker container labels."""

    def __init__(
        self,
        *,
        docker_host: str | None = None,
        image_prefix: str = "picard/chunk",
        max_chunk_bytes: int = 65_536,
    ) -> None:
        self._docker_host = docker_host
        self._image_prefix = image_prefix
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="docker-labels",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=60,
        )

    async def _get_client(self):
        import aiodocker

        kwargs = {}
        if self._docker_host:
            kwargs["url"] = self._docker_host
        return aiodocker.Docker(**kwargs)

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        try:
            chunk = Chunk.decode(encoded_chunk)
            name = f"{self._image_prefix}-{chunk.manifest_id[:12]}-b{chunk.block_index}-c{chunk.chunk_index}"
            labels = {
                "picard.manifest": chunk.manifest_id,
                "picard.block": str(chunk.block_index),
                "picard.chunk": str(chunk.chunk_index),
                "picard.data": encoded_chunk,
            }
        except ValueError:
            uid = uuid.uuid4().hex[:12]
            name = f"{self._image_prefix}-{uid}"
            labels = {"picard.data": encoded_chunk}

        docker = await self._get_client()
        try:
            container = await docker.containers.create_or_replace(
                name=name,
                config={
                    "Image": "alpine:latest",
                    "Cmd": ["true"],
                    "Labels": labels,
                },
            )
            container_id = container.id
        finally:
            await docker.close()

        location = f"docker://{container_id}/labels/picard.data"
        return StoredChunkRef(adapter_name="docker-labels", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        parts = ref.location.split("/")
        container_id = parts[2]

        docker = await self._get_client()
        try:
            container = await docker.containers.get(container_id)
            info = await container.show()
            labels = info.get("Config", {}).get("Labels", {})
            data = labels.get("picard.data")
            if data is None:
                raise KeyError(f"no picard.data label on {container_id}")
            return data
        finally:
            await docker.close()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        parts = ref.location.split("/")
        container_id = parts[2]

        docker = await self._get_client()
        try:
            container = await docker.containers.get(container_id)
            await container.delete(force=True)
            return True
        except Exception:
            return False
        finally:
            await docker.close()

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        docker = await self._get_client()
        try:
            containers = await docker.containers.list(
                all=True,
                filters={"label": [f"picard.manifest={manifest_id}"]},
            )
            refs: list[StoredChunkRef] = []
            for c in containers:
                cid = c.id if hasattr(c, "id") else c["Id"]
                refs.append(
                    StoredChunkRef(
                        adapter_name="docker-labels",
                        location=f"docker://{cid}/labels/picard.data",
                    )
                )
            return refs
        finally:
            await docker.close()

    async def health_check(self) -> bool:
        docker = await self._get_client()
        try:
            await docker.ping()
            return True
        except Exception:
            return False
        finally:
            await docker.close()
