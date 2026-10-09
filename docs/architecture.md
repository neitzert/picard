# Architecture

## Data Flow

```
                           PICARD Data Pipeline
                           ====================

File (plaintext bytes)
  |
  v
[SHA-256 hash] -- stored in manifest for integrity verification
  |
  v
[Split into blocks] -- default 64 KiB per block
  |
  v
[Per-block encryption] -- AES-256-GCM, independent salt + nonce per block
  |
  v
[Block digest] -- SHA-256 of ciphertext, stored in manifest
  |
  v
[Chunkify] -- split ciphertext block into adapter-sized PCHK chunks
  |
  v
[Envelope seal] -- (optional) wrap with per-adapter sub-key, removes PCHK header
  |
  v
[Scatter] -- round-robin across adapters (or RAID pool)
  |
  v
[Index] -- SQLite records: manifest -> blocks -> chunks -> adapter locations
```

Retrieval reverses the pipeline: gather chunks from adapters, unseal envelopes,
reassemble blocks, verify digests, decrypt, verify plaintext hash.

## Encryption

PICARD uses AES-256-GCM for authenticated encryption with Argon2id for key derivation.

### Key Derivation

```
passphrase --> Argon2id --> 32-byte master key
                 |
                 +-- time_cost:   3
                 +-- memory_cost: 65536 (64 MiB)
                 +-- parallelism: 4
                 +-- salt:        16 random bytes (per block)
```

Every block generates a fresh random salt and nonce, so identical plaintext blocks
produce different ciphertext. This means each block is independently decryptable
with no inter-block state.

### Ciphertext Format (v2)

```
[version 1B][alg 1B][salt 16B][nonce 12B][AES-256-GCM ciphertext + 16B GCM tag]
```

Version is `0x02`. Algorithm `0x01` denotes AES-256-GCM with Argon2id key derivation.
The version and algorithm bytes enable future cipher rotation without re-encrypting
every stored chunk across all adapters.

The `encrypt()` function always emits v2 format. The `decrypt()` function accepts
both v2 and legacy format (no version/alg prefix) for backward compatibility with
data written before the header was introduced.

### Derived Key Cache

Since Argon2id is intentionally expensive (~100ms per call at these parameters),
a `DerivedKeyCache` stores recently derived keys keyed by `(HMAC-SHA256(passphrase), salt)`,
where the HMAC uses a per-boot random 32-byte secret so cache keys cannot be brute-forced
from a core dump.
Max 16 entries, 5-minute TTL. The raw passphrase is never stored.

## Key Hierarchy

For multi-adapter deployments, PICARD supports a key hierarchy:

```
passphrase (or keyfile)
  |
  v
Argon2id --> master key (32 bytes)
               |
               v
           HKDF-SHA256 --> sub-key per adapter (keyed by adapter name + version)
               |
               v
           AES-256-GCM --> sealed envelope per chunk
```

The `KeyRing` manages sub-key derivation and rotation. Version-based rotation
allows key changes without re-encrypting existing data.

## PCHK Wire Format

Each chunk is encoded as a tagged string:

```
PCHK:<version>:<manifest_id>:<block_index>:<chunk_index>:<total_chunks>:<base64url_payload>
```

Fields:
- `version`: protocol version (currently `1`)
- `manifest_id`: hex UUID linking chunk to its manifest
- `block_index`: which block of the file (0-based)
- `chunk_index`: which chunk of the block (0-based)
- `total_chunks`: how many chunks in this block
- `base64url_payload`: the actual ciphertext fragment

## Sealed Envelope Format

When a KeyRing is active, PCHK strings are wrapped in an opaque binary envelope:

```
[version 1B][correlation_tag 8B][nonce 12B][ciphertext + GCM tag]
```

The ciphertext contains the original PCHK string, encrypted with the adapter's
HKDF-derived sub-key. The correlation tag is derived deterministically from
the manifest ID so chunks can be grouped without revealing content.
Total overhead: 37 bytes (1 + 8 + 12 + 16 GCM tag).

The sealed format is indistinguishable from random data to anyone without the sub-key.

## Index

The index is a SQLite database with three core tables:

```sql
manifests   -- one row per file version (path, size, hash, metadata, timestamps)
blocks      -- one row per encrypted block (digest, size)
chunks      -- one row per scattered chunk (adapter, location, status, digest)
```

Plus supporting tables for journal entries and peer registry.

Indexes on `path`, `parent_path`, and `(path, version)` support efficient
directory listing, tree traversal, and version lookup.

The index can be encrypted at rest using the PCIX format: the entire database
is dumped to SQL, encrypted with AES-256-GCM, and written with a `PCIX` magic header.
On open, the encrypted blob is decrypted into an in-memory database.
Activate via `--index-passphrase` on any CLI command or set `PICARD_INDEX_PASSPHRASE`.
Use `picard encrypt-index` / `picard decrypt-index` to convert existing databases.

## RAID Levels

The `RaidPool` wraps multiple adapters and implements the `Adapter` interface:

| Level | Strategy | Min Adapters | Capacity |
|-------|----------|-------------|----------|
| RAID 0 | Stripe chunks round-robin | 2 | Sum of all |
| RAID 1 | Mirror every chunk to N adapters | 2 | Smallest member |
| RAID 5 | XOR parity across pool, one parity chunk per stripe | 3 | (N-1)/N of total |
| RAID 10 | Striped mirrors (mirror groups, striped across groups) | 4 | Half of total |

RAID references are packed as JSON containing each member adapter's ref,
so a single `RaidRef` encodes the locations across all pool members.

## Scatter Profiles

Four built-in profiles control how adapters are selected and grouped:

| Profile | RAID Level | Strategy |
|---------|-----------|----------|
| stealth | RAID 1 | Mirror to multiple adapters for deniability |
| resilient | RAID 5 | Parity for fault tolerance |
| speed | RAID 0 | Stripe for throughput |
| paranoid | RAID 10 | Striped mirrors for maximum redundancy |

Custom profiles are defined in `config.toml` under `[scatter.<name>]` sections.

## Filesystem Layer

PICARD provides real filesystem semantics on top of the scatter layer:

- **FileType enum**: `regular`, `directory`, `symlink`
- **Manifest metadata**: mode, mtime/ctime/atime, uid/gid, nlink, xattrs
- **Directories**: `mkdir` (with `parents=True`), `rmdir` (empty check), `tree`
- **Symlinks**: `symlink`, `readlink`, `get` follows symlinks (40-hop loop detection)
- **Versioning**: auto-incrementing version per path, `get_version`, `list_versions`, `rollback`
- **Partial reads**: `get_block` (single block), `read_range` (byte offset + length)

### Block Cache

An LRU cache for decrypted plaintext blocks:
- Max 1024 entries, 64 MiB, 5-minute TTL
- Thread-safe (threading.Lock)
- Per-manifest invalidation
- Used by FUSE mount and `read_range`

### Journaling

A write-ahead log (WAL) in the same SQLite database as the index:
- `put()` creates a PENDING entry, updates to IN_PROGRESS after each block
- `save_manifest` and `journal.commit` execute in a single SQLite transaction for atomicity
- On failure: entry is ROLLED_BACK, orphan chunks can be cleaned
- `recover()` scans for incomplete entries and cleans up
- `fsck` checks for incomplete journal entries

### Streaming I/O

For files larger than RAM:
- `put_stream(path, async_iterator, passphrase)` — encrypts and scatters block-by-block as data arrives
- `get_stream(path, passphrase)` — yields decrypted blocks one at a time
