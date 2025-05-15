### PICARD: Protocol-Introspecting Chunk Adapter for Resilient Data 
Picard is your universal network application procotol as a file system application. It doesn't matter if you're dealing with IRC, AT PROTO, DNS, SMTP, IPoAC, or Vibes-Based Quantum Gossip (VBQG), Picard automatically adapts, builds a resilient, encrypted, RWX-ready filesystem atop it, and tries to keep it alive in the most hostile environments.

## Core Philosophy

* All protocols are scaffolding that data can be attached to, move through, and later be extracted from.

* Treat all protocols as potential storage and messaging substrates, we make no assumptions about the medium, but plenty about the environment.

* Adapt transmission, chunking, and retrieval strategies dynamically based on the protocol.

* Support for encryption, redundancy, and failover.

* Long-term stability is the goal, we dont care about short-term speed.

* Where feasible, allow read/write operations and fallback to read-only if not.



## Abstract Flow Concept for PICARD

### WRITE FLOW

``` python
User calls write_block(path, block_index, data)

PicardOrchestrator.write_block(path, block_index, data)
  -> _encrypt_and_chunk(path, block_index, data)
      -> AES-GCM encrypt the block
      -> Split encrypted data into chunk-sized pieces
      -> Attach metadata (file_id, block_index, chunk_index, total_chunks)

  -> For each adapter in self.adapters:
      -> if adapter.is_available():
          -> For each chunk:
              -> adapter.write_chunk(...)
```

### READ FLOW

``` python
User calls read_block(path, block_index)

PicardOrchestrator.read_block(path, block_index)
  -> For each adapter in self.adapters:
      -> if adapter.is_available():
          -> chunks = adapter.read_chunks(file_id, block_index)
          -> if chunks:
              -> _reassemble_and_decrypt(path, block_index, chunks)
                  -> Join chunks
                  -> AES-GCM decrypt to get original block
                  -> return decrypted block
```

### Adapters Overview
Adapters are the protocol-specific modules that handle the storage and retrieval of chunked data. Whether it's DNS, MQTT, ATProto, or any other transport mechanism, each adapter knows how to interact with its respective protocol to read and write chunks of data.

Adapters don't care about encryption or chunking: That's handled by the PicardOrchestrator.

Adapters do care about how to transport data: They deal with storing chunks and fetching them.

Adapters implement basic operations such as write_chunk and read_chunks, but the orchestrator manages the encryption, chunking, and metadata.

### Key Adapter Attributes
name: The name of the protocol (e.g., mqtt, dns).

persistent: Whether the adapter can store data persistently.

ordered: Whether the adapter maintains the order of chunks.

max_chunk_size: The maximum size for each chunk, adjusted based on protocol limitations (e.g., DNS TXT records).


### Example Adapters
dns_adapter.py: Uses DNS to store and retrieve encrypted data chunks as TXT records.

The max_chunk_size is 384 bytes to fit within DNS record size limits, taking base64 encoding overhead into account.

The adapter stores the encrypted data as TXT records, where each record can hold a maximum of 512 bytes (after encoding).

mqtt_adapter.py: Uses MQTT topics with base64-encoded payloads (supports retained messages).

Designed for low-latency, lightweight storage in IoT systems or other MQTT-based environments.

atproto_adapter.py: Uses the ATProto/Bluesky API to publish and retrieve data chunks as posts.

Adapts data to the social media-oriented ATProto network.


### Adding New Adapters
Adding new protocols to Picard is simple:

Create a new Python file in the adapters/ directory.

Ensure it inherits from BaseTransportAdapter and implements the required methods (write_chunk and read_chunks).

Add any necessary configuration handling to interact with the chosen protocol.


### Configuration
Adapters typically require configuration to function properly. These configurations are defined in JSON files associated with each adapter.

For example, the DNS Adapter (dns_adapter.py) uses a configuration like this in dns_adapter.json:
``` JSON
{
    "dns_server": {
        "ip": "192.168.1.100",
        "port": 53,
        "domain": "example.com",
        "server_name": "DNSaaFS_DNS"
    },
    "timeout": 5,
    "retries": 3
}
```
* dns_server: The DNS server's IP, port, domain, and server name.
* timeout and retries: Control the DNS query behavior for timeouts and retries.


# Extending PICARD with New Protocols
Add new adapters by following the same template as the existing ones.

Update PicardOrchestrator to detect and use new adapters automatically by placing them in the adapters/ directory.

Adapters can be configured dynamically using the configuration files, which Picard reads at runtime.

---

### Example Use Case
Initialization:
The orchestrator is initialized with a list of adapters (e.g., DNSAdapter, MQTTAdapter) and an encryption key.

``` python
orchestrator = PicardOrchestrator(
    adapters=[DNSAdapter(config_file="dns_adapter.json"), MQTTAdapter(config_file="mqtt_adapter.json")],
    encryption_key=hashlib.sha256("mysecurekey".encode()).digest()
)
```
Write Data:
Data is encrypted, chunked, and then distributed to the adapters for storage:

```
orchestrator.write_block(path="testfile", block_index=0, data=b"Some encrypted data")
```
Read Data:
The orchestrator retrieves and decrypts the data from the adapters:
```
retrieved_data = orchestrator.read_block(path="testfile", block_index=0)
```
---

Picard aims to be a versatile and modular experiment designed for resilient data storage across a wide variety of protocols.
