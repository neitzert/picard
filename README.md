# PICARD — Protocol-Introspecting Chunk Adapter for Resilient Data

> A push for protocol security and away from platforms for the integrity of compute.

PICARD encrypts your data with AES-256-GCM, splits it into blocks, chunks each block,
and scatters the chunks across 260 protocol adapters — from social media APIs to DNS
TXT records to Bluetooth beacons. No single adapter holds enough to reconstruct anything.

## Features

- **260 protocol adapters** across 15+ categories (social, network, storage, P2P, exotic)
- **AES-256-GCM encryption** with Argon2id key derivation (per-block salt + nonce)
- **RAID redundancy** — levels 0/1/5/10 across adapter pools
- **Scatter profiles** — quiet, resilient, speed, paranoid (config-driven)
- **Full filesystem** — directories, symlinks, versioning, partial reads
- **FUSE mount** — mount as a real filesystem (Linux/macOS/Windows)
- **WebDAV server** — mount as a network drive for native OS drag-and-drop
- **Block cache** — LRU with TTL for fast repeated reads
- **Journaling** — crash recovery via write-ahead log
- **fsck** — integrity checking, orphan detection, repair
- **Streaming I/O** — handle files larger than RAM
- **Encrypted index** — AES-256-GCM encrypted SQLite at rest
- **Network transport** — peer-to-peer via HTTP with HMAC auth
- **Key hierarchy** — HKDF sub-keys per adapter with version rotation
- **Sealed envelopes** — no plaintext PCHK visible on the wire
- **CLI** — 35+ commands with progress bars, --json output, colored display
- **Web dashboard** — dark/light theme, drag-and-drop upload, storage health, per-adapter privacy routing
- **MCP server** — agent-accessible storage tools for any MCP-compatible client
- **Tor integration** — per-adapter or global SOCKS5 routing, hidden services, .onion peers
- **Browser automation** — Playwright headless/headed mode for adapters that use web interfaces
- **VPN device binding** — route adapter traffic through specific network interfaces

## Research and Experimental Use

PICARD is a computer science research project exploring protocol-agnostic storage abstractions. It is published for educational, experimental, and interoperability research purposes under the Apache 2.0 license.

Users are solely responsible for ensuring their use complies with applicable law and the terms of service of any third-party platform or network accessed through a PICARD adapter. PICARD does not authorize access to systems, accounts, or infrastructure the user does not own or have explicit permission to use. Unauthorized access to computer systems is illegal in most jurisdictions and is not a supported use case.

Think before you act. By using PICARD you accept full responsibility for your actions and their consequences. You agree to indemnify, defend, and hold harmless the authors, contributors, and maintainers of PICARD from and against any and all claims, liabilities, damages, losses, costs, and expenses (including reasonable attorneys' fees) arising out of or related to your use or misuse of this software.

## Quickstart

```bash
pip install -e ".[dev]"

picard put myfile.txt --as /docs/myfile.txt
picard get /docs/myfile.txt -o recovered.txt
picard ls
picard tree /
picard mount /mnt/picard    # requires libfuse/WinFSP/macFUSE
```

## Architecture

```
plaintext --> [AES-256-GCM per block] --> [PCHK chunking] --> [scatter across adapters]
                    |                                                  |
              Argon2id KDF                                   RAID pool / envelope seal
              (per-block salt+nonce)                         (HKDF sub-keys per adapter)
```

Each block is independently decryptable with its own random salt (16 bytes) and nonce (12 bytes).
Chunks are scattered round-robin across adapters. RAID pools provide redundancy.
The sealed envelope format wraps chunks with per-adapter sub-keys and correlation tags,
producing output indistinguishable from random data.

## Cryptographic Posture

PICARD's data path is entirely symmetric: Argon2id key derivation, HKDF-SHA256
sub-keys, AES-256-GCM authenticated encryption, HMAC-SHA256 for transport auth
and correlation tags. No public-key cryptography sits in the storage path.

AES-256 provides 128-bit security against Grover's algorithm on a fault-tolerant
quantum computer, matching the security target of NIST post-quantum standards.
The ciphertext format carries a version and algorithm byte so the cipher can be
replaced without re-encrypting stored data.

## Adapter Categories

| Category | Count | Examples |
|----------|-------|---------|
| Social / Messaging | 12 | Bluesky, Reddit, Discord, Telegram, Matrix, Slack |
| Network Protocols | 20 | DNS, MQTT, gRPC, WebSocket, CoAP, TFTP, UDP |
| Storage / KV | 11 | S3, Redis, PostgreSQL, DynamoDB, CouchDB |
| Mail / Calendar | 4 | SMTP/IMAP, CalDAV, CardDAV, WebDAV |
| P2P / Exotic | 10 | IPFS, BitTorrent DHT, Ethereum, QR codes, Steganography |
| Format / Encoding | 7+ | XLSX hidden cells, EXIF metadata, PDF, ZIP comments |
| Broadcast / Discovery | 10+ | mDNS, SSDP, BLE, NFC, WiFi beacons |
| ... | ... | 180+ more across every protocol gap we could find |

## Configuration

```toml
# picard.toml
[general]
block_size = 65536

[[adapters]]
name = "s3-chunks"
type = "s3"
enabled = true
tor_proxy = true              # route through Tor SOCKS5
browser_mode = "headless"     # api | headless | headed
params = { bucket = "picard-chunks", prefix = "data/" }

[[adapters]]
name = "reddit-stash"
type = "reddit"
enabled = true
browser_mode = "headed"       # visible browser for login
params = { subreddit = "u_myuser" }

[[raid]]
level = "raid5"
adapters = ["s3-chunks", "redis-cache", "filesystem"]

[scatter.paranoid]
raid_level = "raid10"
min_adapters = 4
```

## CLI Commands

### Core
- `picard put` / `get` / `ls` / `rm` — basic file operations
- `picard cp` / `mv` — copy and rename
- `picard info` / `stat` — metadata inspection
- `picard verify` — chunk integrity check
- `picard export` / `import` — manifest bundles

### Filesystem
- `picard mkdir` / `rmdir` — directory management (via `rm`)
- `picard tree` / `find` — directory browsing
- `picard ln` — symbolic links
- `picard head` / `tail` — partial reads
- `picard touch` — create or update timestamps
- `picard log` — version history
- `picard fsck` — filesystem consistency check
- `picard mount` — FUSE mount

### Infrastructure
- `picard web` — launch web dashboard (dark/light theme, drag-and-drop, health monitoring)
- `picard webdav` — mount as a WebDAV network drive
- `picard recover` — journal crash recovery
- `picard demo` — scatter across all adapters
- `picard scatter-report` — show storage surface map
- `picard adapters` / `profiles` — list available adapters and scatter profiles
- `picard init` — create default config.toml
- `picard tor-check` — verify Tor connectivity
- `picard tor-serve` / `tor-peer` / `tor-mount` — Tor hidden service operations

Most commands support `--json` for machine-readable output.

## Documentation

- [Architecture](docs/architecture.md) — data flow, key hierarchy, RAID, scatter
- [Adapters](docs/adapters.md) — adapter list, writing custom adapters
- [CLI Reference](docs/cli.md) — all commands with examples
- [MCP Server](docs/mcp.md) — agent storage interface, setup, tools
- [Network](docs/network.md) — transport layer, peer discovery, remote adapters
- [FUSE Mount](docs/fuse.md) — mounting, performance, platform notes
- [Tor](docs/tor.md) — anonymity, hidden services, .onion peers
- [WebDAV](docs/webdav.md) — network drive mounting, native OS drag-and-drop

## Development

```bash
pip install -e ".[dev]"
python -m pytest --tb=short -q
```

Python 3.11+ required. Core dependencies: `cryptography`, `argon2-cffi`, `typer`, `rich`, `aiohttp`, `httpx`.

## License

MIT
