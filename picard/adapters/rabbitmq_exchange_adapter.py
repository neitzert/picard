"""RabbitMQ exchange adapter -- stores chunks as custom arguments on named exchanges.

Each write_chunk declares a durable direct exchange picard.{token} with
x-picard-chunk in its arguments. Cold reads use the management HTTP API.

Requires: pip install aio-pika aiohttp
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "rabbitmq-exchange"


def _manifest_id(chunk: str) -> str:
    try:
        parts = chunk.split(":")
        return parts[2] if parts[0] == "PCHK" and len(parts) >= 3 else ""
    except Exception:
        return ""


class RabbitmqExchangeAdapter(Adapter):

    def __init__(self, *, host: str = "127.0.0.1", port: int = 5672,
                 user: str = "guest", password: str = "guest",
                 vhost: str = "/", mgmt_port: int = 15672) -> None:
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._vhost = vhost
        self._mgmt_port = mgmt_port
        self._conn = None
        self._reg: dict[str, str] = {}

    async def _get_conn(self):
        if self._conn is None or self._conn.is_closed:
            import aio_pika
            self._conn = await aio_pika.connect_robust(
                host=self._host, port=self._port,
                login=self._user, password=self._password,
                virtualhost=self._vhost,
            )
        return self._conn

    def _mgmt_url(self, path: str) -> str:
        import urllib.parse
        vhost_enc = urllib.parse.quote(self._vhost, safe="")
        return f"http://{self._host}:{self._mgmt_port}/api{path.format(vhost=vhost_enc)}"

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=True,
            ordered=False,
            tags={"requires": "aio-pika", "substrate": "rabbitmq-exchange-args"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import aio_pika
        token = uuid.uuid4().hex[:16]
        exch_name = f"picard.{token}"
        conn = await self._get_conn()
        async with conn.channel() as ch:
            await ch.declare_exchange(
                exch_name,
                type=aio_pika.ExchangeType.DIRECT,
                durable=True,
                arguments={"x-picard-chunk": encoded_chunk,
                           "x-picard-manifest": _manifest_id(encoded_chunk)},
            )
        self._reg[token] = encoded_chunk
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token,
                                                   "exchange": exch_name}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token]
        import aiohttp
        import urllib.parse
        vhost_enc = urllib.parse.quote(self._vhost, safe="")
        url = (f"http://{self._host}:{self._mgmt_port}/api/exchanges"
               f"/{vhost_enc}/{urllib.parse.quote(loc['exchange'], safe='')}")
        async with aiohttp.ClientSession() as session:
            async with session.get(url, auth=aiohttp.BasicAuth(self._user,
                                                                self._password)) as resp:
                if resp.status != 200:
                    raise KeyError(f"rabbitmq-exchange not found: {token}")
                data = await resp.json()
        chunk = data.get("arguments", {}).get("x-picard-chunk")
        if chunk is None:
            raise KeyError(f"rabbitmq-exchange x-picard-chunk missing: {token}")
        self._reg[token] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        conn = await self._get_conn()
        async with conn.channel() as ch:
            exch = await ch.get_exchange(loc["exchange"])
            await exch.delete()
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        for token, chunk in self._reg.items():
            if _manifest_id(chunk) == manifest_id:
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token,
                                         "exchange": f"picard.{token}"}),
                ))
        return refs

    async def health_check(self) -> bool:
        try:
            import aiohttp
            url = f"http://{self._host}:{self._mgmt_port}/api/healthchecks/node"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, auth=aiohttp.BasicAuth(self._user,
                                                                    self._password)) as resp:
                    if resp.status != 200:
                        return False
                    data = await resp.json()
            return data.get("status") == "ok"
        except Exception:
            return False

    async def close(self) -> None:
        if self._conn is not None and not self._conn.is_closed:
            await self._conn.close()
        self._conn = None
