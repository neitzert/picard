"""InfluxDB adapter -- stores chunks as time-series points.

Each chunk becomes a point in an InfluxDB v2 bucket.  The chunk data
lives in a field value, tagged by manifest/block/chunk index.

Requires: pip install picard[influxdb]  (aiohttp)
"""

from __future__ import annotations

import base64
import time
import uuid
from typing import Sequence
from urllib.parse import quote

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class InfluxdbAdapter(Adapter):
    """Scatter chunks into InfluxDB v2 as time-series points."""

    def __init__(
        self,
        url: str,
        token: str,
        org: str,
        bucket: str,
        *,
        measurement: str = "picard_chunks",
        max_chunk_bytes: int = 65535,
    ) -> None:
        self._url = url.rstrip("/")
        self._token = token
        self._org = org
        self._bucket = bucket
        self._measurement = measurement
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="influxdb",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=1000,
        )

    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Token {self._token}",
            "Content-Type": "text/plain; charset=utf-8",
        }

    def _line_protocol(
        self, manifest_id: str, block_idx: str, chunk_idx: str, data: str
    ) -> str:
        ts_ns = int(time.time() * 1e9)
        tags = (
            f"manifest_id={manifest_id},"
            f"block_index={block_idx},"
            f"chunk_index={chunk_idx}"
        )
        escaped_data = data.replace('"', '\\"')
        return (
            f"{self._measurement},{tags} "
            f'data="{escaped_data}" {ts_ns}'
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import aiohttp
        from picard.chunker import Chunk

        try:
            chunk = Chunk.decode(encoded_chunk)
            manifest_id = chunk.manifest_id
            block_idx = str(chunk.block_index)
            chunk_idx = str(chunk.chunk_index)
        except ValueError:
            manifest_id = uuid.uuid4().hex
            block_idx = "0"
            chunk_idx = "0"

        b64_data = base64.b64encode(encoded_chunk.encode()).decode()
        line = self._line_protocol(manifest_id, block_idx, chunk_idx, b64_data)

        url = (
            f"{self._url}/api/v2/write"
            f"?org={quote(self._org)}&bucket={quote(self._bucket)}"
            f"&precision=ns"
        )

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, data=line, headers=self._auth_headers()
            ) as resp:
                if resp.status not in (200, 204):
                    body = await resp.text()
                    raise IOError(f"influxdb write failed: {resp.status} {body}")

        location = f"influx://{self._bucket}/{manifest_id}/{block_idx}_{chunk_idx}"
        return StoredChunkRef(adapter_name="influxdb", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        import aiohttp

        parts = ref.location.removeprefix("influx://").split("/")
        manifest_id = parts[1]
        block_chunk = parts[2].split("_")
        block_idx = block_chunk[0]
        chunk_idx = block_chunk[1]

        flux_query = (
            f'from(bucket: "{self._bucket}")\n'
            f"  |> range(start: -30d)\n"
            f'  |> filter(fn: (r) => r._measurement == "{self._measurement}")\n'
            f'  |> filter(fn: (r) => r.manifest_id == "{manifest_id}")\n'
            f'  |> filter(fn: (r) => r.block_index == "{block_idx}")\n'
            f'  |> filter(fn: (r) => r.chunk_index == "{chunk_idx}")\n'
            f"  |> last()"
        )

        url = f"{self._url}/api/v2/query?org={quote(self._org)}"
        headers = self._auth_headers()
        headers["Content-Type"] = "application/vnd.flux"
        headers["Accept"] = "application/csv"

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, data=flux_query, headers=headers
            ) as resp:
                resp.raise_for_status()
                csv_body = await resp.text()
                b64_data = _extract_csv_value(csv_body)
                return base64.b64decode(b64_data).decode()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        import aiohttp

        parts = ref.location.removeprefix("influx://").split("/")
        manifest_id = parts[1]
        block_chunk = parts[2].split("_")
        block_idx = block_chunk[0]
        chunk_idx = block_chunk[1]

        url = f"{self._url}/api/v2/delete?org={quote(self._org)}&bucket={quote(self._bucket)}"
        body = {
            "start": "1970-01-01T00:00:00Z",
            "stop": "2100-01-01T00:00:00Z",
            "predicate": (
                f'_measurement="{self._measurement}" AND '
                f'manifest_id="{manifest_id}" AND '
                f'block_index="{block_idx}" AND '
                f'chunk_index="{chunk_idx}"'
            ),
        }
        headers = self._auth_headers()
        headers["Content-Type"] = "application/json"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=body, headers=headers) as resp:
                    return resp.status in (200, 204)
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        import aiohttp

        flux_query = (
            f'from(bucket: "{self._bucket}")\n'
            f"  |> range(start: -30d)\n"
            f'  |> filter(fn: (r) => r._measurement == "{self._measurement}")\n'
            f'  |> filter(fn: (r) => r.manifest_id == "{manifest_id}")\n'
            f'  |> keep(columns: ["block_index", "chunk_index"])\n'
            f"  |> distinct()"
        )

        url = f"{self._url}/api/v2/query?org={quote(self._org)}"
        headers = self._auth_headers()
        headers["Content-Type"] = "application/vnd.flux"
        headers["Accept"] = "application/csv"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, data=flux_query, headers=headers
                ) as resp:
                    resp.raise_for_status()
                    csv_body = await resp.text()
                    return _parse_chunk_list(csv_body, self._bucket, manifest_id)
        except Exception:
            return []

    async def health_check(self) -> bool:
        import aiohttp

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self._url}/health", headers=self._auth_headers()
                ) as resp:
                    return resp.status == 200
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        return {
            "used_bytes": None,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": None,
        }


def _extract_csv_value(csv_body: str) -> str:
    """Extract the _value field from InfluxDB CSV response."""
    lines = [l for l in csv_body.strip().splitlines() if l and not l.startswith("#")]
    if len(lines) < 2:
        raise KeyError("no data returned from influxdb query")
    headers = lines[0].split(",")
    values = lines[1].split(",")
    try:
        idx = headers.index("_value")
        return values[idx]
    except (ValueError, IndexError):
        raise KeyError("_value column not found in influxdb response")


def _parse_chunk_list(
    csv_body: str, bucket: str, manifest_id: str
) -> list[StoredChunkRef]:
    lines = [l for l in csv_body.strip().splitlines() if l and not l.startswith("#")]
    if len(lines) < 2:
        return []
    headers = lines[0].split(",")
    refs = []
    try:
        bi_idx = headers.index("block_index")
        ci_idx = headers.index("chunk_index")
    except ValueError:
        return []
    for row in lines[1:]:
        cols = row.split(",")
        if len(cols) > max(bi_idx, ci_idx):
            loc = f"influx://{bucket}/{manifest_id}/{cols[bi_idx]}_{cols[ci_idx]}"
            refs.append(StoredChunkRef(adapter_name="influxdb", location=loc))
    return refs
