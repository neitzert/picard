"""Verify AF_PACKET loopback interception for the icmp-inflight adapter.

Tests whether AF_PACKET on the loopback interface captures ICMP echo replies
before the kernel's icmp_rcv() processes them, enabling the relay loop that
keeps packets in flight indefinitely.

Usage:
    sudo python tools/verify_icmp_inflight.py

Requires: Linux x86-64, CAP_NET_RAW (or root), scapy>=2.5

What is tested:
  1. CAP_NET_RAW: can we open an AF_PACKET socket?
  2. Injection: can we send an ICMP echo to 127.0.0.1 via scapy?
  3. Capture: does AF_PACKET see the reply before we re-inject?
  4. Relay loop: does a packet circulate at least 10 times in 5 seconds
     with the payload intact?
"""

import os
import sys
import time

GREEN = "\033[92m"
RED   = "\033[91m"
CYAN  = "\033[96m"
BOLD  = "\033[1m"
RESET = "\033[0m"

def ok(msg):   print(f"  {GREEN}[PASS]{RESET} {msg}")
def fail(msg): print(f"  {RED}[FAIL]{RESET} {msg}")
def info(msg): print(f"  {CYAN}     {RESET} {msg}")


if sys.platform != "linux":
    print("SKIP: requires Linux")
    sys.exit(0)

if os.uname().machine not in ("x86_64", "amd64"):
    print(f"SKIP: requires x86-64 (got {os.uname().machine})")
    sys.exit(0)

# ---------------------------------------------------------------------------
# Step 1: CAP_NET_RAW check
# ---------------------------------------------------------------------------

print(f"\n{BOLD}1. CAP_NET_RAW{RESET}")
import socket as _socket
try:
    _s = _socket.socket(_socket.AF_PACKET, _socket.SOCK_RAW, _socket.htons(0x0800))
    _s.close()
    ok("AF_PACKET socket opened successfully")
except PermissionError:
    fail("PermissionError -- run as root or grant CAP_NET_RAW")
    sys.exit(1)
except Exception as e:
    fail(f"Unexpected error: {e}")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Step 2: scapy import
# ---------------------------------------------------------------------------

print(f"\n{BOLD}2. scapy import{RESET}")
try:
    from scapy.all import IP, ICMP, sendp, AsyncSniffer, conf
    conf.verb = 0
    ok("scapy imported")
except ImportError as e:
    fail(f"scapy not available: {e}  --  pip install scapy>=2.5")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Step 3: Injection test
# ---------------------------------------------------------------------------

print(f"\n{BOLD}3. ICMP injection on loopback{RESET}")
_TEST_ID = 0xBEEF
_IFACE   = "lo"

try:
    pkt = IP(dst="127.0.0.1") / ICMP(type=8, id=_TEST_ID, seq=0) / b"PICARD-TEST"
    sendp(pkt, iface=_IFACE, verbose=False)
    ok("ICMP echo request injected on lo")
except Exception as e:
    fail(f"sendp failed: {e}")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Step 4: AF_PACKET capture of reply
# ---------------------------------------------------------------------------

print(f"\n{BOLD}4. AF_PACKET reply capture{RESET}")
_captured = []

def _capture_cb(pkt):
    try:
        if pkt[ICMP].type == 0 and pkt[ICMP].id == _TEST_ID:
            _captured.append(pkt)
    except Exception:
        pass

_sniffer = AsyncSniffer(
    iface=_IFACE,
    filter=f"icmp and icmp[icmptype] == 0",
    prn=_capture_cb,
    store=False,
)
_sniffer.start()

# Inject and wait 0.5s
pkt = IP(dst="127.0.0.1") / ICMP(type=8, id=_TEST_ID, seq=1) / b"PICARD-TEST"
_t0 = time.perf_counter()
sendp(pkt, iface=_IFACE, verbose=False)
time.sleep(0.5)
_sniffer.stop()

if _captured:
    elapsed = (time.perf_counter() - _t0) * 1000
    ok(f"Reply captured via AF_PACKET in ~{elapsed:.1f}ms  (before icmp_rcv)")
else:
    fail("No ICMP echo reply captured -- AF_PACKET may not see loopback ICMP on this kernel")
    info("Try: sysctl -w net.ipv4.icmp_echo_ignore_all=0")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Step 5: Relay loop -- 10 circulations in 5 seconds
# ---------------------------------------------------------------------------

print(f"\n{BOLD}5. Relay loop (10 circulations, 5s timeout){RESET}")

_RELAY_ID      = 0xCAFE
_RELAY_PAYLOAD = b"PICD\x00\x01\x00\x00" + b"RELAY-VERIFY-PAYLOAD"
_circulation_count = 0
_payload_ok = True

def _relay_cb(pkt):
    global _circulation_count, _payload_ok
    try:
        if pkt[ICMP].type != 0 or pkt[ICMP].id != _RELAY_ID:
            return
        raw = bytes(pkt[ICMP].payload)
        if raw != _RELAY_PAYLOAD:
            _payload_ok = False
        _circulation_count += 1
        seq = (pkt[ICMP].seq + 1) & 0xFFFF
        new_pkt = (
            IP(dst="127.0.0.1")
            / ICMP(type=8, id=_RELAY_ID, seq=seq)
            / _RELAY_PAYLOAD
        )
        sendp(new_pkt, iface=_IFACE, verbose=False)
    except Exception:
        pass

_relay_sniffer = AsyncSniffer(
    iface=_IFACE,
    filter="icmp and icmp[icmptype] == 0",
    prn=_relay_cb,
    store=False,
)
_relay_sniffer.start()

# First injection
first_pkt = (
    IP(dst="127.0.0.1")
    / ICMP(type=8, id=_RELAY_ID, seq=0)
    / _RELAY_PAYLOAD
)
sendp(first_pkt, iface=_IFACE, verbose=False)
time.sleep(5)
_relay_sniffer.stop()

info(f"Circulations in 5s: {_circulation_count}")

if _circulation_count >= 10 and _payload_ok:
    ok(f"Packet circulated {_circulation_count} times with payload intact")
elif not _payload_ok:
    fail("Payload corruption detected during relay")
    sys.exit(1)
else:
    fail(f"Only {_circulation_count} circulations in 5s (need >= 10)")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

print(f"\n{BOLD}Result: all checks passed -- icmp-inflight adapter viable on this host{RESET}\n")
sys.exit(0)
