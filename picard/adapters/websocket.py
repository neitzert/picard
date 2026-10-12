"""WebSocket adapter — stores chunks via a cooperating WebSocket server.

Chunks are transmitted as JSON commands over a persistent WebSocket
connection. The server implements a simple key-value store protocol.

Requires: pip install picard[websocket]  (websockets)
"""

from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class WebSocketAdapter(Adapter):
    """Scatter chunks over a WebSocket key-value protocol."""

    def __init__(
        self,
        url: str,
        auth_token: str | None = None,
        *,
        max_chunk_bytes: int = 60_000,
    ) -> None:
        self._url = url
        self._auth_token = auth_token
        self._max = max_chunk_bytes
        self._ws: object | None = None

    async def _ensure_connection(self):
        if self._ws is not None:
            return self._ws
        import websockets

        headers = {}
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"
        self._ws = await websockets.connect(self._url, extra_headers=headers)
        return self._ws

    async def _send_command(self, cmd: dict) -> dict:
        try:
            ws = await self._ensure_connection()
            await ws.send(json.dumps(cmd))
            resp = await ws.recv()
            return json.loads(resp)
        except Exception:
            self._ws = None
            raise

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="websocket",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=120,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        msg_id = uuid.uuid4().hex
        resp = await self._send_command({
            "op": "write",
            "id": msg_id,
            "data": encoded_chunk,
        })
        if not resp.get("ok"):
            raise RuntimeError(f"WebSocket write failed: {resp}")
        return StoredChunkRef(adapter_name="websocket", location=msg_id)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        resp = await self._send_command({
            "op": "read",
            "id": ref.location,
        })
        if not resp.get("ok"):
            raise RuntimeError(f"WebSocket read failed: {resp}")
        return resp["data"]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        try:
            resp = await self._send_command({
                "op": "delete",
                "id": ref.location,
            })
            return bool(resp.get("ok"))
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        resp = await self._send_command({
            "op": "list",
            "manifest": manifest_id,
        })
        if not resp.get("ok"):
            return []
        return [
            StoredChunkRef(adapter_name="websocket", location=mid)
            for mid in resp.get("ids", [])
        ]

    async def health_check(self) -> bool:
        try:
            resp = await self._send_command({"op": "ping"})
            return bool(resp.get("ok"))
        except Exception:
            return False
