# PICARD

**Protocol-Introspecting Chunk Adapter for Resilient Data**

Encrypt, chunk, and scatter data across protocol gaps — then pull it back together on demand.

## What it does

PICARD takes a file, encrypts it (AES-256-GCM, Argon2id key derivation), splits the ciphertext into tagged chunks, and distributes those chunks across pluggable **adapters** — each adapter speaks one protocol or platform.  To retrieve the file, PICARD gathers the chunks, verifies them, reassembles the ciphertext, and decrypts.

Currently implemented adapters:
- **memory** — in-process dict (for tests)
- **filesystem** — chunks as files on disk (for local testing)
- **bluesky** — ATProto blobs on Bluesky PDS (`pip install picard[bluesky]`)
- **reddit** — Reddit wiki pages as chunk storage (`pip install picard[reddit]`)
- **dns** — DNS TXT records via RFC 2136 dynamic updates (`pip install picard[dns]`)
- **cloudflare-dns** — DNS TXT records via Cloudflare API (`pip install picard[cloudflare]`)
- **mqtt** — retained MQTT messages (`pip install picard[mqtt]`)
- **icmp** — raw ICMP echo request/reply payloads (requires root/CAP_NET_RAW)

## Quickstart

```bash
# Install in development mode
pip install -e ".[dev]"

# Run the test suite
pytest

# Use the CLI
picard put myfile.txt --as /notes/myfile.txt
picard ls
picard get /notes/myfile.txt -o recovered.txt
picard rm /notes/myfile.txt
```

## Architecture

```
Orchestrator
├── Crypto (AES-256-GCM + Argon2id)
├── Chunker (split → tag → encode)
├── Index (SQLite manifest store)
└── Adapters
    ├── memory
    ├── filesystem
    ├── bluesky
    ├── reddit
    ├── dns
    ├── cloudflare-dns
    ├── mqtt
    └── icmp
```

## Project structure

```
picard/
  pyproject.toml
  picard/
    __init__.py
    crypto.py          # Encryption + key derivation
    chunker.py         # Block → sized+tagged chunks
    manifest.py        # Metadata model (file → blocks → chunks)
    index.py           # SQLite manifest/chunk store
    orchestrator.py    # put / get / ls / rm pipeline
    cli.py             # Typer CLI
    config.py          # TOML/env configuration loader
    registry.py        # Adapter discovery + instantiation
    adapters/
      base.py          # Adapter Protocol + AdapterProfile
      memory.py        # In-memory (tests)
      filesystem.py    # On-disk (local testing)
      bluesky.py       # ATProto / Bluesky PDS
      reddit.py        # Reddit wiki pages
      dns.py           # DNS TXT records (RFC 2136)
      cloudflare_dns.py # DNS TXT via Cloudflare API
      mqtt.py          # MQTT retained messages
      icmp.py          # ICMP echo payloads
  tests/
```

## Requirements

- Python 3.11+
- Core: `cryptography`, `argon2-cffi`, `typer`
- Adapter extras: `atproto`, `praw`, `dnspython`, `httpx`, `paho-mqtt`
- Dev: `pytest`, `pytest-asyncio`

## License

MIT
