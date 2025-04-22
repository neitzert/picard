# picard/base_adapter.py
from abc import ABC, abstractmethod

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
