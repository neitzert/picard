# PICARD Adapters

Each file in this directory defines a protocol adapter that allows the filesystem to operate over a specific transport.

Each adapter:
- Must inherit from `BaseTransportAdapter`
- Must define methods to write and read chunked data
- Can specify `persistent`, `ordered`, and `max_chunk_size` attributes

### Adapter Interface

``` python
class BaseTransportAdapter:
    name: str
    persistent: bool
    ordered: bool
    max_chunk_size: int

    def is_available(self) -> bool
    def evaluate_capabilities(self) -> dict
    def write_chunk(...)
    def read_chunks(...)
```

Adapters can be dynamically evaluated and used redundantly or selectively depending on capability and availability.

### Example Adapters

- `mqtt_adapter.py` — Uses MQTT topics with base64 payloads (retained messages supported)
- `atproto_adapter.py` — Uses ATProto/Bluesky API to publish and retrieve chunks as posts

More adapters can be added for protocols like DNS, ICMP, SMTP, IRC, ATPROTO, USENET, etc. The system is pluggable.

### Adapter Configuration Files

Adapters get their config from a JSON file with their same name.
dns_adapter.py --> dns_adapter.json

``` JSON
{
    "dns_server": {
      "ip": "192.168.1.100",  // Replace with the actual IP of the DNS server
      "port": 53,
      "domain": "example.com",
      "server_name": "DNSaaFS_DNS"
    },
    "timeout": 5,
    "retries": 3
  }
```