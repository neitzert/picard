# Adapters

PICARD uses pluggable adapters to scatter encrypted chunks across protocols and platforms.
Each adapter speaks one protocol or storage backend.

## Categories

| Category | Count | Adapters |
|----------|-------|----------|
| Social / Messaging | 12 | bluesky, reddit, mastodon, discord, telegram, matrix, slack, nostr, github, gitlab, irc, xmpp |
| Network Protocols | 20 | dns, cloudflare-dns, mqtt, icmp, ntp, snmp, sip, http-headers, coap, amqp, tftp, ftp, websocket, grpc-metadata, mdns, ssdp, udp, ipv6-flowlabel, tcp-options, named-pipe |
| Storage / KV | 11 | memory, filesystem, s3, redis, memcached, etcd, consul-kv, couchdb, sqlite-remote, postgresql, dynamodb |
| Mail / Calendar | 4 | smtp-imap, caldav, carddav, webdav |
| P2P / Exotic | 10 | ipfs, bittorrent-dht, ethereum, qr-codes, steganography, gopher, gemini, bluetooth-le, nfc-ndef, usb-hid |
| Format / Stealth | 7+ | xlsx-hidden, exif, pdf-metadata, zip-comment, docx-custom-xml, font-name-table, ico-comment |
| Broadcast / Discovery | 10+ | mdns, ssdp, wifi-beacon, llmnr, netbios, upnp, bluetooth-le adv, nfc-broadcast |
| Platform / Browser | 11 | twitter/x, youtube, tiktok, instagram, pinterest, linkedin, medium, notion, trello, jira, confluence |
| Remote | 1 | remote (proxy to peer's transport API) |
| ... | 160+ | Every other protocol gap: syslog, kafka, zeromq, rsync, git-notes, dns-over-https, tor hidden services, and more |

Total: **260 adapter implementations** (263 files including base.py, __init__.py, remote.py).

## Adapter Interface

Every adapter implements the `Adapter` abstract base class from `picard/adapters/base.py`:

```python
from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

class MyAdapter(Adapter):
    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="my-adapter",
            max_chunk_bytes=4096,
            tags={"category": "custom"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        # Store the chunk data, return a reference
        ...

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        # Retrieve chunk data by reference
        ...

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        # Remove stored chunk
        ...

    async def list_chunks(self, manifest_id: str) -> list[StoredChunkRef]:
        # List all chunks for a manifest (used by fsck rebuild)
        ...

    # Optional:
    async def health_check(self) -> bool:
        ...

    async def capacity(self) -> dict[str, int | None]:
        ...
```

### AdapterProfile

```python
@dataclass
class AdapterProfile:
    name: str                       # unique identifier
    max_chunk_bytes: int = 65536    # maximum chunk payload size
    tags: dict[str, str] = {}       # metadata for scatter profile filtering
```

Tags are used by scatter profiles to filter adapters. For example:
`tags={"category": "social", "stealth": "true"}`.

### StoredChunkRef

```python
@dataclass
class StoredChunkRef:
    adapter_name: str   # which adapter stored it
    location: str       # adapter-specific identifier (URL, key, path, etc.)
```

## Writing a Custom Adapter

1. Create a file in `picard/adapters/`, e.g. `picard/adapters/my_protocol.py`
2. Implement the `Adapter` ABC (all five required methods)
3. The adapter is auto-discovered by the registry via importlib scanning

### Example: File-Based Adapter

```python
"""Custom adapter that stores chunks as files in a directory."""
from pathlib import Path
import uuid
from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

class MyFileAdapter(Adapter):
    def __init__(self, directory: str = "/tmp/my-chunks") -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)

    def profile(self) -> AdapterProfile:
        return AdapterProfile(name="my-file", max_chunk_bytes=1_000_000)

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        name = uuid.uuid4().hex
        (self._dir / name).write_text(encoded_chunk)
        return StoredChunkRef(adapter_name="my-file", location=name)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        return (self._dir / ref.location).read_text()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        path = self._dir / ref.location
        if path.exists():
            path.unlink()
            return True
        return False

    async def list_chunks(self, manifest_id: str) -> list[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="my-file", location=f.name)
            for f in self._dir.iterdir()
        ]
```

3. Register in config.toml:

```toml
[[adapters]]
name = "my-storage"
type = "my_file"
enabled = true
params = { directory = "/data/picard-chunks" }
```

## Browser-Mode Adapters

Some social/platform adapters support dual-mode operation:
- **API mode**: direct HTTP calls (faster, needs credentials)
- **Browser mode**: headless Playwright browser (slower, more resilient to API changes)

Browser-mode adapters use a shared `BrowserPool` singleton that manages Playwright
browser contexts with persistent sessions. The pool handles context reuse,
credential persistence, and graceful shutdown.

Adapters with browser support: bluesky, reddit, discord, twitter, youtube,
tiktok, instagram, pinterest, linkedin, medium.

## Testing Adapters

The `MemoryAdapter` (in-process dict) is the standard test adapter:

```python
from picard.adapters.memory import MemoryAdapter
from picard.index import Index
from picard.orchestrator import Orchestrator

adapter = MemoryAdapter()
index = Index(":memory:")
orch = Orchestrator([adapter], index)

manifest = await orch.put("/test.txt", b"hello", "passphrase")
data = await orch.get("/test.txt", "passphrase")
assert data == b"hello"
```

All 4050+ tests use this pattern, exercising the full pipeline without
external service dependencies.
