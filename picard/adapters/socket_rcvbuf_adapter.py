"""Socket receive buffer adapter -- stores chunks as pending UDP datagrams.

Each write_chunk creates a UDP loopback socket pair. The encoded chunk
is sent to the receiver socket. The datagram sits in the kernel receive
buffer until read_chunk consumes and re-sends it.

Cross-platform. No root required.
"""
from __future__ import annotations

import asyncio
import json
import socket
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "socket_rcvbuf"


class SocketRcvbufAdapter(Adapter):

    def __init__(self) -> None:
        self._reg: dict[str, tuple[socket.socket, socket.socket, str]] = {}
        # token -> (sender, receiver, encoded_chunk)

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65507,
            persistent=False,
            ordered=False,
            tags={"requires": "stdlib", "substrate": "udp-socket-receive-buffer"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        def _write():
            token = uuid.uuid4().hex[:16]
            # Receiver: bind to any port on loopback
            recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            recv_sock.bind(("127.0.0.1", 0))
            port = recv_sock.getsockname()[1]
            # Sender: send to the receiver
            send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            send_sock.sendto(encoded_chunk.encode("utf-8"), ("127.0.0.1", port))
            return token, send_sock, recv_sock

        token, send_sock, recv_sock = await asyncio.to_thread(_write)
        self._reg[token] = (send_sock, recv_sock, encoded_chunk)
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        token = json.loads(ref.location)["token"]
        if token not in self._reg:
            raise KeyError(f"socket chunk not in registry: {token}")
        send_sock, recv_sock, _ = self._reg[token]

        def _read():
            recv_sock.settimeout(2.0)
            data, _ = recv_sock.recvfrom(65507)
            # Immediately re-send so data persists in buffer
            port = recv_sock.getsockname()[1]
            send_sock.sendto(data, ("127.0.0.1", port))
            return data.decode("utf-8")

        return await asyncio.to_thread(_read)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        token = json.loads(ref.location)["token"]
        entry = self._reg.pop(token, None)
        if entry:
            for s in (entry[0], entry[1]):
                try:
                    s.close()
                except Exception:
                    pass
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        from picard.chunker import Chunk
        refs = []
        for token, (_, _, data) in self._reg.items():
            try:
                if Chunk.decode(data).manifest_id == manifest_id:
                    refs.append(StoredChunkRef(adapter_name=_NAME,
                                               location=json.dumps({"token": token})))
            except Exception:
                continue
        return refs

    async def health_check(self) -> bool:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            s2 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s2.sendto(b"test", ("127.0.0.1", port))
            s.settimeout(1.0)
            data, _ = s.recvfrom(16)
            s.close(); s2.close()
            return data == b"test"
        except Exception:
            return False

    async def close(self) -> None:
        for token in list(self._reg):
            entry = self._reg.pop(token, None)
            if entry:
                for s in (entry[0], entry[1]):
                    try:
                        s.close()
                    except Exception:
                        pass
