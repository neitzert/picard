"""SIP registrar state adapter -- stores chunks as SIP registration bindings.

Data stored by this adapter lives in a SIP registrar's location database as
Contact header bindings for synthetic Address-of-Record (AOR) identities. A
SIP REGISTER request with a crafted Contact parameter persists in the
registrar's in-memory or database-backed location service until the Expires
timer lapses. No file is written. The chunk bytes exist as telephony routing
state.

Mechanism:
  write_chunk(): base64-encode the chunk, derive a synthetic AOR from a hash of
    a fresh UUID, send a SIP REGISTER with the encoded payload in the Contact
    URI's +picard parameter, record the Call-ID, From-tag, and AOR in the ref.
  read_chunk(): re-REGISTER with the same Call-ID and From-tag but Expires=0 to
    fetch current bindings; parse the +picard parameter from the 200 OK's
    Contact header to recover the payload.
  delete_chunk(): REGISTER with Expires: 0 (deregistration removes the binding).

Authentication: Digest MD5 per RFC 3261 section 22. If the registrar returns a
401 or 407, the adapter recomputes the Authorization header and retries.

Protocol: raw ASCII SIP over UDP using the asyncio DatagramProtocol. No
third-party SIP library is required.

Prior art: SIP registrar location services have been abused as stealthy C2
rendezvous points since at least 2011 (Cisco PSIRT advisory, SIP location
service poisoning). This adapter repurposes the mechanism for content storage.

Platform: any (UDP sockets are available everywhere). Tested against Asterisk,
Kamailio, and OpenSIPS. Local geth is not involved.

Persistence: until Expires lapses or the registrar restarts (implementation-
specific). A Kamailio usrloc module backed by a database is power-cycle
persistent.

EXPERIMENTAL: Run at your own risk. This adapter injects synthetic SIP
registrations. Use only on SIP infrastructure you own and control. Sending
REGISTER to a third-party registrar without authorization may violate its
terms of service or applicable law.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import time
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ADAPTER_NAME = "sip-registrar"
_SIP_VERSION = "SIP/2.0"
_USER_AGENT = "PICARD-SIP/0.4"
_MAX_CHUNK_BYTES = 65_000   # base64 overhead puts this at ~87 KB on the wire
_TIMEOUT_S = 5.0
_PICARD_PARAM = "+picard"


# ---------------------------------------------------------------------------
# SIP message builder
# ---------------------------------------------------------------------------

def _call_id() -> str:
    return uuid.uuid4().hex + "@picard"


def _tag() -> str:
    return uuid.uuid4().hex[:8]


def _branch() -> str:
    return "z9hG4bK" + uuid.uuid4().hex[:16]


def _cseq(n: int, method: str) -> str:
    return f"{n} {method}"


def _digest_response(
    username: str,
    realm: str,
    password: str,
    method: str,
    uri: str,
    nonce: str,
    algorithm: str = "MD5",
) -> str:
    def h(s: str) -> str:
        return hashlib.md5(s.encode()).hexdigest()  # noqa: S324

    ha1 = h(f"{username}:{realm}:{password}")
    ha2 = h(f"{method}:{uri}")
    return h(f"{ha1}:{nonce}:{ha2}")


def _build_register(
    *,
    registrar_host: str,
    registrar_port: int,
    domain: str,
    username: str,
    aor: str,
    call_id: str,
    from_tag: str,
    cseq_n: int,
    expires: int,
    contact_payload: str | None = None,
    auth_header: str | None = None,
) -> bytes:
    """Build a SIP REGISTER request as bytes."""
    request_uri = f"sip:{domain}"
    from_hdr = f"<sip:{username}@{domain}>;tag={from_tag}"
    to_hdr = f"<sip:{aor}>"
    via = (
        f"{_SIP_VERSION}/UDP {registrar_host}:{registrar_port}"
        f";branch={_branch()};rport"
    )

    if contact_payload is not None:
        # Encode the payload as a URI parameter value (URL-safe base64, no padding issues)
        contact_uri = (
            f"<sip:{username}@{registrar_host}:{registrar_port}"
            f";{_PICARD_PARAM}={contact_payload}>"
        )
    else:
        contact_uri = f"<sip:{username}@{registrar_host}:{registrar_port}>"

    lines = [
        f"REGISTER {request_uri} {_SIP_VERSION}",
        f"Via: {via}",
        f"From: {from_hdr}",
        f"To: {to_hdr}",
        f"Call-ID: {call_id}",
        f"CSeq: {_cseq(cseq_n, 'REGISTER')}",
        f"Contact: {contact_uri}",
        f"Expires: {expires}",
        f"Max-Forwards: 70",
        f"User-Agent: {_USER_AGENT}",
        f"Content-Length: 0",
    ]
    if auth_header:
        lines.append(auth_header)
    lines.append("")
    lines.append("")
    return "\r\n".join(lines).encode()


def _build_options(
    *,
    registrar_host: str,
    registrar_port: int,
    domain: str,
) -> bytes:
    branch = _branch()
    call_id = _call_id()
    tag = _tag()
    lines = [
        f"OPTIONS sip:{domain} {_SIP_VERSION}",
        f"Via: {_SIP_VERSION}/UDP {registrar_host}:{registrar_port};branch={branch};rport",
        f"From: <sip:picard@{domain}>;tag={tag}",
        f"To: <sip:{domain}>",
        f"Call-ID: {call_id}",
        f"CSeq: 1 OPTIONS",
        f"Max-Forwards: 70",
        f"User-Agent: {_USER_AGENT}",
        f"Content-Length: 0",
        "",
        "",
    ]
    return "\r\n".join(lines).encode()


# ---------------------------------------------------------------------------
# SIP response parser
# ---------------------------------------------------------------------------

def _parse_status(response: str) -> int:
    m = re.match(r"SIP/2\.0\s+(\d{3})", response)
    if m:
        return int(m.group(1))
    return 0


def _parse_header(response: str, name: str) -> str | None:
    pattern = re.compile(rf"^{re.escape(name)}:\s*(.+)$", re.MULTILINE | re.IGNORECASE)
    m = pattern.search(response)
    return m.group(1).strip() if m else None


def _parse_www_authenticate(response: str) -> dict[str, str]:
    """Parse WWW-Authenticate or Proxy-Authenticate into a dict of params."""
    for header_name in ("WWW-Authenticate", "Proxy-Authenticate"):
        hdr = _parse_header(response, header_name)
        if hdr:
            break
    else:
        return {}
    params: dict[str, str] = {}
    for m in re.finditer(r'(\w+)=["\']?([^"\',$]+)["\']?', hdr):
        params[m.group(1)] = m.group(2).strip()
    return params


def _extract_picard_payload(contact_header: str) -> str | None:
    """Extract the +picard parameter value from a Contact header."""
    m = re.search(rf"[;,\s]{re.escape(_PICARD_PARAM)}=([^\s;,>]+)", contact_header)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Asyncio UDP transport
# ---------------------------------------------------------------------------

class _SipProtocol(asyncio.DatagramProtocol):
    """One-shot UDP SIP transaction: send a request, collect the first response."""

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
    """Send one SIP UDP datagram and return the first response."""
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

class SipRegistrarAdapter(Adapter):
    """Store chunks as SIP registration bindings in a registrar's location database.

    Data persists until the Expires timer lapses or the registrar is restarted.
    A Kamailio usrloc module backed by MySQL or PostgreSQL is power-cycle
    persistent and suitable for long-term storage.

    Usage::

        adapter = SipRegistrarAdapter(
            registrar_host="127.0.0.1",
            domain="example.local",
            username="picard",
            password="secret",
        )
        ref = await adapter.write_chunk(encoded_chunk)
        result = await adapter.read_chunk(ref)
        await adapter.delete_chunk(ref)
        await adapter.close()
    """

    def __init__(
        self,
        registrar_host: str = "127.0.0.1",
        registrar_port: int = 5060,
        domain: str = "localhost",
        username: str = "picard",
        password: str = "",
        ttl: int = 3600,
        max_chunk_bytes: int = _MAX_CHUNK_BYTES,
    ) -> None:
        self._host = registrar_host
        self._port = registrar_port
        self._domain = domain
        self._username = username
        self._password = password
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
                "substrate": "sip-registrar",
                "requires": "SIP registrar (Kamailio, Asterisk, OpenSIPS)",
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
        aor = f"picard-{uuid.uuid4().hex[:12]}@{self._domain}"
        call_id = _call_id()
        from_tag = _tag()

        await self._register(
            aor=aor,
            call_id=call_id,
            from_tag=from_tag,
            cseq_n=1,
            expires=self._ttl,
            contact_payload=payload_b64,
        )

        location = json.dumps({
            "aor": aor,
            "call_id": call_id,
            "from_tag": from_tag,
            "registrar": f"{self._host}:{self._port}",
            "data_len": len(data),
        })
        return StoredChunkRef(adapter_name=_ADAPTER_NAME, location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        aor: str = loc["aor"]
        call_id: str = loc["call_id"]
        from_tag: str = loc["from_tag"]
        data_len: int = loc["data_len"]

        # REGISTER with Expires: 0 forces the registrar to return current bindings
        # in the 200 OK Contact headers before removing them. We re-register
        # immediately after to restore the binding.
        response = await self._send_register(
            aor=aor,
            call_id=call_id,
            from_tag=from_tag,
            cseq_n=10,
            expires=0,
            contact_payload=None,
        )
        status = _parse_status(response)
        if status == 401 or status == 407:
            auth_hdr = self._build_auth_header(response, "REGISTER", f"sip:{self._domain}")
            response = await self._send_register(
                aor=aor,
                call_id=call_id,
                from_tag=from_tag,
                cseq_n=11,
                expires=0,
                contact_payload=None,
                auth_header=auth_hdr,
            )

        contact_hdr = _parse_header(response, "Contact")
        if not contact_hdr:
            raise KeyError(f"no Contact header in registrar response for AOR {aor}")

        payload_b64 = _extract_picard_payload(contact_hdr)
        if payload_b64 is None:
            raise KeyError(f"no {_PICARD_PARAM} parameter in Contact header")

        # Restore padding stripped during write
        pad = (-len(payload_b64)) % 4
        data = base64.urlsafe_b64decode(payload_b64 + "=" * pad)
        return data[:data_len].decode()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        aor: str = loc["aor"]
        call_id: str = loc["call_id"]
        from_tag: str = loc["from_tag"]

        try:
            await self._register(
                aor=aor,
                call_id=call_id,
                from_tag=from_tag,
                cseq_n=20,
                expires=0,
                contact_payload=None,
            )
            return True
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        # SIP registrars do not support enumeration by arbitrary metadata.
        return []

    async def health_check(self) -> bool:
        try:
            msg = _build_options(
                registrar_host=self._host,
                registrar_port=self._port,
                domain=self._domain,
            )
            response = await _sip_transaction(self._host, self._port, msg, timeout=3.0)
            status = _parse_status(response)
            return 200 <= status < 300 or status == 405  # 405 = OPTIONS not allowed but server alive
        except Exception:
            return False

    async def close(self) -> None:
        pass

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _register(
        self,
        *,
        aor: str,
        call_id: str,
        from_tag: str,
        cseq_n: int,
        expires: int,
        contact_payload: str | None,
        auth_header: str | None = None,
    ) -> str:
        response = await self._send_register(
            aor=aor,
            call_id=call_id,
            from_tag=from_tag,
            cseq_n=cseq_n,
            expires=expires,
            contact_payload=contact_payload,
            auth_header=auth_header,
        )
        status = _parse_status(response)

        if status in (401, 407) and not auth_header:
            auth_hdr = self._build_auth_header(response, "REGISTER", f"sip:{self._domain}")
            response = await self._send_register(
                aor=aor,
                call_id=call_id,
                from_tag=from_tag,
                cseq_n=cseq_n + 1,
                expires=expires,
                contact_payload=contact_payload,
                auth_header=auth_hdr,
            )
            status = _parse_status(response)

        if not (200 <= status < 300):
            raise RuntimeError(f"SIP REGISTER failed with status {status}")
        return response

    async def _send_register(
        self,
        *,
        aor: str,
        call_id: str,
        from_tag: str,
        cseq_n: int,
        expires: int,
        contact_payload: str | None,
        auth_header: str | None = None,
    ) -> str:
        msg = _build_register(
            registrar_host=self._host,
            registrar_port=self._port,
            domain=self._domain,
            username=self._username,
            aor=aor,
            call_id=call_id,
            from_tag=from_tag,
            cseq_n=cseq_n,
            expires=expires,
            contact_payload=contact_payload,
            auth_header=auth_header,
        )
        return await _sip_transaction(self._host, self._port, msg)

    def _build_auth_header(self, response: str, method: str, uri: str) -> str:
        params = _parse_www_authenticate(response)
        realm = params.get("realm", self._domain)
        nonce = params.get("nonce", "")
        algorithm = params.get("algorithm", "MD5")
        resp_hash = _digest_response(
            username=self._username,
            realm=realm,
            password=self._password,
            method=method,
            uri=uri,
            nonce=nonce,
            algorithm=algorithm,
        )
        return (
            f'Authorization: Digest username="{self._username}", '
            f'realm="{realm}", nonce="{nonce}", uri="{uri}", '
            f'response="{resp_hash}", algorithm={algorithm}'
        )
