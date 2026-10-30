# PICARD

**Protocol-Introspecting Chunk Adapter for Resilient Data**

*For the self-taught, the unconventional, and anyone who was ever told to stay in their lane. Keep building, keep exploring.*

---

*This release is dedicated to Sir Patrick Stewart (1940–2026), who spent thirty-five years making a starship captain the most compelling argument for curiosity, decency, and the refusal to accept unnecessary limits.*

---

PICARD encrypts your data, slices it into chunks, and scatters those chunks across whatever protocol endpoints you configure: DNS TXT records, social media APIs, MQTT brokers, S3 buckets, Bluetooth advertisements, or all of the above at once. Pull it back and it reassembles. With multi-adapter scatter, no single endpoint holds enough ciphertext to reconstruct anything. No central server. No monthly storage fees. It mounts as a regular filesystem.

Not efficient. Protocol-agnostic.

> [!IMPORTANT]
> **Research project.** PICARD is a working computer science experiment, not a production storage system. Read the [research and legal notice](#research-and-legal-notice) before you proceed.

---

## New in 0.4.0

**505 adapters** -- up from 264. 4,159 tests. Five additions in this release:

---

### Reed-Solomon erasure coding

A new `erasure` RAID level wraps `zfec` to provide configurable k-of-m shard recovery. Any k of m shards reconstruct the original block; the remaining m-k are parity. Tolerates up to m-k simultaneous adapter failures with (m/k - 1) storage overhead.

```toml
[[raid]]
level     = "erasure"
erasure_k = 4
erasure_m = 6
adapters  = ["s3", "dns", "redis", "dropbox", "onedrive", "matrix"]
```

Install the optional dependency: `pip install 'picard-store[erasure]'`

---

### Unconventional substrates

Ten new adapters push storage into places that were never designed to hold data: kernel state, circulating packets, process memory, hardware telemetry. None of these leave ciphertext at rest in the conventional sense.

> [!WARNING]
> Platform-specific, privilege-hungry, and ranging from "genuinely useful" to "demonstration only." Read the adapter source before use.

| Adapter | Where the data lives | Requirements |
|---------|---------------------|--------------|
| [`mempool`](picard/adapters/mempool_adapter.py) | Ethereum queued tx pool. EIP-1559 tx with nonce gap prevents mining. Data in the `input` field. Configurable lifetime up to 50 years via geth flags. 128 KB per tx. | Local geth node (`--txpool.lifetime 438000h`), `web3>=6.0` |
| [`latency`](picard/adapters/latency_adapter.py) | Linux `tc netem` delay rules on 127.x.0.x loopback IPs. Bits encoded as RTT. Data is kernel traffic-shaping state, not a file. ~63 bytes capacity. | Linux, `CAP_NET_ADMIN`, iproute2 |
| [`icmp-inflight`](picard/adapters/icmp_inflight_adapter.py) | ICMP echo payloads circulating on the loopback interface. AF_PACKET relay thread keeps each packet in flight. No at-rest state. ~87 KB capacity. | Linux, `CAP_NET_RAW`, `scapy>=2.5` |
| [`proc-mem`](picard/adapters/proc_mem_adapter.py) | Anonymous memfd held open by a companion daemon via `memfd_create` + SCM_RIGHTS. No filesystem path, no name, no socket. | Linux, companion daemon |
| [`thermal`](picard/adapters/thermal_adapter.py) | CPU die temperature as a covert channel. Encodes bits via CPU load; reads sysfs thermal zone sensors. Demonstration only -- heat dissipates. | Linux, sysfs thermal zones |
| [`hdd-smart`](picard/adapters/hdd_smart_adapter.py) | HDD adaptive calibration tables via seek pattern conditioning. SMART attribute deltas encode bits. Power-cycle persistent. Unverified hypothesis -- run the characterization script first. | Spinning-platter HDD, root, `smartmontools` |
| [`sip-registrar`](picard/adapters/sip_registrar_adapter.py) | SIP REGISTER state in a registrar's location database. Data lives as telephony routing bindings until `Expires` lapses. Raw UDP SIP, Digest MD5 auth. | Kamailio, OpenSIPS, or any RFC 3261 registrar |
| [`sip-presence`](picard/adapters/sip_presence_adapter.py) | PIDF+XML presence documents on a SIP presence server. Data lives as subscription state until `Expires` lapses. | SIP presence server (RFC 3903 PUBLISH) |
| [`turn`](picard/adapters/turn_adapter.py) | STUN/TURN relay allocation state. Background Refresh keeps the allocation alive. No at-rest state. | TURN server (RFC 5766), stdlib only |
| [`rtp-inflight`](picard/adapters/rtp_inflight_adapter.py) | RTP payloads circulating on a UDP loopback call leg. SSRC-keyed relay thread, no at-rest state. | stdlib only, any platform |

---

### Expanded protocol coverage

**Integration-tested additions** (Docker-backed, full write/read/delete/list cycle verified):

| Adapter | Notes |
|---------|-------|
| `scylladb-row` | Complete rewrite. Now uses native CQL via `cassandra-driver`. Previous REST-based implementation removed -- ScyllaDB exposes no REST API. |
| `redis-acl`, `redis-json`, `redis-lua`, `redis-timeseries`, `redis-module` | Five new Redis storage variants. Sit alongside the existing `redis`, `redis-stream`, `redis-pubsub`, and `redis-bitmap` adapters. |
| `nats-kv`, `nats-kv-ttl`, `nats-object-store` | Three NATS storage modes beyond the existing JetStream adapter. |
| `cockroachdb-row` | PostgreSQL-wire-compatible distributed SQL. Standard `asyncpg` driver. |
| `influxdb`, `influxdb-field` | Time-series with line protocol. `influxdb-field` variant for per-measurement-field scatter. |
| `victoria-metrics`, `victoria-metrics-metric`, `victoria-metrics-series`, `victoria-metrics-tag` | Four VictoriaMetrics variants covering the push, metric name, series label, and tag storage surfaces. |

**Cloud and SaaS API adapters** (each requires credentials for the relevant service):

| Category | Services |
|----------|---------|
| Work and project management | Jira, Confluence, Notion, Linear, Asana, ClickUp, Trello, Airtable, Contentful |
| CRM and support | HubSpot, Freshdesk, Zendesk, Intercom, ServiceNow |
| Observability and APM | Datadog, Datadog Events, New Relic, Splunk, Prometheus, Grafana, Loki, Sentry, WandB, OpenTelemetry, Jaeger (log + trace), Zipkin (annotation + span) |
| Analytics | Amplitude, Segment, Mixpanel |
| Cloud databases | DynamoDB, Firestore, Firebase RTDB, Azure Table (CosmosDB), ClickHouse, PlanetScale, Supabase, Elasticsearch, OpenSearch, Hazelcast, Qdrant |
| Cloud storage | GCS, Azure Blob, Dropbox, OneDrive, Box, Storj, Backblaze B2, DigitalOcean Spaces, Cloudflare KV, Cloudflare R2, Wasabi, Vercel KV, Google Drive |
| Message queues | AWS SQS, AWS SNS, GCP Pub/Sub, AWS Lambda |
| Identity and secrets | Auth0, Okta, AWS Secrets Manager, Azure Key Vault, HashiCorp Vault |
| Comms and alerting | Twilio, Mailgun, Mailchimp, SendGrid, Pushover, PagerDuty, OpsGenie, ntfy |
| Webhooks and bots | Slack, Discord, Microsoft Teams, Webex, Telegram bot |
| Commerce | Stripe (payment intent metadata), Shopify (metafield), Algolia (record) |
| Developer platforms | GitLab (snippet + CI config), Bitbucket (snippet), HackMD, GitHub Actions, GCP Cloud Functions |
| Content and publishing | Ghost, WordPress, MediaWiki, Fandom |
| Knowledge and community | Stack Overflow, HuggingFace |

---

### Security hardening

Injection vulnerabilities found and fixed across the adapter layer:

- **Redis**: Lua code injection in parameterized EVAL scripts
- **MySQL**: SQL injection in DDL for views, triggers, column defaults, and stored routines
- **ClickHouse**: SQL injection via f-string WHERE clauses
- **WebDAV**: XML injection in PROPPATCH request body
- **InfluxDB**: line protocol injection via unescaped tag values
- **Azure Table**: OData filter injection in list queries
- **LDAP**: filter injection in attribute search
- **ScyllaDB**: path traversal in the (now-removed) REST URL construction
- **PostgreSQL**: identifier injection in schema DDL, operator family DDL, and role management

All inputs are now escaped or parameterized at the protocol level.

---

### Quality pass

Approximately 45 adapters corrected across three categories:

- **`list_chunks` returning stale cache** -- 30+ adapters that returned only in-process state now do real remote API scans when credentials are available
- **`persistent=True` on write-only substrates** -- SMS, push notifications, analytics event pipelines, and similar one-way channels corrected to `persistent=False`
- **Silent cold-read failures** -- adapters that fell through to `KeyError` on a cache miss now fetch from the remote service

---

## How It Works

```
plaintext
  → AES-256-GCM encrypt     (Argon2id key, per-block salt + nonce)
  → PCHK chunk              (protocol-sized pieces with manifest + position metadata)
  → scatter across adapters (DNS, Redis, Reddit, S3, MQTT, …)
  → sealed envelope          (HKDF sub-key, HMAC-SHA256 correlation tag per adapter)

retrieve
  ← fan out to all adapters
  ← collect, verify, reassemble
  ← decrypt
```

Each block is independently encrypted with its own random salt and nonce. Chunks scatter round-robin or via RAID profiles. No adapter sees plaintext; no adapter sees enough ciphertext to reconstruct anything.

The local index (AES-256-GCM encrypted SQLite) maps virtual paths to chunk locations. Lose the passphrase, lose the data.

<details>
<summary>The origin: a wall and some birds</summary>

*It was a cool Berlin morning. From my patio I stared at a plain stucco wall capped by red brick. A blur of wings caught my eye, a small bird veering into a drain hole near the roofline and vanishing. Another head appeared from a different hole, chirping. Five drain holes, five nests. The gutters were meant to move water, not host families, yet the birds had found shelter, cover, and easy access. I had never noticed because I had never bothered to look.*

*Our digital infrastructure carries more than it was designed to store. DNS TXT records accept arbitrary text within spec. MQTT retained messages persist data on the broker. ICMP echo packets carry a payload field. Social media APIs accept freeform structured text. Each of these protocols defines fields intended for metadata, discovery, or diagnostics. Those fields can carry bytes just as well as their designers' intended use cases.*

[Medium: A Wall, Some Birds, and The Internet Protocols We Aren't Looking At, Christopher Neitzert, May 2025](https://ckn46.medium.com/a-wall-some-birds-and-the-internet-protocols-we-arent-looking-at-0500922f3806?sharedUserId=ckn46)

</details>

---

## Web Dashboard

`picard web` opens a browser dashboard at [http://localhost:8471](http://localhost:8471).

![PICARD dashboard overview](docs/img/ui-overview.png)
*Storage surface by category, adapter health, and encryption details at a glance.*

![PICARD adapter registry](docs/img/ui-adapters.png)
*505 adapters browsable by category, with required parameters and one-click add.*

---

## Quickstart

```bash
pip install picard-store

export PICARD_PASSPHRASE="your-passphrase"   # or picard will prompt

picard init                                  # create picard.toml

# edit picard.toml to add your adapters, then:
picard put myfile.txt --as /docs/myfile.txt
picard get /docs/myfile.txt -o recovered.txt
picard ls
picard tree /
```

**From source:**

```bash
git clone https://github.com/neitzert/picard
cd picard
pip install -e ".[dev]"
python -m pytest --tb=short -q
```

---

## Configuration

```toml
# picard.toml

[general]
block_size = 65536

[[adapters]]
name       = "dns-primary"
type       = "dns"
enabled    = true
params     = { nameserver = "10.10.10.10", domain = "chunks.example.com" }

[[adapters]]
name       = "s3-backup"
type       = "s3"
enabled    = true
tor_proxy  = true              # route through Tor SOCKS5
params     = { bucket = "picard-chunks", prefix = "data/" }

[[adapters]]
name         = "reddit-stash"
type         = "reddit"
enabled      = true
browser_mode = "headed"        # api | headless | headed
params       = { subreddit = "u_myuser" }

[[raid]]
level    = "raid5"
adapters = ["dns-primary", "s3-backup", "filesystem"]

# Reed-Solomon erasure coding
[[raid]]
level     = "erasure"
erasure_k = 4
erasure_m = 6
adapters  = ["s3", "dropbox", "onedrive", "gcs", "redis", "dns"]

[scatter.paranoid]
raid_level   = "raid10"
min_adapters = 4
```

**RAID levels:** `raid0` (stripe), `raid1` (mirror), `raid5` (parity), `raid10` (stripe+mirror), `erasure` (Reed-Solomon k-of-m)

**Scatter profiles:** `quiet`, `resilient`, `speed`, `paranoid`

---

## CLI Reference

### Core operations

| Command | Description |
|---------|-------------|
| `picard put <file> --as <path>` | Store a file |
| `picard get <path> -o <file>` | Retrieve a file |
| `picard ls [path]` | List files |
| `picard rm <path>` | Delete a file and its chunks |
| `picard cp / mv` | Copy / rename |
| `picard tree /` | Directory tree |
| `picard verify <path>` | Chunk integrity check against all adapters |

### Filesystem

| Command | Description |
|---------|-------------|
| `picard mkdir / ln / head / tail / touch / log / stat / find` | Standard filesystem operations |
| `picard fsck` | Consistency check and repair |
| `picard mount <mountpoint>` | FUSE mount |
| `picard webdav` | WebDAV server (port 8471) |

### Infrastructure

| Command | Description |
|---------|-------------|
| `picard web` | Web dashboard at port 8471 (dark/light, drag-and-drop, adapter health) |
| `picard scatter-report` | Storage surface map across configured adapters |
| `picard adapters` | List all registered adapters and their profiles |
| `picard profiles` | List scatter profiles |
| `picard recover` | Journal crash recovery |
| `picard tor-check / tor-serve / tor-peer / tor-mount` | Tor operations |
| `picard init` | Create default `picard.toml` |

Most commands accept `--json` for machine-readable output.

---

## Filesystem Mount

`picard mount` exposes the virtual filesystem via FUSE. Standard shell tools work: `cp`, `ls`, `mv`, `rm`, `mkdir`, `cat`, `chmod`, `ln -s`, and `df` all behave as expected.

**Platform requirements:**

| Platform | Requirement |
|----------|-------------|
| Linux | `sudo apt install libfuse-dev` (or distro equivalent) |
| macOS | `brew install --cask macfuse` |
| Windows | [WinFSP](https://winfsp.dev/), or use `picard webdav` with Explorer's Map Network Drive |

**Known limitations:** no hard links, no file locking (`flock`/`fcntl`). Writes buffer the whole file in RAM and flush on close. Do not run SQLite WAL mode or Git object stores directly inside a mount.

`picard webdav` is the zero-driver alternative. Start it, point Explorer, Finder, or Nautilus at `http://localhost:8471`, and drag files in.

---

## MCP Server

PICARD exposes storage tools as an MCP server for Claude and other MCP-compatible agents:

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

See [docs/mcp.md](docs/mcp.md) for available tools and setup details.

---

## Adapters

505 adapters in five tiers by authorization and risk level. Full list: [docs/adapters.md](docs/adapters.md).

Each adapter implements `write_chunk()` and `read_chunk()`. It does not manage encryption or track manifests. It stores bytes and retrieves them.

---

### Tier 1: Your own infrastructure

No third-party authorization required. Own the hardware, own the data.

| Category | Adapters |
|----------|---------|
| Databases | PostgreSQL (12 variants: schema objects, types, comments, rules, triggers, sequences, roles, aggregates, functions, views, matviews, publications), MySQL (10 variants: views, triggers, events, routines, index, partition, column, column comment, user attrs), Redis (9 variants: string-ttl, stream, pubsub, bitmap, geo, hyperloglog, hash, list, sorted-set), SQLite (local + remote), CouchDB, MongoDB (document + index key + metadata), Neo4j, RethinkDB, Cassandra, etcd, Zookeeper (znode, config, ACL, sequential), Consul KV, Hazelcast map, Memcached, Valkey |
| Time-series and search | InfluxDB, VictoriaMetrics (4 variants), Meilisearch, Elasticsearch, OpenSearch, Qdrant (collection + payload) |
| Object and blob storage | S3, MinIO, filesystem, WebDAV (property variant), NFS, IPFS, BitTorrent DHT |
| Self-hosted services | Matrix (room variant), MQTT (retained + will), XMPP (message variant), IRC (channel), SIP (registrar + presence), TURN, CalDAV, CardDAV, NATS (JetStream, KV, KV-TTL, Object Store), RabbitMQ (queue + exchange), Kafka, AMQP, STOMP |
| Steganography | EXIF, ID3 tags, PNG/BMP/GIF/FLAC/MP4/OGG comment, XLSX hidden cells, ZIP comment, PDF metadata, DOCX/EPUB/ODT/RTF metadata, SVG metadata, CSS/HTML/JSON/YAML/TOML/Markdown/INI/CSV/VTT comments, QR code, barcode, ELF section, PE resource, Mach-O segment, audio WAV LSB, Unicode whitespace, GeoJSON property, ndjson record |
| P2P / decentralized | Nostr, Ethereum (own node), Arweave, IPFS (with pinning), Tor hidden services |
| OS and kernel | Filesystem, named pipe, anonymous pipe, clipboard, environment variables, hosts file, crontab, authorized keys, known hosts, bash/git config, sysctl, xattr, proc fd, socket buffer, CPU frequency, Windows registry (Windows only) |
| Protocol covert channels | ARP table, NTP (in-flight + timestamp + extension), TCP ISN, TCP options, UDP, GRE, multicast, ICMP, IPv6 flow label, VXLAN, OSPF, BGP, LLDP, LLMNR, mDNS, SSDP, SNMP (trap variant), NetFlow, RADIUS, proxy protocol |

---

### Tier 2: Third-party platforms

Require a valid account and API authorization. Each adapter's source includes a platform-specific Terms of Service notice.

| Category | Adapters |
|----------|---------|
| Social | Bluesky, Mastodon, Twitter/X, Tumblr, Reddit, Pinterest, LinkedIn, Signal, WhatsApp, Telegram, Discord, Slack, Matrix (hosted), XMPP (hosted), Nostr |
| Media and creative | YouTube, TikTok, Spotify (playlist), SoundCloud, Twitch, Imgur, Flickr |
| Professional | GitHub (Gist, Actions), HuggingFace, Medium, Hacker News, Stack Overflow, Steam (Workshop), Fandom, MediaWiki |
| Package registries | PyPI, npm, crates.io, RubyGems, Homebrew, Maven |

> **Note on package registry adapters:** these are research demonstrations. Some contain intentional friction in their write paths that prevents out-of-the-box use against public registries. If an adapter silently succeeds but data does not appear, read the source. The fix in each case is a small, deliberate change. This is by design.

---

### Tier 3: Cloud and SaaS platforms

Require API credentials. Adapters call real APIs; you need an account and the service's terms of service apply.

| Category | Adapters |
|----------|---------|
| Work and project management | Jira, Confluence, Notion, Linear, Asana, ClickUp, Trello, Airtable, Contentful, Google Sheets |
| CRM and support | HubSpot, Freshdesk, Zendesk, Intercom, ServiceNow |
| Observability and APM | Datadog, Datadog Events, New Relic, Splunk, Prometheus, Grafana, Loki, Sentry, WandB, OpenTelemetry, Jaeger (log + trace), Zipkin (annotation + span) |
| Analytics | Amplitude, Segment, Mixpanel |
| Cloud databases | DynamoDB, Firestore, Firebase RTDB, Azure Table, ClickHouse, PlanetScale, Supabase, ScyllaDB, CockroachDB, Elasticsearch (hosted), OpenSearch (hosted), Hazelcast (hosted), Qdrant (hosted) |
| Cloud storage | GCS, Azure Blob, Dropbox, OneDrive, Box, Storj, Backblaze B2, DigitalOcean Spaces, Cloudflare KV, Cloudflare R2, Wasabi, Vercel KV, Google Drive |
| Message queues | AWS SQS, AWS SNS, GCP Pub/Sub, AWS Lambda |
| Identity and secrets | Auth0, Okta, AWS Secrets Manager, Azure Key Vault, HashiCorp Vault |
| Comms and alerting | Twilio (SMS), Mailgun, Mailchimp, SendGrid, Pushover, PagerDuty (incident), OpsGenie, ntfy |
| Webhooks and bots | Slack webhook, Discord webhook, Microsoft Teams webhook, Webex, Telegram bot |
| Commerce | Stripe (payment intent metadata), Shopify (metafield), Algolia |
| Developer platforms | GitLab (snippet + CI), Bitbucket (snippet), HackMD, GCP Cloud Functions |
| Content and publishing | Ghost, WordPress, MediaWiki (hosted) |
| Blockchain | Ethereum, Bitcoin (OP_RETURN), Solana, Cardano, Polygon, Arweave, Stellar, NEAR, Lightning Network, Tezos |

---

### Tier 4: RF spectrum and regulated hardware

Authorization requirements vary by jurisdiction, frequency, and device type. Read the regulatory notice in each adapter's source before transmitting.

| Category | Adapters |
|----------|---------|
| Wide-area radio | LoRa direct, LoRaWAN, APRS (AX.25/amateur radio), Iridium (satellite) |
| Short-range | Bluetooth LE (advertisement), NFC (NDEF record), ZigBee |
| Industrial / hardware buses | CAN bus, Modbus (register variant), BACnet, OPC-UA, I2C, SPI, UART, LorRaWAN (industrial) |
| Passive / receive-only | WiFi beacon (passive scan, no transmission), SDR (receive-only without license) |

---

### Tier 5: Hardware vulnerability substrates

> [!WARNING]
> Run only on hardware you own and control. Results are hardware-specific and non-deterministic across machines. DRAM bit flips are irreversible without a power cycle. Running against shared or cloud infrastructure is unauthorized access in most jurisdictions.

| Adapter | Technique | Requirements |
|---------|-----------|--------------|
| [`rowhammer`](picard/adapters/rowhammer_adapter.py) | DRAM bit flips via repeated row reads. Adjacent-row charge disturbance encodes bits into victim cells. | Linux x86-64, non-ECC DDR3/DDR4, root or `CAP_SYS_PTRACE`, huge pages |
| [`halfdouble`](picard/adapters/halfdouble_adapter.py) | Two-hop rowhammer. Near/far aggressor ratio bypasses DDR4 Target Row Refresh (TRR). | Same as `rowhammer`; effective on DDR4 that defeats classic rowhammer |
| [`throwhammer`](picard/adapters/throwhammer_adapter.py) | Remote DRAM bit flips over RDMA. The remote CPU is not involved -- the NIC DMA-reads directly to DRAM. | Linux, RDMA NIC (Mellanox/ConnectX), libibverbs, pyverbs |
| [`cpu-cache`](picard/adapters/cpu_cache_adapter.py) | CPU L3 SRAM via Flush+Reload. N_REPS=5 majority-vote encoding across page-isolated slots. BER below 5e-4 on i7-12700KF. | Linux or macOS x86-64, shared mmap file, same physical host |
| [`hdd-smart`](picard/adapters/hdd_smart_adapter.py) | HDD adaptive calibration tables via seek pattern conditioning. Power-cycle persistent. Unverified hypothesis. | Spinning-platter HDD, root, `smartmontools` |
| [`thermal`](picard/adapters/thermal_adapter.py) | CPU die temperature as a covert channel. Demonstration only -- heat dissipates. Not persistent storage. | Linux, sysfs thermal zones |
| [`mempool`](picard/adapters/mempool_adapter.py) | Ethereum transaction mempool as queued storage. Data in tx `input` field. No at-rest state on-chain. | Local geth node, `web3>=6.0` |
| [`latency`](picard/adapters/latency_adapter.py) | `tc netem` delay rules as bit-encoded kernel state. ~63 bytes. No disk. | Linux, `CAP_NET_ADMIN` |
| [`icmp-inflight`](picard/adapters/icmp_inflight_adapter.py) | ICMP payloads circulating on loopback. No at-rest state. ~87 KB. | Linux, `CAP_NET_RAW`, `scapy>=2.5` |
| [`rtp-inflight`](picard/adapters/rtp_inflight_adapter.py) | RTP payloads on a loopback call leg. No at-rest state. | stdlib only |
| [`proc-mem`](picard/adapters/proc_mem_adapter.py) | Anonymous memfd via companion daemon. No filesystem path. | Linux, companion daemon |
| [`sip-registrar`](picard/adapters/sip_registrar_adapter.py) | Data as SIP REGISTER location bindings. Persists until `Expires`. | RFC 3261 registrar |
| [`sip-presence`](picard/adapters/sip_presence_adapter.py) | Data as PIDF+XML subscription state. Persists until `Expires`. | RFC 3903 presence server |
| [`turn`](picard/adapters/turn_adapter.py) | Data as TURN relay allocation state. Background Refresh required. | RFC 5766 TURN server |
| [`gpu-vram`](picard/adapters/gpu_vram_adapter.py) | Data in GPU VRAM via persistent buffer. Companion process holds the allocation. | CUDA or OpenCL GPU, companion |

All Tier 5 substrates are ephemeral to varying degrees. See [docs/adapters.md](docs/adapters.md#tier-5-unconventional-substrates) for requirements and threat models.

---

## Cryptographic Posture

All symmetric: Argon2id key derivation, HKDF-SHA256 sub-keys, AES-256-GCM authenticated encryption, HMAC-SHA256 for transport auth and correlation tags. No public-key cryptography in the storage path.

AES-256 provides 128-bit security against Grover's algorithm on a fault-tolerant quantum computer, matching the security target of NIST post-quantum standards. The ciphertext format carries a version byte so the cipher suite can be swapped without re-encrypting stored data.

The local index is itself AES-256-GCM encrypted (`PICARD_INDEX_PASSPHRASE`). At rest, nothing is plaintext.

---

## Honest Downsides

PICARD is not magic, perhaps poetic. It is certainly not efficient, will not be fast, and offers zero guarantees.

- **Reads are slow.** Argon2id key derivation runs per block (~100ms on commodity hardware). A 1 MB file is ~16 blocks.
- **Writes may partially fail.** Network adapters depend on credentials, rate limits, and platform availability.
- **Chunk loss means data loss.** No global index, no recovery path if too many chunks go missing without a RAID profile.
- **Best for small, critical datasets** -- a few MB to low GB. Larger files compound retrieval slowness and chunk-loss risk.
- **Adapters age.** As protocols evolve or get patched, adapters may need updates.
- **Invisible to standard monitoring.** Chunks are opaque to conventional storage tools.
- **API adapters are stubs.** The Tier 3 cloud/SaaS adapters require credentials and working accounts. Many have not been tested against live infrastructure.

If you need uptime guarantees, deterministic performance, or expect everything to work without tinkering, this is not for you.

---

## Research and Legal Notice

PICARD is an experimental computer science research project exploring protocol-agnostic storage abstractions. It is published for educational, experimental, and interoperability research purposes under the MIT license.

Users are solely responsible for ensuring their use complies with applicable law and the terms of service of any third-party platform or network accessed through a PICARD adapter. **PICARD does not authorize access to systems, accounts, or infrastructure the user does not own or have explicit permission to use.**

<details>
<summary>Full dual-use notice and terms</summary>

Like most security and systems research, PICARD has dual-use capabilities: the same techniques that enable resilient personal storage can, in principle, be misused. Publishing this work does not endorse or encourage misuse; it serves the community's interest in understanding these surfaces so they can be studied, defended against, and properly accounted for.

**Acceptable use.** Any use of PICARD to violate applicable law (including but not limited to computer fraud and abuse statutes, electronic communications privacy law, or the Terms of Service of any platform) immediately and irrevocably voids any claim to acceptable use under this project. If you use PICARD to break the law or violate platform Terms of Service, you are entirely on your own. The authors provide no support, defense, or indemnification for unlawful or unauthorized use. This is not a disclaimer of liability; it is a boundary.

Each adapter's source includes a platform-specific notice. Read it before you use the adapter.

The full terms are in [TERMS.md](TERMS.md). On first run, `picard` will prompt you to accept them. Pass `--accept-terms` to accept permanently, or set `PICARD_ACCEPT_TERMS=1` for scripted use.

By using PICARD you accept full responsibility for your actions and their consequences. You agree to indemnify, defend, and hold harmless the authors, contributors, and maintainers of PICARD from and against any and all claims, liabilities, damages, losses, costs, and expenses arising out of or related to your use or misuse of this software.

**On ethics.** Ethics in this context is the mortar between the bricks, not an optional coat of paint. The premise of this research is not that protocol fields are available for any purpose; it is that understanding them as storage substrates is a useful contribution to the literature, and that the community benefits from knowing these surfaces exist. Exploration is fine, even necessary, but it comes with the obligation to think through collateral damage, disclose what you find, and leave breadcrumbs for defenders to address if they choose. Running PICARD responsibly means using it on infrastructure you control, resisting the urge to turn research findings into personal leverage, and being ready to stop when the experiment tips from curiosity into something else.

</details>

---

## Test Coverage and Contributing

The core cryptographic path, orchestrator, RAID layer (including Reed-Solomon erasure coding), index, and scatter profiles are fully tested (4,159 tests across 505 adapters). Many protocol adapters have unit tests with mocked transports; a significant portion has not been exercised against live infrastructure, particularly browser-automation paths and adapters for exotic protocols (RF, hardware buses, satellite, raw sockets). The Tier 3 cloud/SaaS adapters are validated structurally but not against live APIs.

**Pull requests are welcomed**, especially for:

- Integration tests for Tier 3 API adapters against live services
- Tests or fixes for adapter cold reads (cross-session retrieval)
- Real-world validation of exotic protocol adapters
- New adapters (include at least a mocked unit test covering `write_chunk()`, `read_chunk()`, and `delete_chunk()`)
- Documentation improvements and usage examples

Open an issue for bugs or questions.

---

## Documentation

| Doc | Contents |
|-----|---------|
| [Architecture](docs/architecture.md) | Data flow, key hierarchy, RAID, scatter profiles, erasure coding |
| [Adapters](docs/adapters.md) | Full adapter list, tier requirements, writing custom adapters |
| [CLI Reference](docs/cli.md) | All commands with examples |
| [MCP Server](docs/mcp.md) | Agent storage interface, setup, available tools |
| [FUSE Mount](docs/fuse.md) | Mounting, performance, platform notes |
| [WebDAV](docs/webdav.md) | Network drive mounting, native OS drag-and-drop |
| [Tor](docs/tor.md) | Anonymity, hidden services, .onion peers |
| [Network](docs/network.md) | Transport layer, peer discovery, remote adapters |
| [Identifying PICARD in the Wild](docs/identifying-picard-in-the-wild.md) | Observability, confidentiality, and attribution across 18 platforms |

---

## License

MIT. Copyright 2024–2026 Christopher Neitzert.

*In the wild, anything left open long enough becomes habitat.*
