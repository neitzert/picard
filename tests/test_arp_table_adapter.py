"""Unit tests for arp_table_adapter (Linux-only)."""
import sys
import pytest

if sys.platform != "linux":
    pytest.skip("arp_table_adapter requires Linux", allow_module_level=True)

import asyncio
from unittest.mock import patch, MagicMock
from picard.adapters.base import StoredChunkRef

CHUNK = "hello"


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_proc(returncode=0, stdout="", stderr=""):
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout.encode() if isinstance(stdout, str) else stdout
    m.stderr = stderr.encode() if isinstance(stderr, str) else stderr
    return m


def _make_adapter():
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = _make_proc(returncode=0)
        from picard.adapters.arp_table_adapter import ArpTableAdapter
        return ArpTableAdapter()


def test_profile():
    a = _make_adapter()
    p = a.profile()
    assert p.name == "arp-table"


def test_write_read_delete():
    a = _make_adapter()
    data = CHUNK
    ip_lines = []
    for i, v in enumerate(data.encode()):
        ip_lines.append(f"10.61.{i}.1 lladdr 02:61:{i:02x}:{v:02x}:00:00 PERMANENT")
    neigh_output = "\n".join(ip_lines)

    with patch("subprocess.run", return_value=_make_proc(returncode=0)):
        ref = _run(a.write_chunk(data))

    assert ref.adapter_name == "arp-table"

    with patch("subprocess.run", return_value=_make_proc(returncode=0, stdout=neigh_output)):
        result = _run(a.read_chunk(ref))
    assert result == data

    with patch("subprocess.run", return_value=_make_proc(returncode=0)):
        deleted = _run(a.delete_chunk(ref))
    assert deleted is True


def test_health_check():
    a = _make_adapter()
    with patch("subprocess.run", return_value=_make_proc(returncode=0)):
        result = _run(a.health_check())
    assert result is True
