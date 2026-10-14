"""Connectivity probes (ping / traceroute) - lab-allowlisted only.

Safety model (spec section 5):
  * The destination MUST pass ``LabInventory.validate_destination`` -
    an allowlist built from the lab topology. Arbitrary user shell input is
    never executed; ``subprocess.run`` uses argv lists with ``shell=False``.
  * Every probe has a hard timeout and its output is size-limited.
  * Failures (unreachable host, missing binary, timeout) are returned as
    structured results, never raised as crashes into the UI.

Parsed output gives real measurements only: success flag, packet loss and
average RTT when the underlying tool reported them; otherwise None.
"""
from __future__ import annotations

import platform
import re
import shutil
import subprocess
from dataclasses import dataclass

from config import get_settings
from network.device_inventory import DestinationNotAllowed, LabInventory


@dataclass
class PingResult:
    destination: str
    success: bool
    packets_sent: int | None = None
    packets_received: int | None = None
    loss_pct: float | None = None
    avg_rtt_ms: float | None = None
    error: str | None = None
    raw_output: str | None = None


@dataclass
class TracerouteResult:
    destination: str
    supported: bool
    success: bool
    hops: list[dict] | None = None      # [{ttl, ip, rtt_ms|None}, ...]
    error: str | None = None
    raw_output: str | None = None


_WINDOWS = platform.system() == "Windows"


def run_selftest_ping(count: int | None = None,
                      timeout_seconds: int | None = None) -> PingResult:
    """DEMO/SELFTEST ONLY: run the real ping binary against the host's own
    loopback address (127.0.0.1) to prove the toolchain (execution,
    parsing, storage) works. This touches no network at all and is never
    presented as a lab measurement - results are stored with
    data_source='demo'. Used when no lab is reachable."""
    settings = get_settings()
    result = run_ping(_LoopbackSelftestInventory(), "127.0.0.1",
                      count=count, timeout_seconds=timeout_seconds)
    result.destination = "127.0.0.1 (host loopback self-test)"
    return result


class _LoopbackSelftestInventory(LabInventory):
    """Tiny inventory containing only the host loopback address, used
    exclusively by run_selftest_ping()."""
    def __init__(self) -> None:  # noqa: D401 - intentional minimal init
        from network.device_inventory import InventoryNode
        super().__init__(
            nodes=[InventoryNode(name="localhost", type="host",
                                 loopback_ip="127.0.0.1")],
            links=[])


def _truncate(text: str) -> str:
    limit = get_settings().command_output_limit_bytes
    return text[:limit]


def _require_allowed(inventory: LabInventory, destination: str) -> str:
    """Resolve a UI-selected name/IP to a validated lab IP literal."""
    node = inventory.validate_destination(destination)
    # Prefer the exact validated target if it is an IP; else node's lab IP;
    # else its management IP. Never fall back to an unvalidated string.
    try:
        import ipaddress
        ipaddress.ip_address(destination)
        return str(destination)
    except ValueError:
        pass
    target = node.lab_ip or node.mgmt_ip
    if not target:
        raise DestinationNotAllowed(
            f"Lab node {node.name!r} has no measurable address (is the lab deployed?)"
        )
    return target


def run_ping(inventory: LabInventory, destination: str,
             count: int | None = None,
             timeout_seconds: int | None = None) -> PingResult:
    """Ping an allowlisted lab destination from the collector host."""
    settings = get_settings()
    count = count or settings.ping_count
    timeout = timeout_seconds or settings.ping_timeout_seconds
    try:
        target = _require_allowed(inventory, destination)
    except DestinationNotAllowed as exc:
        return PingResult(destination=str(destination), success=False,
                          error=str(exc))

    if shutil.which("ping") is None:
        return PingResult(destination=target, success=False,
                          error="ping binary not available on this host")

    if _WINDOWS:
        # -w per-reply timeout (ms), -n count.
        argv = ["ping", "-n", str(count), "-w", str(timeout * 1000), target]
        hard_timeout = timeout * count + 10
    else:
        argv = ["ping", "-c", str(count), "-W", str(timeout), target]
        hard_timeout = timeout * count + 10

    try:
        proc = subprocess.run(argv, shell=False, capture_output=True,
                              text=True, timeout=hard_timeout)
    except subprocess.TimeoutExpired:
        return PingResult(destination=target, success=False,
                          error=f"ping timed out after {hard_timeout}s")
    except OSError as exc:
        return PingResult(destination=target, success=False,
                          error=f"{type(exc).__name__}")

    output = _truncate((proc.stdout or "") + (proc.stderr or ""))
    loss, sent, recv, avg = _parse_ping_output(output)
    success = proc.returncode == 0 and (loss is None or loss < 100.0)
    error = None
    if proc.returncode != 0 and loss is None:
        error = output.strip().splitlines()[-1][:200] if output.strip() else \
            f"ping exit code {proc.returncode}"
    return PingResult(destination=target, success=success, packets_sent=sent,
                      packets_received=recv, loss_pct=loss, avg_rtt_ms=avg,
                      error=error, raw_output=output)


def _parse_ping_output(output: str) -> tuple[float | None, int | None, int | None, float | None]:
    """Return (loss_pct, sent, received, avg_rtt_ms) - None when absent.

    Handles Windows ("Packets: Sent = 4, Received = 3, Lost = 1 (25% loss)"
    + "Average = 12ms") and Linux ("4 packets transmitted, 3 received,
    25% packet loss" + "rtt ... mavg = 1.234 ms") formats. Pure function -
    unit-tested with recorded fixtures.
    """
    loss = sent = recv = avg = None

    m = re.search(r"Sent\s*=\s*(\d+),\s*Received\s*=\s*(\d+),\s*Lost\s*=\s*(\d+)\s*\((\d+)%",
                  output, re.I)
    if m:
        sent, recv, lost, loss = (int(m.group(1)), int(m.group(2)),
                                  int(m.group(3)), float(m.group(4)))
    else:
        m = re.search(r"(\d+)\s+packets transmitted.*?(\d+)\s+(?:packets )?received.*?"
                      r"(\d+(?:\.\d+)?)%\s+packet loss", output, re.I | re.S)
        if m:
            sent, recv, loss = int(m.group(1)), int(m.group(2)), float(m.group(3))

    m = re.search(r"round[- ]trip.*?Average\s*=\s*(\d+)\s*ms", output,
                  re.I | re.S)
    if m:
        avg = float(m.group(1))
    else:
        m = re.search(r"(?:min/avg/max|mavg)[^=]*=\s*([\d.]+)/([\d.]+)/([\d.]+)",
                      output, re.I)
        if m:
            avg = float(m.group(2))
    return loss, sent, recv, avg


def traceroute_supported() -> bool:
    return _traceroute_binary() is not None


def _traceroute_binary() -> str | None:
    for candidate in ("tracert" if _WINDOWS else "traceroute",):
        if shutil.which(candidate):
            return candidate
    return None


def _parse_traceroute(output: str) -> list[dict]:
    """Parse Windows tracert / POSIX traceroute hop lines. Pure function."""
    hops: list[dict] = []
    for line in output.splitlines():
        line = line.strip()
        # Windows: "  1    2 ms    2 ms    1 ms  10.0.12.1"
        m = re.match(r"^(\d+)\s+(.*?)\s+(\S+)\s*$", line)
        if not m:
            continue
        ttl = int(m.group(1))
        host = m.group(3)
        if ttl <= 0 or ttl > 30:
            continue
        rtts: list[float] = []
        for tm in re.finditer(r"(\d+(?:\.\d+)?)\s*ms", m.group(2), re.I):
            rtts.append(float(tm.group(1)))
        if "*" in m.group(2) and not rtts:
            hops.append({"ttl": ttl, "ip": host, "rtt_ms": None})
        else:
            ip_match = re.search(r"\((\d+\.\d+\.\d+\.\d+)\)\s*$", line)
            ip = ip_match.group(1) if ip_match else (
                host if re.match(r"^\d+\.\d+\.\d+\.\d+$", host) else None)
            avg = round(sum(rtts) / len(rtts), 3) if rtts else None
            hops.append({"ttl": ttl, "ip": ip, "rtt_ms": avg})
    # Deduplicate by TTL keeping the first observation.
    seen: set[int] = set()
    unique = []
    for hop in hops:
        if hop["ttl"] not in seen:
            seen.add(hop["ttl"])
            unique.append(hop)
    return unique


def run_traceroute(inventory: LabInventory, destination: str,
                   max_hops: int = 8,
                   timeout_seconds: int | None = None) -> TracerouteResult:
    settings = get_settings()
    timeout = timeout_seconds or settings.traceroute_timeout_seconds
    try:
        target = _require_allowed(inventory, destination)
    except DestinationNotAllowed as exc:
        return TracerouteResult(destination=str(destination), supported=True,
                                success=False, error=str(exc))

    binary = _traceroute_binary()
    if binary is None:
        return TracerouteResult(destination=target, supported=False, success=False,
                                error="traceroute/tracert not available on this host")

    if _WINDOWS:
        argv = [binary, "-d", "-h", str(max_hops), "-w", "2000", target]
    else:
        argv = [binary, "-n", "-m", str(max_hops), "-w", "2", target]

    try:
        proc = subprocess.run(argv, shell=False, capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return TracerouteResult(destination=target, supported=True, success=False,
                                error=f"traceroute timed out after {timeout}s")
    except OSError as exc:
        return TracerouteResult(destination=target, supported=True, success=False,
                                error=f"{type(exc).__name__}")

    output = _truncate((proc.stdout or "") + (proc.stderr or ""))
    hops = _parse_traceroute(output)
    reached = any(h.get("ip") == target for h in hops)
    return TracerouteResult(destination=target, supported=True,
                            success=bool(hops) and reached,
                            hops=hops, raw_output=output)
