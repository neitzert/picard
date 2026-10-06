"""Windows Registry adapter — stores chunks as registry values.

Hides data in the most fundamental Windows storage mechanism.
Registry is rarely monitored for data content, only for known
malware keys.  ``ls``, ``dir``, Explorer never show this data.

Requires: Windows OS (winreg stdlib module).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class WindowsRegistryAdapter(Adapter):
    """Scatter chunks into Windows Registry values."""

    def __init__(
        self,
        *,
        hive: str = "HKCU",
        base_key: str = r"Software\PICARD\Chunks",
        max_chunk_bytes: int = 1_048_576,
    ) -> None:
        self._hive_name = hive
        self._base_key = base_key
        self._max = max_chunk_bytes

    def _hive_const(self):
        import winreg

        return {
            "HKCU": winreg.HKEY_CURRENT_USER,
            "HKLM": winreg.HKEY_LOCAL_MACHINE,
            "HKU": winreg.HKEY_USERS,
            "HKCR": winreg.HKEY_CLASSES_ROOT,
            "HKCC": winreg.HKEY_CURRENT_CONFIG,
        }[self._hive_name]

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="windows-registry",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=1000,
        )

    def _chunk_key(self, encoded_chunk: str) -> str:
        from picard.chunker import Chunk

        try:
            chunk = Chunk.decode(encoded_chunk)
            return f"{chunk.manifest_id}_{chunk.block_index}_{chunk.chunk_index}"
        except (ValueError, Exception):
            return uuid.uuid4().hex

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import winreg

        key_name = self._chunk_key(encoded_chunk)

        def _write():
            key = winreg.CreateKeyEx(
                self._hive_const(), self._base_key, 0, winreg.KEY_WRITE
            )
            winreg.SetValueEx(key, key_name, 0, winreg.REG_SZ, encoded_chunk)
            winreg.CloseKey(key)

        await asyncio.to_thread(_write)
        location = f"winreg://{self._hive_name}/{self._base_key}/{key_name}"
        return StoredChunkRef(adapter_name="windows-registry", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        import winreg

        val_name = ref.location.rsplit("/", 1)[-1]

        def _read():
            key = winreg.OpenKeyEx(
                self._hive_const(), self._base_key, 0, winreg.KEY_READ
            )
            value, _ = winreg.QueryValueEx(key, val_name)
            winreg.CloseKey(key)
            return value

        return await asyncio.to_thread(_read)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        import winreg

        val_name = ref.location.rsplit("/", 1)[-1]

        def _delete():
            try:
                key = winreg.OpenKeyEx(
                    self._hive_const(), self._base_key, 0, winreg.KEY_SET_VALUE
                )
                winreg.DeleteValue(key, val_name)
                winreg.CloseKey(key)
                return True
            except FileNotFoundError:
                return False

        return await asyncio.to_thread(_delete)

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        import winreg

        def _list():
            refs: list[StoredChunkRef] = []
            try:
                key = winreg.OpenKeyEx(
                    self._hive_const(), self._base_key, 0, winreg.KEY_READ
                )
                i = 0
                while True:
                    try:
                        val_name, _, _ = winreg.EnumValue(key, i)
                        if val_name.startswith(manifest_id):
                            loc = f"winreg://{self._hive_name}/{self._base_key}/{val_name}"
                            refs.append(
                                StoredChunkRef(
                                    adapter_name="windows-registry", location=loc
                                )
                            )
                        i += 1
                    except OSError:
                        break
                winreg.CloseKey(key)
            except FileNotFoundError:
                pass
            return refs

        return await asyncio.to_thread(_list)

    async def health_check(self) -> bool:
        import winreg

        def _check():
            try:
                key = winreg.CreateKeyEx(
                    self._hive_const(), self._base_key, 0, winreg.KEY_READ
                )
                winreg.CloseKey(key)
                return True
            except Exception:
                return False

        return await asyncio.to_thread(_check)

    async def capacity(self) -> dict[str, int | None]:
        return {
            "used_bytes": None,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": None,
        }
