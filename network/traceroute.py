"""OS-level traceroute for real topology discovery (Windows tracert / Unix traceroute).

Provides safe, allowlisted traceroute execution with:
- No shell usage (direct subprocess with argv list)
- Fixed timeout
- Defensive parsing of Windows and localized output
- Per-hop RTT extraction
- Handling of '*' (unresponsive) hops

The discovered hops become devices, consecutive hops become links,
and paths to different destinations merge on shared hops to form a real tree/graph.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime

from config import utc_now

#: Default maximum number of hops; keep well below the 37s Windows default loop.
MAX_HOPS = 12
#: Timeout in seconds for traceroute command
TRACEROUTE_TIMEOUT = 10
#: Maximum number of concurrent traceroutes
MAX_CONCURRENT = 5

#: Allowlisted command names (no shell, argv list only)
TRACEROUTE_CMD = {
    "win32": "tracert",
    "linux": "traceroute",
    "darwin": "traceroute",
}

#: Pattern to detect Windows (case-insensitive)
WINDOWS_PATTERN = re.compile(r"Microsoft\s+Windows", re.IGNORECASE)


@dataclass
class Hop:
    """One hop in a traceroute path."""

    ttl: int
    address: str | None
    hostname: str | None
    rtt1: float | None  # ms
    rtt2: float | None
    rtt3: float | None
    resolved: bool
    responsive: bool


@dataclass
class TracerouteResult:
    """Result of a single traceroute operation."""

    destination: str
    success: bool
    hops: list[Hop] = field(default_factory=list)
    error: str | None = None
    started_at: datetime = field(default_factory=utc_now)
    finished_at: datetime | None = None


@dataclass
class DiscoveredPath:
    """Discovered path from traceroute with device/link information."""

    destination: str
    devices: list[dict]  # device_name, lab_ip, type, etc.
    links: list[dict]    # device_a, if_a, device_b, if_b, etc.
    metrics: list[dict] # device_name, metric, value, collected_at, etc.


def _get_platform() -> str:
    """Detect platform: win32, linux, or darwin."""
    import sys
    if sys.platform.startswith("win"):
        return "win32"
    elif sys.platform.startswith("linux"):
        return "linux"
    elif sys.platform.startswith("darwin"):
        return "darwin"
    return "unknown"


def _is_windows_output(output: str) -> bool:
    """Check if output is from Windows tracert."""
    return bool(WINDOWS_PATTERN.search(output))


def _parse_tracert_output(destination: str, output: str) -> list[Hop]:
    """Parse Windows tracert output.

    Handles the common English tracert variants as well as the captured operator
    output where RTTs use ``<1 ms`` and trailing hops silently time out.
    """
    hops: list[Hop] = []
    for line in output.strip().splitlines():
        line = line.strip()
        if not line:
           continue

        lower = line.lower()
        if lower.startswith(("tracing route to", "over a maximum of")):
           continue
        if lower.startswith("trace complete"):
           continue
        if lower.startswith("request timed out"):
           continue
        if "maximum of" in lower and "hops" in lower:
            continue

        match = re.match(r"^\s*(\d+)\s+(.*)$", line)
        if not match:
           continue
        ttl = int(match.group(1))
        rest = match.group(2).strip()
        if not rest:
           continue

        rtt_values: list[float | None] = []
        rtt_tokens = re.findall(r"(?:<\d+(?:\.\d+)?|\d+(?:\.\d+)?|\*)\s*ms", rest, flags=re.IGNORECASE)
        for token in rtt_tokens[:3]:
           value = token.split("ms", 1)[0].strip()
           if value == "*":
               rtt_values.append(None)
               continue
           if value.startswith("<"):
               try:
                   rtt_values.append(float(value[1:]) * 0.5)
               except ValueError:
                   rtt_values.append(None)
               continue
           try:
               rtt_values.append(float(value))
           except ValueError:
               rtt_values.append(None)

        if "request timed out" in rest.lower():
           hops.append(Hop(
               ttl=ttl,
               address=None,
               hostname=None,
               rtt1=None,
               rtt2=None,
               rtt3=None,
               resolved=False,
               responsive=False,
           ))
           continue

        if len(rtt_values) < 3:
           # Some windows variants omit the final milliseconds marker on a timeout
           # line or a partial response. Preserve the hop if it still has an IP.
           rtt_values.extend([None] * (3 - len(rtt_values)))

        responsive = any(v is not None for v in rtt_values)
        if not responsive:
           hops.append(Hop(
               ttl=ttl,
               address=None,
               hostname=None,
               rtt1=None,
               rtt2=None,
               rtt3=None,
               resolved=False,
               responsive=False,
           ))
           continue

        remaining = re.sub(r"(?:<\d+(?:\.\d+)?|\d+(?:\.\d+)?|\*)\s*ms", " ", rest, flags=re.IGNORECASE)
        remaining = re.sub(r"\s+", " ", remaining).strip()
        if not remaining:
           address = None
           hostname = None
        else:
           bracket_match = re.search(r"\[([^\]]+)\]", remaining)
           if bracket_match:
               hostname = remaining[:bracket_match.start()].strip() or None
               address = bracket_match.group(1).strip()
           else:
               parts = remaining.split()
               if parts and re.fullmatch(r"\d+\.\d+\.\d+\.\d+", parts[-1]):
                   address = parts[-1]
                   hostname = " ".join(parts[:-1]).strip() or None
               else:
                   hostname = remaining
                   address = None

        hops.append(Hop(
           ttl=ttl,
           address=address,
           hostname=hostname,
           rtt1=rtt_values[0] if len(rtt_values) > 0 else None,
           rtt2=rtt_values[1] if len(rtt_values) > 1 else None,
           rtt3=rtt_values[2] if len(rtt_values) > 2 else None,
           resolved=address is not None,
           responsive=responsive,
        ))

    return hops


def _parse_traceroute_output(destination: str, output: str) -> list[Hop]:
    """Parse Unix traceroute output.
    
    Unix traceroute format:
        1  gateway (192.168.1.1)  1.234 ms  1.567 ms  1.890 ms
        2  10.0.0.1 (10.0.0.1)  2.345 ms  3.456 ms  4.567 ms
        3  * * *
        4  203.0.113.45 (203.0.113.45)  5.678 ms  6.789 ms  7.890 ms
    """
    hops: list[Hop] = []
    lines = output.strip().split("\n")
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # Skip header
        if "traceroute to" in line.lower():
            continue
        
        # Handle timeout lines: "3  *" or "3  * * *"
        timeout_match = re.match(r"^\s*(\d+)\s+\*\s*$", line)
        if timeout_match:
            ttl = int(timeout_match.group(1))
            hops.append(Hop(
                ttl=ttl,
                address=None,
                hostname=None,
                rtt1=None,
                rtt2=None,
                rtt3=None,
                resolved=False,
                responsive=False
            ))
            continue
        
        # Handle timeout lines with multiple asterisks: "3  * * *"
        timeout_multi_match = re.match(r"^\s*(\d+)\s+\*\s+\*\s+\*\s*$", line)
        if timeout_multi_match:
            ttl = int(timeout_multi_match.group(1))
            hops.append(Hop(
                ttl=ttl,
                address=None,
                hostname=None,
                rtt1=None,
                rtt2=None,
                rtt3=None,
                resolved=False,
                responsive=False
            ))
            continue
        
        # Unix format: "1  gateway (192.168.1.1)  1.234 ms  1.567 ms  1.890 ms"
        match = re.match(
            r"^\s*(\d+)\s+([^(]+)(?:\(([^)]+)\))?\s+((?:\d+\.?\d*\s+ms\s*){0,3})\s*$",
            line
        )
        if match:
            ttl = int(match.group(1))
            hostname_part = match.group(2).strip()
            address = match.group(3)
            rtt_part = match.group(4)
            
            # Parse RTTs
            rtts = re.findall(r"(\d+\.?\d*)\s+ms", rtt_part)
            rtt1 = float(rtts[0]) if len(rtts) > 0 else None
            rtt2 = float(rtts[1]) if len(rtts) > 1 else None
            rtt3 = float(rtts[2]) if len(rtts) > 2 else None
            
            responsive = any(r is not None for r in (rtt1, rtt2, rtt3))
            
            # Clean hostname
            hostname = hostname_part if hostname_part and hostname_part != address else None
            
            hops.append(Hop(
                ttl=ttl,
                address=address,
                hostname=hostname,
                rtt1=rtt1,
                rtt2=rtt2,
                rtt3=rtt3,
                resolved=address is not None or hostname is not None,
                responsive=responsive
            ))
    
    return hops


def run_traceroute(destination: str, timeout: int = TRACEROUTE_TIMEOUT) -> TracerouteResult:
    """Run traceroute to a destination.
    
    Uses OS-native traceroute command with allowlisted argv.
    No shell, timeout enforced, output captured.
    
    Args:
        destination: IP address or hostname to trace
        timeout: Maximum seconds to wait for the command
    
    Returns:
        TracerouteResult with hops, success status, and error (if any)
    """
    platform = _get_platform()
    cmd_name = TRACEROUTE_CMD.get(platform)
    
    if cmd_name is None:
        return TracerouteResult(
            destination=destination,
            success=False,
            error=f"Traceroute not supported on platform: {platform}"
        )
    
    # Build safe argv list (no shell injection possible)
    if platform == "win32":
        # Encourage a bounded, fast probe on Windows: keep the TTL/timeout small
        # enough to complete promptly while still capturing responsive hops.
        argv = [cmd_name, "-d", "-h", str(12), "-w", "1000", destination]
    else:
        # Unix traceroute: traceroute [-m max_ttl] [-w wait_time] [-q nqueries] host
        argv = [cmd_name, "-m", str(MAX_HOPS), "-w", str(timeout),
                "-q", "3", destination]
    
    result = TracerouteResult(
        destination=destination,
        success=False,  # Will be updated later
        started_at=utc_now()
    )
    
    try:
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        try:
            stdout, stderr = process.communicate(timeout=timeout)
            return_code = process.returncode
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            return_code = -1

        result.finished_at = utc_now()
        stdout_text = stdout.decode("utf-8", errors="replace") if isinstance(stdout, bytes) else (stdout or "")
        stderr_text = stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else (stderr or "")
        output = stdout_text + stderr_text
        if platform == "win32" or _is_windows_output(output):
            result.hops = _parse_tracert_output(destination, output)
        else:
            result.hops = _parse_traceroute_output(destination, output)

        if result.hops:
            result.success = True
            result.error = None
            return result

        if return_code != 0:
            error_msg = (stderr_text or stdout_text or "").strip()
            result.error = f"Command failed with exit code {return_code}: {error_msg[:500]}"
            result.success = False
        else:
            result.success = False
            result.error = None
        return result
        
    except OSError as e:
        result.finished_at = utc_now()
        result.success = False
        result.error = f"{type(e).__name__}: {str(e)[:500]}"
        return result


def discover_topology_from_traceroute(
    destinations: list[str],
    timeout: int = TRACEROUTE_TIMEOUT
) -> DiscoveredPath:
    """Discover network topology by running traceroute to multiple destinations.

    Responsive hops become real devices; trailing timeout hops are collapsed into
    a single synthetic node so the topology stays honest without inventing IPs.
    """
    devices_dict: dict[str, dict] = {}  # address -> device info
    links_set: set[tuple[str, str]] = set()  # (src_addr, dst_addr)
    metrics_list: list[dict] = []
    friendly_names = {
        "1.1.1.1": "cloudflare-dns",
        "8.8.8.8": "google-dns",
        "9.9.9.9": "quad9-dns",
        "208.67.222.222": "opendns",
    }

    def canonical_device_name(address: str | None, *, default: str) -> str:
        if not address:
            return default
        return friendly_names.get(address, default)

    def ensure_device(key: str, *, name: str, lab_ip: str | None, status: str = "up") -> None:
        if key not in devices_dict:
            devices_dict[key] = {
               "name": name,
               "type": "router",
               "lab_ip": lab_ip,
               "mgmt_ip": None,
               "status": status,
            }
            return
        existing = devices_dict[key]
        if not existing["name"] or existing["name"] == key:
            existing["name"] = name
        if not existing["lab_ip"] and lab_ip:
            existing["lab_ip"] = lab_ip
        if status != "up" and existing["status"] == "up":
            return
        existing["status"] = status

    for destination in destinations:
        traceroute_result = run_traceroute(destination, timeout=timeout)
        if not traceroute_result.hops:
            continue

        prev_hop: Hop | None = None
        last_responsive: Hop | None = None
        trailing_unresponsive = 0
        consecutive_unresponsive = 0

        for hop in traceroute_result.hops:
            # Never walk past the documented hop budget (defensive: some
            # localized parsers can emit more lines than the command allowed).
            if hop.ttl > MAX_HOPS:
                break
            if hop.address is not None:
               last_responsive = hop
               trailing_unresponsive = 0
               consecutive_unresponsive = 0
               resolved_name = canonical_device_name(hop.address, default=hop.address)
               ensure_device(
                   hop.address,
                   name=resolved_name,
                   lab_ip=hop.address,
                   status="up" if hop.responsive else "unknown",
               )
               if prev_hop and prev_hop.address and hop.address:
                   prev_name = canonical_device_name(prev_hop.address, default=devices_dict[prev_hop.address]["name"])
                   curr_name = canonical_device_name(hop.address, default=devices_dict[hop.address]["name"])
                   link_key = tuple(sorted((prev_name, curr_name)))
                   links_set.add(link_key)
               prev_hop = hop
            else:
               trailing_unresponsive += 1
               consecutive_unresponsive += 1
               # The ISP hides the rest of the path; three silent hops in a row
               # is enough evidence for one honest "unresponsive path" marker.
               if consecutive_unresponsive >= 3:
                   break

            if hop.responsive and hop.address is not None:
               for i, rtt in enumerate([hop.rtt1, hop.rtt2, hop.rtt3], 1):
                   if rtt is not None:
                       metrics_list.append({
                           "device_name": devices_dict[hop.address]["name"],
                           "interface_name": None,
                           "metric": f"traceroute.hop_{i}_rtt_ms",
                           "value": rtt,
                           "unit": "ms",
                           "data_source": "live",
                           "collected_at": utc_now(),
                       })

        if trailing_unresponsive and last_responsive and last_responsive.address:
            destination_key = destination
            destination_name = destination_key
            ensure_device(destination_key, name=destination_name,
                          lab_ip=destination, status="up")
            synthetic_key = f"__unresponsive__{destination_key}"
            synthetic_name = f"unresponsive-path:{destination_key}"
            ensure_device(synthetic_key, name=synthetic_name, lab_ip=None, status="unknown")
            last_name = devices_dict[last_responsive.address].get("name", last_responsive.address)
            for pair in (
               tuple(sorted((last_name, synthetic_name))),
               tuple(sorted((synthetic_name, destination_name))),
            ):
               links_set.add(pair)

    devices = list(devices_dict.values())
    links = [{"device_a": src, "device_b": dst, "status": "up"}
             for src, dst in sorted(set(links_set))]

    return DiscoveredPath(
        destination=", ".join(destinations),
        devices=devices,
        links=links,
        metrics=metrics_list,
    )
