# Changelog

## 0.2.0 (2026-10-12)

### Added
- Version gap detection in `fsck`: reports missing version numbers in the per-path version history
- Web dashboard screenshots in README with full UI overview and adapter registry views
- `docs/identifying-picard-in-the-wild.md`: three-layer observability analysis across 18 platforms
- Blockchain adapter rewrites: Nostr (BIP-340 Schnorr), Solana (compact-u16 + Memo v2), NEAR (Borsh + Ed25519), Ethereum (EIP-1559), Polygon (EIP-1559), Stellar (manage_data, 6144 bytes/chunk), Tezos (server-side forge + Ed25519 watermark), Cardano (pycardano + AlonzoMetadata label 8888)
- Optional dependency support: `pycardano`, `eth_account`/`web3`, `stellar_sdk` skipped gracefully in tests when not installed
- Intentional write-path friction in package registry adapters (PyPI, npm, crates.io, RubyGems) to prevent accidental use against public registries

### Changed
- README restructured: cleaner hero, collapsible origin story, screenshots before quickstart, CLI as tables, tier headings with colons, legal section as collapsible
- Version bumped to 0.2.0

### Fixed
- `fsck.py`: `version_gaps` field was declared but never computed; now implemented
- Solana `test_write_chunk`: mock updated to match rewritten adapter (two RPC calls, `_load_seed` patched)
- Stellar, NEAR, Tezos, Cardano, Polygon test mocks updated to match rewritten adapter internals
- Adapter count in README: 260 corrected to 259
- Test count in README: 4159 corrected to 4236 (now 4156 passing)

---

## 0.1.0 (2025-05-15)

Initial feature-complete release.

### Core
- AES-256-GCM encryption with Argon2id key derivation (per-block salt + nonce)
- Ciphertext format v2 with version + algorithm header for cipher agility
- Legacy format backward compatibility with automatic detection
- HKDF-SHA256 sub-keys per adapter with version rotation
- Sealed envelope format (indistinguishable from random data)
- PCHK wire format for chunk encoding
- Block cache (LRU, TTL, thread-safe, key zeroization)
- Derived key cache (HMAC-SHA256 keyed, TTL, zeroization on eviction)
- AESGCM cipher object caching alongside derived keys

### Storage
- 260 protocol adapters across 15+ categories
- Persistent aiohttp sessions with proper cleanup for 91 HTTP adapters
- RAID levels 0/1/5/10 across adapter pools with XOR parity reconstruction
- Scatter profiles: quiet, resilient, speed, paranoid
- Parallel block scatter (8-way semaphore), parallel verify, stream prefetch
- Adapter config validation (name, RAID level, block size bounds)

### Filesystem
- Full filesystem semantics: directories, symlinks, versioning, partial reads
- FUSE mount (Linux/macOS/Windows via WinFSP)
- WebDAV server with Basic auth for native OS file manager access
- Journaling with crash recovery
- fsck integrity checking and repair
- Streaming I/O for files larger than RAM

### Interfaces
- CLI with 35+ commands, progress bars, --json output
- Web dashboard (dark/light theme, drag-and-drop, storage health)
- MCP server for agent-accessible storage
- HTTP transport with HMAC-SHA256 auth and replay protection
- Peer discovery

### Error Handling
- CLI: Clean error messages for all 20+ commands (no raw tracebacks)
- MCP: Structured error responses for all tool failures
- Web: Proper HTTP status codes (409 for non-empty directory delete)

### Security
- Post-quantum symmetric-only data path (128-bit security vs Grover)
- CSRF protection, XSS prevention, security headers
- Path traversal prevention, SSRF blocking
- SQL injection prevention (PostgreSQL adapter)
- GraphQL injection prevention (Arweave adapter)
- Input validation on all MCP tool inputs
- WebDAV Basic auth
- Config key whitelist

### Networking
- Tor integration: per-adapter SOCKS5 routing, hidden services, .onion peers
- VPN device binding per adapter
- Browser automation (Playwright headless/headed fallback)

### Infrastructure
- Encrypted index (AES-256-GCM at rest)
- Recursive CTE tree queries
- Bulk list queries (eliminate N+1)
- Thread-safe index writes, journal atomicity
- GitHub Actions CI (3 OSes x 3 Python versions)
- 4159 tests
