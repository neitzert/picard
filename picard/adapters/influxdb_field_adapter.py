"""InfluxDB v1 field adapter -- stores chunks via InfluxDB v1 line protocol.

Each write_chunk writes a point with measurement "picard_chunks" and a field
"data" containing the encoded chunk. Reads use the v1 query API (/query) with
InfluxQL. Different from influxdb_adapter.py which targets InfluxDB v2 API.

Requires: pip install aiohttp
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "influxdb-field"
_MEASUREMENT = "picard_chunks"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


def _escape_tag(value: str) -> str:
    return (value.replace(",", r"\,").replace("=", r"\=")
            .replace(" ", r"\ ").replace("\n", "\\n").replace("\r", "\\r"))


def _escape_field(value: str) -> str:
    return (value.replace("\\", "\\\\").replace('"', '\\"')
            .replace("\n", "\\n").replace("\r", "\\r"))


class InfluxdbFieldAdapter(Adapter):

    def __init__(
        self,
        *,
        url: str = "http://127.0.0.1:8086",
        database: str = "picard",
        username: str = "",
        password: str = "",
        retention_policy: str = "autogen",
    ) -> None:
        self._url = url.rstrip("/")
        self._database = database
        self._username = username
        self._password = password
        self._rp = retention_policy
        self._session = None
        self._reg: dict[str, str] = {}

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            auth = None
            if self._username:
                auth = aiohttp.BasicAuth(self._username, self._password)
            self._session = aiohttp.ClientSession(auth=auth)
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=True,
            ordered=False,
            tags={"requires": "aiohttp", "substrate": "influxdb-v1-field"},
        )

    async def _ensure_db(self) -> None:
        session = await self._get_session()
        async with session.post(
            f"{self._url}/query",
            params={"q": f"CREATE DATABASE {self._database}"},
        ) as resp:
            resp.raise_for_status()

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        ts_ns = time.time_ns()

        await self._ensure_db()

        # InfluxDB v1 line protocol: measurement,tag=val field="val" timestamp_ns
        line = (
            f'{_MEASUREMENT},'
            f'token={_escape_tag(token)},'
            f'manifest={_escape_tag(manifest_id)} '
            f'data="{_escape_field(encoded_chunk)}" '
            f'{ts_ns}'
        )
        session = await self._get_session()
        async with session.post(
            f"{self._url}/write",
            params={"db": self._database, "rp": self._rp, "precision": "ns"},
            data=line.encode(),
            headers={"Content-Type": "application/octet-stream"},
        ) as resp:
            resp.raise_for_status()

        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "ts_ns": ts_ns,
                                 "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token]

        q = (
            f"SELECT data FROM {_MEASUREMENT} "
            f"WHERE token='{token}' LIMIT 1"
        )
        session = await self._get_session()
        async with session.get(
            f"{self._url}/query",
            params={"db": self._database, "q": q},
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()

        try:
            chunk = data["results"][0]["series"][0]["values"][0][1]
        except (KeyError, IndexError, TypeError):
            raise KeyError(f"influxdb-field chunk not found: {token}")

        self._reg[token] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        token = loc["token"]
        self._reg.pop(token, None)

        q = f"DELETE FROM {_MEASUREMENT} WHERE token='{token}'"
        try:
            session = await self._get_session()
            async with session.post(
                f"{self._url}/query",
                params={"db": self._database, "q": q},
            ) as resp:
                return resp.status in (200, 204)
        except Exception:
            return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs: list[StoredChunkRef] = []

        for token, chunk in self._reg.items():
            if _manifest_id_from_chunk(chunk) == manifest_id:
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token, "ts_ns": 0,
                                         "manifest": manifest_id}),
                ))

        try:
            q = (
                f"SELECT token FROM {_MEASUREMENT} "
                f"WHERE manifest='{manifest_id}'"
            )
            session = await self._get_session()
            async with session.get(
                f"{self._url}/query",
                params={"db": self._database, "q": q},
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    tokens_seen = {json.loads(r.location)["token"] for r in refs}
                    try:
                        for row in data["results"][0]["series"][0]["values"]:
                            tok = row[1]
                            if tok not in tokens_seen:
                                refs.append(StoredChunkRef(
                                    adapter_name=_NAME,
                                    location=json.dumps({"token": tok, "ts_ns": 0,
                                                         "manifest": manifest_id}),
                                ))
                    except (KeyError, IndexError, TypeError):
                        pass
        except Exception:
            pass

        return refs

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            async with session.get(f"{self._url}/ping") as resp:
                return resp.status in (200, 204)
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
