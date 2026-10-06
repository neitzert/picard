# PICARD

**Protocol-Introspecting Chunk Adapter for Resilient Data**

Encrypt, chunk, and scatter data across protocol gaps — then pull it back together on demand.

## What it does

PICARD takes a file, encrypts it (AES-256-GCM, Argon2id key derivation), splits the ciphertext into tagged chunks, and distributes those chunks across pluggable **adapters** — each adapter speaks one protocol or platform.  To retrieve the file, PICARD gathers the chunks, verifies them, reassembles the ciphertext, and decrypts.

Currently implemented adapters:
- **memory** — in-process dict (for tests)
- **filesystem** — chunks as files on disk (for local testing)

Planned adapters:
- Bluesky / ATProto
- Reddit

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
    ├── bluesky (planned)
    └── reddit (planned)
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
    adapters/
      base.py          # Adapter Protocol + AdapterProfile
      memory.py        # In-memory (tests)
      filesystem.py    # On-disk (local testing)
  tests/
```

## Requirements

- Python 3.11+
- Dependencies: `cryptography`, `argon2-cffi`, `typer`
- Dev: `pytest`, `pytest-asyncio`

## License

MIT
