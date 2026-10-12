# Writing a PICARD Adapter

An adapter is a small, self-contained Python class that knows two things:

1. How to place an encrypted chunk string into a protocol, API, or storage backend.
2. How to get that chunk string back out.

That's it. The adapter does not encrypt, does not chunk, does not track manifests. It just offers shelter. The orchestrator handles everything else.

This guide covers the complete contract, walks through a working example, and gives explicit rules for agents writing adapters automatically.

---

## Adapter Tiers

Adapters are organized by what authorization they require. Choose the right tier for your use case.

### Tier 1: Your own infrastructure

No third-party authorization needed. These are the primary use case for PICARD. You own the endpoint and control the data at rest.

| Category | Adapters |
|----------|---------|
| Local storage | filesystem, memory, unix-xattr, named-pipe |
| Databases / KV | redis, postgresql, sqlite-remote, dynamodb, couchdb, cassandra, memcached, etcd, consul-kv |
| Object storage | s3, backblaze-b2 |
| Self-hosted services | webdav, wordpress (self-hosted), nfs, vault |
| Protocols on own infrastructure | mqtt (own broker), dns (own zone), grpc-metadata, websocket, coap, amqp, tftp, ftp |
| Mail / calendar (own server) | smtp-imap, caldav, carddav |
| Self-hosted messaging | matrix (own homeserver), xmpp, irc |
| P2P / decentralized | ipfs, bittorrent-dht, nostr, ethereum (own wallet), tor hidden services |
| Version control | git-notes, gitlab (own instance) |
| Format / encoding on own files | xlsx-hidden, exif, pdf-metadata, zip-comment, docx-custom-xml, steganography, audio-stego, unicode-stego |

### Tier 2: Third-party platforms

Require a valid account you own on the platform and an API key or OAuth token. Each adapter's source file includes a platform-specific Terms of Service notice; read it before use. Never use these against accounts or infrastructure you do not own.

| Platform | Adapter | Auth required |
|----------|---------|---------------|
| Bluesky | bluesky | ATP credentials |
| Reddit | reddit | PRAW OAuth or browser session |
| Discord | discord | bot token + webhook URL |
| Telegram | telegram | bot API token |
| Mastodon | mastodon | OAuth access token |
| Twitter/X | twitter | API v2 bearer token |
| YouTube | youtube-comments | OAuth (Data API v3) |
| TikTok | tiktok | Content API access token |
| LinkedIn | linkedin | OAuth (UGC API) |
| Pinterest | pinterest | OAuth access token |
| Medium | medium | integration token |
| Tumblr | tumblr | OAuth |
| Twitch | twitch | Helix API OAuth |
| Steam | steam | Web API key |
| Spotify | spotify | OAuth (Web API) |
| Imgur | imgur | client ID or OAuth |
| Flickr | flickr | OAuth |
| SoundCloud | soundcloud | OAuth |
| Hugging Face | huggingface | API token |
| GitHub | github | personal access token |
| Slack | slack | bot OAuth token |
| Notion | notion | integration token |
| Freshdesk | freshdesk | API key |
| Hacker News | hackernews | account credentials |
| Stack Overflow | stackoverflow | API key |
| Confluence / Jira | confluence, jira | API token |
| Trello | trello | API key |

### Tier 3: RF spectrum and regulated hardware

These adapters involve radio frequency transmission or hardware buses with physical safety implications. Authorization requirements vary by jurisdiction, frequency band, transmit power, and device type. Read the regulatory and safety notice in each adapter's source before use.

| Adapter | Notes |
|---------|-------|
| lora / lorawan | Frequency plan compliance required; licensed spectrum in some jurisdictions |
| sdr | Receiving is generally unrestricted; transmitting requires a license |
| wifi-beacon | Passive listening is unrestricted; injecting beacon frames requires authorization |
| bluetooth-le | Own devices; check jurisdiction-specific EIRP limits |
| nfc-ndef | Own devices |
| canbus | Own vehicle or hardware only. Injecting frames onto a live bus is a physical safety hazard. |
| zigbee | Own devices; ISM band compliance |
| ax25 | Requires an amateur radio license in most jurisdictions |
| serial, usb-hid | Own hardware |

Total: **260 adapter implementations** (263 files including base.py, __init__.py, remote.py).

---

## The Interface

Every adapter extends `Adapter` from `picard/adapters/base.py` and implements five required methods:

```python
from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

class MyAdapter(Adapter):

    def profile(self) -> AdapterProfile:
        """Describe this adapter's capabilities."""
        ...

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        """Store one chunk. Return a reference to find it again."""
        ...

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        """Retrieve the exact string that was passed to write_chunk."""
        ...

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        """Delete the chunk. Return True if it existed, False if not found."""
        ...

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        """Return all stored chunks whose location contains manifest_id.
        Used for scan-based recovery when the local index is lost."""
        ...
```

Two optional methods are strongly recommended for production adapters:

```python
    async def close(self) -> None:
        """Release resources: close HTTP sessions, DB connections, etc."""
        ...

    async def health_check(self) -> bool:
        """Return True if the backend is currently reachable."""
        ...
```

And one optional method for capacity reporting:

```python
    async def capacity(self) -> dict[str, int | None]:
        """Return storage stats. Any unknown value should be None.
        Keys: used_bytes, free_bytes, total_bytes, chunk_count."""
        return {"used_bytes": None, "free_bytes": None,
                "total_bytes": None, "chunk_count": None}
```

---

## AdapterProfile Fields

```python
@dataclass(frozen=True, slots=True)
class AdapterProfile:
    name: str                        # unique lowercase identifier, e.g. "my-adapter"
    max_chunk_bytes: int             # largest encoded chunk the backend accepts
    persistent: bool = True          # False if chunks vanish on session end (e.g. ICMP)
    ordered: bool = True             # False if retrieval order != write order
    ttl_seconds: int | None = None   # seconds until chunks auto-expire; None = forever
    rate_limit_per_min: int = 60     # writes per minute the backend tolerates
    max_total_bytes: int | None = None  # storage quota; None = unknown/unlimited
    tags: dict[str, str] = {}        # metadata for scatter profile filtering
```

`max_chunk_bytes` is the most important field. If you set it too high the backend will reject writes. When in doubt, be conservative. DNS TXT records: 4096 bytes. Social media posts: 2000–8000 bytes. S3 objects: ~5 GB but PICARD's chunker caps at 65536 by default.

Common tag keys used by scatter profiles: `"category"`, `"persistent"`, `"low-profile"`, `"steganographic"`.

---

## StoredChunkRef

```python
@dataclass(frozen=True, slots=True)
class StoredChunkRef:
    adapter_name: str   # must match profile().name exactly
    location: str       # whatever your adapter needs to retrieve the chunk:
                        # a URL, an ID, a key, a file path, a composite string
```

`location` is a free-form string. Pack in whatever the read path needs. For example: `"reddit://t3_abc123"`, `"s3://my-bucket/chunks/a1b2c3"`, `"dns://chunks.example.com/pchk-0001"`.

---

## Complete Example: an HTTPS Pastebin Adapter

This example shows every required and recommended method with resource cleanup:

```python
"""Pastebin adapter — stores chunks as paste text.

Each chunk is uploaded as an anonymous paste via the Pastebin API.
The paste key is the chunk reference.

Notice: This adapter must only be used with Pastebin accounts you own
and control.  Use in a manner that violates Pastebin's Terms of Service
is outside the scope of this project and the responsibility of the user.
No endorsement by or affiliation with Pastebin is implied.

Requires: pip install picard[pastebin]  (uses aiohttp)
"""

from __future__ import annotations

from typing import Sequence

import aiohttp

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_API = "https://pastebin.com/api/api_post.php"
_RAW = "https://pastebin.com/raw/{key}"


class PastebinAdapter(Adapter):
    """Scatter chunks into Pastebin pastes."""

    def __init__(
        self,
        api_key: str,
        *,
        max_chunk_bytes: int = 8_000,
        expire: str = "N",       # N=never, 1H, 1D, 1W, 2W, 1M
    ) -> None:
        self._api_key = api_key
        self._max = max_chunk_bytes
        self._expire = expire
        self._session: aiohttp.ClientSession | None = None

    # ── resource management ─────────────────────────────────────────────────

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    # ── profile ─────────────────────────────────────────────────────────────

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="pastebin",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=10,
            tags={"category": "social"},
        )

    # ── required operations ─────────────────────────────────────────────────

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        session = await self._get_session()
        async with session.post(
            _API,
            data={
                "api_dev_key": self._api_key,
                "api_option": "paste",
                "api_paste_code": encoded_chunk,
                "api_paste_private": "1",          # unlisted
                "api_paste_expire_date": self._expire,
            },
        ) as resp:
            resp.raise_for_status()
            body = await resp.text()

        # Pastebin returns the full URL on success, or "Bad API Request: ..."
        if body.startswith("Bad API"):
            raise RuntimeError(f"Pastebin write failed: {body}")

        key = body.rsplit("/", 1)[-1]
        return StoredChunkRef(adapter_name="pastebin", location=key)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        session = await self._get_session()
        async with session.get(_RAW.format(key=ref.location)) as resp:
            resp.raise_for_status()
            return await resp.text()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        # Pastebin's free API doesn't support deletion; guest pastes expire only
        return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        # No listing API for anonymous pastes; return empty (index-dependent recovery)
        return []

    # ── optional ────────────────────────────────────────────────────────────

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            async with session.get("https://pastebin.com") as resp:
                return resp.status == 200
        except Exception:
            return False
```

---

## Rules and Checklist

The following rules apply to every adapter. When in doubt, look at any existing adapter in `picard/adapters/` for reference.

### Required

- [ ] Extend `Adapter` from `picard.adapters.base`
- [ ] Implement `profile()`, `write_chunk()`, `read_chunk()`, `delete_chunk()`, `list_chunks()`
- [ ] `profile().name` is lowercase, hyphen-separated, unique across all adapters
- [ ] `profile().max_chunk_bytes` is conservative (what the backend actually accepts)
- [ ] `write_chunk` returns a `StoredChunkRef` with `adapter_name` matching `profile().name`
- [ ] `read_chunk` returns the **exact string** passed to `write_chunk`, with no transformation
- [ ] `delete_chunk` returns `True` if the chunk existed, `False` if not found; never raises on 404
- [ ] `list_chunks` filters by `manifest_id` appearing in the location or stored data

### Strongly Recommended

- [ ] Implement `close()` and release all HTTP sessions, DB connections, file handles
- [ ] Use `async with session.get(...)` or equivalent; never leave connections open on error
- [ ] Implement `health_check()`: a simple connectivity probe, swallow all exceptions, return bool
- [ ] Set `persistent=False` if chunks do not survive a process restart
- [ ] Set `ttl_seconds` if the backend expires content automatically

### Docstring Format

Every adapter file must have a module docstring in this format:

```python
"""<Protocol> adapter — <one line what it stores and where>.

<One to three sentences describing the storage mechanism.>
<If dual-mode: document both API mode and browser mode.>

Notice: This adapter must only be used with <Platform> accounts and
infrastructure you own and control.  Use in a manner that violates
<Platform>'s Terms of Service is outside the scope of this project and
the responsibility of the user.  No endorsement by or affiliation with
<Company> is implied.

Requires: pip install picard[<extra>]   (<dependency>)
"""
```

The Notice block is required for any adapter that connects to a third-party service or platform. Skip it only for adapters that operate entirely on infrastructure the user controls (local filesystem, self-hosted servers, raw hardware).

### Testing

Every adapter must have a mocked unit test covering `write_chunk()`, `read_chunk()`, and `delete_chunk()` directly, or exercising the full orchestrator pipeline with `MemoryAdapter`. The standard pattern:

```python
import pytest
from picard.adapters.pastebin import PastebinAdapter
from unittest.mock import AsyncMock, patch

@pytest.mark.asyncio
async def test_pastebin_roundtrip():
    adapter = PastebinAdapter(api_key="test-key")

    with patch.object(adapter, "_get_session") as mock_sess:
        # mock write: returns paste URL
        mock_post = AsyncMock()
        mock_post.__aenter__ = AsyncMock(return_value=mock_post)
        mock_post.__aexit__ = AsyncMock(return_value=False)
        mock_post.raise_for_status = AsyncMock()
        mock_post.text = AsyncMock(return_value="https://pastebin.com/abc123")

        # mock read: returns original text
        mock_get = AsyncMock()
        mock_get.__aenter__ = AsyncMock(return_value=mock_get)
        mock_get.__aexit__ = AsyncMock(return_value=False)
        mock_get.raise_for_status = AsyncMock()
        mock_get.text = AsyncMock(return_value="PCHK:1:test_data")

        session = AsyncMock()
        session.post.return_value = mock_post
        session.get.return_value = mock_get
        mock_sess.return_value = session

        ref = await adapter.write_chunk("PCHK:1:test_data")
        assert ref.adapter_name == "pastebin"
        assert ref.location == "abc123"

        result = await adapter.read_chunk(ref)
        assert result == "PCHK:1:test_data"

    await adapter.close()
```

For a simpler end-to-end test without mocks, use `MemoryAdapter`:

```python
from picard.adapters.memory import MemoryAdapter
from picard.index import Index
from picard.orchestrator import Orchestrator

@pytest.mark.asyncio
async def test_full_pipeline():
    orch = Orchestrator([MemoryAdapter()], Index(":memory:"))
    await orch.put("/test.txt", b"hello world", "passphrase")
    data = await orch.get("/test.txt", "passphrase")
    assert data == b"hello world"
```

---

## Registration

Adapters are discovered automatically by the registry via `importlib` scanning of `picard/adapters/`. Place your file in that directory and it will be found.

To use it from config:

```toml
[[adapters]]
name = "my-pastebin"
type = "pastebin"           # derived from the filename — see below
enabled = true
params = { api_key = "your-api-key", expire = "1W" }
```

The `type` field is derived from the filename by the registry (`picard/registry.py`):

1. Strip `.py`
2. Strip a trailing `_adapter` suffix if present
3. Replace all remaining underscores with hyphens

| File | `type =` |
|------|----------|
| `pastebin.py` | `pastebin` |
| `my_protocol.py` | `my-protocol` |
| `netflow_adapter.py` | `netflow` |
| `backblaze_b2.py` | `backblaze-b2` |

The registry discovers the adapter class by finding the first `Adapter` subclass in the module. The class name does not need to follow any particular convention, though `<CamelCase>Adapter` is the project style.

---

## Browser-Mode Adapters

Some social platform adapters support dual-mode operation:

- **API mode**: direct HTTP calls, faster, requires credentials
- **Browser mode**: headless Playwright browser, more resilient to API changes

Browser-mode adapters use a shared `BrowserPool` singleton:

```python
from picard.browser_pool import BrowserPool

pool = await BrowserPool.get(headless=self._headless)
ctx = await pool.context("my-platform")
page = await ctx.new_page()
try:
    await page.goto("https://example.com", wait_until="domcontentloaded")
    # ... interact with the page ...
    await pool.save_state("my-platform")   # persist login cookies
finally:
    await page.close()
```

Adapters with browser support: bluesky, reddit, discord, twitter, youtube, tiktok, pinterest, linkedin, medium. See any of these for a full dual-mode pattern.

Requires: `pip install picard[browser]` (installs `playwright` and runs `playwright install chromium`).

---

## Agent Notes

If you are an AI agent writing a new PICARD adapter:

1. **Read an existing adapter first.** Pick one in the same category as what you're building. For HTTP APIs: `spotify_adapter.py`. For network protocols: `dns.py`. For browser-mode: `reddit.py`.

2. **`read_chunk` must return the exact encoded_chunk string.** No decoding, no stripping, no transformation. The orchestrator owns the encoding.

3. **Never raise in `delete_chunk` on 404.** Return `False`.

4. **Never swallow all exceptions broadly.** `except Exception: return False` is only acceptable in `health_check()` and `delete_chunk()`. Everywhere else, let errors propagate so the orchestrator can retry or fall back.

5. **Always implement `close()`** if you open any session, connection, or file handle. Use `async with` context managers where possible.

6. **The Notice block in the docstring is not optional** for any third-party platform. Write it.

7. **Write the test first.** Even a minimal mock test forces you to think about what `location` should contain and what the read path needs.

8. **`list_chunks` is used for disaster recovery**, not for normal reads. It's OK for it to return `[]` if the backend has no listing API, but document why.
