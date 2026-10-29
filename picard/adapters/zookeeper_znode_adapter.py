"""ZooKeeper znode adapter -- stores chunks as ZooKeeper znode data.

Each write_chunk creates an ephemeral znode at /picard/{token} holding the
encoded chunk as the node data. Cold reads fetch via znode path.

Requires: pip install kazoo
"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "zookeeper_znode"
_ROOT = "/picard"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class ZookeeperZnodeAdapter(Adapter):

    def __init__(self, *, hosts: str = "127.0.0.1:2181") -> None:
        self._hosts = hosts
        self._client = None

    def _get_client(self):
        if self._client is None:
            from kazoo.client import KazooClient
            self._client = KazooClient(hosts=self._hosts)
            self._client.start()
            self._client.ensure_path(_ROOT)
        return self._client

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=1_000_000,
            persistent=True,
            ordered=False,
            tags={"requires": "kazoo", "substrate": "zookeeper-znode"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        def _write():
            client = self._get_client()
            token = uuid.uuid4().hex[:16]
            manifest_id = _manifest_id_from_chunk(encoded_chunk)
            path = f"{_ROOT}/{token}"
            data = json.dumps({
                "chunk": encoded_chunk,
                "manifest": manifest_id,
            }).encode("utf-8")
            client.create(path, data)
            return token, path

        token, path = await asyncio.to_thread(_write)
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token, "path": path}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)

        def _read():
            client = self._get_client()
            data, _ = client.get(loc["path"])
            return json.loads(data.decode("utf-8"))["chunk"]

        return await asyncio.to_thread(_read)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)

        def _del():
            client = self._get_client()
            client.delete(loc["path"])

        await asyncio.to_thread(_del)
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        def _list():
            client = self._get_client()
            children = client.get_children(_ROOT)
            refs = []
            for token in children:
                path = f"{_ROOT}/{token}"
                try:
                    data, _ = client.get(path)
                    obj = json.loads(data.decode("utf-8"))
                    if obj.get("manifest") == manifest_id:
                        refs.append(StoredChunkRef(adapter_name=_NAME,
                                                   location=json.dumps({"token": token,
                                                    "path": path})))
                except Exception:
                    continue
            return refs

        return await asyncio.to_thread(_list)

    async def health_check(self) -> bool:
        try:
            def _hc():
                client = self._get_client()
                return client.state == "CONNECTED"

            return await asyncio.to_thread(_hc)
        except Exception:
            return False

    async def close(self) -> None:
        if self._client is not None:
            await asyncio.to_thread(self._client.stop)
            self._client = None
