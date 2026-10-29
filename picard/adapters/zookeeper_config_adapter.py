"""ZooKeeper config adapter -- stores chunks under /picard_cfg/ znodes.

Each write_chunk creates a persistent znode at /picard_cfg/{token}
containing the JSON-encoded chunk and manifest ID. Cold reads fetch the
znode data directly.

Requires: pip install kazoo
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "zookeeper-config"
_ZK_ROOT = "/picard_cfg"


def _manifest_id(chunk: str) -> str:
    try:
        parts = chunk.split(":")
        return parts[2] if parts[0] == "PCHK" and len(parts) >= 3 else ""
    except Exception:
        return ""


class ZookeeperConfigAdapter(Adapter):

    def __init__(self, *, hosts: str = "127.0.0.1:2181",
                 timeout: float = 10.0) -> None:
        self._hosts = hosts
        self._timeout = timeout
        self._client = None
        self._reg: dict[str, str] = {}

    def _get_client(self):
        if self._client is None:
            from kazoo.client import KazooClient
            self._client = KazooClient(hosts=self._hosts, timeout=self._timeout)
            self._client.start()
            self._client.ensure_path(_ZK_ROOT)
        return self._client

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=1_000_000,
            persistent=True,
            ordered=False,
            tags={"requires": "kazoo", "substrate": "zookeeper-config-znode"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import asyncio
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id(encoded_chunk)
        path = f"{_ZK_ROOT}/{token}"
        payload = json.dumps({"chunk": encoded_chunk, "manifest": manifest_id})
        await asyncio.to_thread(
            self._get_client().create, path, payload.encode("utf-8"),
            makepath=True,
        )
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "path": path,
                                  "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        import asyncio
        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token]
        raw, _ = await asyncio.to_thread(self._get_client().get, loc["path"])
        obj = json.loads(raw.decode("utf-8"))
        chunk = obj["chunk"]
        self._reg[token] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        import asyncio
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        try:
            await asyncio.to_thread(self._get_client().delete, loc["path"])
        except Exception:
            pass
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        import asyncio
        client = self._get_client()
        children = await asyncio.to_thread(client.get_children, _ZK_ROOT)
        refs = []
        for token in children:
            path = f"{_ZK_ROOT}/{token}"
            try:
                raw, _ = await asyncio.to_thread(client.get, path)
                obj = json.loads(raw.decode("utf-8"))
                if obj.get("manifest") == manifest_id:
                    refs.append(StoredChunkRef(
                        adapter_name=_NAME,
                        location=json.dumps({"token": token, "path": path,
                                              "manifest": manifest_id}),
                    ))
            except Exception:
                continue
        return refs

    async def health_check(self) -> bool:
        try:
            client = self._get_client()
            return client.connected
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._client is not None:
            self._client.stop()
            self._client = None
