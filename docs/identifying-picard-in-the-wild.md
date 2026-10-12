# Identifying PICARD in the Wild

*Observability, Confidentiality, and Attribution: A Three-Layer Analysis*

**Author:** Christopher Neitzert · October 2026

---

## Purpose

This document characterises how PICARD-scattered data appears across public platforms to an observer who encounters it without a passphrase, without a PICARD client, and without prior knowledge of the protocol.

It is intended for platform operators who want to understand what PICARD traffic looks like on their infrastructure, for security researchers studying protocol-layer steganographic storage, and for anyone evaluating the privacy and attribution properties of PICARD as a research instrument.

PICARD is best understood as a **systems-security research instrument with a working storage implementation**, not as a replacement for conventional cloud storage. Its value lies in making the relationship between protocol design, storage behaviour, cryptography, and observability testable against real platforms. The implementation exists to make these claims concrete, not theoretical.

---

## The Three-Layer Model

Any analysis of "what an observer can see" should separate three distinct questions:

| Layer | Question | What PICARD exposes |
|-------|----------|---------------------|
| **Protocol observability** | What is visible in the raw content of a chunk? | PCHK prefix, manifest_id, and block/chunk indices (intentionally visible) |
| **Content confidentiality** | What remains inaccessible without the key? | All plaintext; the encrypted payload is computationally opaque |
| **Operational attribution** | What can a platform operator or investigator learn from account records, API logs, and network activity? | Depends entirely on the adapter: wallet addresses, usernames, upload IPs, account metadata |

These are independent questions with different answers. The protocol is designed to expose layer 1 fully and protect layer 2 fully. Layer 3 is not a cryptographic property; it depends on which adapter is used, how accounts are provisioned, and what the platform operator logs.

---

## The PCHK Wire Format

Every chunk that PICARD writes to any platform uses the same wire format:

```
PCHK:<version>:<manifest_id>:<block_index>:<chunk_index>:<total_chunks>:<payload>
```

**Example:**
```
PCHK:2:a3f8e2c1b4d90567:0:0:3:AgGVm3K8nQpR7xY2wZdE1c4hTjsFvBiUoN6XPeHqLkA9MfCgDr0bOsIyuJWZnRT5...
```

| Field | Visible? | What an observer can infer |
|-------|----------|---------------------------|
| `PCHK` | Yes | This string is a PICARD chunk |
| `version` | Yes | Protocol generation (current: 2) |
| `manifest_id` | Yes | All chunks with the same value belong to the same encrypted file |
| `block_index` | Yes | Which encryption block this chunk came from |
| `chunk_index` | Yes | Position of this chunk within its block |
| `total_chunks` | Yes | How many chunks this block was split into; approximate lower bound on file size |
| `payload` | Yes (bytes) | Nothing. AES-256-GCM ciphertext is computationally indistinguishable from random bytes without the key. |

An observer who finds a PCHK string learns the file's structure but not its contents.

---

## Intentional Observability

**The PCHK prefix is intentionally visible.** This is a design decision, not an oversight.

PICARD's security rests entirely on AES-256-GCM encryption and Argon2id key derivation. The wire format is not a security boundary; the encryption is. Hiding the prefix would add no cryptographic protection while removing two things that matter:

1. **Platform operator agency.** Any platform that wishes to detect or restrict PICARD usage can do so on the basis of the `PCHK:` prefix alone, without decrypting any content. The protocol does not try to hide from platform operators. Designs that obscure their own format from defenders are adversarial tools; designs that are openly identifiable leave operators in control of their own infrastructure.

2. **Auditability and cleanup.** Because every chunk carries a manifest_id, a PICARD user can locate their own data, verify it, and clean it up. A protocol that hides even its own existence from its operator cannot be governed or removed. That is a property of malware, not research software.

The `PCHK:` prefix is the breadcrumb the ethics section of the README commits to leaving. This document is its companion.

---

## The manifest_id as a Research Signal

The manifest_id is a **globally consistent, cross-platform identifier** present in every chunk of a file, regardless of which adapter stored it. This is intentional.

An observer who finds one PCHK chunk on Nostr and another on PyPI with the same manifest_id can confirm they belong to the same file upload, without decrypting either. This cross-platform correlation is a designed observable, and it makes PICARD useful as a research instrument in a specific way: it gives investigators a defined signal and lets researchers measure how well that signal can be followed across different storage systems.

The detection ratings in this document should be treated as **hypotheses**, not verdicts. A more rigorous version of this research would:

- Scatter a file across N platforms
- Give an observer one chunk and the manifest_id
- Measure how many remaining chunks they can locate, how long it takes, and whether they need content inspection or behavioural signals
- Repeat across monitoring systems (platform-native search, passive DNS, blockchain explorers, archive crawlers)

That methodology would produce false-positive rates, detection rates, and platform-by-platform signal strengths. The qualitative ratings below are starting points for that experiment.

---

## The HKDF Subkeys and manifest_id: Explicit Relationship

The README describes per-adapter HKDF-derived subkeys and correlation tags (via `picard/envelope.py`). These operate at the **transport authentication layer**, not the wire format layer. They prevent an observer with chunks from one adapter from inferring the encryption key, and they create per-adapter HMAC tags for adapter-level authentication.

The **manifest_id** operates at the **wire format layer** and is globally consistent across all adapters by design.

These serve different purposes and do not contradict each other, but the relationship needs to be stated clearly:

> If an observer has visibility across multiple adapters simultaneously, or if they can correlate activity across platforms by other means, the manifest_id creates a cross-platform fingerprint that the per-adapter HKDF compartmentalisation does not suppress.

**Single-adapter use:** the HKDF subkeys are the relevant protection layer. An observer with chunks from one platform cannot infer the key from the chunk content alone.

**Multi-adapter scatter:** the manifest_id is a designed cross-platform signal. If you scatter across Reddit, Ethereum, and PyPI, an investigator with access to all three can link all those chunks to the same file. The encryption still protects the content; the structure of the scatter is visible.

This is not a flaw. It is the behaviour the research is designed to study.

---

## Platform Analysis

The following sections cover the 18 adapter categories in the current release. Each entry covers all three observability layers.

---

### Package Registries

#### PyPI

**Protocol observability:** The chunk appears in the package `long_description` field, rendered as the README on the package page. The `PCHK:` prefix and all wire format fields are visible to anyone browsing the page.

**Content confidentiality:** The AES-256-GCM payload is opaque. A moderator sees base64url text with no recoverable meaning.

**Operational attribution:** PyPI requires a registered account with a verified email address. Upload requests carry the uploader's IP address in PyPI's server logs. The package author field is set at publish time and is public. Account credentials are retained by PyPI indefinitely.

**Retention:** PyPI does not delete old package versions. A yanked version remains in the index; the uploaded files remain on the CDN. The chunk persists even after a yank.

**Detectability hypothesis:** Low. No automated README scanning for encoded data exists at PyPI at time of writing. A human reviewer examining a package with a README section that is pure base64url would find it unusual.

---

#### npm

**Protocol observability:** The chunk appears in the package README. All PCHK fields are visible.

**Content confidentiality:** Payload is opaque.

**Operational attribution:** npm requires a registered account. The package author and publish timestamp are public. npm's access logs retain uploader IP. An `npm audit` or security scan focuses on dependency graphs and known-malicious code; README content is not analysed.

**Retention:** npm deprecates but does not delete packages. The 72-hour unpublish window closes permanently; after that, the version is immutable.

**Detectability hypothesis:** Low. npm security infrastructure targets code, not metadata text.

---

#### Crates.io

**Protocol observability:** The chunk appears in the crate README. All PCHK fields are visible.

**Content confidentiality:** Payload is opaque.

**Operational attribution:** Crates.io uses GitHub OAuth. The publisher's GitHub account is permanently associated with the crate. Upload IP is in crates.io server logs.

**Retention:** Crates.io forbids version deletion. A yanked version remains downloadable. The chunk is permanent.

**Detectability hypothesis:** Very low. The Rust ecosystem has no automated README scanning. Crates.io does not review README content at publish time.

---

#### RubyGems

**Protocol observability:** The chunk appears in the gem README. All PCHK fields are visible.

**Content confidentiality:** Payload is opaque.

**Operational attribution:** RubyGems requires a registered account with email. The gem owner is public. Upload IP is in server logs.

**Retention:** RubyGems allows deletion within a 30-day window. After 30 days, the version is permanent.

**Detectability hypothesis:** Low. No automated content scanning. A human reviewer examining the gem's page would see an unusual README section.

---

### Social and Forums

#### Nostr

**Protocol observability:** PICARD posts kind:1 notes with the chunk as the full note content. All PCHK fields are visible on any relay that accepted the note.

**Content confidentiality:** Payload is opaque.

**Operational attribution:** The Nostr public key is the posting identity. It is permanently attached to every note and is globally visible. Unlike platform accounts, a Nostr keypair has no registration record; attribution requires linking the public key to a real-world identity through external signals (relay sign-up records, IP logs at specific relays, and cross-correlation with other platforms). The BIP-340 signature over each event is cryptographically verifiable: anyone can confirm the note was signed by the key that claims authorship.

**Retention:** Notes persist on any relay that accepted them. NIP-09 delete events are advisory; relays may ignore them. Multiple relays receiving the note means multiple independent retention decisions.

**Detectability hypothesis:** Low to medium. The note content is obviously automated: a single chunk with no surrounding context is atypical. Valid signatures pass relay verification. The note will survive automated spam filters that focus on volume and link patterns.

---

#### Reddit

**Protocol observability:** The chunk appears as post body text (self-post). All PCHK fields are visible.

**Content confidentiality:** Payload is opaque.

**Operational attribution:** Reddit account username is public on the post. Reddit logs IP, device fingerprint, and session data for all submissions. Account age and karma are visible and may indicate a purpose-created account. Third-party archive services (Pushshift, Arctic Shift) index posts shortly after submission; even a removed post may persist in those archives.

**Retention:** Variable. AutoModerator on some subreddits flags encoded content. Removed posts persist in Reddit's soft-delete storage and are recoverable by Reddit admins. Third-party archives may retain the content regardless of moderation action.

**Detectability hypothesis:** High. AutoModerator frequently flags large blocks of base64-encoded text. The pattern of posting encoded content with no context matches known spam and exfiltration patterns.

---

#### Twitter / X

**Protocol observability:** Each tweet contains one chunk. The 280-character limit constrains chunk size; larger files produce a reply thread. All PCHK fields are visible in each tweet.

**Content confidentiality:** Payload is opaque.

**Operational attribution:** Account username is attached to every tweet. Twitter/X retains phone number, email, registration IP, and login history. A thread of tweets containing only PCHK strings with no human-readable context is easily identifiable as automated posting.

**Retention:** Tweets persist until account deletion or platform enforcement. The owner can delete individual tweets; there is no undelete.

**Detectability hypothesis:** High. Spam classifiers will likely suppress the account's visibility. The pattern of posting structured base64url strings in rapid succession is strongly associated with bot accounts.

---

#### Usenet (NNTP)

**Protocol observability:** PICARD posts a plain-text article to an alt.binaries group. All PCHK fields are in the article body. The article headers include Message-ID, newsgroup, and date.

**Content confidentiality:** Payload is opaque.

**Operational attribution:** The NNTP account credentials used to post are held by the provider (e.g., Newshosting, Giganews). The posting IP is in the article's `X-Forwarded-For` or `NNTP-Posting-Host` header unless the provider strips it. Article Message-IDs are unique and may be traceable to a specific provider.

**Retention:** Commercial providers retain alt.binaries content for 3,000+ days on average. Articles are replicated across all subscribing servers globally. Cancel messages are advisory and largely ignored.

**Detectability hypothesis:** Very low. Binaries groups receive hundreds of posts per day. A short article that resembles a binary segment or MIME attachment is invisible in the volume.

---

#### MediaWiki

**Protocol observability:** The chunk is written to a user sandbox page or talk-page subsection via the MediaWiki action API. All PCHK fields are in the page wikitext.

**Content confidentiality:** Payload is opaque.

**Operational attribution:** If the account is logged in, the username is attached to the edit in the revision history. If anonymous, the IP address is recorded. Every edit creates a revision that persists in the database even after page deletion (visible to admins via "undelete"). Wikipedia and major wikis publish regular data dumps that may include sandbox edits.

**Retention:** All revisions are retained server-side. Pages can be deleted but revisions are recoverable by admins. The chunk may appear in public data dumps.

**Detectability hypothesis:** Low. User sandbox pages are largely unpatrolled. Automated tools monitor for vandalism and copyright violations, not encoded content. A bot-flagging tool might flag the account for unusual edit patterns.

---

#### Google Maps Reviews

**Protocol observability:** The chunk appears as the full text of a review. All PCHK fields are visible to anyone viewing the business listing.

**Content confidentiality:** Payload is opaque.

**Operational attribution:** The review is permanently attached to the Google account. Google retains account registration information, login history, and IP. The business owner can see and flag the review.

**Retention:** Reviews persist until Google moderation removes them or the account owner deletes them. Google's review moderation focuses on fake sentiment; a 5-star review consisting of base64url text is atypical and may trigger human review.

**Detectability hypothesis:** Medium. Automated sentiment analysis will not flag the content. The business owner may report it as unusual. A human moderator would find it unambiguously anomalous.

---

### Blockchain

For all blockchain adapters: once a transaction is confirmed to sufficient depth, the data is **immutable**. No party, including the original sender, can alter or delete it. Operational attribution via the sending address is permanent and public.

#### Ethereum

**Protocol observability:** The chunk is calldata in a transaction to a storage contract. The raw hex input is visible on Etherscan and any archive node. Decoding the calldata to UTF-8 reveals the PCHK string.

**Content confidentiality:** Payload is opaque.

**Operational attribution:** The sending wallet address is permanently public on-chain. Gas fees are paid from that address, creating a permanent financial record. Archive nodes and full nodes retain calldata indefinitely. The contract address is also public and links all PICARD writes using the same contract deployment.

**Retention:** Permanent. Archive nodes retain all historical calldata.

**Detectability hypothesis:** Low at the content level: the calldata requires a UTF-8 decode step that is not performed by default block explorers. Medium at the behavioural level: a wallet that repeatedly calls the same contract with varying calldata lengths is a detectable pattern.

---

#### Polygon

**Protocol observability:** The chunk is calldata in a zero-value self-transfer (sender == recipient). This is a common on-chain memo pattern. The PCHK string is visible in the decoded calldata.

**Content confidentiality:** Payload is opaque.

**Operational attribution:** Sending wallet address is permanent and public. Self-transfers with calldata are routine on Polygon, so the pattern is less distinctive than on Ethereum.

**Retention:** Permanent. Checkpointed to Ethereum approximately every 30 minutes.

**Detectability hypothesis:** Very low. The transaction type is indistinguishable from normal memo usage.

---

#### Solana

**Protocol observability:** The chunk is posted as a Memo v2 program instruction. The memo text is the PCHK string, visible in any transaction inspector.

**Content confidentiality:** Payload is opaque.

**Operational attribution:** The signing keypair's public key is the permanent sender identity. Solana transaction fees are minimal; the account balance history is public. Memo instructions are ubiquitous on Solana, used by wallets, DEXes, and analytics tools.

**Retention:** Permanent on-chain. Retrieval of older transactions requires an archive RPC provider; standard validator nodes do not retain all historical slots.

**Detectability hypothesis:** Very low. The Memo program is one of the most-used programs on Solana. A memo containing a PCHK string is distinguishable only if the observer is specifically scanning memo text for the `PCHK:` prefix.

---

#### NEAR Protocol

**Protocol observability:** The chunk is a FunctionCall action to a deployed storage contract. The method name and JSON arguments (including the chunk value) are visible in NEAR Explorer.

**Content confidentiality:** Payload is opaque.

**Operational attribution:** The signing account ID is attached to every transaction and is human-readable (e.g., `user.near`). NEAR account IDs are public and permanent. The contract address is visible and links all PICARD writes to the same deployment.

**Retention:** Permanent. NEAR stores full transaction history on-chain.

**Detectability hypothesis:** Low to medium. The method name and contract address are visible signals. A researcher scanning for calls to a known PICARD storage contract would find them. The chunk content requires no decoding to surface the PCHK prefix.

---

#### Stellar

**Protocol observability:** The chunk is split into 64-byte segments stored as `manage_data` operations. Key names follow the pattern `pcd-{chunk_id}-{seq}`. These key names are visible in Horizon's operation records. Segment values are returned by Horizon as base64-encoded bytes.

**Content confidentiality:** Payload is opaque.

**Operational attribution:** The source account public key is attached to every transaction and is permanently public. The `pcd-` key name prefix is a protocol-level fingerprint visible in the operation metadata.

**Retention:** Permanent in ledger history via Horizon.

**Detectability hypothesis:** Low. The `pcd-` prefix in manage_data key names is the primary fingerprint. A scan for manage_data operations with keys matching this pattern would surface PICARD usage. The pattern is currently unique to PICARD.

---

#### Tezos

**Protocol observability:** PICARD originates a minimal contract with the chunk as the initial storage string. The KT1 contract address and its storage are publicly readable. The origination operation hash is the chunk location.

**Content confidentiality:** Payload is opaque.

**Operational attribution:** The originating tz1 address is permanently public on-chain. There are thousands of abandoned zero-balance KT1 contracts on Tezos from earlier experimental periods; a new origination with a long string in storage is not visually distinctive. The TzKT indexer records and indexes all originations.

**Retention:** Permanent. The contract and its storage are part of the Tezos chain state.

**Detectability hypothesis:** Very low. The pattern of a zero-balance contract with a long string as its sole storage value is indistinguishable from experimental contracts. Targeted scanning for PCHK strings in contract storage is possible but requires deliberate effort.

---

#### Cardano

**Protocol observability:** The chunk is attached as transaction metadata under label 8888. Label 8888 has no registered meaning in the Cardano CIP-10 metadata registry. The metadata is visible in any Cardano blockchain explorer.

**Content confidentiality:** Payload is opaque.

**Operational attribution:** The sending payment address is permanently public on-chain. The metadata label 8888 is a detectable signal if investigators are specifically scanning for it.

**Retention:** Permanent. Transaction metadata is sealed in the UTXO ledger.

**Detectability hypothesis:** Low. The transaction looks like any custom-application metadata write. Targeted scanning for label 8888 with a `chunk` key containing a PCHK string would surface PICARD usage.

---

### Infrastructure

#### ACME / DNS TXT Records

**Protocol observability:** The chunk is stored in a DNS TXT record under a patterned subdomain. It resembles an un-cleaned ACME DNS-01 challenge response (`_acme-challenge.chunk-00.example.com`). The PCHK string is the TXT record value, visible via any DNS query tool.

**Content confidentiality:** Payload is opaque.

**Operational attribution:** The domain registrant is publicly visible in WHOIS (or RDAP) unless a privacy proxy is used. The DNS provider account is held by the registrant. Passive DNS services (SecurityTrails, CIRCL, DNSDB) log TXT record history and retain it indefinitely, long after the live record is deleted.

**Retention:** The live record is deletable by the domain owner. Passive DNS logs are permanent and outside the owner's control.

**Detectability hypothesis:** Low for live use. Passive DNS history is permanent and indexed by commercial threat-intelligence providers. A researcher scanning passive DNS for PCHK strings in TXT records would find historical records even if the live record no longer exists.

---

## Platform Comparison Matrix

| Platform | Category | Protocol signal | Operational attribution | Retention | Reversible | Detectability |
|----------|----------|----------------|------------------------|-----------|-----------|---------------|
| PyPI | Package | PCHK in README | Account, upload IP | Permanent (yank only) | Yank only | Low |
| npm | Package | PCHK in README | Account, upload IP | Permanent after 72 h | 72 h window | Low |
| Crates.io | Package | PCHK in README | GitHub account, IP | Permanent | No deletion | Very low |
| RubyGems | Package | PCHK in README | Account, email, IP | Permanent after 30 d | 30-day window | Low |
| Nostr | Social | PCHK note content | Public key | Indefinite | Advisory delete | Low–Medium |
| Reddit | Social | PCHK post body | Username, IP, device | Archived externally | Soft-delete | High |
| Twitter/X | Social | PCHK tweet text | Account, phone, IP | Until ban/deletion | Owner delete | High |
| Usenet | Forums | PCHK article body | Provider account, IP | 3,000+ days | Cancel ignored | Very low |
| MediaWiki | Wiki | PCHK page wikitext | Username or IP | All revisions retained | Soft-delete | Low |
| Google Maps | Reviews | PCHK review text | Google account | Until policy action | Owner or Google | Medium |
| Ethereum | Blockchain | PCHK in calldata | Wallet address (permanent) | Permanent | Immutable | Low |
| Polygon | Blockchain | PCHK in calldata | Wallet address (permanent) | Permanent | Immutable | Very low |
| Solana | Blockchain | PCHK memo text | Keypair pubkey (permanent) | Permanent | Immutable | Very low |
| NEAR | Blockchain | PCHK in call args | Account ID (permanent) | Permanent | Immutable | Low–Medium |
| Stellar | Blockchain | pcd- key names + payload | Account pubkey (permanent) | Permanent | Immutable | Low |
| Tezos | Blockchain | PCHK in contract storage | tz1 address (permanent) | Permanent | Immutable | Very low |
| Cardano | Blockchain | PCHK under label 8888 | Payment address (permanent) | Permanent | Immutable | Low |
| ACME/DNS | Infrastructure | PCHK TXT record | Domain registrant, passive DNS | Live: deletable | Live record only | Low |

---

## Detection Notes

### What an observer can determine without a key

From the wire format alone, an observer can determine:

- That the string is a PICARD chunk (PCHK prefix)
- Which file it belongs to (manifest_id enables cross-platform correlation)
- How many total chunks exist for that block (total_chunks × blocks = lower bound on file size)
- Which position this chunk occupies in the reconstruction sequence

They cannot determine: the file's contents, its original name, its size beyond a lower bound, who encrypted it, or what passphrase protects it.

### The PCHK prefix as the primary fingerprint

The literal string `PCHK:` at the start of every chunk is the only protocol-level content fingerprint. Any platform can search for this prefix in stored content. At time of writing, no major platform applies this filter. The prefix is intentionally stable: it is the signal defenders need to detect PICARD usage on their infrastructure.

### Behavioural signals

Beyond content scanning, PICARD creates behavioural patterns that are detectable independent of the PCHK prefix:

- Repeated writes to the same platform in short succession (rate-limited by the adapter profile, but the volume is atypical for human users)
- Account activity with no conversational context, consisting only of structured data posts
- Blockchain wallet addresses that do nothing except call a specific contract or post memos
- Stellar accounts with `pcd-` prefixed manage_data keys

These behavioural signals are independent of encryption and apply even to a modified version of PICARD that used a different prefix.

### Payload entropy

The AES-256-GCM ciphertext in the payload field is computationally indistinguishable from random bytes. Base64url-encoded random bytes have no statistical properties that distinguish them from legitimate encoded data (public keys, content hashes, API tokens, binary exports). No content-based analysis of the payload is possible without the key.

---

## A Note on Scope

This document covers the 18 adapter categories in the current release. PICARD includes adapters for RF spectrum, hardware buses, steganographic embedding, and additional network protocols not covered here. The three-layer analysis applies to all of them; the specific observability and attribution properties depend on the transport.

---

*See also: [Architecture](architecture.md) · [Adapters](adapters.md) · [Research and legal notice](../README.md#research-and-experimental-use)*
