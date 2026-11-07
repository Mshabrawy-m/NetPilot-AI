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

#: Maximum number of hops to trace
MAX_HOPS = 30
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
    
    Windows tracert format:
        1     1 ms     1 ms     1 ms  192.168.1.1
        2     2 ms     1 ms     2 ms  10.0.0.1
        3    *        *        *     Request timed out.
        4     5 ms     6 ms     4 ms  203.0.113.45
    """
    hops: list[Hop] = []
    lines = output.strip().split("\n")
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # Skip header lines
        if line.startswith(("Tracing", "Over")) or \
           "maximum" in line.lower() or "timeout" in line.lower():
            continue
        
        # Parse hop line: "  1     1 ms     1 ms     1 ms  192.168.1.1"
        # or: "  3    *        *        *     Request timed out."
        match = re.match(
            r"^\s*(\d+)\s+(?:(\d+)\s+ms|\*)\s+(?:(\d+)\s+ms|\*)\s+(?:(\d+)\s+ms|\*)\s+(.*?)\s*$",
            line
        )
        if match:
            ttl = int(match.group(1))
            rtt1_str, rtt2_str, rtt3_str = match.group(2), match.group(3), match.group(4)
            rest = match.group(5).strip()
            
            # Parse RTT values
            def parse_rtt(s):
                if s == "*":
                    return None
                try:
                    return float(s)
                except (ValueError, TypeError):
                    return None
            
            rtt1 = parse_rtt(rtt1_str)
            rtt2 = parse_rtt(rtt2_str)
            rtt3 = parse_rtt(rtt3_str)
            
            # Check if responsive (at least one RTT is not None)
            responsive = any(r is not None for r in (rtt1, rtt2, rtt3))
            
            # Extract address and hostname
            address = None
            hostname = None
            
            # Rest might be "192.168.1.1" or "router.example.com [192.168.1.1]"
            # or "Request timed out."
            if "Request timed out." in rest or not responsive:
                address = None
                hostname = None
            else:
                # Try to extract IP address from brackets
                bracket_match = re.search(r"\[([^\]]+)\]", rest)
                if bracket_match:
                    hostname = rest.replace(bracket_match.group(0), "").strip()
                    address = bracket_match.group(1)
                else:
                    # Might be just an IP or hostname
                    parts = rest.split()
                    if parts:
                        # Last part is usually the address
                        potential_addr = parts[-1]
                        # Check if it's an IP
                        if re.match(r"^\d+\.\d+\.\d+\.\d+$", potential_addr):
                            address = potential_addr
                            hostname = " ".join(parts[:-1]).strip() or None
                        else:
                            hostname = rest
                            address = None
            
            hops.append(Hop(
                ttl=ttl,
                address=address,
                hostname=hostname,
                rtt1=rtt1,
                rtt2=rtt2,
                rtt3=rtt3,
                resolved=address is not None,
                responsive=responsive
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
        # Windows tracert: tracert [-d] [-h max_hops] [-j host_list] [-w timeout] target
        argv = [cmd_name, "-h", str(MAX_HOPS), "-w", str(timeout * 1000), destination]
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
        # Run with timeout
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
        )
        
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            return_code = process.returncode
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            return_code = -1
        
        result.finished_at = utc_now()
        
        if return_code != 0:
            error_msg = stderr.strip() or stdout.strip()
            result.success = False
            result.error = f"Command failed with exit code {return_code}: {error_msg[:500]}"
            return result
        
        # Parse output based on platform
        output = stdout
        if platform == "win32" or _is_windows_output(output):
            result.hops = _parse_tracert_output(destination, output)
        else:
            result.hops = _parse_traceroute_output(destination, output)
        
        result.success = True
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
    
    Each distinct hop IP becomes a device, consecutive hops become links.
    Paths to different destinations merge on shared hops to form a tree/graph.
    
    Args:
        destinations: List of IP addresses or hostnames to trace
        timeout: Maximum seconds to wait for each traceroute
    
    Returns:
        DiscoveredPath with devices, links, and per-hop metrics
    """
    devices_dict: dict[str, dict] = {}  # address -> device info
    links_set: set[tuple[str, str]] = set()  # (src_addr, dst_addr)
    metrics_list: list[dict] = []
    
    for destination in destinations:
        traceroute_result = run_traceroute(destination, timeout=timeout)
        
        if not traceroute_result.success:
            continue
        
        # Track previous hop for link creation
        prev_hop: Hop | None = None
        
        for hop in traceroute_result.hops:
            if hop.address is None:
                # Can't create a device without an address
                continue
            
            # Add device if not already seen
            if hop.address not in devices_dict:
                devices_dict[hop.address] = {
                    "name": hop.address,
                    "type": "router",  # Default, could be refined
                    "lab_ip": hop.address,
                    "mgmt_ip": None,
                    "status": "up" if hop.responsive else "down"
                }
            
            # Create link from previous hop to this hop
            if prev_hop and prev_hop.address and hop.address:
                link_key = (prev_hop.address, hop.address)
                if link_key not in links_set:
                    links_set.add(link_key)
                # Also add reverse for undirected graph
                reverse_key = (hop.address, prev_hop.address)
                if reverse_key not in links_set:
                    links_set.add(reverse_key)
            
            # Add per-hop RTT metrics
            if hop.responsive:
                for i, rtt in enumerate([hop.rtt1, hop.rtt2, hop.rtt3], 1):
                    if rtt is not None:
                        metrics_list.append({
                            "device_name": hop.address,
                            "interface_name": None,
                            "metric": f"traceroute.hop_{i}_rtt_ms",
                            "value": rtt,
                            "unit": "ms",
                            "data_source": "live",
                            "collected_at": utc_now()
                        })
            
            prev_hop = hop
    
    # Convert to lists
    devices = list(devices_dict.values())
    links = [{"device_a": src, "device_b": dst, "status": "up"} 
             for src, dst in links_set]
    
    return DiscoveredPath(
        destination=", ".join(destinations),
        devices=devices,
        links=links,
        metrics=metrics_list
    )
