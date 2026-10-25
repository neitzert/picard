"""proc_mem_companion -- long-lived daemon that holds memfd file descriptors.

Chunks written by ProcMemAdapter (mode='memfd') are passed here as open file
descriptors via SCM_RIGHTS. The daemon holds them open, indexed by token.
Data persists for as long as this process runs; power cycle or process exit
erases everything.

Usage:
    python -m picard.proc_mem_companion
    python -m picard.proc_mem_companion --socket /tmp/picard-procmem.sock
    python -m picard.proc_mem_companion --systemd-unit

To run as a systemd service:
    python -m picard.proc_mem_companion --systemd-unit | sudo tee \\
        /etc/systemd/system/picard-procmem.service
    sudo systemctl enable --now picard-procmem.service
"""

from __future__ import annotations

import array
import asyncio
import json
import os
import socket
import sys
from typing import Any

_DEFAULT_SOCKET = "/tmp/picard-procmem.sock"
_VERSION = "1.0"

_SYSTEMD_UNIT = """\
[Unit]
Description=PICARD process-memory companion daemon
After=network.target

[Service]
Type=simple
ExecStart={python} -m picard.proc_mem_companion --socket {socket}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


# ---------------------------------------------------------------------------
# SCM_RIGHTS helpers
# ---------------------------------------------------------------------------

def _send_fd(conn: socket.socket, fd: int, msg: bytes = b"\x00") -> None:
    fds = array.array("i", [fd])
    conn.sendmsg([msg], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, fds)])


def _recv_fd(conn: socket.socket) -> tuple[bytes, int]:
    fds = array.array("i")
    msg, ancdata, _, _ = conn.recvmsg(256, socket.CMSG_LEN(fds.itemsize))
    for cmsg_level, cmsg_type, cmsg_data in ancdata:
        if cmsg_level == socket.SOL_SOCKET and cmsg_type == socket.SCM_RIGHTS:
            fds.frombytes(
                cmsg_data[: len(cmsg_data) - (len(cmsg_data) % fds.itemsize)]
            )
    return msg, fds[0] if len(fds) > 0 else -1


# ---------------------------------------------------------------------------
# Daemon state
# ---------------------------------------------------------------------------

# token -> (fd, size, manifest_id)
_store: dict[str, tuple[int, int, str]] = {}


def _handle_request(conn: socket.socket, request: dict[str, Any]) -> dict[str, Any]:
    op = request.get("op")

    if op == "ping":
        return {"ok": True, "version": _VERSION}

    if op == "write":
        token = request["token"]
        size = request["size"]
        manifest_id = request.get("manifest_id", "")
        _, fd = _recv_fd(conn)
        if fd < 0:
            return {"ok": False, "error": "No fd received"}
        # Duplicate so we own a copy independent of the sender
        owned_fd = os.dup(fd)
        os.close(fd)
        _store[token] = (owned_fd, size, manifest_id)
        return {"ok": True}

    if op == "read":
        token = request["token"]
        if token not in _store:
            return {"ok": False, "error": "token not found"}
        owned_fd, size, _ = _store[token]
        os.lseek(owned_fd, 0, os.SEEK_SET)
        readable_fd = os.dup(owned_fd)
        resp = json.dumps({"ok": True, "size": size}).encode() + b"\n"
        conn.sendall(resp)
        _send_fd(conn, readable_fd)
        os.close(readable_fd)
        return None  # type: ignore[return-value]  # response already sent

    if op == "delete":
        token = request["token"]
        entry = _store.pop(token, None)
        if entry is not None:
            os.close(entry[0])
            return {"ok": True, "existed": True}
        return {"ok": True, "existed": False}

    if op == "list":
        manifest_id = request.get("manifest_id", "")
        tokens = [t for t, (_, _, mid) in _store.items() if mid == manifest_id]
        return {"ok": True, "tokens": tokens}

    return {"ok": False, "error": f"unknown op {op!r}"}


def _serve_connection(conn: socket.socket) -> None:
    try:
        buf = b""
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line:
                    continue
                try:
                    request = json.loads(line)
                except json.JSONDecodeError:
                    conn.sendall(b'{"ok":false,"error":"bad json"}\n')
                    continue
                resp = _handle_request(conn, request)
                if resp is not None:
                    conn.sendall(json.dumps(resp).encode() + b"\n")
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        conn.close()


def _serve(socket_path: str) -> None:
    if os.path.exists(socket_path):
        os.unlink(socket_path)

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(socket_path)
    os.chmod(socket_path, 0o600)
    srv.listen(32)

    print(f"picard-procmem ready at {socket_path}", flush=True)

    import threading
    try:
        while True:
            conn, _ = srv.accept()
            t = threading.Thread(target=_serve_connection, args=(conn,), daemon=True)
            t.start()
    except KeyboardInterrupt:
        pass
    finally:
        srv.close()
        if os.path.exists(socket_path):
            os.unlink(socket_path)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="PICARD proc-mem companion daemon")
    parser.add_argument(
        "--socket", default=_DEFAULT_SOCKET, help="Unix socket path"
    )
    parser.add_argument(
        "--systemd-unit",
        action="store_true",
        help="Print a systemd service unit and exit",
    )
    args = parser.parse_args()

    if args.systemd_unit:
        print(
            _SYSTEMD_UNIT.format(python=sys.executable, socket=args.socket).strip()
        )
        return

    if sys.platform != "linux":
        print(
            "proc_mem_companion requires Linux (memfd_create, SCM_RIGHTS).",
            file=sys.stderr,
        )
        sys.exit(1)

    _serve(args.socket)


if __name__ == "__main__":
    main()
