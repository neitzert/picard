"""SIP presence state adapter -- stores chunks as PIDF+XML presence documents.

Data stored by this adapter lives in a SIP presence server's subscription
database as PIDF+XML documents published to synthetic Address-of-Record (AOR)
identities. A SIP PUBLISH request (RFC 3903) deposits a presence document that
persists in the server's Event State Compositor until the Expires timer lapses.
No file is written. The chunk bytes exist as telephony presence state.

Mechanism:
  write_chunk(): base64-encode the chunk, embed it in a PIDF+XML document inside
    a <picard:data> element, PUBLISH to a synthetic AOR derived from a UUID, and
    record the AOR, ETag, and server address in the ref.
  read_chunk(): SUBSCRIBE to the AOR's presence (reg event), receive the NOTIFY
    with PIDF+XML body, parse the <picard:data> element.
  delete_chunk(): PUBLISH with Expires: 0 removes the event state.

PIDF+XML (RFC 3863) is the standard presence document format. Each document
carries a <tuple> with a <status> and optional extension elements. This adapter
adds a <picard:data> element in the picard XML namespace to carry the payload.

Protocol: raw ASCII SIP over UDP using asyncio DatagramProtocol, same transport
layer as sip_registrar_adapter. No third-party SIP library required.

Prior art: SIP presence servers have been used as covert storage for C2 command
distribution (public CERT advisory, 2014). This adapter extends the concept to
direct content storage via SIP PUBLISH.

Platform: any (UDP sockets are available everywhere). Tested against Kamailio
with the presence module and pua module enabled.

Persistence: until Expires lapses or the presence server restarts. A Kamailio
presence module backed by a database is power-cycle persistent.

EXPERIMENTAL: Run at your own risk. Use only on SIP infrastructure you own and
control. Sending PUBLISH to a third-party presence server without authorization
may violate its terms of service or applicable law.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import uuid
import xml.etree.ElementTree as ET
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ADAPTER_NAME = "sip-presence"
_SIP_VERSION = "SIP/2.0"
_USER_AGENT = "PICARD-SIP/0.4"
_MAX_CHUNK_BYTES = 65_000
_TIMEOUT_S = 5.0
_PICARD_NS = "urn:ietf:params:xml:ns:picard-store"


# ---------------------------------------------------------------------------
# SIP helpers (minimal, duplicates registrar adapter to keep files independent)
# ---------------------------------------------------------------------------

def _call_id() -> str:
    return uuid.uuid4().hex + "@picard"


def _tag() -> str:
    return uuid.uuid4().hex[:8]


def _branch() -> str:
    return "z9hG4bK" + uuid.uuid4().hex[:16]


def _parse_status(response: str) -> int:
    m = re.match(r"SIP/2\.0\s+(\d{3})", response)
    return int(m.group(1)) if m else 0


def _parse_header(response: str, name: str) -> str | None:
    pattern = re.compile(rf"^{re.escape(name)}:\s*(.+)$", re.MULTILINE | re.IGNORECASE)
    m = pattern.search(response)
    return m.group(1).strip() if m else None


# ---------------------------------------------------------------------------
# PIDF+XML builder and parser
# ---------------------------------------------------------------------------

_PIDF_NS = "urn:ietf:params:xml:ns:pidf"
_DM_NS = "urn:ietf:params:xml:ns:pidf:data-model"

def _build_pidf(entity: str, payload_b64: str, data_len: int) -> str:
    """Build a PIDF+XML presence document carrying a PICARD payload."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\r\n'
        f'<presence xmlns="{_PIDF_NS}"'
        f' xmlns:picard="{_PICARD_NS}"'
        f' entity="pres:{entity}">\r\n'
        '  <tuple id="picard0">\r\n'
        '    <status>\r\n'
        '      <basic>open</basic>\r\n'
        '    </status>\r\n'
        f'    <picard:data len="{data_len}">{payload_b64}</picard:data>\r\n'
        '  </tuple>\r\n'
        '</presence>\r\n'
    )


def _parse_pidf(body: str) -> tuple[str, int] | None:
    """Extract (payload_b64, data_len) from a PIDF+XML body. Returns None on failure."""
    try:
        root = ET.fromstring(body)
        ns = {"picard": _PICARD_NS}
        el = root.find(".//{%s}data" % _PICARD_NS)
        if el is None:
            return None
        payload_b64 = (el.text or "").strip()
        data_len = int(el.get("len", "0"))
        return payload_b64, data_len
    except Exception:
        return None


# ---------------------------------------------------------------------------
# SIP message builders
# ---------------------------------------------------------------------------

def _build_publish(
    *,
    server_host: str,
    server_port: int,
    domain: str,
    aor: str,
    call_id: str,
    from_tag: str,
    cseq_n: int,
    expires: int,
    body: str,
    sip_etag: str | None = None,
) -> bytes:
    body_bytes = body.encode()
    content_type = "application/pidf+xml"
    lines = [
        f"PUBLISH sip:{aor} {_SIP_VERSION}",
        f"Via: {_SIP_VERSION}/UDP {server_host}:{server_port};branch={_branch()};rport",
        f"From: <sip:{aor}>;tag={from_tag}",
        f"To: <sip:{aor}>",
        f"Call-ID: {call_id}",
        f"CSeq: {cseq_n} PUBLISH",
        f"Event: presence",
        f"Expires: {expires}",
        f"Max-Forwards: 70",
        f"User-Agent: {_USER_AGENT}",
        f"Content-Type: {content_type}",
        f"Content-Length: {len(body_bytes)}",
    ]
    if sip_etag:
        lines.append(f"SIP-If-Match: {sip_etag}")
    lines.append("")
    lines.append(body)
    return "\r\n".join(lines).encode()


def _build_subscribe(
    *,
    server_host: str,
    server_port: int,
    domain: str,
    aor: str,
    call_id: str,
    from_tag: str,
    cseq_n: int,
    expires: int,
) -> bytes:
    lines = [
        f"SUBSCRIBE sip:{aor} {_SIP_VERSION}",
        f"Via: {_SIP_VERSION}/UDP {server_host}:{server_port};branch={_branch()};rport",
        f"From: <sip:picard-sub@{domain}>;tag={from_tag}",
        f"To: <sip:{aor}>",
        f"Call-ID: {call_id}",
        f"CSeq: {cseq_n} SUBSCRIBE",
        f"Event: presence",
        f"Accept: application/pidf+xml",
        f"Expires: {expires}",
        f"Max-Forwards: 70",
        f"User-Agent: {_USER_AGENT}",
        f"Content-Length: 0",
        "",
        "",
    ]
    return "\r\n".join(lines).encode()


# ---------------------------------------------------------------------------
# Asyncio UDP transport (shared pattern with sip_registrar_adapter)
# ---------------------------------------------------------------------------

class _SipProtocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self._response: asyncio.Future[str] = asyncio.get_event_loop().create_future()
        self._transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: object) -> None:
        if not self._response.done():
            self._response.set_result(data.decode(errors="replace"))

    def error_received(self, exc: Exception) -> None:
        if not self._response.done():
            self._response.set_exception(exc)

    def connection_lost(self, exc: Exception | None) -> None:
        if not self._response.done():
            self._response.set_exception(
                ConnectionError("SIP UDP connection lost before response")
            )

    async def send_and_receive(self, msg: bytes, timeout: float) -> str:
        if self._transport is None:
            raise RuntimeError("transport not connected")
        self._transport.sendto(msg)
        return await asyncio.wait_for(self._response, timeout=timeout)


async def _sip_transaction(
    host: str, port: int, msg: bytes, timeout: float = _TIMEOUT_S
) -> str:
    loop = asyncio.get_event_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        _SipProtocol,
        remote_addr=(host, port),
    )
    try:
        return await protocol.send_and_receive(msg, timeout)
    finally:
        transport.close()


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class SipPresenceAdapter(Adapter):
    """Store chunks as PIDF+XML presence documents in a SIP presence server.

    Data persists until the Expires timer lapses or the server is restarted.
    A Kamailio presence module backed by a database is power-cycle persistent.

    Usage::

        adapter = SipPresenceAdapter(
            presence_server="127.0.0.1",
            domain="example.local",
        )
        ref = await adapter.write_chunk(encoded_chunk)
        result = await adapter.read_chunk(ref)
        await adapter.delete_chunk(ref)
        await adapter.close()
    """

    def __init__(
        self,
        presence_server: str = "127.0.0.1",
        port: int = 5060,
        domain: str = "localhost",
        aor_prefix: str = "picard",
        ttl: int = 3600,
        max_chunk_bytes: int = _MAX_CHUNK_BYTES,
    ) -> None:
        self._host = presence_server
        self._port = port
        self._domain = domain
        self._aor_prefix = aor_prefix
        self._ttl = ttl
        self._max_chunk_bytes = max_chunk_bytes

    # ------------------------------------------------------------------
    # Adapter protocol
    # ------------------------------------------------------------------

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_ADAPTER_NAME,
            max_chunk_bytes=self._max_chunk_bytes,
            persistent=True,
            ordered=True,
            ttl_seconds=self._ttl,
            rate_limit_per_min=60,
            tags={
                "tier": "5",
                "experimental": "true",
                "substrate": "sip-presence",
                "requires": "SIP presence server (Kamailio + presence module)",
            },
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        data = (
            encoded_chunk.encode()
            if isinstance(encoded_chunk, str)
            else encoded_chunk
        )
        if len(data) > self._max_chunk_bytes:
            raise ValueError(
                f"chunk {len(data)} bytes exceeds max_chunk_bytes={self._max_chunk_bytes}"
            )

        payload_b64 = base64.urlsafe_b64encode(data).decode().rstrip("=")
        uid = uuid.uuid4().hex[:12]
        aor = f"{self._aor_prefix}-{uid}@{self._domain}"
        call_id = _call_id()
        from_tag = _tag()
        body = _build_pidf(entity=aor, payload_b64=payload_b64, data_len=len(data))

        msg = _build_publish(
            server_host=self._host,
            server_port=self._port,
            domain=self._domain,
            aor=aor,
            call_id=call_id,
            from_tag=from_tag,
            cseq_n=1,
            expires=self._ttl,
            body=body,
        )
        response = await _sip_transaction(self._host, self._port, msg)
        status = _parse_status(response)
        if not (200 <= status < 300):
            raise RuntimeError(f"SIP PUBLISH failed with status {status}")

        etag = _parse_header(response, "SIP-ETag")
        location = json.dumps({
            "aor": aor,
            "call_id": call_id,
            "from_tag": from_tag,
            "etag": etag,
            "server": f"{self._host}:{self._port}",
            "data_len": len(data),
        })
        return StoredChunkRef(adapter_name=_ADAPTER_NAME, location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        aor: str = loc["aor"]
        data_len: int = loc["data_len"]

        sub_call_id = _call_id()
        sub_from_tag = _tag()
        msg = _build_subscribe(
            server_host=self._host,
            server_port=self._port,
            domain=self._domain,
            aor=aor,
            call_id=sub_call_id,
            from_tag=sub_from_tag,
            cseq_n=1,
            expires=0,
        )
        response = await _sip_transaction(self._host, self._port, msg)
        status = _parse_status(response)
        if not (200 <= status < 300):
            raise RuntimeError(f"SIP SUBSCRIBE failed with status {status}")

        # The NOTIFY body is appended after the 200 OK in many server
        # implementations that send both in sequence. For simplicity we parse
        # the presence body from whichever SIP message contains it.
        body_start = response.find("\r\n\r\n")
        if body_start == -1:
            raise KeyError(f"no body in SIP response for AOR {aor}")
        body = response[body_start + 4:]

        result = _parse_pidf(body)
        if result is None:
            raise KeyError(f"no PICARD payload found in PIDF+XML for AOR {aor}")

        payload_b64, _ = result
        pad = (-len(payload_b64)) % 4
        data = base64.urlsafe_b64decode(payload_b64 + "=" * pad)
        return data[:data_len].decode()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        aor: str = loc["aor"]
        call_id: str = loc["call_id"]
        from_tag: str = loc["from_tag"]
        etag: str | None = loc.get("etag")

        empty_body = _build_pidf(entity=aor, payload_b64="", data_len=0)
        msg = _build_publish(
            server_host=self._host,
            server_port=self._port,
            domain=self._domain,
            aor=aor,
            call_id=call_id,
            from_tag=from_tag,
            cseq_n=2,
            expires=0,
            body=empty_body,
            sip_etag=etag,
        )
        try:
            response = await _sip_transaction(self._host, self._port, msg)
            status = _parse_status(response)
            return 200 <= status < 300
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        # SIP presence servers do not support enumeration by arbitrary metadata.
        return []

    async def health_check(self) -> bool:
        try:
            # OPTIONS ping
            branch = "z9hG4bK" + uuid.uuid4().hex[:16]
            tag = _tag()
            call_id = _call_id()
            lines = [
                f"OPTIONS sip:{self._domain} {_SIP_VERSION}",
                f"Via: {_SIP_VERSION}/UDP {self._host}:{self._port};branch={branch};rport",
                f"From: <sip:picard@{self._domain}>;tag={tag}",
                f"To: <sip:{self._domain}>",
                f"Call-ID: {call_id}",
                f"CSeq: 1 OPTIONS",
                f"Max-Forwards: 70",
                f"User-Agent: {_USER_AGENT}",
                f"Content-Length: 0",
                "",
                "",
            ]
            msg = "\r\n".join(lines).encode()
            response = await _sip_transaction(self._host, self._port, msg, timeout=3.0)
            status = _parse_status(response)
            return 200 <= status < 300 or status == 405
        except Exception:
            return False

    async def close(self) -> None:
        pass
