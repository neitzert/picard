# PICARD — Protocol-Introspecting Chunk Adapter for Resilient Data

## Abstract Flow Concept for PICARD

### WRITE FLOW

```
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

```
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

Each adapter (like MQTT or ATProto) is simple: it just knows how to send and receive chunk-sized messages. The orchestrator manages logic, encryption, metadata, and redundancy.
```
