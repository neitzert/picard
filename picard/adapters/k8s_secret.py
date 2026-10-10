"""Kubernetes Secret adapter — stores chunks as K8s Secrets.

Each chunk becomes a Secret object in the target namespace with the
chunk data base64-encoded in the data field.

Requires: pip install picard[k8s]  (kubernetes-asyncio)
"""

from __future__ import annotations

import base64
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class K8sSecretAdapter(Adapter):
    """Scatter chunks into Kubernetes Secrets."""

    def __init__(
        self,
        *,
        namespace: str = "default",
        label_prefix: str = "picard.io",
        kubeconfig: str | None = None,
        max_chunk_bytes: int = 1_048_576,
    ) -> None:
        self._namespace = namespace
        self._label_prefix = label_prefix
        self._kubeconfig = kubeconfig
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="k8s-secret",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=100,
        )

    async def _get_api(self):
        from kubernetes_asyncio import client, config

        if self._kubeconfig:
            await config.load_kube_config(config_file=self._kubeconfig)
        else:
            try:
                config.load_incluster_config()
            except config.ConfigException:
                await config.load_kube_config()
        return client.CoreV1Api()

    def _secret_name(self, encoded_chunk: str) -> str:
        try:
            chunk = Chunk.decode(encoded_chunk)
            return f"picard-{chunk.manifest_id[:16]}-b{chunk.block_index}-c{chunk.chunk_index}"
        except ValueError:
            return f"picard-{uuid.uuid4().hex[:16]}"

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        name = self._secret_name(encoded_chunk)
        b64_data = base64.b64encode(encoded_chunk.encode()).decode()

        try:
            chunk = Chunk.decode(encoded_chunk)
            labels = {
                f"{self._label_prefix}/manifest": chunk.manifest_id,
                f"{self._label_prefix}/type": "chunk",
            }
        except ValueError:
            labels = {f"{self._label_prefix}/type": "chunk"}

        api = await self._get_api()
        from kubernetes_asyncio import client

        secret = client.V1Secret(
            metadata=client.V1ObjectMeta(name=name, labels=labels),
            data={"chunk": b64_data},
            type="Opaque",
        )
        await api.create_namespaced_secret(self._namespace, secret)

        location = f"k8s://{self._namespace}/secret/{name}"
        return StoredChunkRef(adapter_name="k8s-secret", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        name = ref.location.rsplit("/", 1)[-1]
        api = await self._get_api()
        secret = await api.read_namespaced_secret(name, self._namespace)
        raw = base64.b64decode(secret.data["chunk"])
        return raw.decode()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        name = ref.location.rsplit("/", 1)[-1]
        api = await self._get_api()
        try:
            await api.delete_namespaced_secret(name, self._namespace)
            return True
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        api = await self._get_api()
        label_sel = f"{self._label_prefix}/manifest={manifest_id}"
        resp = await api.list_namespaced_secret(
            self._namespace, label_selector=label_sel
        )
        return [
            StoredChunkRef(
                adapter_name="k8s-secret",
                location=f"k8s://{self._namespace}/secret/{s.metadata.name}",
            )
            for s in resp.items
        ]

    async def health_check(self) -> bool:
        try:
            api = await self._get_api()
            await api.list_namespace(limit=1)
            return True
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        try:
            api = await self._get_api()
            label_sel = f"{self._label_prefix}/type=chunk"
            resp = await api.list_namespaced_secret(
                self._namespace, label_selector=label_sel
            )
            count = len(resp.items)
            used = sum(
                len(base64.b64decode(s.data.get("chunk", "")))
                for s in resp.items
                if s.data
            )
            return {
                "used_bytes": used,
                "free_bytes": None,
                "total_bytes": None,
                "chunk_count": count,
            }
        except Exception:
            return {
                "used_bytes": None,
                "free_bytes": None,
                "total_bytes": None,
                "chunk_count": None,
            }
