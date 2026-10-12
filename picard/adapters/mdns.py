"""mDNS/Bonjour adapter — announces chunks as service TXT records.

Each chunk becomes an mDNS service instance on the local network with
the encoded chunk data stored in TXT records.  A cooperating listener
collects them.  Uses multicast DNS (RFC 6762) over UDP to 224.0.0.251:5353.

No external dependencies — uses raw asyncio UDP multicast.
"""

from __future__ import annotations

import asyncio
import struct
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_MDNS_ADDR = "224.0.0.251"
_MDNS_PORT = 5353


class MdnsAdapter(Adapter):
    """Scatter chunks into mDNS TXT records on the local network."""

    def __init__(
        self,
        *,
        service_type: str = "_picard._tcp.local.",
        interface: str = "0.0.0.0",
        max_chunk_bytes: int = 200,
    ) -> None:
        self._service_type = service_type
        self._interface = interface
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="mdns",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=30,
            ttl_seconds=120,
        )

    @staticmethod
    def _encode_dns_name(name: str) -> bytes:
        parts = name.rstrip(".").split(".")
        result = b""
        for part in parts:
            encoded = part.encode("utf-8")
            result += struct.pack("B", len(encoded)) + encoded
        result += b"\x00"
        return result

    @staticmethod
    def _encode_txt(data: str) -> bytes:
        encoded = data.encode("utf-8")
        if len(encoded) > 255:
            segments = []
            for i in range(0, len(encoded), 255):
                seg = encoded[i : i + 255]
                segments.append(struct.pack("B", len(seg)) + seg)
            return b"".join(segments)
        return struct.pack("B", len(encoded)) + encoded

    def _instance_name(self, msg_id: str) -> str:
        return f"chunk-{msg_id}.{self._service_type}"

    def _build_announce_packet(
        self, instance_name: str, txt_data: str, ttl: int = 120
    ) -> bytes:
        name_bytes = self._encode_dns_name(instance_name)
        txt_bytes = self._encode_txt(txt_data)
        header = struct.pack(
            "!HHHHHH",
            0x0000,  # ID
            0x8400,  # flags: QR=1, AA=1
            0,       # questions
            1,       # answers (TXT record)
            0,       # authority
            0,       # additional
        )
        txt_record = (
            name_bytes
            + struct.pack("!HHI", 16, 1, ttl)  # type=TXT, class=IN, TTL
            + struct.pack("!H", len(txt_bytes))
            + txt_bytes
        )
        return header + txt_record

    def _build_query_packet(self, name: str, qtype: int = 16) -> bytes:
        name_bytes = self._encode_dns_name(name)
        header = struct.pack("!HHHHHH", 0x0000, 0x0000, 1, 0, 0, 0)
        question = name_bytes + struct.pack("!HH", qtype, 1)
        return header + question

    async def _send_recv(self, packet: bytes) -> bytes:
        loop = asyncio.get_event_loop()
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: _MdnsProtocol(),
            local_addr=(self._interface, 0),
            family=2,
        )
        try:
            transport.sendto(packet, (_MDNS_ADDR, _MDNS_PORT))
            return await asyncio.wait_for(protocol.response, timeout=2.0)
        except asyncio.TimeoutError:
            return b""
        finally:
            transport.close()

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        msg_id = uuid.uuid4().hex[:12]
        instance = self._instance_name(msg_id)
        packet = self._build_announce_packet(instance, encoded_chunk)
        await self._send_recv(packet)
        return StoredChunkRef(adapter_name="mdns", location=msg_id)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        instance = self._instance_name(ref.location)
        query = self._build_query_packet(instance, qtype=16)
        response = await self._send_recv(query)
        if not response:
            raise ValueError(f"no mDNS response for {ref.location}")
        return self._parse_txt_response(response)

    @staticmethod
    def _parse_txt_response(data: bytes) -> str:
        offset = 12
        # skip questions
        qdcount = struct.unpack_from("!H", data, 4)[0]
        for _ in range(qdcount):
            while offset < len(data) and data[offset] != 0:
                offset += data[offset] + 1
            offset += 5  # null + qtype + qclass
        # parse first answer's rdata
        while offset < len(data) and data[offset] != 0:
            if data[offset] & 0xC0 == 0xC0:
                offset += 2
                break
            offset += data[offset] + 1
        else:
            offset += 1
        offset += 8  # type + class + ttl
        rdlength = struct.unpack_from("!H", data, offset)[0]
        offset += 2
        rdata = data[offset : offset + rdlength]
        segments = []
        pos = 0
        while pos < len(rdata):
            slen = rdata[pos]
            pos += 1
            segments.append(rdata[pos : pos + slen].decode("utf-8"))
            pos += slen
        return "".join(segments)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        instance = self._instance_name(ref.location)
        packet = self._build_announce_packet(instance, "", ttl=0)
        try:
            await self._send_recv(packet)
            return True
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        query = self._build_query_packet(self._service_type, qtype=12)
        response = await self._send_recv(query)
        if not response:
            return []
        prefix = f"PCHK:1:{manifest_id}:"
        instances = self._parse_ptr_response(response)
        refs: list[StoredChunkRef] = []
        for inst in instances:
            try:
                txt_query = self._build_query_packet(inst, qtype=16)
                txt_resp = await self._send_recv(txt_query)
                if txt_resp:
                    txt = self._parse_txt_response(txt_resp)
                    if txt.startswith(prefix):
                        chunk_id = inst.split(".")[0].removeprefix("chunk-")
                        refs.append(
                            StoredChunkRef(adapter_name="mdns", location=chunk_id)
                        )
            except Exception:
                continue
        return refs

    @staticmethod
    def _parse_ptr_response(data: bytes) -> list[str]:
        offset = 12
        qdcount = struct.unpack_from("!H", data, 4)[0]
        ancount = struct.unpack_from("!H", data, 6)[0]
        for _ in range(qdcount):
            while offset < len(data) and data[offset] != 0:
                offset += data[offset] + 1
            offset += 5
        names: list[str] = []
        for _ in range(ancount):
            while offset < len(data) and data[offset] != 0:
                if data[offset] & 0xC0 == 0xC0:
                    offset += 2
                    break
                offset += data[offset] + 1
            else:
                offset += 1
            offset += 8
            rdlength = struct.unpack_from("!H", data, offset)[0]
            offset += 2
            rdata = data[offset : offset + rdlength]
            parts = []
            pos = 0
            while pos < len(rdata) and rdata[pos] != 0:
                slen = rdata[pos]
                pos += 1
                parts.append(rdata[pos : pos + slen].decode("utf-8"))
                pos += slen
            names.append(".".join(parts))
            offset += rdlength
        return names

    async def health_check(self) -> bool:
        try:
            query = self._build_query_packet(
                "_services._dns-sd._udp.local.", qtype=12
            )
            response = await self._send_recv(query)
            return len(response) > 0
        except Exception:
            return False


class _MdnsProtocol(asyncio.DatagramProtocol):
    def __init__(self):
        self.response: asyncio.Future[bytes] = asyncio.get_event_loop().create_future()

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        if not self.response.done():
            self.response.set_result(data)

    def error_received(self, exc: Exception) -> None:
        if not self.response.done():
            self.response.set_exception(exc)
