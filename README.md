# PICARD — Protocol-Introspecting Chunk Adapter for Resilient Data

*It was a cool Berlin morning. From my patio I stared at a plain stucco wall capped by red brick. A blur of wings caught my eye — a small bird veered into a drain hole near the roofline and vanished. Another head appeared from a different hole, chirping. Five drain holes, five nests. The gutters were meant to move water, not host families, yet the birds had found shelter, cover, and easy access. I had never noticed because I had never bothered to look.*

*Our digital infrastructure is full of holes like those drainpipes. DNS, MQTT, ICMP, social media — built to transmit data and vanish, not to hold it. Yet they're wide open, with few guardrails and little scrutiny. Decades-old, they still route, respond, and hum along. What if anything left open long enough becomes habitat?*

**PICARD is the experiment.** It encrypts your data, slices it into chunks, and scatters those chunks across whatever protocols are available — DNS TXT records, social media posts, MQTT retained messages, S3 buckets, Bluetooth beacons, or all of the above at once. Pull it back and it reassembles. No single protocol holds enough to reconstruct anything. No central server. No monthly storage fees.

It mounts as a regular filesystem. Files show up where they're supposed to. Underneath, everything lives in back channels, forgotten protocols, and digital crevices.

Not elegant. Adaptive.

---

> **This is a computer science research project and proof of concept.** It is not a product, a platform, or a production storage system. It is a working hypothesis: that data, like wildlife, will colonize whichever crevice protocols leave open. Read the [ethics and legal notice](#research-and-experimental-use) before you proceed.

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

260 adapters across 15+ categories. A partial list:

| Category | Examples |
|----------|---------|
| Social / Messaging | Bluesky, Reddit, Discord, Telegram, Mastodon, Twitter/X, Matrix |
| Network Protocols | DNS TXT, MQTT, gRPC, WebSocket, CoAP, ICMP, TFTP, NTP |
| Storage / KV | S3, Redis, PostgreSQL, DynamoDB, CouchDB, Cassandra |
| Mail / Calendar | SMTP/IMAP, CalDAV, CardDAV, WebDAV |
| P2P / Exotic | IPFS, BitTorrent DHT, Ethereum event logs, Nostr, Tor hidden services |
| Steganography | Image LSB, audio WAV LSB, Unicode zero-width, PDF metadata |
| Format / Encoding | XLSX hidden cells, EXIF fields, ZIP comments, DOCX custom XML |
| Broadcast / Discovery | mDNS, SSDP, BLE advertisements, NFC NDEF, WiFi beacon |
| Hardware / RF | CAN bus*, LoRa*, SDR*, serial ports* |
| ...and more | 180+ additional adapters across every protocol gap we could find |

*Hardware adapters require physical access to the relevant bus or radio. See safety notices in each adapter's docstring.

Each adapter implements `write_chunk()` and `read_chunk()`. They don't know what they're carrying. They don't manage encryption or track state. They just offer shelter.

## Quickstart

```bash
pip install picard-store

# create a config
picard init

# edit picard.toml to add your adapters, then:
picard put myfile.txt --as /docs/myfile.txt
picard get /docs/myfile.txt -o recovered.txt
picard ls
picard tree /

# mount as a filesystem
picard mount /mnt/picard      # Linux/macOS — requires libfuse/macFUSE
picard mount P:               # Windows — requires WinFSP
picard webdav                 # or: WebDAV for any OS, no kernel driver needed
```

From source:

```bash
git clone https://github.com/neitzert/picard-dev
cd picard-dev
pip install -e ".[dev]"
python -m pytest --tb=short -q
```

## Filesystem Mount

`picard mount` exposes the virtual filesystem via FUSE. Regular shell tools work:
`cp`, `ls`, `mv`, `rm`, `mkdir`, `cat`, `chmod`, `ln -s`, `df` — all work as expected.

**Platform requirements:**
- Linux: `sudo apt install libfuse-dev` (or distro equivalent)
- macOS: `brew install --cask macfuse`
- Windows: [WinFSP](https://winfsp.dev/) — or use `picard webdav` with Explorer's Map Network Drive

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
- As protocols evolve or get patched, adapters may need updates — much like birds adapting to new environments.
- CISOs and sysadmins will not love that PICARD inhabits protocol assumptions that weren't designed with this kind of activity in mind.

If you need guarantees, if uptime matters more than resilience, or if you expect everything to behave without some hacking — this probably isn't for you.

## Research and Experimental Use

PICARD is a computer science research project exploring protocol-agnostic storage abstractions. It is published for educational, experimental, and interoperability research purposes under the MIT license.

Users are solely responsible for ensuring their use complies with applicable law and the terms of service of any third-party platform or network accessed through a PICARD adapter. **PICARD does not authorize access to systems, accounts, or infrastructure the user does not own or have explicit permission to use.** Unauthorized access to computer systems is illegal in most jurisdictions and is not a supported use case.

Each adapter's source includes a platform-specific notice. Read it before you use the adapter.

Think before you act. By using PICARD you accept full responsibility for your actions and their consequences. You agree to indemnify, defend, and hold harmless the authors, contributors, and maintainers of PICARD from and against any and all claims, liabilities, damages, losses, costs, and expenses (including reasonable attorneys' fees) arising out of or related to your use or misuse of this software.

### A note on ethics

*Ethics in this context is the mortar between the bricks, not an optional coat of paint. If we decide the gutter is fair game simply because no one bolted a lock over the drain hole, we become the pigeons that foul every ledge and wonder why the neighbors hate us. Exploration is fine, even necessary, but it comes with the obligation to think through collateral damage, disclose what we find, and leave breadcrumbs for defenders to patch the holes if they choose. If PICARD is a flashlight into forgotten corners, then running it responsibly means logging the cracks we illuminate, resisting the urge to turn every gap into personal leverage, and being ready to shut it down when the experiment tips from curiosity to exploitation.*

## Test Coverage and Contributing

The core cryptographic path, orchestrator, RAID layer, index, and scatter profiles are fully tested (4159 tests). Many protocol adapters have unit tests with mocked transports; however, a significant portion — particularly browser-automation paths and adapters for exotic protocols (RF, hardware buses, satellite, raw sockets) — has not been exercised against live infrastructure.

**Pull requests are warmly welcomed**, especially for:

- Integration tests for adapter browser paths (see `tests/integration/`)
- Tests or fixes for untested or partially-tested adapters
- Real-world validation of exotic protocol adapters
- New adapters (include a mocked unit test covering `put()`, `get()`, and `rm()`)
- Documentation improvements and usage examples

Open an issue for bugs or questions.

## Documentation

- [Architecture](docs/architecture.md) — data flow, key hierarchy, RAID, scatter profiles
- [Adapters](docs/adapters.md) — full adapter list, writing custom adapters
- [CLI Reference](docs/cli.md) — all commands with examples
- [MCP Server](docs/mcp.md) — agent storage interface, setup, available tools
- [FUSE Mount](docs/fuse.md) — mounting, performance, platform notes
- [WebDAV](docs/webdav.md) — network drive mounting, native OS drag-and-drop
- [Tor](docs/tor.md) — anonymity, hidden services, .onion peers
- [Network](docs/network.md) — transport layer, peer discovery, remote adapters

## License

MIT — © 2024–2026 Christopher Neitzert

*In the wild, anything left open long enough becomes habitat.*
