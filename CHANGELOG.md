# Changelog

## 0.4.0 (unreleased)

### Added
- `mempool_adapter.py`: Ethereum transaction mempool as a storage substrate. EIP-1559 type-2 transactions with `nonce=current+1000` and `maxFeePerGas=0` sit in the queued pool indefinitely; data lives in the `input` field. Delete via replace-by-fee. Up to 128 KB per transaction. Requires a local geth node (`--txpool.lifetime 438000h --txpool.journal`). Optional dep: `web3>=6.0`.
- `latency_adapter.py`: Linux kernel traffic-shaping rules as a storage substrate. Each 127.x.0.x loopback address carries one data bit encoded as a `tc qdisc netem delay`. Read by pinging and measuring RTT against a calibrated threshold. Requires `CAP_NET_ADMIN` and `tc` (iproute2). Capacity ~63 bytes on loopback.
- `icmp_inflight_adapter.py`: ICMP echo payloads circulating on the loopback interface as a storage substrate. An AF_PACKET relay thread intercepts each echo-request and re-injects it, keeping the packet in flight indefinitely. No at-rest state; relay thread crash means data loss. Requires `CAP_NET_RAW` and scapy. Optional dep: `scapy>=2.5`. Capacity ~87 KB.
- `proc_mem_adapter.py`: Foreign process address space as a storage substrate. Primary mode uses `memfd_create` anonymous RAM-backed file descriptors passed to a companion daemon (`picard/proc_mem_companion.py`) via `SCM_RIGHTS` over a Unix socket. Fallback self-mode uses a cross-platform `mmap.mmap`. No filesystem path for the primary mode. Includes `--systemd-unit` flag for the companion daemon.
- `thermal_adapter.py`: CPU die temperature as a covert channel. Encodes bits by alternating CPU load between idle and busy states; reads back by sampling sysfs thermal zone sensors. Explicitly labeled as a demonstration channel, not persistent storage (heat dissipates on power cycle). Linux only. Throughput ~1 bit per 30 seconds.
- `hdd_smart_adapter.py`: HDD adaptive calibration tables as a storage substrate. Encodes bits by conditioning seek patterns to shift SMART attribute values; decodes by reading attribute deltas after a calibration baseline. Power-cycle persistent. Requires a spinning-platter HDD, root access, and `smartmontools`. Unverified hypothesis; use `tools/characterize_hdd_smart.py` to assess viability on your hardware before deploying.
- `picard/proc_mem_companion.py`: asyncio Unix socket daemon that holds `memfd_create` file descriptors on behalf of the main process, with `--systemd-unit` flag to emit a ready-made service unit.
- `tools/characterize_hdd_smart.py`: five-trial SMART calibration go/no-go script for the HDD SMART adapter.
- `tools/verify_icmp_inflight.py`: AF_PACKET loopback interception verification script for the ICMP in-flight adapter.
- Optional dependency extras: `mempool` (requires `web3>=6.0`), `icmp-inflight` (requires `scapy>=2.5`), `proc-mem`, `latency`, `thermal`, `hdd-smart` (all stdlib/system-tool only).
- New adapter category `unconventional-substrate` in CLI and web UI, with experimental warning in the configure modal.
- `sip_registrar_adapter.py`: SIP REGISTER state as a storage substrate. Encodes chunks in Contact header `+picard` parameters of synthetic AOR bindings. Raw UDP SIP, Digest MD5 auth (RFC 3261). No external deps. Data lives as telephony routing state until `Expires` lapses.
- `sip_presence_adapter.py`: SIP PUBLISH presence documents as a storage substrate. Chunks embedded in PIDF+XML `<picard:data>` elements published to synthetic AORs. ETag-tracked conditional updates. No external deps.
- `turn_adapter.py`: STUN/TURN relay allocation state as a storage substrate. Long-term HMAC-SHA1 credential, channel binding, background Refresh loop. Data lives in the relay server's RAM as routing state. No at-rest state; deallocation on delete. No external deps.
- `rtp_inflight_adapter.py`: RTP packet payloads circulating on UDP loopback port pairs as a storage substrate. SSRC-keyed relay thread re-injects each packet. No at-rest state. Cross-platform, no elevated privileges. No external deps.
- Optional dependency extras: `sip-registrar`, `sip-presence`, `turn`, `rtp-inflight` (all stdlib only).

- `tmpfs_xattr_adapter.py`: All chunks as extended attributes on a single /dev/shm sentinel file. No privileges needed. Linux xattr (user.picard.* namespace), 65535 bytes per chunk.
- `keyring_adapter.py`: Linux kernel session keyring via `keyctl padd/pipe/revoke`. No elevated privileges, 32 KB per key, evicted at session end.
- `dram_refresh_adapter.py`: Six bytes encoded across `/proc/sys/vm` kernel parameters (swappiness, dirty_ratio, etc.). Root required. Original values restored on delete. Single-chunk limit by design.
- `cpu_freq_adapter.py`: CPU P-state bit encoding via cpufreq sysfs. Transparent VM fallback encodes chunk in location JSON when no cpufreq is available.
- `arp_table_adapter.py`: Kernel neighbor table via `ip neigh replace` on a `dummy` interface (`picard-arp0`). CAP_NET_ADMIN required. 64 bytes capacity, one byte per ARP entry.
- `tcp_isn_adapter.py` + `tcp_isn_companion.py`: TCP Initial Sequence Number covert channel. Companion (scapy SYN listener + asyncio Unix socket) holds ISNs. In-process fallback when companion absent.
- `dns_question_adapter.py` + `dns_companion.py`: DNS TXT record storage via an authoritative companion on port 5354 (dnslib). Unix socket control channel. In-process fallback.
- `ntp_inflight_adapter.py`: Chunk data embedded in NTP fractional timestamp fields. In-process registry (demo mode) without companion. Fire-and-forget UDP.
- `gpu_vram_adapter.py` + `gpu_vram_companion.py`: OpenCL buffer storage via companion daemon (pyopencl). Falls back to in-process dict when companion absent or pyopencl unavailable.
- `mdns_zeroconf_adapter.py`: mDNS service TXT records via the `zeroconf` library. Registers `_picard._tcp.local.` services. LAN-visible. 1 KB per chunk.
- `wifi_beacon_adapter.py`: 802.11 vendor IE construction (OUI `f0:9f:c2`, type `01`). In-process registry, `generate_hostapd_conf()` helper for deployment. Requires hostapd and a Wi-Fi interface.
- `browser_indexeddb_adapter.py`: Browser IndexedDB via Playwright. Persistent across page reloads in the same context. 10 MB per chunk.
- `browser_cache_adapter.py`: Browser Cache API via Playwright with `page.route()` secure-context trick on localhost. 10 MB per chunk.
- `lightning_adapter.py`: Core Lightning hold invoices via `pyln-client`. Invoice is a durable commitment anchor; encoded chunk held in-process registry. Requires CLN node with `holdinvoice` plugin.
- `bitcoin_opreturn_adapter.py`: Bitcoin OP_RETURN outputs via `python-bitcoinlib`. 80 bytes per transaction. WARNING: data is permanent and public on mainnet. RBF delete is best-effort while in mempool. Regtest default.
- `ebpf_map_adapter.py`: Pinned `BPF_MAP_TYPE_HASH` via ctypes `bpf(2)` syscall. No external deps. Pinned at `/sys/fs/bpf/picard/`. 65535 bytes per value, survives process death. Requires CAP_BPF (Linux 5.8+).
- Reed-Solomon erasure coding via `zfec` (`picard/erasure.py`). Integrated into RAID layer as `RaidLevel.ERASURE` with configurable `erasure_k`/`erasure_m` params.
- Bug fixes: `sqlite_local` manifest_id parsing, `latency` read-after-delete guard and live baseline, `proc_mem` manifest_id parsing, `crontab` and `gitconfig` health_check false negatives.
- 16 new unit test files for novel substrate adapters.

### Changed
- Adapter count: 264 -> 290+.
- Version bumped to 0.4.0.

---

## 0.3.1 (2026-10-25)

### Fixed
- `cpu_cache_adapter.py`: BYTES_PER_BIT raised to 4096 (one OS page per slot) to defeat Intel SMS/IP prefetcher contamination; N_REPS=5 majority voting across independent physical slots drives post-vote BER below 5e-4 on i7-12700KF; per-write warmup pass and four flush sweeps added for reliable 0-bit eviction.
- `aiohttp` minimum bumped to 3.13.4: fixes CVE-2024-52303 (DNS cache growth), CVE-2024-52304 (multipart DoS), CVE-2024-52305 (multipart DoS), CVE-2024-52306 (Windows NTLM auth disclosure).

### Added
- `tools/test_cpu_cache_live.py`: live hardware test for the cpu_cache Flush+Reload channel (JIT compile, calibration, single-process roundtrip, cross-process covert channel, BER sweep).
- Experimental warning in CLI (`picard adapters`) and web UI configure modal for all four Tier 4 hardware-vulnerability adapters.
- `.github/dependabot.yml`: automated weekly dependency and Actions update PRs.

---

## 0.3.0 (2026-10-25)

### Added
- `rowhammer_adapter.py`: DRAM bit flips as a storage substrate. Double-sided rowhammer write primitive using x86-64 CLFLUSH+RDTSC JIT code, `/proc/pagemap` physical address resolution, `IntelSandyBridgeGeometry` bank XOR function, empirical profiler that discovers flippable bits without knowing the memory controller geometry, and full chunk encode/decode to DRAM bit positions. Ephemeral (power cycle erases). HAL-pattern: subclass for other memory controllers.
- `halfdouble_adapter.py`: Two-hop rowhammer extending the rowhammer substrate to DDR4 hardware with Target Row Refresh (TRR) mitigation. Near/far aggressor pattern (near accessed every iteration, far at 1/ratio rate) bypasses TRR counters that only protect directly adjacent rows. Inherits all profile and encoding logic from RowhammerAdapter.
- `throwhammer_adapter.py`: Remote DRAM bit flips over RDMA. Issues RDMA READ operations to a remote host's aggressor rows; the remote CPU is not involved at all (NIC DMA-reads directly to DRAM). Full pyverbs (libibverbs) implementation: QP setup, RESET/INIT/RTR/RTS state machine, one-sided RDMA READ/WRITE, TCP side-channel for QP parameter and rkey exchange. Requires RDMA-capable NIC on both ends.
- `cpu_cache_adapter.py`: CPU L3 SRAM as covert storage via Flush+Reload. Each logical bit is encoded across N_REPS=5 independent physical slots (4096 bytes/slot, one OS page each) in a shared file-backed mmap; majority vote across replicas drives BER below 5e-4 under typical load. Write: page-isolated slots prevent SMS/IP prefetcher contamination; four flush sweeps evict 0-bit slots after a full per-write warmup pass. Read: JIT RDTSC-timed access, threshold-gated, auto-calibrated on first use. Cross-process on same host; cross-VM applicable via KSM page deduplication.
- Tier 4 section in `docs/adapters.md` documenting all four substrates with hardware requirements and paper citations.
- Optional dependency extras: `rowhammer`, `halfdouble`, `cpu-cache` (stdlib only), `throwhammer` (requires pyverbs).

### Changed
- Adapter count: 260 -> 264.
- Version bumped to 0.3.0.

---

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
