"""RAID controller — redundancy across adapter pools.

Wraps a set of adapters and provides RAID-like data distribution:
  - RAID 0: striping (round-robin across adapters)
  - RAID 1: mirroring (write to all adapters)
  - RAID 5: striping with single parity
  - RAID 10: striped mirrors (mirror groups, striped across groups)

The RaidPool itself implements the Adapter interface, so it can be
used as a drop-in replacement anywhere an Adapter is expected.
"""

from __future__ import annotations

import asyncio
import base64
import json
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class RaidLevel(str, Enum):
    RAID0 = "raid0"
    RAID1 = "raid1"
    RAID5 = "raid5"
    RAID10 = "raid10"
    ERASURE = "erasure"


def _xor_bytes(a: bytes, b: bytes) -> bytes:
    """XOR two byte strings, padding the shorter one with zeros."""
    length = max(len(a), len(b))
    ai = int.from_bytes(a.ljust(length, b"\x00"), "big")
    bi = int.from_bytes(b.ljust(length, b"\x00"), "big")
    return (ai ^ bi).to_bytes(length, "big")


@dataclass(frozen=True, slots=True)
class RaidRef:
    """Reference that tracks chunk placement across multiple adapters."""

    refs: tuple[StoredChunkRef, ...]
    parity_refs: tuple[StoredChunkRef, ...] = ()
    stripe_id: int = -1
    shard_indices: tuple[int, ...] = ()

    def pack(self) -> str:
        data: dict = {
            "refs": [
                {"adapter_name": r.adapter_name, "location": r.location}
                for r in self.refs
            ],
        }
        if self.parity_refs:
            data["parity_refs"] = [
                {"adapter_name": r.adapter_name, "location": r.location}
                for r in self.parity_refs
            ]
        if self.stripe_id >= 0:
            data["stripe_id"] = self.stripe_id
        if self.shard_indices:
            data["shard_indices"] = list(self.shard_indices)
        return json.dumps(data, separators=(",", ":"))

    @classmethod
    def unpack(cls, location: str) -> RaidRef:
        data = json.loads(location)
        refs = tuple(
            StoredChunkRef(adapter_name=r["adapter_name"], location=r["location"])
            for r in data["refs"]
        )
        parity_refs = tuple(
            StoredChunkRef(adapter_name=r["adapter_name"], location=r["location"])
            for r in data.get("parity_refs", [])
        )
        stripe_id = data.get("stripe_id", -1)
        shard_indices = tuple(data.get("shard_indices", []))
        return cls(
            refs=refs,
            parity_refs=parity_refs,
            stripe_id=stripe_id,
            shard_indices=shard_indices,
        )


@dataclass
class _StripeState:
    """Accumulator for RAID 5 stripe parity computation."""

    chunks: list[tuple[str, StoredChunkRef]] = field(default_factory=list)
    parity_accum: bytes = b""
    parity_adapter_idx: int = 0


class RaidPool(Adapter):
    """Adapter wrapper that distributes chunks across a pool with redundancy."""

    def __init__(
        self,
        adapters: list[Adapter],
        level: RaidLevel = RaidLevel.RAID0,
        *,
        mirror_count: int = 2,
        erasure_k: int = 4,
        erasure_m: int = 6,
    ) -> None:
        if len(adapters) < 2:
            raise ValueError("RaidPool requires at least 2 adapters")
        if level == RaidLevel.RAID5 and len(adapters) < 3:
            raise ValueError("RAID 5 requires at least 3 adapters")
        if level == RaidLevel.RAID10:
            if len(adapters) < 4:
                raise ValueError("RAID 10 requires at least 4 adapters")
            if len(adapters) % 2 != 0:
                raise ValueError("RAID 10 requires an even number of adapters")
        if level == RaidLevel.ERASURE:
            if not (1 <= erasure_k < erasure_m):
                raise ValueError(
                    f"erasure requires 1 <= erasure_k < erasure_m, "
                    f"got erasure_k={erasure_k} erasure_m={erasure_m}"
                )
            if len(adapters) < erasure_m:
                raise ValueError(
                    f"erasure RS({erasure_k},{erasure_m}) needs at least "
                    f"{erasure_m} adapters, got {len(adapters)}"
                )
        self._erasure_k = erasure_k
        self._erasure_m = erasure_m

        self._adapters = list(adapters)
        self._adapter_map: dict[str, Adapter] = {
            a.profile().name: a for a in self._adapters
        }
        self._level = level
        self._mirror_count = min(mirror_count, len(adapters))
        self._counter = 0

        # RAID 5 state
        if level == RaidLevel.RAID5:
            self._stripe_size = len(adapters) - 1
            self._stripe = _StripeState()
            self._stripe_id = 0
            self._parity_refs: list[StoredChunkRef] = []
            self._stripe_lock = threading.Lock()
            self._stripe_registry: dict[int, dict] = {}
            self._parity_writing: bool = False

    def profile(self) -> AdapterProfile:
        min_bytes = min(a.profile().max_chunk_bytes for a in self._adapters)
        all_persistent = all(a.profile().persistent for a in self._adapters)
        return AdapterProfile(
            name=f"raid-pool-{self._level.value}",
            max_chunk_bytes=min_bytes,
            persistent=all_persistent,
            ordered=False,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        if self._level == RaidLevel.RAID0:
            return await self._write_raid0(encoded_chunk)
        elif self._level == RaidLevel.RAID1:
            return await self._write_raid1(encoded_chunk)
        elif self._level == RaidLevel.RAID5:
            return await self._write_raid5(encoded_chunk)
        elif self._level == RaidLevel.RAID10:
            return await self._write_raid10(encoded_chunk)
        elif self._level == RaidLevel.ERASURE:
            return await self._write_erasure(encoded_chunk)
        raise ValueError(f"unsupported RAID level: {self._level}")

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        raid_ref = RaidRef.unpack(ref.location)

        if self._level == RaidLevel.ERASURE:
            return await self._read_erasure(raid_ref)

        if self._level == RaidLevel.RAID0:
            return await self._read_single(raid_ref.refs[0])

        if self._level in (RaidLevel.RAID1, RaidLevel.RAID10):
            return await self._read_with_fallback(raid_ref.refs)

        if self._level == RaidLevel.RAID5:
            return await self._read_raid5(raid_ref)

        raise ValueError(f"unsupported RAID level: {self._level}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        raid_ref = RaidRef.unpack(ref.location)
        all_refs = list(raid_ref.refs) + list(raid_ref.parity_refs)
        results = await asyncio.gather(
            *(self._delete_from(r) for r in all_refs),
            return_exceptions=True,
        )
        return any(r is True for r in results)

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        all_results = await asyncio.gather(
            *(a.list_chunks(manifest_id) for a in self._adapters),
            return_exceptions=True,
        )
        seen_locations: set[str] = set()
        refs: list[StoredChunkRef] = []
        for result in all_results:
            if isinstance(result, Exception):
                continue
            for r in result:
                if r.location not in seen_locations:
                    seen_locations.add(r.location)
                    refs.append(r)
        return refs

    async def health_check(self) -> bool:
        results = await asyncio.gather(
            *(a.health_check() for a in self._adapters),
            return_exceptions=True,
        )
        healthy = [r for r in results if r is True]

        if self._level == RaidLevel.RAID0:
            return len(healthy) == len(self._adapters)
        elif self._level == RaidLevel.RAID1:
            return len(healthy) >= 1
        elif self._level == RaidLevel.RAID5:
            return len(healthy) >= len(self._adapters) - 1
        elif self._level == RaidLevel.ERASURE:
            return len(healthy) >= self._erasure_k
        elif self._level == RaidLevel.RAID10:
            groups = self._mirror_groups()
            for group in groups:
                group_healthy = sum(
                    1 for i, a in enumerate(self._adapters)
                    if i in group and i < len(results) and results[i] is True
                )
                if group_healthy == 0:
                    return False
            return True
        return False

    # --- Erasure coding (Reed-Solomon via zfec) -----------------------------

    async def _write_erasure(self, encoded_chunk: str) -> StoredChunkRef:
        from picard.erasure import encode as rs_encode

        data = encoded_chunk.encode("utf-8")
        shards = rs_encode(data, self._erasure_k, self._erasure_m)

        # Store each shard as a base64 string -- adapters accept arbitrary strings.
        shard_strs = [base64.b64encode(s).decode() for s in shards]

        results = await asyncio.gather(
            *(self._adapters[i].write_chunk(shard_strs[i]) for i in range(self._erasure_m))
        )
        raid_ref = RaidRef(
            refs=tuple(results),
            shard_indices=tuple(range(self._erasure_m)),
        )
        return StoredChunkRef(
            adapter_name=self.profile().name,
            location=raid_ref.pack(),
        )

    async def _read_erasure(self, raid_ref: RaidRef) -> str:
        from picard.erasure import decode as rs_decode

        async def _try_read(ref: StoredChunkRef, idx: int) -> tuple[int, bytes | None]:
            try:
                chunk_str = await self._read_single(ref)
                return idx, base64.b64decode(chunk_str)
            except Exception:
                return idx, None

        results = await asyncio.gather(
            *(_try_read(ref, idx) for ref, idx in zip(raid_ref.refs, raid_ref.shard_indices))
        )

        shards: list[bytes | None] = [None] * self._erasure_m
        for idx, payload in results:
            shards[idx] = payload

        data = rs_decode(shards, self._erasure_k, self._erasure_m)
        return data.decode("utf-8")

    # --- RAID 5 flush -------------------------------------------------------

    async def flush_parity(self) -> None:
        """Write accumulated parity for any partial stripe (RAID 5 only)."""
        if self._level != RaidLevel.RAID5:
            return
        with self._stripe_lock:
            has_chunks = bool(self._stripe.chunks)
            if has_chunks and not self._parity_writing:
                self._parity_writing = True
            else:
                has_chunks = False
        if not has_chunks:
            return
        await self._write_parity()

    # --- RAID 0 (striping) --------------------------------------------------

    async def _write_raid0(self, encoded_chunk: str) -> StoredChunkRef:
        idx = self._counter % len(self._adapters)
        self._counter += 1
        ref = await self._adapters[idx].write_chunk(encoded_chunk)
        raid_ref = RaidRef(refs=(ref,))
        return StoredChunkRef(
            adapter_name=self.profile().name,
            location=raid_ref.pack(),
        )

    # --- RAID 1 (mirroring) -------------------------------------------------

    async def _write_raid1(self, encoded_chunk: str) -> StoredChunkRef:
        targets = self._adapters[: self._mirror_count]
        results = await asyncio.gather(
            *(a.write_chunk(encoded_chunk) for a in targets)
        )
        raid_ref = RaidRef(refs=tuple(results))
        return StoredChunkRef(
            adapter_name=self.profile().name,
            location=raid_ref.pack(),
        )

    # --- RAID 5 (striping + parity) -----------------------------------------

    async def _write_raid5(self, encoded_chunk: str) -> StoredChunkRef:
        with self._stripe_lock:
            n = len(self._adapters)
            stripe = self._stripe

            parity_idx = stripe.parity_adapter_idx
            data_slot = len(stripe.chunks) % self._stripe_size
            data_adapters = [i for i in range(n) if i != parity_idx]
            adapter_idx = data_adapters[data_slot]

            current_stripe_id = self._stripe_id
            stripe_full = False

        ref = await self._adapters[adapter_idx].write_chunk(encoded_chunk)

        with self._stripe_lock:
            stripe = self._stripe
            chunk_bytes = encoded_chunk.encode("utf-8")
            if not stripe.parity_accum:
                stripe.parity_accum = chunk_bytes
            else:
                stripe.parity_accum = _xor_bytes(stripe.parity_accum, chunk_bytes)

            stripe.chunks.append((encoded_chunk, ref))

            if current_stripe_id not in self._stripe_registry:
                self._stripe_registry[current_stripe_id] = {"data_refs": [], "parity_ref": None}
            self._stripe_registry[current_stripe_id]["data_refs"].append(ref)

            stripe_full = len(stripe.chunks) >= self._stripe_size

        parity_ref: StoredChunkRef | None = None
        if stripe_full:
            with self._stripe_lock:
                if self._parity_writing:
                    stripe_full = False
                else:
                    self._parity_writing = True
        if stripe_full:
            parity_ref = await self._write_parity()

        # Include the parity ref in the RaidRef so recovery survives restarts.
        # Only the last chunk per stripe has parity_refs populated; earlier
        # chunks rely on _stripe_registry (in-process only).
        raid_ref = RaidRef(
            refs=(ref,),
            stripe_id=current_stripe_id,
            parity_refs=(parity_ref,) if parity_ref else (),
        )
        return StoredChunkRef(
            adapter_name=self.profile().name,
            location=raid_ref.pack(),
        )

    async def _write_parity(self) -> StoredChunkRef:
        with self._stripe_lock:
            stripe = self._stripe
            current_stripe_id = self._stripe_id
            parity_accum = stripe.parity_accum
            parity_adapter_idx = stripe.parity_adapter_idx

        parity_chunk = Chunk(
            version=1,
            manifest_id=f"__parity_s{current_stripe_id}",
            block_index=0,
            chunk_index=0,
            total_chunks=1,
            payload=parity_accum,
        )
        parity_adapter = self._adapters[parity_adapter_idx]
        parity_ref = await parity_adapter.write_chunk(parity_chunk.encode())

        with self._stripe_lock:
            self._parity_refs.append(parity_ref)
            if current_stripe_id in self._stripe_registry:
                self._stripe_registry[current_stripe_id]["parity_ref"] = parity_ref

            self._stripe = _StripeState(
                parity_adapter_idx=(parity_adapter_idx + 1) % len(self._adapters),
            )
            self._stripe_id += 1
            self._parity_writing = False

        return parity_ref

    async def _read_raid5(self, raid_ref: RaidRef) -> str:
        try:
            return await self._read_single(raid_ref.refs[0])
        except Exception:
            pass

        stripe_info = self._stripe_registry.get(raid_ref.stripe_id)
        if stripe_info and stripe_info.get("parity_ref"):
            try:
                parity_data = await self._read_single(stripe_info["parity_ref"])
                parity_bytes = parity_data.encode("utf-8")

                surviving = []
                for dr in stripe_info["data_refs"]:
                    if dr.location == raid_ref.refs[0].location:
                        continue
                    try:
                        surviving.append(await self._read_single(dr))
                    except Exception:
                        raise OSError("RAID 5: multiple data chunks missing, cannot reconstruct")

                reconstructed = parity_bytes
                for s in surviving:
                    reconstructed = _xor_bytes(reconstructed, s.encode("utf-8"))
                return reconstructed.decode("utf-8")
            except OSError:
                raise
            except Exception:
                pass

        if raid_ref.parity_refs:
            try:
                parity_data = await self._read_single(raid_ref.parity_refs[0])
                return parity_data
            except Exception:
                pass

        raise OSError("RAID 5 read failed: data and parity both unavailable")

    # --- RAID 10 (striped mirrors) ------------------------------------------

    async def _write_raid10(self, encoded_chunk: str) -> StoredChunkRef:
        groups = self._mirror_groups()
        group_idx = self._counter % len(groups)
        self._counter += 1

        group = groups[group_idx]
        targets = [self._adapters[i] for i in group]
        results = await asyncio.gather(
            *(a.write_chunk(encoded_chunk) for a in targets)
        )
        raid_ref = RaidRef(refs=tuple(results))
        return StoredChunkRef(
            adapter_name=self.profile().name,
            location=raid_ref.pack(),
        )

    def _mirror_groups(self) -> list[list[int]]:
        group_size = self._mirror_count
        groups = []
        for i in range(0, len(self._adapters), group_size):
            groups.append(list(range(i, min(i + group_size, len(self._adapters)))))
        return groups

    async def capacity(self) -> dict[str, int | None]:
        results = await asyncio.gather(
            *(a.capacity() for a in self._adapters),
            return_exceptions=True,
        )
        total_used = 0
        total_free: int | None = 0
        total_total: int | None = 0
        total_chunks = 0
        for r in results:
            if isinstance(r, Exception):
                continue
            total_used += r.get("used_bytes", 0) or 0
            total_chunks += r.get("chunk_count", 0) or 0
            if total_free is not None and r.get("free_bytes") is not None:
                total_free += r["free_bytes"]
            else:
                total_free = None
            if total_total is not None and r.get("total_bytes") is not None:
                total_total += r["total_bytes"]
            else:
                total_total = None
        return {
            "used_bytes": total_used,
            "free_bytes": total_free,
            "total_bytes": total_total,
            "chunk_count": total_chunks,
        }

    # --- shared read helpers ------------------------------------------------

    async def _read_single(self, ref: StoredChunkRef) -> str:
        adapter = self._adapter_by_name(ref.adapter_name)
        return await adapter.read_chunk(ref)

    async def _read_with_fallback(self, refs: tuple[StoredChunkRef, ...]) -> str:
        for ref in refs:
            try:
                adapter = self._adapter_by_name(ref.adapter_name)
                return await adapter.read_chunk(ref)
            except Exception:
                continue
        raise OSError(
            f"all {len(refs)} mirrors failed to read chunk"
        )

    async def _delete_from(self, ref: StoredChunkRef) -> bool:
        try:
            adapter = self._adapter_by_name(ref.adapter_name)
            return await adapter.delete_chunk(ref)
        except Exception:
            return False

    def _adapter_by_name(self, name: str) -> Adapter:
        adapter = self._adapter_map.get(name)
        if adapter is None:
            raise LookupError(f"no adapter named {name!r} in pool")
        return adapter
