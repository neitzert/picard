# Network Transport

PICARD nodes can expose their local adapters over HTTP, allowing remote nodes
to scatter chunks to each other. This creates a peer-to-peer network where
each node contributes its adapter pool.

## Overview

```
Node A (adapters: s3, redis)        Node B (adapters: fs, mqtt)
  |                                    |
  +-- transport server :8471          +-- transport server :8472
  |                                    |
  +-- RemoteAdapter("B:fs")           +-- RemoteAdapter("A:s3")
  +-- RemoteAdapter("B:mqtt")         +-- RemoteAdapter("A:redis")
```

Each node runs a transport server that exposes its adapters via HTTP.
Remote nodes create `RemoteAdapter` instances that proxy chunk operations
to the peer's transport API.

## Starting a Transport Server

The transport server is a FastAPI sub-app. It can run standalone or alongside
the web dashboard.

The transport module (`picard/transport.py`) exposes these endpoints:

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/transport/v1/write_chunk` | Write a chunk to a named adapter |
| POST | `/transport/v1/read_chunk` | Read a chunk by adapter + ref |
| POST | `/transport/v1/delete_chunk` | Delete a chunk |
| GET | `/transport/v1/adapters` | List available adapters + profiles |
| GET | `/transport/v1/health` | Node health + per-adapter capacity |

## Peer Discovery

### Manual Registration

Add peers directly via the `PeerRegistry`:

```python
from picard.peer import PeerRegistry
registry = PeerRegistry(index.connection)
registry.add("http://peer-b:8472", ["filesystem", "mqtt"])
```

The peer registry stores peers in the same SQLite database as the index:

```sql
CREATE TABLE peers (
    url        TEXT PRIMARY KEY,
    adapters   TEXT NOT NULL DEFAULT '[]',
    last_seen  REAL NOT NULL,
    status     TEXT NOT NULL DEFAULT 'active'
);
```

### Querying a Peer

```python
adapters = await registry.discover_from_peer("http://peer-b:8472")
# Queries /transport/v1/adapters, updates registry with adapter list
```

## Remote Adapters

The `RemoteAdapter` implements the full `Adapter` interface, proxying all
operations to a remote peer's transport API:

```python
from picard.adapters.remote import RemoteAdapter

remote_fs = RemoteAdapter(
    peer_url="http://peer-b:8472",
    adapter_name="filesystem",
    secret="shared-secret-key",
)

# Use like any other adapter
ref = await remote_fs.write_chunk(encoded_data)
data = await remote_fs.read_chunk(ref)
```

Remote adapters appear in the adapter list with names like
`remote:filesystem@http://peer-b:8472`.

## Authentication

Transport endpoints support HMAC-SHA256 authentication via a shared secret.

### How It Works

1. Configure both nodes with the same secret
2. The sender computes `HMAC-SHA256(secret, request_body)` and sends it in the `X-Signature` header
3. The receiver verifies the signature before processing the request

### Request Signing

```python
import hmac, hashlib

signature = hmac.new(
    secret.encode(),
    request_body,
    hashlib.sha256,
).hexdigest()

headers = {"X-Signature": signature}
```

When no secret is configured, authentication is disabled (open access).
This is suitable for trusted networks or development.

## Configuration

Add transport and peer settings to `picard.toml`:

```toml
[transport]
port = 8471
secret = "your-shared-secret"

[[peers]]
url = "http://node-b:8472"
adapters = ["filesystem", "mqtt"]

[[peers]]
url = "http://node-c:8473"
adapters = ["s3", "redis"]
```

## Security Considerations

- Always use HMAC authentication in production
- Run transport servers behind a reverse proxy (nginx, caddy) for TLS
- The shared secret should be long and random (32+ characters)
- Chunk data is already encrypted (AES-256-GCM), so transport encryption
  (TLS) adds defense in depth but is not strictly required for confidentiality
- Consider network segmentation: transport servers should not be internet-facing
  unless intentionally creating a wide-area scatter network
