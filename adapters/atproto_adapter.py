from ..base_adapter import BaseTransportAdapter
import base64

class ATProtoAdapter(BaseTransportAdapter):
    name = "atproto"
    persistent = True
    ordered = True
    max_chunk_size = 800  # Base64 safe size to stay under post length limits

    def __init__(self, client):
        self.client = client

    def is_available(self):
        return self.client.ping()

    def evaluate_capabilities(self):
        return {
            "persistent": self.persistent,
            "ordered": self.ordered,
            "max_chunk_size": self.max_chunk_size
        }

    def write_chunk(self, file_id, block_index, chunk_index, total_chunks, data):
        post_id = f"{file_id}-{block_index}-{chunk_index}"
        content = base64.b64encode(data).decode()
        message = {
            "post_id": post_id,
            "file_id": file_id,
            "block": block_index,
            "index": chunk_index,
            "total": total_chunks,
            "data": content
        }
        return self.client.post_chunk(message)

    def read_chunks(self, file_id, block_index):
        return self.client.fetch_chunks(file_id, block_index)
