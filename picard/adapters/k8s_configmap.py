"""Kubernetes ConfigMap adapter — stores chunks as ConfigMaps.

Each chunk becomes a ConfigMap in the target namespace. Blends in
with normal application configuration objects.

Requires: pip install picard[k8s]  (kubernetes-asyncio)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class K8sConfigMapAdapter(Adapter):
    """Scatter chunks into Kubernetes ConfigMaps."""

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
            name="k8s-configmap",
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

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        from kubernetes_asyncio import client

        try:
            chunk = Chunk.decode(encoded_chunk)
            name = f"picard-{chunk.manifest_id[:16]}-b{chunk.block_index}-c{chunk.chunk_index}"
            labels = {
                f"{self._label_prefix}/manifest": chunk.manifest_id,
                f"{self._label_prefix}/type": "chunk",
            }
        except ValueError:
            name = f"picard-{uuid.uuid4().hex[:16]}"
            labels = {f"{self._label_prefix}/type": "chunk"}

        cm = client.V1ConfigMap(
            metadata=client.V1ObjectMeta(name=name, labels=labels),
            data={"chunk": encoded_chunk},
        )

        api = await self._get_api()
        await api.create_namespaced_config_map(self._namespace, cm)

        location = f"k8s://{self._namespace}/configmap/{name}"
        return StoredChunkRef(adapter_name="k8s-configmap", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        name = ref.location.rsplit("/", 1)[-1]
        api = await self._get_api()
        cm = await api.read_namespaced_config_map(name, self._namespace)
        return cm.data["chunk"]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        name = ref.location.rsplit("/", 1)[-1]
        api = await self._get_api()
        try:
            await api.delete_namespaced_config_map(name, self._namespace)
            return True
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        api = await self._get_api()
        label_sel = f"{self._label_prefix}/manifest={manifest_id}"
        resp = await api.list_namespaced_config_map(
            self._namespace, label_selector=label_sel
        )
        return [
            StoredChunkRef(
                adapter_name="k8s-configmap",
                location=f"k8s://{self._namespace}/configmap/{cm.metadata.name}",
            )
            for cm in resp.items
        ]

    async def health_check(self) -> bool:
        try:
            api = await self._get_api()
            await api.list_namespaced_config_map(self._namespace, limit=1)
            return True
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        try:
            api = await self._get_api()
            label_sel = f"{self._label_prefix}/type=chunk"
            resp = await api.list_namespaced_config_map(
                self._namespace, label_selector=label_sel
            )
            count = len(resp.items)
            used = sum(
                len(cm.data.get("chunk", "").encode())
                for cm in resp.items
                if cm.data
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
