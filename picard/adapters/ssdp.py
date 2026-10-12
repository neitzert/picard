"""SSDP/UPnP adapter — carries chunks in SSDP NOTIFY messages.

Each chunk is broadcast as a NOTIFY message on the SSDP multicast
group (239.255.255.250:1900) with the encoded chunk data in a custom
X-PCHK-DATA header.  A cooperating listener collects them.

No external dependencies — uses raw asyncio UDP multicast.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_SSDP_ADDR = "239.255.255.250"
_SSDP_PORT = 1900


class SsdpAdapter(Adapter):
    """Scatter chunks into SSDP NOTIFY messages on the local network."""

    def __init__(
        self,
        *,
        interface: str = "0.0.0.0",
        max_chunk_bytes: int = 800,
    ) -> None:
        self._interface = interface
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="ssdp",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=20,
            ttl_seconds=1800,
        )

    @staticmethod
    def _build_notify(msg_id: str, encoded_chunk: str) -> str:
        return (
            "NOTIFY * HTTP/1.1\r\n"
            f"HOST: {_SSDP_ADDR}:{_SSDP_PORT}\r\n"
            "CACHE-CONTROL: max-age=1800\r\n"
            f"LOCATION: http://picard/{msg_id}\r\n"
            "NT: urn:picard:chunk:1\r\n"
            "NTS: ssdp:alive\r\n"
            f"USN: uuid:{msg_id}::urn:picard:chunk:1\r\n"
            f"X-PCHK-DATA: {encoded_chunk}\r\n"
            "\r\n"
        )

    @staticmethod
    def _build_byebye(msg_id: str) -> str:
        return (
            "NOTIFY * HTTP/1.1\r\n"
            f"HOST: {_SSDP_ADDR}:{_SSDP_PORT}\r\n"
            f"LOCATION: http://picard/{msg_id}\r\n"
            "NT: urn:picard:chunk:1\r\n"
            "NTS: ssdp:byebye\r\n"
            f"USN: uuid:{msg_id}::urn:picard:chunk:1\r\n"
            "\r\n"
        )

    @staticmethod
    def _build_msearch(search_target: str) -> str:
        return (
            "M-SEARCH * HTTP/1.1\r\n"
            f"HOST: {_SSDP_ADDR}:{_SSDP_PORT}\r\n"
            'MAN: "ssdp:discover"\r\n'
            "MX: 2\r\n"
            f"ST: {search_target}\r\n"
            "\r\n"
        )

    @staticmethod
    def _parse_header(response: str, header_name: str) -> str | None:
        for line in response.split("\r\n"):
            if line.lower().startswith(header_name.lower() + ":"):
                return line.split(":", 1)[1].strip()
        return None

    @staticmethod
    def _parse_usn_id(usn: str) -> str | None:
        if usn and usn.startswith("uuid:"):
            parts = usn.split("::", 1)
            return parts[0].removeprefix("uuid:")
        return None

    async def _send_recv(self, message: str) -> str:
        loop = asyncio.get_event_loop()
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: _SsdpProtocol(),
            local_addr=(self._interface, 0),
            family=2,
        )
        try:
            transport.sendto(message.encode("utf-8"), (_SSDP_ADDR, _SSDP_PORT))
            return await asyncio.wait_for(protocol.response, timeout=3.0)
        except asyncio.TimeoutError:
            return ""
        finally:
            transport.close()

    async def _send_recv_all(self, message: str, timeout: float = 3.0) -> list[str]:
        loop = asyncio.get_event_loop()
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: _SsdpMultiProtocol(),
            local_addr=(self._interface, 0),
            family=2,
        )
        try:
            transport.sendto(message.encode("utf-8"), (_SSDP_ADDR, _SSDP_PORT))
            await asyncio.sleep(timeout)
            return list(protocol.responses)
        finally:
            transport.close()

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        msg_id = uuid.uuid4().hex[:16]
        notify = self._build_notify(msg_id, encoded_chunk)
        await self._send_recv(notify)
        return StoredChunkRef(adapter_name="ssdp", location=msg_id)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        search = self._build_msearch(
            f"uuid:{ref.location}::urn:picard:chunk:1"
        )
        response = await self._send_recv(search)
        if not response:
            raise ValueError(f"no SSDP response for {ref.location}")
        data = self._parse_header(response, "X-PCHK-DATA")
        if data is None:
            raise ValueError(f"no X-PCHK-DATA in response for {ref.location}")
        return data

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        byebye = self._build_byebye(ref.location)
        try:
            await self._send_recv(byebye)
            return True
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        search = self._build_msearch("urn:picard:chunk:1")
        responses = await self._send_recv_all(search)
        prefix = f"PCHK:1:{manifest_id}:"
        refs: list[StoredChunkRef] = []
        for resp in responses:
            data = self._parse_header(resp, "X-PCHK-DATA")
            usn = self._parse_header(resp, "USN")
            if data and data.startswith(prefix) and usn:
                msg_id = self._parse_usn_id(usn)
                if msg_id:
                    refs.append(
                        StoredChunkRef(adapter_name="ssdp", location=msg_id)
                    )
        return refs

    async def health_check(self) -> bool:
        try:
            search = self._build_msearch("ssdp:all")
            response = await self._send_recv(search)
            return len(response) > 0
        except Exception:
            return False


class _SsdpProtocol(asyncio.DatagramProtocol):
    def __init__(self):
        self.response: asyncio.Future[str] = asyncio.get_event_loop().create_future()

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        if not self.response.done():
            self.response.set_result(data.decode("utf-8", errors="replace"))

    def error_received(self, exc: Exception) -> None:
        if not self.response.done():
            self.response.set_exception(exc)


class _SsdpMultiProtocol(asyncio.DatagramProtocol):
    def __init__(self):
        self.responses: list[str] = []

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        self.responses.append(data.decode("utf-8", errors="replace"))
