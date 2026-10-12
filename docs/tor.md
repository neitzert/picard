# Tor Integration

PICARD can route all traffic through Tor for anonymity. Two components
work together: the **Tor proxy** (routes outbound adapter traffic through
Tor's SOCKS5 proxy) and the **Tor adapter** (connects to remote PICARD
nodes running as Tor hidden services).

## Expectations

**Tor is slow.** Expect 2-10x latency on every chunk operation. Circuit
setup takes seconds, and hidden service resolution adds more. This is
the price of anonymity.

**Tor is unreliable.** Circuits break, exits disappear, hidden services
go offline. PICARD's retry logic helps, but "maybe" is still the answer.

## Install

```bash
pip install -e ".[tor]"
```

This installs:
- `stem` — Tor controller library for hidden service management
- `httpx-socks` — SOCKS5 proxy transport for httpx

You also need the Tor binary installed on your system:
- Linux: `sudo apt install tor`
- macOS: `brew install tor`
- Windows: download from https://www.torproject.org/download/

## Tor Proxy — Route Existing Adapters Through Tor

The Tor proxy wraps outbound HTTP connections so your real IP is never
exposed to public chunk storage services.

```python
from picard.tor_proxy import TorProxyConfig, tor_session

config = TorProxyConfig(socks_port=9050)

async with tor_session(config) as client:
    resp = await client.get("https://check.torproject.org/api/ip")
    print(resp.json())  # {"IsTor": true, "IP": "..."}
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PICARD_TOR_SOCKS_PORT` | `9050` | Tor SOCKS proxy port |
| `PICARD_TOR_SOCKS_HOST` | `127.0.0.1` | Tor SOCKS proxy host |

### Check Tor Connectivity

```bash
picard tor-check
```

This hits the Tor Project's check API through your SOCKS proxy and
reports whether traffic is actually routed through Tor.

## Tor Adapter — Connect to .onion Peers

The `TorAdapter` connects to a remote PICARD node running as a Tor
hidden service. All chunk operations (write, read, delete, list) go
through Tor's SOCKS5 proxy to the .onion address.

```python
from picard.adapters.tor import TorAdapter

adapter = TorAdapter(
    onion_url="http://abcdef1234567890.onion",
    adapter_name="remote-fs",
    secret="shared-hmac-secret",
    socks_proxy="socks5://127.0.0.1:9050",
    timeout=120.0,
)
```

### Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `onion_url` | — | .onion URL of the remote PICARD peer |
| `adapter_name` | — | Name of the adapter on the remote node |
| `secret` | `""` | HMAC shared secret for request signing |
| `socks_proxy` | `socks5://127.0.0.1:9050` | Local Tor SOCKS proxy |
| `timeout` | `120.0` | Request timeout in seconds |
| `max_chunk_bytes` | `65536` | Maximum chunk size |

### Rate Limiting

The adapter defaults to 30 requests per minute to avoid overloading
Tor circuits. Adjust via RAID pool configuration if needed.

### HMAC Authentication

When `secret` is set, every request includes an `x-signature` header
with an HMAC-SHA256 of the request body. The remote peer must validate
this signature. Set via the `PICARD_TRANSPORT_SECRET` environment variable
on both sides.

## Tor Hidden Service Manager

The `TorManager` class handles Tor process lifecycle and hidden service
creation:

```python
from picard.tor import TorConfig, TorManager

config = TorConfig(
    socks_port=9050,
    control_port=9051,
    existing_tor=True,  # use already-running Tor
)

with TorManager(config) as tor:
    # Ephemeral — address changes on restart
    hs = tor.create_hidden_service(local_port=8471)
    print(f"Reachable at: http://{hs.onion_address}")

    # Persistent — same address across restarts
    hs = tor.create_persistent_hidden_service(local_port=8471)
    print(f"Stable at: http://{hs.onion_address}")
```

### TorConfig

| Field | Default | Description |
|-------|---------|-------------|
| `socks_port` | `9050` | SOCKS5 proxy port |
| `control_port` | `9051` | Tor control port |
| `data_dir` | `~/.picard/tor` | Tor data directory |
| `hidden_service_dir` | `~/.picard/tor/hidden_service` | Hidden service keys |
| `tor_binary` | `tor` | Path to Tor binary |
| `existing_tor` | `False` | Use already-running Tor instance |

## Configuration

Add Tor adapters to `picard.toml`:

```toml
[[adapters]]
name = "tor-peer-1"
type = "tor"
enabled = true
params = {
    onion_url = "http://abcdef1234567890.onion",
    adapter_name = "filesystem",
    secret = "shared-secret"
}

[[adapters]]
name = "tor-peer-2"
type = "tor"
enabled = true
params = {
    onion_url = "http://fedcba0987654321.onion",
    adapter_name = "filesystem",
    secret = "shared-secret"
}

[[raid]]
level = "raid1"
adapters = ["tor-peer-1", "tor-peer-2"]
```

## Security Notes

- **Never log .onion addresses** in production — they identify hidden services
- **Key files** in `~/.picard/tor/hidden_service/` are sensitive — protect with filesystem permissions
- **The SOCKS proxy is local** — if Tor isn't running, connections fail silently (health_check returns False)
- **HMAC secrets** should be strong random values, not passwords
