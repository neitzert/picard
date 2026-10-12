# PICARD: Protocol-Introspecting Chunk Adapter for Resilient Data

*Dedicated to the gatekeepers and doubters.*

*It was a cool Berlin morning. From my patio I stared at a plain stucco wall capped by red brick. A blur of wings caught my eye, a small bird veering into a drain hole near the roofline and vanishing. Another head appeared from a different hole, chirping. Five drain holes, five nests. The gutters were meant to move water, not host families, yet the birds had found shelter, cover, and easy access. I had never noticed because I had never bothered to look.*

*Our digital infrastructure carries more than it was designed to store. DNS TXT records accept arbitrary text within spec. MQTT retained messages persist data on the broker. ICMP echo packets carry a payload field. Social media APIs accept freeform structured text. Each of these protocols defines fields intended for metadata, discovery, or diagnostics. Those fields can carry bytes just as well as their designers' intended use cases.*

**PICARD is the experiment.** It encrypts your data, slices it into chunks, and scatters those chunks across whatever protocol endpoints you configure: DNS TXT records, social media APIs, MQTT brokers, S3 buckets, Bluetooth advertisements, or all of the above at once. Pull it back and it reassembles. With multi-adapter scatter, no single endpoint holds enough ciphertext to reconstruct anything. No central server. No monthly storage fees.

It mounts as a regular filesystem. Files show up where they're supposed to. Underneath, everything lives in protocol metadata fields, retained message queues, and structured text records.

Not efficient. Protocol-agnostic.

Source: [Medium Essay: A Wall, Some Birds, and The Internet Protocols We Aren’t Looking At. Christopher Neitzert, May 16, 2025](https://ckn46.medium.com/a-wall-some-birds-and-the-internet-protocols-we-arent-looking-at-0500922f3806?sharedUserId=ckn46)

---

> **This is a computer science research project and proof of concept.** It is not a product, a platform, or a production storage system. It is a working hypothesis: that protocol-defined metadata fields and optional record types, taken together, can function as a distributed, encrypted storage substrate. Read the [research and legal notice](#research-and-experimental-use) before you proceed.

---

## How It Works

```
plaintext
  → AES-256-GCM encrypt (Argon2id key, per-block salt + nonce)
  → PCHK chunk (protocol-sized pieces)
  → scatter across adapters (DNS, Redis, Reddit, S3, MQTT …)
  → sealed envelope per adapter (HKDF sub-key, correlation tag)

retrieve
  ← fan out to all adapters
  ← collect, verify, assemble
  ← decrypt
```

Each block is independently encrypted with its own random salt and nonce. Chunks are scattered round-robin or via RAID profiles. No adapter sees plaintext; no adapter sees enough ciphertext to reconstruct anything.

The local index (an AES-256-GCM encrypted SQLite database) maps paths to chunk locations. Lose the passphrase, lose the data.

## Adapters

260 adapters organized into three tiers by authorization requirement. See [docs/adapters.md](docs/adapters.md) for the full list.

**Tier 1: Your own infrastructure.** No third-party authorization required. These are the primary use case.

| Category | Examples |
|----------|---------|
| Local / database | Filesystem, Redis, PostgreSQL, S3, SQLite, etcd, CouchDB, Cassandra |
| Self-hosted services | WebDAV, Matrix (own homeserver), MQTT (own broker), XMPP, IRC |
| Mail / calendar | SMTP/IMAP, CalDAV, CardDAV |
| P2P / decentralized | IPFS, BitTorrent DHT, Nostr, Ethereum (own wallet), Tor hidden services |
| Steganography / encoding | Image LSB, audio WAV LSB, EXIF, XLSX hidden cells, ZIP comments |

**Tier 2: Third-party platforms.** Require a valid account and API authorization (API key or OAuth token) on the relevant platform. Each adapter's source includes a platform-specific Terms of Service notice.

| Examples |
|---------|
| Bluesky, Reddit, Discord, Telegram, Mastodon, Twitter/X, YouTube, TikTok |
| LinkedIn, Pinterest, Medium, Tumblr, Twitch, Steam, Spotify, Imgur, Flickr |
| GitHub, Slack, Notion, HuggingFace, SoundCloud, Hacker News |

**Tier 3: RF spectrum and regulated hardware.** Authorization requirements vary by jurisdiction, frequency, and device type. Read the regulatory notice in each adapter's source before use.

| Examples |
|---------|
| LoRa/LoRaWAN, SDR (receive only without license), WiFi beacons, Bluetooth LE |
| NFC, CAN bus (own hardware only), ZigBee, AX.25/amateur radio, serial/USB-HID |

Each adapter implements `write_chunk()` and `read_chunk()`. It does not manage encryption or track manifests. It stores bytes and retrieves them.

## Quickstart

```bash
pip install picard

# set your passphrase (or picard will prompt)
export PICARD_PASSPHRASE="your-passphrase"

# create a config
picard init

# edit picard.toml to add your adapters, then:
picard put myfile.txt --as /docs/myfile.txt
picard get /docs/myfile.txt -o recovered.txt
picard ls
picard tree /

# mount as a filesystem
picard mount /mnt/picard      # Linux/macOS: requires libfuse/macFUSE
picard mount P:               # Windows: requires WinFSP
picard webdav                 # or: WebDAV for any OS, no kernel driver needed
```

From source:

```bash
git clone https://github.com/neitzert/picard
cd picard
pip install -e ".[dev]"
python -m pytest --tb=short -q
```

## Filesystem Mount

`picard mount` exposes the virtual filesystem via FUSE. Regular shell tools work:
`cp`, `ls`, `mv`, `rm`, `mkdir`, `cat`, `chmod`, `ln -s`, and `df` all work as expected.

**Platform requirements:**
- Linux: `sudo apt install libfuse-dev` (or distro equivalent)
- macOS: `brew install --cask macfuse`
- Windows: [WinFSP](https://winfsp.dev/), or use `picard webdav` with Explorer's Map Network Drive

**Known limitations:** no hard links, no file locking (`flock`/`fcntl`). Writes buffer the whole file in RAM and flush on close. Don't run SQLite WAL mode or Git object stores directly inside a mount.

`picard webdav` is the zero-driver alternative: start it, point Explorer/Finder/Nautilus at `http://localhost:8471`, and drag files in.

## MCP Server

PICARD exposes storage tools as an MCP server, making it accessible to Claude and other MCP-compatible agents:

```json
{
  "mcpServers": {
    "picard-store": {
      "command": "picard-mcp",
      "env": { "PICARD_PASSPHRASE": "your-passphrase" }
    }
  }
}
```

See [docs/mcp.md](docs/mcp.md) for available tools and setup.

## Configuration

```toml
# picard.toml
[general]
block_size = 65536

[[adapters]]
name = "dns-primary"
type = "dns"
enabled = true
params = { nameserver = "8.8.8.8", domain = "chunks.example.com" }

[[adapters]]
name = "s3-backup"
type = "s3"
enabled = true
tor_proxy = true              # route through Tor SOCKS5
params = { bucket = "picard-chunks", prefix = "data/" }

[[adapters]]
name = "reddit-stash"
type = "reddit"
enabled = true
browser_mode = "headed"       # api | headless | headed
params = { subreddit = "u_myuser" }

[[raid]]
level = "raid5"
adapters = ["dns-primary", "s3-backup", "filesystem"]

[scatter.paranoid]
raid_level = "raid10"
min_adapters = 4
```

Scatter profiles: `quiet`, `resilient`, `speed`, `paranoid`.

## CLI Reference

### Core operations
```
picard put <file> --as <path>    store a file
picard get <path> -o <file>      retrieve a file
picard ls [path]                 list files
picard rm <path>                 delete
picard cp / mv                   copy / rename
picard tree /                    directory tree
picard verify <path>             chunk integrity check
```

### Filesystem
```
picard mkdir / ln / head / tail / touch / log / stat / find
picard fsck                      consistency check and repair
picard mount <mountpoint>        FUSE mount
picard webdav                    WebDAV server (port 8471)
```

### Infrastructure
```
picard web                       web dashboard (dark/light, drag-and-drop)
picard scatter-report            storage surface map
picard adapters / profiles       list adapters and scatter profiles
picard recover                   journal crash recovery
picard tor-check / tor-serve / tor-peer / tor-mount
picard init                      create default picard.toml
```

Most commands accept `--json` for machine-readable output.

## Cryptographic Posture

All symmetric: Argon2id key derivation, HKDF-SHA256 sub-keys, AES-256-GCM authenticated encryption, HMAC-SHA256 for transport auth and correlation tags. No public-key cryptography in the storage path.

AES-256 provides 128-bit security against Grover's algorithm on a fault-tolerant quantum computer, matching the security target of NIST post-quantum standards. The ciphertext format carries a version byte so the cipher suite can be swapped without re-encrypting stored data.

## Honest Downsides

PICARD is not magic, perhaps it is poetic. It is certainly not efficient, will not be fast, and offers zero guarantees.

- Reads are slow. Writes may partially fail.
- If too many chunks go missing, your data is gone. No global index, no versioning by default.
- Works best for small, critical datasets (a few MB to low GB). Larger files increase the risk of chunk loss and make already-slow retrieval worse.
- As protocols evolve or get patched, adapters may need updates, much like birds adapting to new environments.
- CISOs and sysadmins will not love that PICARD inhabits protocol assumptions that weren't designed with this kind of activity in mind.

If you need guarantees, if uptime matters more than resilience, or if you expect everything to behave without some hacking, this probably isn't for you.

## Research and Experimental Use

PICARD is a computer science research project exploring protocol-agnostic storage abstractions. It is published for educational, experimental, and interoperability research purposes under the MIT license.

Users are solely responsible for ensuring their use complies with applicable law and the terms of service of any third-party platform or network accessed through a PICARD adapter. **PICARD does not authorize access to systems, accounts, or infrastructure the user does not own or have explicit permission to use.** Unauthorized access to computer systems is illegal in most jurisdictions and is not a supported use case.

Each adapter's source includes a platform-specific notice. Read it before you use the adapter.

Think before you act. By using PICARD you accept full responsibility for your actions and their consequences. You agree to indemnify, defend, and hold harmless the authors, contributors, and maintainers of PICARD from and against any and all claims, liabilities, damages, losses, costs, and expenses (including reasonable attorneys' fees) arising out of or related to your use or misuse of this software.

### A note on ethics

*Ethics in this context is the mortar between the bricks, not an optional coat of paint. The premise of this research is not that protocol fields are available for any purpose — it is that understanding them as storage substrates is a useful contribution to the literature, and that the community benefits from knowing these surfaces exist. Exploration is fine, even necessary, but it comes with the obligation to think through collateral damage, disclose what you find, and leave breadcrumbs for defenders to address if they choose. Running PICARD responsibly means using it on infrastructure you control, resisting the urge to turn research findings into personal leverage, and being ready to stop when the experiment tips from curiosity into something else.*

## Test Coverage and Contributing

The core cryptographic path, orchestrator, RAID layer, index, and scatter profiles are fully tested (4159 tests). Many protocol adapters have unit tests with mocked transports; however, a significant portion has not been exercised against live infrastructure, particularly the browser-automation paths and adapters for exotic protocols (RF, hardware buses, satellite, raw sockets).

**Pull requests are warmly welcomed**, especially for:

- Integration tests for adapter browser paths (see `tests/integration/`)
- Tests or fixes for untested or partially-tested adapters
- Real-world validation of exotic protocol adapters
- New adapters (include at least a mocked unit test covering `write_chunk()`, `read_chunk()`, and `delete_chunk()`)
- Documentation improvements and usage examples

Open an issue for bugs or questions.

## Documentation

- [Architecture](docs/architecture.md): data flow, key hierarchy, RAID, scatter profiles
- [Adapters](docs/adapters.md): full adapter list, writing custom adapters
- [CLI Reference](docs/cli.md): all commands with examples
- [MCP Server](docs/mcp.md): agent storage interface, setup, available tools
- [FUSE Mount](docs/fuse.md): mounting, performance, platform notes
- [WebDAV](docs/webdav.md): network drive mounting, native OS drag-and-drop
- [Tor](docs/tor.md): anonymity, hidden services, .onion peers
- [Network](docs/network.md): transport layer, peer discovery, remote adapters

## License

MIT. Copyright 2024-2026 Christopher Neitzert.

*In the wild, anything left open long enough becomes habitat.*
