import json
import os
import importlib.util
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from typing import List

# Define BaseTransportAdapter
class BaseTransportAdapter:
    max_chunk_size = 4096  # Example attribute, adjust as needed
    def is_available(self):
        raise NotImplementedError
    def write_chunk(self, path, block_index, chunk_index, total_chunks, chunk_data):
        raise NotImplementedError
    def read_chunks(self, path, block_index):
        raise NotImplementedError

# Define the PicardOrchestrator class, the thing that makes the machines go brr.
class PicardOrchestrator:
    def __init__(self, config_file: str, adapters: List[BaseTransportAdapter]):
        self.adapters = adapters
        self.load_config(config_file)

        # Fetch the encryption key from the environment variable or use the value in config
        encryption_key_env = os.getenv("ENCRYPTION_KEY")
        self.encryption_key = bytes(encryption_key_env, 'utf-8') if encryption_key_env else bytes(self.config['transport_encryption'], 'utf-8')

        # Initialize AES-GCM encryption
        self.aesgcm = AESGCM(self.encryption_key)

        # Handle local mounts and protomounts
        self.localmounts = self.config['localmounts']
        self.protomounts = self.config['protomounts']

        # Initialize adapters based on protomounts
        self._initialize_adapters()

    def load_config(self, config_file: str):
        """Load configuration settings from the JSON file."""
        with open(config_file, 'r') as file:
            self.config = json.load(file)  # Load the JSON data

    def _initialize_adapters(self):
        """Initialize the adapters specified in the config file."""
        for mount in self.protomounts:
            adapter_type = mount['type']
            adapter_file = mount['adapter_file']
            adapter_config = mount.copy()

            # Source environment variables if present
            for key, value in adapter_config.items():
                if isinstance(value, str) and value.startswith('(') and value.endswith(')'):
                    env_var = value[1:-1]  # Remove the parentheses to get the variable name
                    adapter_config[key] = os.getenv(env_var, None)

            # Dynamically import the adapter file
            adapter_module = self._load_adapter_module(adapter_file)

            # Create an instance of the adapter and pass the config
            adapter_instance = adapter_module(adapter_config=adapter_config)
            self.adapters.append(adapter_instance)

    def _load_adapter_module(self, module_path: str):
        """Dynamically load an adapter module from a given path."""
        module_name = os.path.basename(module_path)[:-3]  # Remove the .py extension
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def write_block(self, path: str, block_index: int, data: bytes):
        """Encrypt, chunk, and store data using the available adapters."""
        encrypted_data = self._encrypt_and_chunk(path, block_index, data)

        for adapter in self.adapters:
            if adapter.is_available():
                for chunk_index, chunk_data in enumerate(encrypted_data):
                    adapter.write_chunk(path, block_index, chunk_index, len(encrypted_data), chunk_data)

    def read_block(self, path: str, block_index: int) -> bytes:
        """Retrieve the encrypted chunks from the adapters and decrypt them."""
        for adapter in self.adapters:
            if adapter.is_available():
                chunks = adapter.read_chunks(path, block_index)
                if chunks:
                    return self._reassemble_and_decrypt(path, block_index, chunks)
        return b''  # If no chunks found

# i think this is ugly, what is the better way to do it?
    def _encrypt_and_chunk(self, path, block_index, data):
        """Encrypt and slice data into chunks using AES-GCM encryption."""
        nonce = os.urandom(12)  # Generate a random nonce for encryption
        encrypted_data = self.aesgcm.encrypt(nonce, data, None)  # Encrypt data
        chunk_size = self.adapters[0].max_chunk_size  # Use the chunk size from the adapter
        chunks = [encrypted_data[i:i + chunk_size] for i in range(0, len(encrypted_data), chunk_size)]
        return chunks

    def _reassemble_and_decrypt(self, path, block_index, chunks):
        """Reassemble the encrypted chunks and decrypt using AES-GCM."""
        encrypted_data = b''.join(chunks)
        nonce = encrypted_data[:12]  # Extract nonce
        ciphertext = encrypted_data[12:]  # Extract ciphertext
        decrypted_data = self.aesgcm.decrypt(nonce, ciphertext, None)  # Decrypt data
        return decrypted_data
# finish the code to make it work someday please?
