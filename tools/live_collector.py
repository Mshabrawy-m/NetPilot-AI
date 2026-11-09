"""Windows-native live collector for NetPilot AI (Phase 3A).

This is the Docker/Containerlab-free collector: it measures real ICMP
round-trip time, jitter and packet loss from the operator's Windows machine to
a small, explicitly configured set of targets, optionally records local NIC
byte/error counters, and writes every row with ``data_source='live'`` to the
shared PostgreSQL database that the hosted Streamlit app reads.

What it is *not*
----------------
* It never configures a device, runs a routing command, or changes a safety
  flag. The only network action is an outbound ``ping`` (and optional local
  interface counter reads).
* It never writes ``data_source='demo'`` and refuses to run in demo mode, so
  live and simulated rows can never be mixed.
* It never prints, logs or commits a DSN/password. The collector role's
  connection string is read in-process from Windows Credential Manager (or the
  ``NETPILOT_DB_URL_COLLECTOR`` environment variable) and is never echoed.

Identity mapping
----------------
Each target becomes a ``devices`` row and a series under a stable label:

    gateway          -> the auto-detected default gateway
    1.1.1.1          -> cloudflare-dns
    8.8.8.8          -> google-dns
    9.9.9.9          -> quad9-dns
    other IP/host    -> target-<slug> (or an explicit ``LABEL=ADDRESS``)

Metrics written per target are ``ping.avg_rtt_ms``, ``ping.jitter_ms`` and
``ping.loss_pct``; a failed probe stores NULL for the values it could not
measure (never an invented number). Local NIC counters use the existing
``if.*`` metric names on a ``collector-host`` device.

Usage
-----
    python tools/live_collector.py --once
    python tools/live_collector.py --interval 30 --duration 900
    python tools/live_collector.py --targets 1.1.1.1,8.8.8.8
    python tools/live_collector.py --targets demo-target=192.0.2.1 --once

Exit codes
----------
    0  at least one cycle persisted (or Ctrl+C)
    1  the last persisted cycle reported a collection error
    2  configuration error (demo mode, safety flag, missing collector DSN)
    3  the collector database was unavailable
"""
from __future__ import annotations

import argparse
import datetime as dt
import errno
import hashlib
import ipaddress
import locale
import logging
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.exc import IntegrityError, OperationalError

from config import get_settings
from database import connection, repository, roles
from database.models import CollectionRun, db_utcnow
from network.connectivity import _WINDOWS, _parse_ping_output, _ping_binary
from network.traceroute import (
    TRACEROUTE_TIMEOUT,
    discover_topology_from_traceroute,
)

try:  # optional: only used when the package is already installed
    import psutil
except ImportError:  # pragma: no cover - depends on the operator machine
    psutil = None  # type: ignore[assignment]

logger = logging.getLogger("netpilot.live_collector")

EXIT_OK = 0
EXIT_COLLECTION_ERROR = 1
EXIT_CONFIG = 2
EXIT_DB = 3

WCM_TARGET_COLLECTOR = "netpilot/prod/netpilot_collector"
DEFAULT_TARGETS = "gateway,1.1.1.1,8.8.8.8"
DEFAULT_TRACEROUTE_DESTINATIONS = [
    "1.1.1.1",
    "8.8.8.8",
    "9.9.9.9",
    "208.67.222.222",
    "142.250.186.46",
]

#: Friendly, stable device labels for well-known public resolvers.
_KNOWN_LABELS = {
    "1.1.1.1": "cloudflare-dns",
    "8.8.8.8": "google-dns",
    "9.9.9.9": "quad9-dns",
    "208.67.222.222": "opendns",
}

_METRIC_UNITS = {
    "ping.avg_rtt_ms": "ms",
    "ping.jitter_ms": "ms",
    "ping.loss_pct": "%",
    "if.rx_bytes": "bytes",
    "if.tx_bytes": "bytes",
    "if.rx_errors": "count",
    "if.tx_errors": "count",
    "if.rx_dropped": "count",
    "if.tx_dropped": "count",
    "if.operstate": "link",
}

_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)"
    r"(?:\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*$"
)
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_RTT_TOKEN = re.compile(
    r"(?i)\btime\s*[=<]\s*(\d+(?:[.,]\d+)?)\s*ms\b")
_TTL_TOKEN = re.compile(r"(?i)\bTTL\s*[=:]\s*\d+")
_ICMP_SEQ = re.compile(r"(?i)\bicmp_seq\s*=\s*\d+")


# ---------------------------------------------------------------------------
# Target validation and identity mapping
# ---------------------------------------------------------------------------
def validate_target(raw: str) -> str:
    """Return a validated IPv4 literal or DNS hostname.

    The result is only ever passed to ``subprocess.run`` as one argv element
    with ``shell=False``. Validation is still strict: whitespace, shell
    metacharacters, path separators and quotes are rejected, and non-IPv4
    address literals are refused (the Windows ping parser here is IPv4).
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("target is empty")
    if len(text) > 253:
        raise ValueError("target is too long")
    forbidden = set(" \t\r\n;&|$`<>(){}[]'\"\\/")
    if any(ch in forbidden for ch in text):
        raise ValueError(
            "target contains characters that are not allowed in a host or IP")
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        if not _HOSTNAME_RE.match(text):
            raise ValueError(
                "target is neither a valid IPv4 address nor a valid hostname")
        return text.lower()
    if address.version != 4:
        raise ValueError("only IPv4 targets are supported")
    return str(address)


def validate_label(raw: str) -> str:
    """Return a safe, stable device label (max 128 chars)."""
    text = (raw or "").strip()
    if not text:
        raise ValueError("target label is empty")
    if not _LABEL_RE.match(text):
        raise ValueError(
            "target label must start alphanumeric and contain only "
            "letters, digits, '.', '_' or '-'")
    return text


def target_label(address: str) -> str:
    """Map a validated target address to a stable device label."""
    if address in _KNOWN_LABELS:
        return _KNOWN_LABELS[address]
    try:
        ipaddress.IPv4Address(address)
    except ValueError:
        slug = re.sub(r"[^a-z0-9]+", "-", address.lower()).strip("-")[:100]
        return f"target-{slug}" if slug else "target-host"
    return "target-" + address.replace(".", "-")


def detect_default_gateway() -> str | None:
    """Best-effort, locale-independent default-IPv4-gateway detection.

    Windows ``route print -4`` is parsed by its numeric ``0.0.0.0 0.0.0.0
    <gateway>`` row, so localized column headings do not matter. POSIX uses
    ``ip route show default`` or ``route -n``. Any failure returns ``None``;
    the collector then simply skips the gateway target.
    """
    try:
        if _WINDOWS:
            binary = shutil.which("route") or r"C:\Windows\System32\route.exe"
            if not os.path.isfile(binary):
                return None
            argv = [binary, "print", "-4"]
        else:
            binary = shutil.which("ip") or shutil.which("route")
            if not binary:
                return None
            argv = ([binary, "route", "show", "default"]
                    if os.path.basename(binary) == "ip"
                    else [binary, "-n"])
        proc = subprocess.run(argv, check=False, shell=False,
                              capture_output=True, timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        return None

    output = _decode_output(proc.stdout)
    for line in output.splitlines():
        parts = line.split()
        if not parts:
            continue
        candidate = None
        if len(parts) >= 3 and parts[0] == "0.0.0.0" and parts[1] == "0.0.0.0":
            candidate = parts[2]
        elif parts[0].lower() == "default" and "via" in parts:
            candidate = parts[parts.index("via") + 1]
        elif len(parts) >= 2 and parts[0] == "0.0.0.0":
            candidate = parts[1]
        if candidate is None:
            continue
        try:
            return str(ipaddress.IPv4Address(candidate))
        except ValueError:
            continue
    return None


@dataclass(frozen=True)
class TargetSpec:
    address: str
    label: str


def build_target_specs(
    raw: str | None,
    gateway_detect: Callable[[], str | None] = detect_default_gateway,
) -> list[TargetSpec]:
    """Parse ``--targets`` into validated, uniquely labelled specs.

    Accepted forms: ``ADDRESS``, ``LABEL=ADDRESS`` and the keyword
    ``gateway`` (auto-detected). ``LABEL=ADDRESS`` is what makes the documented
    anomaly scenario possible: the same device label can be measured against a
    different real target without inventing any data.
    """
    text = (raw or "").strip() or DEFAULT_TARGETS
    specs: list[TargetSpec] = []
    labels: set[str] = set()
    for token in text.split(","):
        token = token.strip()
        if not token:
            continue
        label: str | None = None
        address_text = token
        if "=" in token:
            label, address_text = (part.strip() for part in token.split("=", 1))
        if address_text.lower() == "gateway":
            gateway = gateway_detect()
            if not gateway:
                logger.warning(
                    "default gateway could not be detected; skipping it")
                continue
            address = validate_target(gateway)
            label = label or "gateway"
        else:
            address = validate_target(address_text)
            label = label or target_label(address)
        label = validate_label(label)
        if label in labels:
            raise ValueError(f"duplicate target label {label!r}")
        labels.add(label)
        specs.append(TargetSpec(address=address, label=label))
    if not specs:
        raise ValueError("no usable targets were configured")
    return specs


# ---------------------------------------------------------------------------
# Ping parsing and execution
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ParsedPing:
    loss_pct: float | None = None
    sent: int | None = None
    received: int | None = None
    avg_rtt_ms: float | None = None
    rtts: tuple[float, ...] = ()


def _decode_output(raw: bytes | str | None) -> str:
    """Decode ping/route output without ever raising a Unicode error."""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    encoding = locale.getpreferredencoding(False) or "utf-8"
    return raw.decode(encoding, errors="replace")


def _extract_rtts(output: str) -> tuple[float, ...]:
    """Extract per-reply RTTs in milliseconds from Windows/POSIX output.

    The primary pattern (``time=12ms`` / ``time<1ms``) is locale-independent.
    If a localized ping replaces the word "time", reply lines are still
    identifiable by the protocol tokens ``TTL=`` (Windows) or ``icmp_seq=``
    (POSIX), so the bare numeric ``Nms`` tokens on those lines are used.
    """
    values = [float(match.group(1).replace(",", "."))
              for match in _RTT_TOKEN.finditer(output)]
    if values:
        return tuple(values)
    for line in output.splitlines():
        low = line.lower()
        if "ttl" not in low and "icmp_seq" not in low:
            continue
        for match in re.finditer(r"(?i)(\d+(?:[.,]\d+)?)\s*ms\b", line):
            values.append(float(match.group(1).replace(",", ".")))
    return tuple(values)


def parse_ping_output(output: str, expected_count: int) -> ParsedPing:
    """Parse ping output defensively; never raise on malformed/localized text.

    ``connectivity._parse_ping_output`` handles the English Windows/POSIX
    summary formats. If that yields no loss figure, the reply counts are
    recovered from ``TTL=`` / ``icmp_seq=`` tokens, which survive localization.
    A summary percentage is used as a last resort when no reply lines exist.
    """
    loss, sent, recv, avg = _parse_ping_output(output)
    rtts = _extract_rtts(output)

    if loss is None:
        replies = len(_TTL_TOKEN.findall(output))
        if replies == 0:
            replies = len(_ICMP_SEQ.findall(output))
        if replies:
            sent = max(1, int(expected_count))
            recv = min(replies, sent)
            loss = max(0.0, (sent - recv) / sent * 100.0)
        else:
            percent = re.search(r"\((\d+(?:\.\d+)?)%", output)
            if percent:
                loss = float(percent.group(1))
                sent = max(1, int(expected_count))
                recv = round(sent * (1.0 - loss / 100.0))

    if avg is None and rtts:
        avg = round(statistics.fmean(rtts), 3)
    return ParsedPing(loss_pct=loss, sent=sent, received=recv,
                      avg_rtt_ms=avg, rtts=rtts)


def _jitter_ms(rtts: tuple[float, ...]) -> float | None:
    if len(rtts) < 2:
        return None
    return round(statistics.pstdev(rtts), 3)


@dataclass(frozen=True)
class PingSample:
    target: str
    success: bool
    packets_sent: int | None = None
    packets_received: int | None = None
    loss_pct: float | None = None
    avg_rtt_ms: float | None = None
    jitter_ms: float | None = None
    error: str | None = None
    unsupported: bool = False


def ping_target(target: str, count: int = 4,
                timeout_seconds: float = 1.0) -> PingSample:
    """Run one real OS ping with a strict argv allowlist (never a shell)."""
    try:
        address = validate_target(target)
    except ValueError as exc:
        return PingSample(target=str(target), success=False, error=str(exc))

    count = max(1, min(int(count), 20))
    timeout = max(0.2, min(float(timeout_seconds), 10.0))

    binary = _ping_binary()
    if binary is None:
        return PingSample(
            target=address, success=False, unsupported=True,
            error="ping binary not available on this host")

    if _WINDOWS:
        argv = [binary, "-n", str(count), "-w", str(int(timeout * 1000)),
                "-4", address]
    else:
        argv = [binary, "-4", "-c", str(count),
                "-W", str(max(1, round(timeout))), address]
    hard_timeout = count * timeout + 5.0

    try:
        proc = subprocess.run(argv, check=False, shell=False,
                              capture_output=True, timeout=hard_timeout)
    except subprocess.TimeoutExpired:
        return PingSample(
            target=address, success=False, packets_sent=count,
            packets_received=0, loss_pct=100.0,
            error=f"ping timed out after {hard_timeout:.0f}s")
    except OSError as exc:
        denied = exc.errno in (errno.EACCES, errno.EPERM)
        reason = ("ping cannot open an ICMP socket in this environment"
                  if denied else f"cannot execute {binary}")
        return PingSample(
            target=address, success=False, unsupported=denied,
            error=f"{reason}: {exc.strerror or type(exc).__name__}")

    output = _decode_output(proc.stdout) + _decode_output(proc.stderr)
    parsed = parse_ping_output(output, count)
    success = (proc.returncode == 0 and parsed.loss_pct is not None
               and parsed.loss_pct < 100.0)
    error = None
    if not success:
        if parsed.loss_pct is None:
            error = "ping output could not be parsed"
        elif parsed.loss_pct >= 100.0:
            error = "destination unreachable (100% packet loss)"
        else:
            error = f"ping exit code {proc.returncode}"
    return PingSample(
        target=address, success=success,
        packets_sent=parsed.sent if parsed.sent is not None else count,
        packets_received=parsed.received, loss_pct=parsed.loss_pct,
        avg_rtt_ms=parsed.avg_rtt_ms, jitter_ms=_jitter_ms(parsed.rtts),
        error=error)


# ---------------------------------------------------------------------------
# Optional local interface counters
# ---------------------------------------------------------------------------
def _sanitize_interface_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(name)).strip("_")
    return cleaned[:64]


def collect_interface_samples(psutil_module=None,
                              max_interfaces: int = 8) -> list[dict]:
    """Read local NIC byte/error counters when ``psutil`` is already present.

    Returns an empty list when psutil is unavailable or the platform refuses
    the read - the collector then simply omits this optional source. Values are
    the OS counters exactly as read; no rates are invented here.
    """
    module = psutil_module if psutil_module is not None else psutil
    if module is None:
        return []
    try:
        counters = module.net_io_counters(pernic=True)
        stats = module.net_if_stats()
    except Exception:  # noqa: BLE001 - optional source, never fatal
        return []

    candidates: list[tuple[int, str, object]] = []
    for name, counter in counters.items():
        state = stats.get(name)
        if state is not None and not getattr(state, "isup", True):
            continue
        traffic = (int(getattr(counter, "bytes_recv", 0))
                   + int(getattr(counter, "bytes_sent", 0)))
        if traffic <= 0:
            continue
        candidates.append((traffic, name, counter))
    candidates.sort(key=lambda item: item[0], reverse=True)

    rows: list[dict] = []
    seen: set[str] = set()
    for _traffic, name, counter in candidates[:max(1, int(max_interfaces))]:
        interface = _sanitize_interface_name(name)
        if not interface or interface in seen:
            continue
        seen.add(interface)
        values = (
            ("if.rx_bytes", getattr(counter, "bytes_recv", None)),
            ("if.tx_bytes", getattr(counter, "bytes_sent", None)),
            ("if.rx_errors", getattr(counter, "errin", None)),
            ("if.tx_errors", getattr(counter, "errout", None)),
            ("if.rx_dropped", getattr(counter, "dropin", None)),
            ("if.tx_dropped", getattr(counter, "dropout", None)),
            ("if.operstate", 1.0),
        )
        for metric, value in values:
            rows.append({
                "device_name": "collector-host",
                "interface_name": interface,
                "metric": metric,
                "value": None if value is None else float(value),
                "unit": _METRIC_UNITS[metric],
            })
    return rows


# ---------------------------------------------------------------------------
# One collection cycle
# ---------------------------------------------------------------------------
@dataclass
class CycleResult:
    collected_at: dt.datetime
    targets: list[TargetSpec]
    samples: list[dict]
    device_statuses: dict[str, str]
    devices_total: int
    devices_up: int
    status: str
    errors: list[str]


def _sample(device: str, metric: str, value: float | None, unit: str,
            collected_at: dt.datetime,
            interface: str | None = None) -> dict:
    return {
        "device_name": device,
        "interface_name": interface,
        "metric": metric,
        "value": value,
        "unit": unit,
        "data_source": "live",
        "collected_at": collected_at,
    }


def run_cycle(
    targets: list[TargetSpec],
    *,
    count: int = 4,
    timeout_seconds: float = 1.0,
    ping_fn: Callable[[str, int, float], PingSample] = ping_target,
    interface_fn: Callable[[], list[dict]] = collect_interface_samples,
    now_fn: Callable[[], dt.datetime] = db_utcnow,
) -> CycleResult:
    """Probe every target and assemble one idempotent batch of live samples."""
    collected_at = now_fn()
    try:
        collected_at = collected_at.replace(microsecond=0)
    except AttributeError:  # pragma: no cover - defensive for injected clocks
        pass

    samples: list[dict] = []
    errors: list[str] = []
    statuses: dict[str, str] = {}
    devices_up = 0
    for spec in targets:
        result = ping_fn(spec.address, count, timeout_seconds)
        if result.success:
            devices_up += 1
            statuses[spec.label] = "up"
        elif result.unsupported:
            statuses[spec.label] = "unknown"
        else:
            statuses[spec.label] = "down"
        if result.error:
            errors.append(f"{spec.label}: {result.error}")
        for metric, value, unit in (
            ("ping.avg_rtt_ms", result.avg_rtt_ms, "ms"),
            ("ping.jitter_ms", result.jitter_ms, "ms"),
            ("ping.loss_pct", result.loss_pct, "%"),
        ):
            samples.append(_sample(spec.label, metric, value, unit,
                                   collected_at))

    interface_rows = interface_fn()
    if interface_rows:
        statuses["collector-host"] = "up"
        devices_up += 1
        for row in interface_rows:
            samples.append(_sample(
                row["device_name"], row["metric"], row["value"],
                row["unit"], collected_at, row.get("interface_name")))

    devices_total = len(targets) + (1 if interface_rows else 0)
    if devices_up == 0:
        status = "error"
    elif errors:
        status = "partial"
    else:
        status = "ok"
    return CycleResult(
        collected_at=collected_at, targets=list(targets), samples=samples,
        device_statuses=statuses, devices_total=devices_total,
        devices_up=devices_up, status=status, errors=errors)


# ---------------------------------------------------------------------------
# Persistence (batched, idempotent, retried)
# ---------------------------------------------------------------------------
@dataclass
class PersistOutcome:
    ok: bool
    inserted: int = 0
    skipped: int = 0
    error: str | None = None


def _log_integrity_error(context: str, exc: IntegrityError) -> None:
    """Log DB uniqueness conflicts without exposing credentials or values."""
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    table_name = getattr(diag, "table_name", None) if diag is not None else None
    constraint_name = (
        getattr(diag, "constraint_name", None) if diag is not None else None
    )
    logger.error(
        "%s: IntegrityError class=%s table=%s constraint=%s",
        context,
        type(exc).__name__,
        table_name,
        constraint_name,
    )


def _topology_save(session, label: str, action):
    """Run one topology entity write in its own savepoint.

    Returns ``(ok, value)``. A failed entity rolls back only its savepoint, so
    the outer session stays usable and can never be left in
    ``PendingRollbackError``. If the session was already inactive (a prior
    failure outside a savepoint), it is rolled back explicitly.
    """
    try:
        with session.begin_nested():
            value = action()
        return True, value
    except IntegrityError as exc:
        _log_integrity_error(label, exc)
    except Exception as exc:  # noqa: BLE001 - one entity must not poison the cycle
        logger.warning("%s: topology write failed (%s)",
                       label, type(exc).__name__)
    if not session.is_active:
        try:
            session.rollback()
        except Exception:  # best effort recovery
            logger.debug("%s: rollback after topology failure failed",
                         label, exc_info=True)
    return False, None


def _sync_topology(session, result: CycleResult) -> None:
    """Persist the logical live topology derived from the probe targets.

    Real probe data gives the operator host, its default gateway and the
    measured targets. The edges are a logical model of the path actually
    exercised; they are never a routing table or a fabricated link inventory.
    """
    for spec in result.targets:
        _topology_save(
            session,
            f"device:{spec.label}",
            lambda spec=spec: repository.upsert_device(
                session,
                name=spec.label,
                type_="target",
                mgmt_ip=spec.address,
                status=result.device_statuses.get(spec.label, "unknown"),
            ),
        )

    any_up = any(status == "up" for status in result.device_statuses.values())
    _topology_save(
        session,
        "device:collector-host",
        lambda: repository.upsert_device(
            session,
            name="collector-host",
            type_="host",
            status="up" if any_up else "unknown",
        ),
    )
    host = repository.get_device_by_name(session, "collector-host")
    if host is None:  # pragma: no cover - upsert just created it
        return

    real_interfaces = sorted({
        row["interface_name"] for row in result.samples
        if row["device_name"] == "collector-host" and row["interface_name"]
    })
    for name in real_interfaces:
        _topology_save(
            session,
            f"interface:{name}",
            lambda name=name: repository.upsert_interface(
                session, host, name, status="up"
            ),
        )
    _topology_save(
        session,
        "interface:collector-host:uplink",
        lambda: repository.upsert_interface(
            session, host, "uplink", status="up" if any_up else "unknown"
        ),
    )

    gateway_spec = next(
        (spec for spec in result.targets if spec.label == "gateway"), None)
    for spec in result.targets:
        device = repository.get_device_by_name(session, spec.label)
        if device is None:  # pragma: no cover - upsert just created it
            continue
        _topology_save(
            session,
            f"interface:{spec.label}:wan",
            lambda device=device, spec=spec: repository.upsert_interface(
                session,
                device,
                "wan",
                status=result.device_statuses.get(spec.label, "unknown"),
            ),
        )

    def _link_status(a: str, b: str) -> str:
        sa = result.device_statuses.get(a, "unknown")
        sb = result.device_statuses.get(b, "unknown")
        if sa == "down" or sb == "down":
            return "down"
        if sa == "up" and sb == "up":
            return "up"
        return "unknown"

    if gateway_spec is not None:
        gateway = repository.get_device_by_name(session, gateway_spec.label)
        if gateway is not None:
            _topology_save(
                session,
                f"interface:{gateway_spec.label}:lan",
                lambda gateway=gateway: repository.upsert_interface(
                    session,
                    gateway,
                    "lan",
                    status=result.device_statuses.get(gateway_spec.label, "unknown"),
                ),
            )
        _topology_save(
            session,
            "link:collector-host->gateway",
            lambda: repository.upsert_link(
                session,
                "collector-host",
                "uplink",
                gateway_spec.label,
                "lan",
                status=_link_status("collector-host", gateway_spec.label),
            ),
        )
        for spec in result.targets:
            if spec.label == gateway_spec.label:
                continue
            _topology_save(
                session,
                f"link:{gateway_spec.label}->{spec.label}",
                lambda spec=spec: repository.upsert_link(
                    session,
                    gateway_spec.label,
                    "wan",
                    spec.label,
                    "wan",
                    status=_link_status(gateway_spec.label, spec.label),
                ),
            )
    else:
        for spec in result.targets:
            _topology_save(
                session,
                f"link:collector-host->{spec.label}",
                lambda spec=spec: repository.upsert_link(
                    session,
                    "collector-host",
                    "uplink",
                    spec.label,
                    "wan",
                    status=_link_status("collector-host", spec.label),
                ),
            )


def _trace_interface_name(peer: str) -> str:
    """Stable, bounded interface label for an observed traceroute adjacency.

    Derived only from the peer name, so the same logical link always resolves
    to the same ``(device_a, if_a, device_b, if_b)`` tuple and
    :func:`repository.upsert_link` dedupes it instead of inserting a new row
    on every discovery cycle.
    """
    raw = str(peer or "peer")
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    safe = re.sub(r"[^A-Za-z0-9_.:-]+", "_", raw).strip("_") or "peer"
    budget = 64 - len("trace:") - len("-") - len(digest)
    return f"trace:{safe[:budget]}-{digest}"[:64]


def _traceroute_metric_rows(
    discovered_path, collected_at: dt.datetime,
    name_map: dict[str, str] | None = None,
) -> list[dict]:
    """Convert discovered per-hop RTTs into ``metric_samples`` row dicts."""
    if discovered_path is None:
        return []
    mapping = name_map or {}
    rows: list[dict] = []
    for metric in discovered_path.metrics:
        raw_name = str(metric["device_name"])
        rows.append({
            "device_name": mapping.get(raw_name, raw_name),
            "interface_name": metric.get("interface_name"),
            "metric": metric["metric"],
            "value": metric["value"],
            "unit": metric.get("unit", "ms"),
            "data_source": "live",
            "collected_at": metric.get("collected_at", collected_at),
        })
    return rows


def _resolve_traceroute_metric_names(session, discovered_path) -> dict[str, str]:
    """Read-only map from discovered labels to the device rows they belong to.

    Existing friendly names win (``192.168.1.1`` -> ``gateway``), so traceroute
    metrics are stored under the same device label the topology page shows.
    """
    if discovered_path is None:
        return {}
    mapping: dict[str, str] = {}
    for device in discovered_path.devices:
        candidate_name = str(device.get("name") or "")
        if not candidate_name:
            continue
        existing = repository.find_device_for_hop(
            session,
            hop_ip=device.get("lab_ip"),
            preferred_name=candidate_name,
        )
        if existing is not None:
            mapping[candidate_name] = str(existing.name)
    return mapping


def _sync_traceroute_topology(
    session,
    discovered_path,
    collected_at: dt.datetime,
    samples_out: list[dict] | None,
    name_map: dict[str, str] | None = None,
) -> None:
    """Persist discovered topology from traceroute as devices and links.

    Every device is resolved to one existing row (never renamed, never
    duplicated) inside its own savepoint. Links are built from the resolved
    names with stable interface labels, so repeated cycles dedupe by
    ``(device_a, if_a, device_b, if_b)``. Metric rows are appended to
    ``samples_out`` when supplied (the collector writes them earlier, in their
    own transaction, so a topology failure can never drop a cycle's samples).
    """
    if discovered_path is None:
        return

    resolved_names: dict[str, str] = dict(name_map or {})
    devices_by_name: dict[str, object] = {}

    for device in discovered_path.devices:
        candidate_name = str(device.get("name") or "").strip()
        if not candidate_name:
            continue
        ok, resolved = _topology_save(
            session,
            f"traceroute-device:{candidate_name}",
            lambda candidate_name=candidate_name, device=device: (
                repository.resolve_device_for_hop(
                    session,
                    hop_ip=device.get("lab_ip"),
                    preferred_name=candidate_name,
                    type_=device.get("type", "router"),
                    status=device.get("status", "unknown"),
                )
            ),
        )
        if ok and resolved is not None:
            resolved_names[candidate_name] = str(resolved.name)
            devices_by_name[str(resolved.name)] = resolved
        else:
            logger.warning(
                "traceroute: device %s could not be resolved; its links are "
                "skipped this cycle", candidate_name)

    ok, collector_host = _topology_save(
        session,
        "traceroute-device:collector-host",
        lambda: repository.upsert_device(
            session, name="collector-host", type_="host", status="up"),
    )
    if not ok or collector_host is None:
        collector_host = repository.get_device_by_name(session, "collector-host")
    if collector_host is None:
        logger.warning("traceroute: collector-host unavailable; topology skipped")
        if samples_out is not None:
            samples_out.extend(
                _traceroute_metric_rows(discovered_path, collected_at))
        return
    devices_by_name.setdefault(str(collector_host.name), collector_host)

    if discovered_path.devices:
        first_candidate = str(discovered_path.devices[0].get("name") or "")
        first_device_name = resolved_names.get(first_candidate)
        first_device = (devices_by_name.get(first_device_name)
                        if first_device_name else None)
        if first_device is not None:
            _topology_save(
                session,
                "traceroute-interface:collector-host:uplink",
                lambda: repository.upsert_interface(
                    session, collector_host, "uplink", status="up"),
            )
            _topology_save(
                session,
                f"traceroute-interface:{first_device_name}:ingress",
                lambda first_device=first_device: repository.upsert_interface(
                    session, first_device, "ingress", status="up"),
            )
            _topology_save(
                session,
                "traceroute-link:collector-host->first",
                lambda first_device_name=first_device_name: repository.upsert_link(
                    session,
                    "collector-host",
                    "uplink",
                    first_device_name,
                    "ingress",
                    status="up",
                ),
            )
        else:
            logger.warning(
                "traceroute: first hop %s unresolved; host link skipped",
                first_candidate)

    for link in discovered_path.links:
        raw_a = str(link.get("device_a") or "")
        raw_b = str(link.get("device_b") or "")
        device_a_name = resolved_names.get(raw_a, raw_a)
        device_b_name = resolved_names.get(raw_b, raw_b)
        device_a = devices_by_name.get(device_a_name)
        device_b = devices_by_name.get(device_b_name)
        if device_a is None or device_b is None or device_a_name == device_b_name:
            logger.warning(
                "traceroute: link %s <-> %s references an unresolved device; "
                "skipped this cycle", raw_a, raw_b)
            continue
        status = link.get("status", "up")
        if_name_a = _trace_interface_name(device_b_name)
        if_name_b = _trace_interface_name(device_a_name)
        _topology_save(
            session,
            f"traceroute-interface:{device_a_name}:{if_name_a}",
            lambda device_a=device_a, if_name_a=if_name_a, status=status: (
                repository.upsert_interface(
                    session, device_a, if_name_a, status=status)),
        )
        _topology_save(
            session,
            f"traceroute-interface:{device_b_name}:{if_name_b}",
            lambda device_b=device_b, if_name_b=if_name_b, status=status: (
                repository.upsert_interface(
                    session, device_b, if_name_b, status=status)),
        )
        _topology_save(
            session,
            f"traceroute-link:{device_a_name}->{device_b_name}",
            lambda device_a_name=device_a_name, if_name_a=if_name_a, device_b_name=device_b_name, if_name_b=if_name_b, status=status: repository.upsert_link(
                session,
                device_a_name,
                if_name_a,
                device_b_name,
                if_name_b,
                status=status,
            ),
        )

    if samples_out is not None:
        samples_out.extend(
            _traceroute_metric_rows(discovered_path, collected_at))


#: A sustained loss/RTT breach opens one incident per (device, kind).  These
#: are derivation thresholds over real probe measurements, not detector output;
#: the values are documented here and can be overridden by the environment.
INCIDENT_LOSS_PCT = 20.0
INCIDENT_RTT_MS = 250.0


def _incident_thresholds() -> tuple[float, float]:
    try:
        loss = float(os.getenv("NETPILOT_INCIDENT_LOSS_PCT",
                               str(INCIDENT_LOSS_PCT)))
    except ValueError:
        loss = INCIDENT_LOSS_PCT
    try:
        rtt = float(os.getenv("NETPILOT_INCIDENT_RTT_MS",
                              str(INCIDENT_RTT_MS)))
    except ValueError:
        rtt = INCIDENT_RTT_MS
    return loss, rtt


def _sample_value(result: CycleResult, device: str, metric: str) -> float | None:
    for sample in result.samples:
        if sample["device_name"] == device and sample["metric"] == metric:
            return sample.get("value")
    return None


def _sync_incidents(session, result: CycleResult) -> None:
    """Derive open incidents from real loss/RTT threshold breaches.

    A breach keeps a single open incident for the device until a later cycle
    observes a normal value, at which point it is resolved.  Missing values
    (a probe that could not measure RTT) never create or resolve an RTT
    incident.
    """
    loss_threshold, rtt_threshold = _incident_thresholds()
    for spec in result.targets:
        loss = _sample_value(result, spec.label, "ping.loss_pct")
        if loss is not None and loss >= loss_threshold:
            repository.ensure_open_incident(
                session, spec.label, "packet_loss_breach",
                f"measured loss {loss:.1f}% >= threshold {loss_threshold:.1f}%")
        else:
            repository.resolve_open_incidents(
                session, spec.label, "packet_loss_breach")

        rtt = _sample_value(result, spec.label, "ping.avg_rtt_ms")
        if rtt is not None and rtt >= rtt_threshold:
            repository.ensure_open_incident(
                session, spec.label, "rtt_breach",
                f"measured avg RTT {rtt:.1f} ms >= threshold "
                f"{rtt_threshold:.1f} ms")
        elif rtt is not None:
            repository.resolve_open_incidents(
                session, spec.label, "rtt_breach")


def _sync_devices(session, result: CycleResult) -> None:
    """Backward-compatible alias: devices + topology in one step."""
    _sync_topology(session, result)


def _parse_traceroute_targets(raw: str | None) -> list[str]:
    """Parse traceroute targets from comma-separated string."""
    if raw is None:
        return DEFAULT_TRACEROUTE_DESTINATIONS.copy()
    targets = []
    for token in raw.split(","):
        token = token.strip()
        if token:
            try:
                # Validate it's a valid address
                validate_target(token)
                targets.append(token)
            except ValueError:
                logger.warning("Invalid traceroute target: %s", token)
    if not targets:
        return DEFAULT_TRACEROUTE_DESTINATIONS.copy()
    return targets


def persist_cycle(
    result: CycleResult,
    *,
    data_source: str = "live",
    scope_factory=None,
    sleep_fn: Callable[[float], None] = time.sleep,
    max_attempts: int = 3,
    traceroute_destinations: list[str] | None = None,
) -> PersistOutcome:
    """Write one cycle in a batched transaction with retry/backoff.

    A collection-run row is opened first so even a failed sample write leaves
    an audit trail; the sample batch is then inserted in one executemany
    round-trip. ``collected_at`` is fixed per cycle, and existing
    (device, interface, metric) keys for that timestamp are skipped, so a
    retry or a repeated ``--once`` invocation is idempotent.
    """
    if data_source != "live":
        raise ValueError("live_collector only writes data_source='live'")
    scope_factory = scope_factory or connection.session_scope
    attempts = max(1, int(max_attempts))

    run_id: int | None = None
    for attempt in range(attempts):
        try:
            with scope_factory() as session:
                run = repository.start_collection_run(session, data_source)
                run_id = int(run.id)
            break
        except OperationalError as exc:
            if attempt == attempts - 1:
                return PersistOutcome(ok=False, error=type(exc).__name__)
            sleep_fn(2 ** attempt)
        except Exception as exc:  # noqa: BLE001 - report class, never a DSN
            return PersistOutcome(ok=False, error=type(exc).__name__)

    inserted = 0
    skipped = 0
    persist_error: str | None = None
    # Run traceroute discovery if configured
    discovered_path = None
    if traceroute_destinations:
        try:
            discovered_path = discover_topology_from_traceroute(
                traceroute_destinations, timeout=TRACEROUTE_TIMEOUT)
            logger.info("Traceroute discovered %d devices and %d links",
                        len(discovered_path.devices), len(discovered_path.links))
        except OSError as exc:
            logger.warning("Traceroute discovery failed: %s", type(exc).__name__)
    
    trace_name_map: dict[str, str] = {}
    trace_rows: list[dict] = []
    for attempt in range(attempts):
        try:
            with scope_factory() as session:
                # Resolve existing friendly labels before the sample write so a
                # traceroute hop is stored under the same device the topology
                # page shows. This is read-only: the topology transaction owns
                # every device INSERT.
                try:
                    trace_name_map = _resolve_traceroute_metric_names(
                        session, discovered_path)
                except Exception:  # noqa: BLE001 - fall back to discovered names
                    trace_name_map = {}
                trace_rows = _traceroute_metric_rows(
                    discovered_path, result.collected_at, trace_name_map)
                existing = repository.existing_sample_keys(
                    session, result.collected_at, data_source)
                rows: list[dict] = []
                seen = set(existing)
                for sample in list(result.samples) + trace_rows:
                    key = (sample["device_name"], sample["interface_name"],
                           sample["metric"])
                    if key in seen:
                        skipped += 1
                        continue
                    seen.add(key)
                    rows.append(sample)
                inserted = repository.add_metric_samples_bulk(session, rows)
            persist_error = None
            break
        except OperationalError as exc:
            persist_error = type(exc).__name__
            if attempt == attempts - 1:
                break
            sleep_fn(2 ** attempt)
        except IntegrityError as exc:
            _log_integrity_error("metric_samples", exc)
            persist_error = type(exc).__name__
            break
        except Exception as exc:  # noqa: BLE001 - report class, never a DSN
            persist_error = type(exc).__name__
            break

    for attempt in range(attempts):
        try:
            with scope_factory() as session:
                try:
                    _sync_topology(session, result)
                    _topology_save(
                        session, "incidents",
                        lambda: _sync_incidents(session, result))
                    if discovered_path is not None:
                        _sync_traceroute_topology(
                            session, discovered_path, result.collected_at,
                            samples_out=None, name_map=trace_name_map)
                except Exception:
                    # Never leave the session in PendingRollbackError: the
                    # sample batch is already committed in its own transaction.
                    session.rollback()
                    raise
            persist_error = None
            break
        except OperationalError as exc:
            persist_error = type(exc).__name__
            if attempt == attempts - 1:
                break
            sleep_fn(2 ** attempt)
        except IntegrityError as exc:
            _log_integrity_error("topology", exc)
            persist_error = None
            break
        except Exception as exc:  # noqa: BLE001 - report class, never a DSN
            logger.warning(
                "topology sync failed (%s); the cycle's samples are already "
                "stored", type(exc).__name__)
            persist_error = None
            break

    status = result.status if persist_error is None else "error"
    error_text = "; ".join(result.errors)[:500] if result.errors else None
    if persist_error:
        prefix = f"database: {persist_error}"
        error_text = f"{error_text}; {prefix}" if error_text else prefix
        error_text = error_text[:500]

    if run_id is not None:
        try:
            with scope_factory() as session:
                run = session.get(CollectionRun, run_id)
                if run is not None:
                    repository.finish_collection_run(
                        session, run, status,
                        devices_total=result.devices_total,
                        devices_up=result.devices_up,
                        error_message=error_text)
        except Exception:  # noqa: BLE001,S110 - audit close is best-effort
            pass

    return PersistOutcome(ok=persist_error is None, inserted=inserted,
                          skipped=skipped, error=persist_error)


# ---------------------------------------------------------------------------
# Configuration / credential loading (never prints a DSN)
# ---------------------------------------------------------------------------
def _load_collector_dsn(wcm_target: str, use_wcm: bool) -> str | None:
    """Load the collector DSN into the process environment, silently.

    Returns ``"env"`` or ``"wcm"`` on success, ``None`` when no credential was
    found. The DSN itself is never returned to a caller that prints, logged, or
    included in an exception.
    """
    if (os.getenv("NETPILOT_DB_URL_COLLECTOR", "") or "").strip():
        return "env"
    if not use_wcm:
        return None
    try:
        from tools.diagnose_db_target import _wcm_read
    except Exception:  # noqa: BLE001 - optional platform-specific helper
        return None
    dsn = _wcm_read(wcm_target)
    if dsn and dsn.strip():
        os.environ["NETPILOT_DB_URL_COLLECTOR"] = dsn.strip()
        return "wcm"
    return None


def check_configuration(settings) -> list[str]:
    """Return credential-safe reasons the collector must refuse to run."""
    errors: list[str] = []
    if settings.demo_mode:
        errors.append(
            "live collector requires NETPILOT_DEMO_MODE=false; live and demo "
            "data must never be mixed")
    if settings.change_loop_enabled:
        errors.append(
            "refuses to run with NETPILOT_CHANGE_LOOP_ENABLED=true; this "
            "collector performs no device configuration")
    if settings.lab_execution_enabled:
        errors.append(
            "refuses to run with NETPILOT_LAB_EXECUTION_ENABLED=true; this "
            "collector performs no execution")
    url = None
    try:
        url = roles.resolve_role_url(roles.ROLE_COLLECTOR, settings=settings)
    except roles.RoleConfigError as exc:
        errors.append(str(exc))
    if url is not None and not url.startswith("postgresql"):
        scheme = url.split("://", 1)[0] or "unset"
        errors.append(
            "collector requires a PostgreSQL connection for the "
            f"netpilot_collector role; current scheme is {scheme!r}")
    return errors


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _positive_float(text: str) -> float:
    value = float(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Windows-native NetPilot live collector "
                    "(real ICMP probes, data_source='live').")
    parser.add_argument("--interval", type=_positive_float, default=30.0,
                        help="seconds between cycles (default: 30)")
    parser.add_argument("--once", action="store_true",
                        help="run exactly one cycle and exit")
    parser.add_argument("--duration", type=float, default=0.0,
                        help="stop after this many seconds (default: run "
                             "until Ctrl+C)")
    parser.add_argument("--targets", default=DEFAULT_TARGETS,
                        help="comma-separated ADDRESS or LABEL=ADDRESS "
                             "targets; 'gateway' means the auto-detected "
                             f"default gateway (default: {DEFAULT_TARGETS})")
    parser.add_argument("--count", type=int, default=4,
                        help="ping packets per target per cycle (default: 4)")
    parser.add_argument("--timeout", type=float, default=1.0,
                        help="per-reply timeout in seconds (default: 1.0)")
    parser.add_argument("--no-interfaces", action="store_true",
                        help="skip optional local NIC counter collection")
    parser.add_argument("--no-wcm", action="store_true",
                        help="do not read the collector DSN from Windows "
                             "Credential Manager (use the environment only)")
    parser.add_argument("--wcm-target", default=WCM_TARGET_COLLECTOR,
                        help="Windows Credential Manager target for the "
                             "collector DSN")
    parser.add_argument("--traceroute", action="store_true",
                        help="run OS traceroute/tracert for real topology "
                             "discovery (discover hops as devices/links)")
    parser.add_argument("--traceroute-targets", default=None,
                        help="comma-separated destinations for traceroute "
                             "(default: multiple public hosts)")
    parser.add_argument("--traceroute-every", type=int, default=10,
                        help="run traceroute discovery on the first cycle and "
                             "every N cycles thereafter (default: 10)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    settings = get_settings()
    credential_source = _load_collector_dsn(args.wcm_target, not args.no_wcm)
    if credential_source is None:
        print(
            "ERROR: no collector credential found. Store the "
            f"netpilot_collector DSN in Windows Credential Manager target "
            f"{args.wcm_target!r}, or set NETPILOT_DB_URL_COLLECTOR.",
            file=sys.stderr)
        return EXIT_CONFIG
    logger.info("collector credential loaded from %s (value not printed)",
                credential_source)

    errors = check_configuration(settings)
    if errors:
        for message in errors:
            print(f"ERROR: {message}", file=sys.stderr)
        return EXIT_CONFIG

    connection.set_database_role(roles.ROLE_COLLECTOR)
    try:
        connection.require_schema()
    except Exception as exc:  # noqa: BLE001 - message is credential-safe
        print(f"ERROR: collector database is not ready: "
              f"{type(exc).__name__}: {str(exc)[:300]}", file=sys.stderr)
        return EXIT_DB

    try:
        targets = build_target_specs(args.targets)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    interface_fn: Callable[[], list[dict]] = (
        (list) if args.no_interfaces else collect_interface_samples)

    # Parse traceroute targets if enabled
    traceroute_targets = None
    if args.traceroute:
        traceroute_targets = _parse_traceroute_targets(args.traceroute_targets)
        logger.info("traceroute enabled for destinations: %s", traceroute_targets)

    logger.info("targets: %s",
                ", ".join(f"{spec.label}={spec.address}" for spec in targets))
    logger.info("interval=%.1fs count=%d timeout=%.1fs interfaces=%s",
                args.interval, max(1, min(args.count, 20)),
                max(0.2, min(args.timeout, 10.0)),
                "off" if args.no_interfaces else "on")
    if args.traceroute and args.traceroute_every < 1:
        raise ValueError("--traceroute-every must be at least 1")

    started = time.monotonic()
    cycles = 0
    inserted = 0
    skipped = 0
    db_failures = 0
    last_status = "ok"
    interrupted = False
    try:
        while True:
            cycle_started = time.monotonic()
            result = run_cycle(
                targets, count=args.count, timeout_seconds=args.timeout,
                interface_fn=interface_fn)
            trace_destinations = None
            if traceroute_targets is not None:
                should_trace = cycles == 0 or (cycles + 1) % args.traceroute_every == 0
                if should_trace:
                    trace_destinations = traceroute_targets
            outcome = persist_cycle(result, traceroute_destinations=trace_destinations)
            cycles += 1
            inserted += outcome.inserted
            skipped += outcome.skipped
            last_status = result.status if outcome.ok else "error"
            if outcome.ok:
                logger.info(
                    "cycle %d: status=%s inserted=%d skipped=%d up=%d/%d",
                    cycles, result.status, outcome.inserted, outcome.skipped,
                    result.devices_up, result.devices_total)
                for message in result.errors[:3]:
                    logger.warning("  %s", message)
            else:
                db_failures += 1
                logger.error(
                    "cycle %d: database write failed (%s); the next cycle "
                    "will retry", cycles, outcome.error)

            if args.once:
                break
            if args.duration and (time.monotonic() - started) >= args.duration:
                break
            sleep_for = args.interval - (time.monotonic() - cycle_started)
            if sleep_for > 0:
                time.sleep(sleep_for)
    except KeyboardInterrupt:
        interrupted = True
        print("\nInterrupted; shutting down cleanly.", file=sys.stderr)

    print("Live collector summary")
    print(f"  cycles: {cycles}")
    print(f"  samples inserted: {inserted} (duplicates skipped: {skipped})")
    print("  targets: " + ", ".join(
        f"{spec.label}={spec.address}" for spec in targets))
    print(f"  last cycle status: {last_status}")
    if db_failures:
        print(f"  database write failures: {db_failures}")

    if interrupted:
        return EXIT_OK
    if cycles == 0 or db_failures == cycles:
        return EXIT_DB
    if last_status == "error":
        return EXIT_COLLECTION_ERROR
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
