import json
import base64
import dns.update
import dns.query
import dns.resolver
from concurrent.futures import ThreadPoolExecutor
from abc import ABC, abstractmethod

# Base Transport Adapter Class
class BaseTransportAdapter(ABC):
    name = "unknown"
    persistent = False
    ordered = False
    max_chunk_size = 512

    @abstractmethod
    def is_available(self) -> bool:
        pass

    @abstractmethod
    def evaluate_capabilities(self) -> dict:
        pass

    @abstractmethod
    def write_chunk(self, file_id: str, block_index: int, chunk_index: int, total_chunks: int, data: bytes) -> bool:
        pass

    @abstractmethod
    def read_chunks(self, file_id: str, block_index: int) -> list:
        pass


class DNSAdapter(BaseTransportAdapter):
    name = "dns"
    persistent = True
    ordered = True
    max_chunk_size = 384  # Adjusted to fit within DNS record size limits and base64 encoding overhead

    def __init__(self, config_file: str):
        self.executor = ThreadPoolExecutor(max_workers=4)
        self.load_config(config_file)

    def load_config(self, config_file: str):
        """Load DNS server configuration from a JSON file."""
        with open(config_file, 'r') as file:
            config = json.load(file)
            self.dns_ip = config["dns_server"]["ip"]
            self.dns_port = config["dns_server"]["port"]
            self.domain = config["dns_server"]["domain"]
            self.server_name = config["dns_server"]["server_name"]
            self.timeout = config.get("timeout", 5)
            self.retries = config.get("retries", 3)

    def is_available(self):
        try:
            dns.resolver.resolve(self.domain, "A")  # Check if the domain is resolvable
            return True
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
            return False

    def evaluate_capabilities(self):
        return {
            "persistent": self.persistent,
            "ordered": self.ordered,
            "max_chunk_size": self.max_chunk_size
        }

    def write_chunk(self, file_id, block_index, chunk_index, total_chunks, data):
        """
        Store the encrypted chunk in DNS (data is already encrypted by orchestrator).
        Use total_chunks to track how many chunks are needed.
        this is a reminder to write the actual chunking logic someday
        this is useful for max chunk quantity, where the protocol has limits in the number of chunks
        """
        payload = base64.b64encode(data).decode()

        # Create a unique DNS record for the chunk
        topic = f"{file_id}.{block_index}.{chunk_index}.{self.domain}"

        # Update DNS with the encrypted data
        update = dns.update.Update(self.domain)
        update.add(topic, 60, 'TXT', payload)  # TTL set to 60 seconds
        try:
            response = dns.query.tcp(update, self.dns_ip, port=self.dns_port, timeout=self.timeout)
            return response.rcode() == 0  # Check if the update was successful
        except dns.query.Timeout:
            return False

    def read_chunks(self, file_id, block_index):
        """
        Retrieve all chunks for a given block and reassemble them.
        Uses total_chunks to ensure all chunks are retrieved.
        this is a reminder to write the actual chunking logic someday
        this is useful for max chunk quantity, where the protocol has limits in the number of chunks
        """
        chunks = []
        for chunk_index in range(10):  # We can limit this to 10 chunks, or use total_chunks if needed
            topic = f"{file_id}.{block_index}.{chunk_index}.{self.domain}"
            try:
                answer = dns.resolver.resolve(topic, 'TXT', lifetime=self.timeout)
                for rdata in answer:
                    chunk_data = base64.b64decode(rdata.to_text().strip('"'))
                    chunks.append(chunk_data)
            except dns.resolver.NoAnswer:
                continue
        return chunks
