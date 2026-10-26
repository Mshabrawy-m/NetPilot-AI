"""Deterministic, isolated Digital Twin simulation engine (Phase D1).

This module implements the **real** Digital Twin capability required by the
roadmap's non-negotiable safety chain and by the Phase 4 definition. It is a
*simulation service only*: it never connects to a device, never sends a packet,
never runs a shell command, never calls a provider and never applies, approves,
verifies or rolls back a change.

What is modeled
---------------
The engine operates on an **explicit topology snapshot** (the stored
``devices`` / ``links`` rows, or any in-memory rows with the same shape) plus a
proposed configuration change. It models exactly one topology-affecting
operation family:

* administrative ``shutdown`` of a documented interface -> every documented
  link whose endpoint is that interface is removed from the simulated graph;
* ``no shutdown`` -> only removes a *simulated* shutdown. It never invents
  availability: a link stored as ``down`` stays down (reported as unknown
  recovery), because the engine cannot prove why it was down.

Everything else (``ip address``, ACLs, OSPF, route maps, NAT, ...) is reported
as **unsupported** for reachability modeling, never silently approximated.
Reachability is structural: documented links with status ``up`` whose endpoint
devices are not ``down``. ``unknown`` status is uncertainty, not availability.

Honesty rules
-------------
* A simulated result is **not** proof that a real change is safe, and it is
  **not** approval, execution, verification or a live-network observation.
* Missing/unknown evidence produces ``partial`` or ``insufficient_evidence``,
  never a fabricated success.
* Every result identifies the topology snapshot and proposed change it used
  (stable fingerprints), plus assumptions, conflicts and limitations.
* Identical inputs produce byte-identical JSON output (deterministic ordering,
  no clocks, no randomness).

The pure engine is ``simulate_change``. ``simulate_request`` is the read-only
session wrapper used by the UI. ``TopologyDigitalTwin`` adapts the engine to
the existing ``ai.simulation.DigitalTwin`` ABC without wiring it into the AI
workflow (that integration is deliberately deferred; see the module boundary
note at the bottom).
"""
from __future__ import annotations

import datetime as dt
import difflib
import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import networkx as nx

from ai.simulation import DigitalTwin, SimulationResult
from network.lab_config_ops import is_noise, normalize_line

# ---------------------------------------------------------------------------
# Stable identifiers and vocabulary
# ---------------------------------------------------------------------------
SIMULATOR = "netpilot-digital-twin"
SIMULATOR_VERSION = "d3-1.0"

STATUS_SIMULATED = "simulated"
STATUS_PARTIAL = "partial"
STATUS_INSUFFICIENT = "insufficient_evidence"
STATUS_INVALID = "invalid"
STATUS_NOT_SIMULATED = "not_simulated"
STATUS_ERROR = "error"

TOPOLOGY_STORED = "stored"
TOPOLOGY_DEMO = "demo"
TOPOLOGY_SNAPSHOT = "snapshot"

#: Version of the in-memory snapshot/change binding contract. D3 does not
#: persist bindings; this version lets a future persistence layer reject an
#: incompatible stored binding instead of guessing.
SNAPSHOT_BINDING_VERSION = "d3-1"

#: Integrity statuses. ``verified`` is the only status that permits a normal
#: simulation status; ``unknown_freshness`` is explicitly *not* fresh.
INTEGRITY_VERIFIED = "verified"
INTEGRITY_UNKNOWN_FRESHNESS = "unknown_freshness"
INTEGRITY_STALE = "stale"
INTEGRITY_MISMATCH = "mismatch"
INTEGRITY_INSUFFICIENT = "insufficient_evidence"
INTEGRITY_INVALID = "invalid"
INTEGRITY_ERROR = "error"

#: Freshness sub-status. ``not_configured`` and ``unknown`` are deliberately
#: distinct: neither one is treated as fresh.
FRESHNESS_VERIFIED = "verified"
FRESHNESS_UNKNOWN = "unknown"
FRESHNESS_STALE = "stale"
FRESHNESS_NOT_CONFIGURED = "not_configured"
FRESHNESS_INVALID = "invalid"

_FINGERPRINT_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_REF_LENGTH = 256
_FUTURE_TOLERANCE_SECONDS = 60

_DEVICE_STATUSES = ("unknown", "up", "down")
_LINK_STATUSES = ("unknown", "up", "down")

#: Invariants the engine can actually evaluate. Anything else is reported as
#: unsupported rather than guessed. ``preserve_lab_routing`` is deliberately
#: *not* listed: the engine models structural reachability only, not routing
#: protocol semantics, so claiming to evaluate routing would overstate evidence.
_SUPPORTED_INVARIANTS = frozenset({
    "preserve_reachability",
})

#: Heads that genuinely open a new configuration context. Unsupported lines
#: that are *not* context heads must not silently close an interface block.
_CONTEXT_RESET_HEADS = frozenset({
    "router", "vrf", "line", "access-list", "route-map",
})

#: Config heads that only change identity/metadata and therefore have no
#: modeled topology effect. They are explicitly modeled as "no effect".
_NO_TOPOLOGY_EFFECT_HEADS = frozenset({"hostname", "description"})

#: Context heads whose contents the engine does not model. They are reported
#: as unsupported (never approximated).
_UNMODELED_CONTEXT_HEADS = frozenset({
    "router", "vrf", "line", "access-list", "route-map", "ip", "ipv6",
    "neighbor", "network", "log", "banner", "service", "ntp", "snmp",
    "username", "key", "crypto",
})


# ---------------------------------------------------------------------------
# Small deterministic helpers
# ---------------------------------------------------------------------------
def _get(obj, key, default=None):
    """Read an attribute or mapping key (tests may pass dicts/ORM rows)."""
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _status(value, allowed) -> tuple[str, bool]:
    """Return (normalised_status, was_invalid)."""
    raw = _text(value).lower() or "unknown"
    if raw in allowed:
        return raw, False
    return "unknown", True


def _fingerprint(payload) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      default=str, ensure_ascii=True)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _canonical_link_key(link: dict) -> tuple:
    a, b = link["device_a"], link["device_b"]
    if a <= b:
        return (a, b, link["if_a"], link["if_b"])
    return (b, a, link["if_b"], link["if_a"])


def _pair_key(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


def _conflict(kind: str, element: str, detail: str) -> dict:
    return {"kind": kind, "element": element, "detail": detail}


def _check(name: str, passed, detail: str) -> dict:
    return {"name": name, "passed": passed, "detail": detail}


def _sorted_conflicts(conflicts: list[dict]) -> list[dict]:
    return sorted(conflicts, key=lambda c: (c["kind"], c["element"],
                                            c["detail"]))


def _components(g: nx.Graph) -> list[list[str]]:
    parts = [sorted(comp) for comp in nx.connected_components(g)]
    parts.sort(key=lambda part: (len(part), part))
    return parts


def _reachable(g: nx.Graph, source: str) -> set[str]:
    if source not in g:
        return set()
    return set(nx.node_connected_component(g, source)) - {source}


def _summary(g: nx.Graph, source: str) -> dict:
    return {
        "node_count": g.number_of_nodes(),
        "available_edge_count": g.number_of_edges(),
        "components": _components(g),
        "reachable_from_target": sorted(_reachable(g, source)),
        "isolated_devices": sorted(n for n in g.nodes if g.degree(n) == 0),
    }


# ---------------------------------------------------------------------------
# Topology normalisation
# ---------------------------------------------------------------------------
def _normalise_devices(devices) -> tuple[dict[str, dict], list[dict], bool]:
    """Return (rows_by_name, conflicts, structurally_invalid)."""
    conflicts: list[dict] = []
    if devices is None:
        return {}, conflicts, False
    if isinstance(devices, (str, bytes)) or not isinstance(devices, Sequence):
        return {}, [_conflict(
            "topology_not_sequence", "devices",
            "the devices snapshot is not a sequence; no topology was read")], True

    rows: dict[str, dict] = {}
    for idx, raw in enumerate(devices):
        if isinstance(raw, Mapping):
            name = _text(raw.get("name", ""))
            status_raw = raw.get("status", "unknown")
            type_raw = raw.get("type", "")
        elif hasattr(raw, "name"):
            name = _text(getattr(raw, "name", ""))
            status_raw = getattr(raw, "status", "unknown")
            type_raw = getattr(raw, "type", "")
        else:
            conflicts.append(_conflict(
                "malformed_device_row", f"device row {idx}",
                "row is neither a mapping nor an object with a name; skipped"))
            continue
        if not name:
            conflicts.append(_conflict(
                "blank_device_name", f"device row {idx}",
                "device row has a blank name; skipped"))
            continue
        status, invalid = _status(status_raw, _DEVICE_STATUSES)
        if invalid:
            conflicts.append(_conflict(
                "invalid_device_status", name,
                f"status {status_raw!r} is not one of {list(_DEVICE_STATUSES)}; "
                "treated as unknown"))
        row = {
            "name": name,
            "status": status,
            "type": _text(type_raw) or None,
        }
        if name in rows:
            existing = rows[name]
            differences: list[str] = []
            if existing["status"] != row["status"]:
                differences.append(
                    f"status {existing['status']!r} vs {row['status']!r}")
                # Neither conflicting status can be trusted.
                existing["status"] = "unknown"
            if existing["type"] != row["type"]:
                differences.append(
                    f"type {existing['type']!r} vs {row['type']!r}")
                existing["type"] = None
            detail = "duplicate device row; it cannot be counted twice"
            if differences:
                detail += ("; conflicting metadata: " + ", ".join(differences)
                           + "; the conflicting field(s) were collapsed to "
                           "unknown so row order cannot change the result")
            else:
                detail += "; identical duplicate was collapsed"
            conflicts.append(_conflict("duplicate_device", name, detail))
            continue
        rows[name] = row
    return rows, conflicts, bool(conflicts) and not rows


def _normalise_links(links, device_rows: dict[str, dict]) -> tuple[
        list[dict], list[dict], int, bool]:
    """Return (clean_links, conflicts, duplicates, structurally_invalid)."""
    conflicts: list[dict] = []
    if links is None:
        return [], conflicts, 0, False
    if isinstance(links, (str, bytes)) or not isinstance(links, Sequence):
        return [], [_conflict(
            "topology_not_sequence", "links",
            "the links snapshot is not a sequence; no topology was read")], 0, True

    clean: list[dict] = []
    seen: dict[tuple, dict] = {}
    duplicates = 0
    for idx, raw in enumerate(links):
        if isinstance(raw, Mapping):
            a = _text(raw.get("device_a", ""))
            if_a = _text(raw.get("if_a", ""))
            b = _text(raw.get("device_b", ""))
            if_b = _text(raw.get("if_b", ""))
            subnet = _text(raw.get("subnet", "")) or None
            status_raw = raw.get("status", "unknown")
        elif hasattr(raw, "device_a"):
            a = _text(getattr(raw, "device_a", ""))
            if_a = _text(getattr(raw, "if_a", ""))
            b = _text(getattr(raw, "device_b", ""))
            if_b = _text(getattr(raw, "if_b", ""))
            subnet = _text(getattr(raw, "subnet", "")) or None
            status_raw = getattr(raw, "status", "unknown")
        else:
            conflicts.append(_conflict(
                "malformed_link_row", f"link row {idx}",
                "row is neither a mapping nor an object with device endpoints; "
                "skipped"))
            continue
        if not a or not b:
            conflicts.append(_conflict(
                "blank_link_endpoint", f"link row {idx}",
                "link row has a blank endpoint; skipped"))
            continue
        if a == b:
            conflicts.append(_conflict(
                "self_loop_link", a,
                f"link row {idx} connects {a} to itself; skipped"))
            continue
        if not if_a or not if_b:
            conflicts.append(_conflict(
                "blank_interface", f"{a}-{b}",
                "link row has a blank interface name; it cannot be mapped to "
                "an interface state change and was skipped"))
            continue
        status, invalid = _status(status_raw, _LINK_STATUSES)
        if invalid:
            conflicts.append(_conflict(
                "invalid_link_status", f"{a}-{b}",
                f"status {status_raw!r} is not one of {list(_LINK_STATUSES)}; "
                "treated as unknown"))
        link = {"device_a": a, "if_a": if_a, "device_b": b, "if_b": if_b,
                "subnet": subnet, "status": status}
        key = _canonical_link_key(link)
        if key in seen:
            duplicates += 1
            existing = seen[key]
            differences: list[str] = []
            status_conflict = existing["status"] != link["status"]
            subnet_conflict = existing["subnet"] != link["subnet"]
            if status_conflict:
                differences.append(
                    f"status {existing['status']!r} vs {link['status']!r}")
                # Neither conflicting status can be trusted for availability.
                existing["status"] = "unknown"
            if subnet_conflict:
                differences.append(
                    f"subnet {existing['subnet']!r} vs {link['subnet']!r}")
                existing["subnet"] = None
            detail = ("link row repeats the same endpoints/interfaces; it "
                      "cannot be counted twice")
            if differences:
                detail += ("; conflicting metadata: " + ", ".join(differences)
                           + "; the conflicting field(s) were collapsed to "
                           "unknown so row order cannot change the result")
                conflicts.append(_conflict(
                    "duplicate_link_metadata_conflict",
                    f"{a}:{if_a}-{b}:{if_b}", detail))
            else:
                detail += "; identical duplicate was collapsed"
            conflicts.append(_conflict(
                "duplicate_link", f"{a}:{if_a}-{b}:{if_b}", detail))
            continue
        seen[key] = link
        for name in (a, b):
            if name not in device_rows:
                device_rows[name] = {"name": name, "status": "unknown",
                                     "type": None}
                conflicts.append(_conflict(
                    "link_endpoint_without_device", name,
                    "link endpoint has no device row; added as an unknown "
                    "device so reachability is not silently dropped"))
        clean.append(link)

    clean.sort(key=lambda link: (
        min(link["device_a"], link["device_b"]),
        max(link["device_a"], link["device_b"]),
        link["if_a"], link["if_b"], link["device_a"], link["device_b"]))
    return clean, conflicts, duplicates, bool(conflicts) and not clean


def _topology_fingerprint(device_rows: dict[str, dict],
                          links: list[dict]) -> str:
    return _fingerprint({
        "devices": [[row["name"], row["status"], row["type"]]
                    for _name, row in sorted(device_rows.items())],
        "links": [[link["device_a"], link["if_a"], link["device_b"],
                   link["if_b"], link["subnet"], link["status"]]
                  for link in links],
    })


def _build_multigraph(device_rows: dict[str, dict],
                      links: list[dict]) -> nx.MultiGraph:
    g = nx.MultiGraph()
    for name, row in sorted(device_rows.items()):
        g.add_node(name, status=row["status"], type=row["type"])
    for link in links:
        g.add_edge(link["device_a"], link["device_b"],
                   key=_canonical_link_key(link),
                   device_a=link["device_a"], if_a=link["if_a"],
                   device_b=link["device_b"], if_b=link["if_b"],
                   subnet=link["subnet"], status=link["status"])
    return g


def _simple_graph(mg: nx.MultiGraph, device_rows: dict[str, dict], *,
                  include_unknown: bool, excluded: set[tuple]) -> nx.Graph:
    """Collapse the multigraph to available edges for reachability.

    ``include_unknown`` also admits links whose stored status is unknown; that
    graph is reported as *possible* reachability, never as confirmed.
    """
    g = nx.Graph()
    for name in sorted(device_rows):
        g.add_node(name, status=device_rows[name]["status"])
    allowed = {"up", "unknown"} if include_unknown else {"up"}
    for u, v, key, data in mg.edges(keys=True, data=True):
        if key in excluded:
            continue
        if data["status"] not in allowed:
            continue
        u_status = device_rows.get(u, {}).get("status")
        v_status = device_rows.get(v, {}).get("status")
        if include_unknown:
            # The *possible* graph admits unknown devices but never a device
            # explicitly stored as down.
            if u_status == "down" or v_status == "down":
                continue
        else:
            # The *available* graph requires known-up endpoints: an unknown
            # device is uncertainty, not availability.
            if u_status != "up" or v_status != "up":
                continue
        g.add_edge(u, v)
    return g


# ---------------------------------------------------------------------------
# Proposed-change parsing
# ---------------------------------------------------------------------------
@dataclass
class _Operation:
    line: str
    operation: str
    interface: str | None
    supported: bool
    effect: str
    reason: str = ""

    def as_dict(self) -> dict:
        return {"line": self.line, "operation": self.operation,
                "interface": self.interface, "supported": self.supported,
                "effect": self.effect, "reason": self.reason}


def _parse_operations(proposed_lines: Sequence[str]) -> list[_Operation]:
    """Parse the supported subset; every other line is explicitly unsupported.

    The parser is intentionally strict about the shape of the commands it
    claims to model. A malformed ``shutdown now`` must not be silently treated
    as a valid ``shutdown``. Context tracking is positional: only real context
    heads (``interface``, ``router``, ``vrf``, ``line``, ``access-list``,
    ``route-map``) and explicit ``exit``/``end`` close an interface block.
    """
    ops: list[_Operation] = []
    current_interface: str | None = None
    for raw in proposed_lines:
        norm = normalize_line(raw)
        if not norm:
            continue
        parts = norm.split()
        head = parts[0].lower()

        # ``is_noise`` treats ``end`` as filler, but for simulation it is a
        # real context boundary. Handle it before the noise check so a
        # subsequent command is not misread as being inside the interface.
        if norm.lower() == "end":
            current_interface = None
            ops.append(_Operation(
                norm, "context_end", None, True,
                "closes the interface block; no topology effect"))
            continue

        if is_noise(norm):
            continue

        if head == "interface":
            if len(parts) != 2:
                current_interface = None
                ops.append(_Operation(
                    norm, "interface_context", None, False,
                    "no topology effect",
                    "interface line must be exactly 'interface <name>'"))
            else:
                current_interface = parts[1]
                ops.append(_Operation(
                    norm, "interface_context", current_interface, True,
                    "opens an interface block; no effect by itself"))
            continue

        if head == "no" and len(parts) >= 2 and parts[1].lower() == "interface":
            if len(parts) != 3:
                ops.append(_Operation(
                    norm, "interface_remove", None, False,
                    "no topology effect",
                    "no interface line must be exactly 'no interface <name>'"))
            else:
                ops.append(_Operation(
                    norm, "interface_remove", parts[2], False,
                    "no topology effect",
                    "removing an interface configuration is not modeled as a "
                    "link-state change; its topology effect is unknown"))
            current_interface = None
            continue

        if head == "shutdown":
            if len(parts) != 1:
                ops.append(_Operation(
                    norm, "shutdown", None, False, "no topology effect",
                    "shutdown takes no arguments"))
            elif current_interface is None:
                ops.append(_Operation(
                    norm, "shutdown", None, False, "no topology effect",
                    "shutdown outside an interface block is not modeled"))
            else:
                ops.append(_Operation(
                    norm, "interface_shutdown", current_interface, True,
                    "administratively disables the interface; documented links "
                    "on it become unavailable in the simulation"))
            continue

        if head == "no" and len(parts) >= 2 and parts[1].lower() == "shutdown":
            if len(parts) != 2:
                ops.append(_Operation(
                    norm, "no_shutdown", None, False, "no topology effect",
                    "no shutdown takes no arguments"))
            elif current_interface is None:
                ops.append(_Operation(
                    norm, "no_shutdown", None, False, "no topology effect",
                    "no shutdown outside an interface block is not modeled"))
            else:
                ops.append(_Operation(
                    norm, "interface_no_shutdown", current_interface, True,
                    "cancels a simulated shutdown; availability is never "
                    "invented for a link stored as down"))
            continue

        if head in ("exit", "end"):
            # A malformed context boundary is still a boundary: reset the
            # interface context so a later command cannot be misattributed.
            current_interface = None
            if len(parts) != 1:
                ops.append(_Operation(
                    norm, "context_end", None, False, "no topology effect",
                    f"'{head}' takes no arguments"))
            else:
                ops.append(_Operation(
                    norm, "context_end", None, True,
                    "closes the interface block; no topology effect"))
            continue

        if head in _NO_TOPOLOGY_EFFECT_HEADS:
            if head == "hostname" and len(parts) != 2:
                ops.append(_Operation(
                    norm, "metadata", current_interface, False,
                    "no topology effect",
                    "hostname line must be exactly 'hostname <name>'"))
            elif head == "description" and len(parts) < 2:
                ops.append(_Operation(
                    norm, "metadata", current_interface, False,
                    "no topology effect",
                    "description line must include text"))
            else:
                ops.append(_Operation(
                    norm, "metadata", current_interface, True,
                    "identity/metadata only; no modeled topology effect"))
            continue

        if head in _UNMODELED_CONTEXT_HEADS:
            if head in _CONTEXT_RESET_HEADS:
                current_interface = None
            ops.append(_Operation(
                norm, "unsupported", current_interface, False,
                "no topology effect",
                f"'{head}' configuration semantics are not modeled for "
                "reachability; the effect is unknown"))
            continue

        ops.append(_Operation(
            norm, "unsupported", current_interface, False, "no topology effect",
            f"'{head}' configuration semantics are not modeled for "
            "reachability; the effect is unknown"))
    return ops


def _interface_links(links: list[dict], device: str,
                     interface: str) -> list[dict]:
    return [link for link in links
            if (link["device_a"] == device and link["if_a"] == interface)
            or (link["device_b"] == device and link["if_b"] == interface)]


def _config_diff(base_lines: Sequence[str] | None,
                 proposed_lines: Sequence[str]) -> str | None:
    if base_lines is None:
        return None
    base = [normalize_line(line) for line in base_lines
            if normalize_line(line)]
    proposed = [normalize_line(line) for line in proposed_lines
                if normalize_line(line)]
    return "\n".join(difflib.unified_diff(
        base, proposed, fromfile="base", tofile="proposed", lineterm=""))


# ---------------------------------------------------------------------------
# Snapshot identity and integrity (Phase D3)
# ---------------------------------------------------------------------------
def _normalise_config_lines(lines) -> list[str]:
    return [normalize_line(line) for line in (lines or [])
            if normalize_line(line)]


def _identity_text(value) -> str | None:
    text = _text(value)
    return text or None


def _change_fingerprint(*, device_name, proposed_lines, base_lines,
                        request_uuid, scenario) -> str:
    """Fingerprint the exact normalised proposal used by the engine."""
    base_payload = (None if base_lines is None
                    else _normalise_config_lines(base_lines))
    return _fingerprint({
        "device": _text(device_name),
        "lines": list(proposed_lines or []),
        "base_lines": base_payload,
        "request_uuid": _identity_text(request_uuid),
        "scenario": _identity_text(scenario),
    })


def _topology_source_value(value) -> str:
    label = _text(value).lower()
    if label == TOPOLOGY_DEMO:
        return TOPOLOGY_DEMO
    if label == TOPOLOGY_SNAPSHOT:
        return TOPOLOGY_SNAPSHOT
    return TOPOLOGY_STORED


def _parse_utc(value):
    """Return ``(aware_utc_datetime_or_None, error_or_None)``.

    Naive timestamps are interpreted as UTC, matching the project's stored
    timestamp convention. A value is never invented: ``None`` stays ``None``.
    """
    if value is None:
        return None, None
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None, "timestamp is blank"
        try:
            parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None, f"timestamp {value!r} is not ISO-8601"
    else:
        return None, f"timestamp {value!r} is not a string or datetime"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc), None


def parse_utc(value):
    """Public wrapper for the project's UTC timestamp parser (Phase D4).

    Returns ``(aware_utc_datetime_or_None, error_or_None)``. Naive values are
    interpreted as UTC; no value is invented.
    """
    return _parse_utc(value)


def _positive_int(value):
    """Return ``(positive_int_or_None, error_or_None)`` for a TTL policy."""
    if value is None:
        return None, None
    if isinstance(value, bool):
        return None, "max_age_seconds must be a positive integer"
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
    else:
        return None, "max_age_seconds must be a positive integer"
    if number <= 0:
        return None, "max_age_seconds must be a positive integer"
    return number, None


def _fingerprint_problem(value) -> str | None:
    if value is None:
        return "missing"
    if not isinstance(value, str):
        return "not a string"
    if not value.strip():
        return "blank"
    if not _FINGERPRINT_PATTERN.fullmatch(value.strip()):
        return "not a sha256:<64 hex> fingerprint"
    return None


def _freshness(status: str = FRESHNESS_NOT_CONFIGURED,
               detail: str = "", **overrides) -> dict:
    result = {
        "status": status,
        "captured_at": None,
        "max_age_seconds": None,
        "age_seconds": None,
        "evaluated_at": None,
        "detail": detail,
    }
    result.update(overrides)
    return result


def _integrity_failure_report(binding, *, status: str, reason: str,
                              conflict_kind: str) -> dict:
    """Build a consistent fail-closed report for unusable bindings/errors."""
    if isinstance(binding, SnapshotBinding):
        data = binding.as_dict()
    elif isinstance(binding, Mapping):
        data = dict(binding)
    else:
        data = {}
    return {
        "status": status,
        "binding_version": (data.get("binding_version")
                            or SNAPSHOT_BINDING_VERSION),
        "topology_ref": data.get("topology_ref"),
        "requested_topology_ref": None,
        "topology_source": data.get("topology_source"),
        "expected_topology_fingerprint": data.get("topology_fingerprint"),
        "actual_topology_fingerprint": None,
        "expected_change_fingerprint": data.get("change_fingerprint"),
        "actual_change_fingerprint": None,
        "device_count": None,
        "link_count": None,
        "freshness": _freshness(FRESHNESS_INVALID, reason),
        "checks": [_check("snapshot_binding_well_formed", False, reason)],
        "conflicts": [_conflict(conflict_kind, "snapshot", reason)],
        "reasons": [reason],
        "recommended_simulation_status": {
            INTEGRITY_VERIFIED: None,
            INTEGRITY_UNKNOWN_FRESHNESS: STATUS_PARTIAL,
            INTEGRITY_STALE: STATUS_INSUFFICIENT,
            INTEGRITY_MISMATCH: STATUS_INSUFFICIENT,
            INTEGRITY_INSUFFICIENT: STATUS_INSUFFICIENT,
            INTEGRITY_INVALID: STATUS_INVALID,
            INTEGRITY_ERROR: STATUS_ERROR,
        }.get(status, STATUS_INVALID),
        "not_proof_of_safety": True,
        "read_only": True,
        "database_written": False,
        "device_contacted": False,
        "configuration_applied": False,
    }


def integrity_failure_report(binding=None, *, status: str, reason: str,
                             conflict_kind: str) -> dict:
    """Public fail-closed integrity report (Phase D4 registry reuse).

    Additive wrapper: it does not change D3 validation behaviour. It lets the
    persistent registry report missing/malformed/unsupported records with the
    exact same shape and status vocabulary as an in-memory binding.
    """
    return _integrity_failure_report(
        binding, status=status, reason=reason, conflict_kind=conflict_kind)


@dataclass(frozen=True)
class SnapshotBinding:
    """Identity of one topology snapshot and (optionally) one proposal.

    This is an *in-memory evidence object*, not a persisted database record.
    D3 deliberately does not add a table or service just to make freshness
    look complete. A future integration must store this binding with the
    change request and the human approval, then validate it again before
    simulation (see the D3 report's integration contract).
    """

    topology_ref: str
    topology_fingerprint: str
    topology_source: str = TOPOLOGY_STORED
    change_fingerprint: str | None = None
    device_name: str | None = None
    request_uuid: str | None = None
    scenario: str | None = None
    captured_at: str | None = None
    max_age_seconds: int | None = None
    device_count: int = 0
    link_count: int = 0
    binding_version: str = SNAPSHOT_BINDING_VERSION

    def as_dict(self) -> dict:
        return {
            "binding_version": self.binding_version,
            "topology_ref": self.topology_ref,
            "topology_source": self.topology_source,
            "topology_fingerprint": self.topology_fingerprint,
            "change_fingerprint": self.change_fingerprint,
            "device_name": self.device_name,
            "request_uuid": self.request_uuid,
            "scenario": self.scenario,
            "captured_at": self.captured_at,
            "max_age_seconds": self.max_age_seconds,
            "device_count": self.device_count,
            "link_count": self.link_count,
        }

    @classmethod
    def from_mapping(cls, data: Mapping) -> "SnapshotBinding":
        if not isinstance(data, Mapping):
            raise TypeError("snapshot binding must be a mapping")
        return cls(
            topology_ref=data.get("topology_ref"),
            topology_fingerprint=data.get("topology_fingerprint"),
            topology_source=data.get("topology_source", TOPOLOGY_STORED),
            change_fingerprint=data.get("change_fingerprint"),
            device_name=data.get("device_name"),
            request_uuid=data.get("request_uuid"),
            scenario=data.get("scenario"),
            captured_at=data.get("captured_at"),
            max_age_seconds=data.get("max_age_seconds"),
            device_count=data.get("device_count", 0),
            link_count=data.get("link_count", 0),
            binding_version=data.get(
                "binding_version", SNAPSHOT_BINDING_VERSION),
        )


def capture_snapshot(
    *,
    devices: Sequence = (),
    links: Sequence = (),
    topology_ref: str,
    topology_source: str = TOPOLOGY_STORED,
    device_name: str | None = None,
    proposed_lines: Sequence[str] | None = None,
    base_lines: Sequence[str] | None = None,
    request_uuid: str | None = None,
    scenario: str | None = None,
    captured_at=None,
    max_age_seconds: int | None = None,
) -> SnapshotBinding:
    """Capture a deterministic identity for an explicit snapshot/proposal.

    Pure and read-only. ``captured_at`` is never defaulted to a clock value:
    if the caller cannot supply a genuine capture time, freshness remains
    explicitly unknown. Raises ``ValueError`` for unusable identity metadata.
    """
    if not isinstance(topology_ref, str):
        raise ValueError("topology_ref must be a string")
    ref = _text(topology_ref)
    if not ref:
        raise ValueError("topology_ref is required to capture a snapshot")
    if len(ref) > _MAX_REF_LENGTH:
        raise ValueError(
            f"topology_ref is longer than {_MAX_REF_LENGTH} characters")

    captured, captured_error = _parse_utc(captured_at)
    if captured_error:
        raise ValueError(f"captured_at: {captured_error}")
    policy, policy_error = _positive_int(max_age_seconds)
    if policy_error:
        raise ValueError(policy_error)

    device_rows, _device_conflicts, devices_invalid = _normalise_devices(
        devices)
    link_rows, _link_conflicts, _duplicates, links_invalid = _normalise_links(
        links, device_rows)
    if devices_invalid or links_invalid:
        raise ValueError("topology snapshot is not a usable sequence")

    change_fp = None
    if device_name is not None or proposed_lines is not None:
        change_fp = _change_fingerprint(
            device_name=device_name,
            proposed_lines=_normalise_config_lines(proposed_lines),
            base_lines=base_lines,
            request_uuid=request_uuid,
            scenario=scenario,
        )

    return SnapshotBinding(
        topology_ref=ref,
        topology_fingerprint=_topology_fingerprint(device_rows, link_rows),
        topology_source=_topology_source_value(topology_source),
        change_fingerprint=change_fp,
        device_name=(_text(device_name) if device_name is not None else None),
        request_uuid=_identity_text(request_uuid),
        scenario=_identity_text(scenario),
        captured_at=captured.isoformat() if captured is not None else None,
        max_age_seconds=policy,
        device_count=len(device_rows),
        link_count=len(link_rows),
    )


def _validate_binding(
    binding,
    *,
    device_rows: dict[str, dict],
    link_rows: list[dict],
    actual_topology_fp: str | None,
    actual_change_fp: str | None,
    topology_ref: str | None,
    device_name: str | None,
    request_uuid: str | None,
    scenario: str | None,
    now=None,
    max_age_seconds: int | None = None,
    require_change_fingerprint: bool = True,
) -> dict:
    """Validate a binding against already-normalised current inputs."""
    if isinstance(binding, SnapshotBinding):
        data = binding.as_dict()
    elif isinstance(binding, Mapping):
        data = dict(binding)
    else:
        return _integrity_failure_report(
            binding, status=INTEGRITY_INVALID,
            reason=("the snapshot binding is neither a mapping nor a "
                    "SnapshotBinding"),
            conflict_kind="snapshot_binding_invalid")

    checks: list[dict] = []
    conflicts: list[dict] = []
    reasons: list[str] = []
    missing: list[str] = []
    malformed: list[str] = []

    # ---- Identity fields -------------------------------------------------
    raw_ref = data.get("topology_ref")
    expected_ref = None
    if raw_ref is None:
        missing.append("topology_ref")
    elif not isinstance(raw_ref, str):
        malformed.append("topology_ref must be a string")
    elif not raw_ref.strip():
        malformed.append("topology_ref is blank")
    elif len(raw_ref.strip()) > _MAX_REF_LENGTH:
        malformed.append(
            f"topology_ref is longer than {_MAX_REF_LENGTH} characters")
    else:
        expected_ref = raw_ref.strip()

    raw_fp = data.get("topology_fingerprint")
    fp_problem = _fingerprint_problem(raw_fp)
    expected_fp = None
    if fp_problem == "missing":
        missing.append("topology_fingerprint")
    elif fp_problem:
        malformed.append(f"topology_fingerprint is {fp_problem}")
    else:
        expected_fp = raw_fp.strip()

    raw_cfp = data.get("change_fingerprint")
    cfp_problem = _fingerprint_problem(raw_cfp)
    expected_cfp = None
    if raw_cfp is None:
        if require_change_fingerprint:
            missing.append("change_fingerprint")
    elif cfp_problem:
        malformed.append(f"change_fingerprint is {cfp_problem}")
    else:
        expected_cfp = raw_cfp.strip()

    binding_version = data.get("binding_version")
    if (binding_version is not None
            and binding_version != SNAPSHOT_BINDING_VERSION):
        malformed.append(
            f"binding_version {binding_version!r} is not supported "
            f"(expected {SNAPSHOT_BINDING_VERSION!r})")

    raw_source = data.get("topology_source")
    if raw_source is not None:
        if not isinstance(raw_source, str):
            malformed.append("topology_source must be a string")
        elif not raw_source.strip():
            malformed.append("topology_source is blank")

    # ---- Reference match -------------------------------------------------
    requested_ref = None
    ref_mismatch = False
    if topology_ref is not None:
        if not isinstance(topology_ref, str):
            malformed.append("the requested topology_ref must be a string")
        elif not topology_ref.strip():
            malformed.append("the requested topology_ref is blank")
        else:
            requested_ref = topology_ref.strip()
            if expected_ref is not None and requested_ref != expected_ref:
                ref_mismatch = True
                conflicts.append(_conflict(
                    "topology_ref_mismatch", requested_ref,
                    f"the snapshot binding is {expected_ref!r}"))

    # ---- Fingerprint match ----------------------------------------------
    topology_mismatch = False
    change_mismatch = False
    if actual_topology_fp is None:
        malformed.append("the supplied topology snapshot has no fingerprint")
    elif expected_fp is not None and expected_fp != actual_topology_fp:
        topology_mismatch = True
        conflicts.append(_conflict(
            "topology_fingerprint_mismatch", expected_ref or "topology",
            f"expected {expected_fp}, current snapshot is "
            f"{actual_topology_fp}"))

    if expected_cfp is not None:
        if actual_change_fp is None:
            malformed.append(
                "the supplied proposal has no change fingerprint")
        elif expected_cfp != actual_change_fp:
            change_mismatch = True
            conflicts.append(_conflict(
                "change_fingerprint_mismatch", expected_ref or "change",
                f"expected {expected_cfp}, current proposal is "
                f"{actual_change_fp}"))

    # ---- Freshness / TTL -------------------------------------------------
    policy_raw = (max_age_seconds if max_age_seconds is not None
                  else data.get("max_age_seconds"))
    policy, policy_error = _positive_int(policy_raw)
    if policy_error:
        malformed.append(policy_error)
        freshness = _freshness(FRESHNESS_INVALID, policy_error,
                               max_age_seconds=policy_raw)
    else:
        captured, captured_error = _parse_utc(data.get("captured_at"))
        if captured_error:
            malformed.append(f"captured_at: {captured_error}")
            freshness = _freshness(
                FRESHNESS_INVALID, f"captured_at: {captured_error}",
                max_age_seconds=policy)
        elif captured is None:
            freshness_status = (FRESHNESS_UNKNOWN if policy is not None
                                else FRESHNESS_NOT_CONFIGURED)
            detail = (
                "a freshness policy is configured but no capture timestamp "
                "was supplied; freshness cannot be established"
                if policy is not None else
                "no capture timestamp or freshness policy was supplied; "
                "freshness cannot be established")
            freshness = _freshness(freshness_status, detail,
                                   max_age_seconds=policy)
            reasons.append(detail)
        elif policy is None:
            detail = ("a capture timestamp is present but no freshness "
                      "policy was supplied; expiry cannot be evaluated")
            freshness = _freshness(
                FRESHNESS_NOT_CONFIGURED, detail,
                captured_at=captured.isoformat(), max_age_seconds=None)
            reasons.append(detail)
        else:
            evaluated, now_error = _parse_utc(now)
            if now_error:
                malformed.append(f"now: {now_error}")
                freshness = _freshness(
                    FRESHNESS_INVALID, f"now: {now_error}",
                    captured_at=captured.isoformat(),
                    max_age_seconds=policy)
            else:
                if evaluated is None:
                    from config import utc_now
                    evaluated = utc_now().astimezone(dt.timezone.utc)
                age = (evaluated - captured).total_seconds()
                if age < -_FUTURE_TOLERANCE_SECONDS:
                    detail = (
                        "the capture timestamp is in the future; the "
                        "snapshot time cannot be trusted")
                    malformed.append(detail)
                    conflicts.append(_conflict(
                        "snapshot_capture_in_future",
                        expected_ref or "topology", detail))
                    reasons.append(detail)
                    freshness = _freshness(
                        FRESHNESS_INVALID, detail,
                        captured_at=captured.isoformat(),
                        max_age_seconds=policy,
                        age_seconds=age,
                        evaluated_at=evaluated.isoformat())
                elif age > policy:
                    detail = (
                        f"the snapshot is {int(age)}s old, which exceeds the "
                        f"{policy}s freshness policy")
                    freshness = _freshness(
                        FRESHNESS_STALE, detail,
                        captured_at=captured.isoformat(),
                        max_age_seconds=policy,
                        age_seconds=age,
                        evaluated_at=evaluated.isoformat())
                    conflicts.append(_conflict(
                        "stale_topology", expected_ref or "topology", detail))
                    reasons.append(detail)
                else:
                    freshness = _freshness(
                        FRESHNESS_VERIFIED,
                        f"the snapshot is within the {policy}s freshness "
                        "policy",
                        captured_at=captured.isoformat(),
                        max_age_seconds=policy,
                        age_seconds=age,
                        evaluated_at=evaluated.isoformat())

    # ---- Assemble checks and status -------------------------------------
    checks.extend([
        _check("snapshot_binding_well_formed", not malformed,
               "identity metadata is well formed" if not malformed
               else "; ".join(malformed)),
        _check("topology_ref_present", expected_ref is not None,
               f"topology_ref={expected_ref or '(missing)'}"),
        _check("topology_ref_matches",
               None if requested_ref is None else not ref_mismatch,
               ("no requested reference was supplied" if requested_ref is None
                else f"requested={requested_ref}, bound={expected_ref}")),
        _check("topology_fingerprint_present", expected_fp is not None,
               f"topology_fingerprint={expected_fp or '(missing)'}"),
        _check("topology_fingerprint_matches",
               expected_fp is not None and expected_fp == actual_topology_fp,
               f"expected={expected_fp}, actual={actual_topology_fp}"),
        _check("change_fingerprint_present",
               expected_cfp is not None or not require_change_fingerprint,
               f"change_fingerprint={expected_cfp or '(missing)'}"),
        _check("change_fingerprint_matches",
               None if expected_cfp is None
               else expected_cfp == actual_change_fp,
               f"expected={expected_cfp}, actual={actual_change_fp}"),
        _check("freshness_established",
               freshness["status"] == FRESHNESS_VERIFIED,
               freshness["detail"]),
        _check("snapshot_not_stale",
               freshness["status"] != FRESHNESS_STALE,
               freshness["detail"]),
    ])

    if missing:
        detail = ("snapshot identity metadata is incomplete: "
                  + ", ".join(missing))
        conflicts.append(_conflict(
            "snapshot_identity_missing", expected_ref or "snapshot", detail))
        reasons.append(detail)
    if malformed:
        detail = ("snapshot identity metadata is malformed: "
                  + "; ".join(malformed))
        conflicts.append(_conflict(
            "snapshot_identity_invalid", expected_ref or "snapshot", detail))
        reasons.append(detail)
    if ref_mismatch:
        reasons.append("the requested topology_ref does not match the "
                       "snapshot binding")
    if topology_mismatch:
        reasons.append("the topology changed since the snapshot binding was "
                       "captured")
    if change_mismatch:
        reasons.append("the proposed change changed since the snapshot "
                       "binding was captured")

    if malformed or ref_mismatch:
        status = INTEGRITY_INVALID
    elif topology_mismatch or change_mismatch:
        status = INTEGRITY_MISMATCH
    elif freshness["status"] == FRESHNESS_STALE:
        status = INTEGRITY_STALE
    elif missing:
        status = INTEGRITY_INSUFFICIENT
    elif freshness["status"] in (FRESHNESS_UNKNOWN, FRESHNESS_NOT_CONFIGURED):
        status = INTEGRITY_UNKNOWN_FRESHNESS
    elif freshness["status"] == FRESHNESS_VERIFIED:
        status = INTEGRITY_VERIFIED
    else:
        status = INTEGRITY_INVALID

    if status == INTEGRITY_VERIFIED:
        recommended = None
    elif status == INTEGRITY_UNKNOWN_FRESHNESS:
        recommended = STATUS_PARTIAL
    elif status in (INTEGRITY_STALE, INTEGRITY_INSUFFICIENT):
        recommended = STATUS_INSUFFICIENT
    elif status == INTEGRITY_MISMATCH:
        recommended = (STATUS_INVALID if change_mismatch
                       else STATUS_INSUFFICIENT)
    elif status == INTEGRITY_INVALID:
        recommended = STATUS_INVALID
    else:
        recommended = STATUS_ERROR

    def _count(value):
        return (value if isinstance(value, int) and not isinstance(value, bool)
                and value >= 0 else None)

    return {
        "status": status,
        "binding_version": (binding_version or SNAPSHOT_BINDING_VERSION),
        "topology_ref": expected_ref,
        "requested_topology_ref": requested_ref,
        "topology_source": data.get("topology_source"),
        "expected_topology_fingerprint": expected_fp,
        "actual_topology_fingerprint": actual_topology_fp,
        "expected_change_fingerprint": expected_cfp,
        "actual_change_fingerprint": actual_change_fp,
        "device_count": _count(data.get("device_count")),
        "link_count": _count(data.get("link_count")),
        "freshness": freshness,
        "checks": checks,
        "conflicts": _sorted_conflicts(conflicts),
        "reasons": sorted(set(reasons)),
        "recommended_simulation_status": recommended,
        "not_proof_of_safety": True,
        "read_only": True,
        "database_written": False,
        "device_contacted": False,
        "configuration_applied": False,
    }


def validate_snapshot(
    binding,
    *,
    devices: Sequence = (),
    links: Sequence = (),
    topology_ref: str | None = None,
    device_name: str | None = None,
    proposed_lines: Sequence[str] | None = None,
    base_lines: Sequence[str] | None = None,
    request_uuid: str | None = None,
    scenario: str | None = None,
    now=None,
    max_age_seconds: int | None = None,
) -> dict:
    """Recompute a snapshot/proposal identity and validate a binding.

    Pure and read-only. It never refreshes a snapshot, never writes, and
    never treats an unverifiable snapshot as fresh. If the topology rows are
    unusable it fails closed instead of comparing partial evidence.
    """
    try:
        device_rows, _device_conflicts, devices_invalid = _normalise_devices(
            devices)
        link_rows, _link_conflicts, _duplicates, links_invalid = \
            _normalise_links(links, device_rows)
        if devices_invalid or links_invalid:
            return _integrity_failure_report(
                binding, status=INTEGRITY_INVALID,
                reason=("the supplied topology snapshot is not a usable "
                        "sequence"),
                conflict_kind="topology_snapshot_unusable")

        actual_topology_fp = _topology_fingerprint(device_rows, link_rows)
        require_change = (device_name is not None
                          or proposed_lines is not None)
        actual_change_fp = None
        if require_change:
            actual_change_fp = _change_fingerprint(
                device_name=device_name,
                proposed_lines=_normalise_config_lines(proposed_lines),
                base_lines=base_lines,
                request_uuid=request_uuid,
                scenario=scenario,
            )
        return _validate_binding(
            binding,
            device_rows=device_rows,
            link_rows=link_rows,
            actual_topology_fp=actual_topology_fp,
            actual_change_fp=actual_change_fp,
            topology_ref=topology_ref,
            device_name=device_name,
            request_uuid=request_uuid,
            scenario=scenario,
            now=now,
            max_age_seconds=max_age_seconds,
            require_change_fingerprint=require_change,
        )
    except Exception as exc:  # fail closed as an explicit error report
        return _integrity_failure_report(
            binding, status=INTEGRITY_ERROR,
            reason=("snapshot integrity validation failed "
                    f"({type(exc).__name__})"),
            conflict_kind="snapshot_integrity_error")


_STATUS_SEVERITY = {
    STATUS_SIMULATED: 1,
    STATUS_PARTIAL: 2,
    STATUS_NOT_SIMULATED: 2.5,
    STATUS_INSUFFICIENT: 3,
    STATUS_INVALID: 4,
    STATUS_ERROR: 5,
}


def _more_severe_status(current: str, candidate: str | None) -> str:
    """Never let integrity validation upgrade a weaker simulation status."""
    if candidate is None:
        return current
    return (candidate
            if _STATUS_SEVERITY.get(candidate, 0)
            > _STATUS_SEVERITY.get(current, 0)
            else current)


# ---------------------------------------------------------------------------
# Public pure engine
# ---------------------------------------------------------------------------
def simulate_change(
    *,
    devices: Sequence = (),
    links: Sequence = (),
    device_name: str,
    proposed_lines: Sequence[str] | None = None,
    base_lines: Sequence[str] | None = None,
    topology_ref: str | None = None,
    topology_source: str | None = None,
    invariants: Sequence[str] | None = None,
    scenario: str | None = None,
    request_uuid: str | None = None,
    request_status: str | None = None,
    data_source: str | None = None,
    snapshot_binding=None,
    integrity_now=None,
    max_age_seconds: int | None = None,
) -> dict:
    """Simulate one proposed change against an explicit topology snapshot.

    Pure with respect to the database and the network: it receives already-read
    rows and returns a deterministic, JSON-serialisable report. It never writes,
    applies, connects, probes or executes anything.

    ``snapshot_binding`` is optional. When supplied, the engine recomputes the
    snapshot/change fingerprints and refuses to report an unqualified success
    for a stale, mismatched, malformed, or freshness-unknown binding. A
    binding is never refreshed automatically.
    """
    if snapshot_binding is not None and topology_ref is None:
        topology_ref = _text(_get(snapshot_binding, "topology_ref", "")) or None
    if snapshot_binding is not None and topology_source is None:
        topology_source = _get(snapshot_binding, "topology_source",
                               TOPOLOGY_STORED)
    source = _topology_source_value(topology_source)
    device_name = _text(device_name)
    proposed = _normalise_config_lines(proposed_lines)
    invariants = [str(item) for item in (invariants or []) if str(item).strip()]

    conflicts: list[dict] = []
    reasons: list[str] = []
    assumptions: list[str] = [
        "The stored Link schema has no direction field, so links are modeled "
        "as undirected.",
        "Reachability uses documented links whose stored status is 'up' and "
        "whose endpoint devices are stored as 'up'. 'unknown' is uncertainty, "
        "not availability; a separate possible-reachability view admits "
        "unknown links/devices but never a device stored as 'down'.",
        "An administrative shutdown of a documented interface is modeled as "
        "removing that interface's documented links from the simulated graph.",
        "A simulated result is not proof that a real change is safe; it is not "
        "approval, execution, verification, rollback or a live observation.",
        "No device was contacted, no packet was sent, and no configuration was "
        "applied.",
    ]
    limitations: list[str] = [
        "Reachability is structural only; no routing protocol, route table, "
        "ACL, NAT, firewall, service or traffic-flow semantics are modeled.",
        "No convergence, timing, route-flap or packet-loss behavior is modeled.",
        "Interface existence is inferred only from documented links; an "
        "interface with no documented link has unknown topology support.",
        "A simulated reachability diff does not predict service-level impact.",
        "Simulation never authorizes, approves, applies, verifies or rolls "
        "back a change.",
    ]
    if source == TOPOLOGY_DEMO:
        limitations.append(
            "Topology source is demo/synthetic; it is not live production "
            "topology.")
    else:
        limitations.append(
            "Topology source is stored data; it may be stale relative to the "
            "lab and is not a live reachability observation.")

    # ---- Normalise the snapshot -----------------------------------------
    device_rows, device_conflicts, devices_invalid = _normalise_devices(devices)
    conflicts.extend(device_conflicts)
    link_rows, link_conflicts, duplicates, links_invalid = _normalise_links(
        links, device_rows)
    conflicts.extend(link_conflicts)
    structurally_invalid = devices_invalid or links_invalid

    topology_fp = _topology_fingerprint(device_rows, link_rows)
    change_fp = _change_fingerprint(
        device_name=device_name,
        proposed_lines=proposed,
        base_lines=base_lines,
        request_uuid=request_uuid,
        scenario=scenario,
    )
    integrity_report = None
    if snapshot_binding is not None:
        try:
            integrity_report = _validate_binding(
                snapshot_binding,
                device_rows=device_rows,
                link_rows=link_rows,
                actual_topology_fp=topology_fp,
                actual_change_fp=change_fp,
                topology_ref=topology_ref,
                device_name=device_name,
                request_uuid=request_uuid,
                scenario=scenario,
                now=integrity_now,
                max_age_seconds=max_age_seconds,
                require_change_fingerprint=bool(device_name or proposed),
            )
        except Exception as exc:  # fail closed as an explicit error report
            integrity_report = _integrity_failure_report(
                snapshot_binding, status=INTEGRITY_ERROR,
                reason=("snapshot integrity validation failed "
                        f"({type(exc).__name__})"),
                conflict_kind="snapshot_integrity_error")
    config_diff = _config_diff(base_lines, proposed)

    mg = _build_multigraph(device_rows, link_rows)
    device_status = {name: row["status"] for name, row in device_rows.items()}
    unknown_links = [link for link in link_rows if link["status"] == "unknown"]
    unknown_devices = [name for name, row in device_rows.items()
                       if row["status"] == "unknown"]

    # ---- Parse and validate the proposed operations ---------------------
    ops = _parse_operations(proposed)
    unsupported = [op for op in ops if not op.supported]
    supported = [op for op in ops if op.supported]
    topology_ops = [op for op in supported
                    if op.operation in ("interface_shutdown",
                                        "interface_no_shutdown")]
    metadata_ops = [op for op in supported if op.operation == "metadata"]

    target_present = bool(device_name) and device_name in device_rows
    target_links = [link for link in link_rows
                    if device_name in (link["device_a"], link["device_b"])]
    target_has_links = bool(target_links)
    target_is_down = bool(target_present
                          and device_status.get(device_name) == "down")
    if target_is_down:
        conflicts.append(_conflict(
            "target_device_down", device_name,
            "the target device is stored as down; the simulation cannot "
            "establish that it is available for the proposed change"))

    # Final administrative intent per interface: last operation wins. This is
    # deterministic and mirrors the order-sensitive CLI.
    desired: dict[str, str] = {}
    for op in topology_ops:
        if op.interface:
            desired[op.interface] = (
                "down" if op.operation == "interface_shutdown" else "up")

    excluded: set[tuple] = set()
    unavailable: list[dict] = []
    interface_effects: list[dict] = []
    for interface in sorted(desired):
        state = desired[interface]
        matched = _interface_links(link_rows, device_name, interface)
        if not matched:
            conflicts.append(_conflict(
                "undocumented_interface", f"{device_name}:{interface}",
                "the interface is not referenced by any documented link; its "
                "topology effect is unknown and no link was changed"))
            interface_effects.append({
                "interface": interface, "requested_state": state,
                "documented_links": 0, "effect": "unknown"})
            continue
        changed = 0
        for link in matched:
            key = _canonical_link_key(link)
            if state == "down":
                if link["status"] == "up":
                    if key not in excluded:
                        excluded.add(key)
                        changed += 1
                    unavailable.append({
                        "link": f"{link['device_a']}:{link['if_a']}-"
                                f"{link['device_b']}:{link['if_b']}",
                        "stored_status": link["status"],
                        "reason": "administrative shutdown in the proposed "
                                  "change",
                    })
                elif link["status"] == "unknown":
                    if key not in excluded:
                        excluded.add(key)
                    conflicts.append(_conflict(
                        "unknown_link_status", f"{link['device_a']}:"
                        f"{link['if_a']}-{link['device_b']}:{link['if_b']}",
                        "the link had no 'up' observation; the simulated "
                        "outage cannot be distinguished from an already-"
                        "unavailable link"))
                else:
                    conflicts.append(_conflict(
                        "link_already_down", f"{link['device_a']}:"
                        f"{link['if_a']}-{link['device_b']}:{link['if_b']}",
                        "the link is already down in stored data; the "
                        "shutdown has no modeled effect"))
            else:
                if link["status"] == "down":
                    conflicts.append(_conflict(
                        "cannot_infer_recovery", f"{link['device_a']}:"
                        f"{link['if_a']}-{link['device_b']}:{link['if_b']}",
                        "the link is down in stored data; 'no shutdown' does "
                        "not prove it becomes available, so no recovery was "
                        "assumed"))
                elif link["status"] == "unknown":
                    conflicts.append(_conflict(
                        "unknown_link_status", f"{link['device_a']}:"
                        f"{link['if_a']}-{link['device_b']}:{link['if_b']}",
                        "the link has no 'up' observation; 'no shutdown' "
                        "cannot establish availability"))
                else:
                    interface_effects.append({
                        "interface": interface, "requested_state": state,
                        "documented_links": len(matched),
                        "effect": "no change (already up)"})
        if state == "down" and matched:
            interface_effects.append({
                "interface": interface, "requested_state": state,
                "documented_links": len(matched),
                "effect": f"{changed} documented link(s) unavailable"})

    # ---- Before/after reachability --------------------------------------
    before_available = _simple_graph(
        mg, device_rows, include_unknown=False, excluded=set())
    after_available = _simple_graph(
        mg, device_rows, include_unknown=False, excluded=excluded)
    before_possible = _simple_graph(
        mg, device_rows, include_unknown=True, excluded=set())
    after_possible = _simple_graph(
        mg, device_rows, include_unknown=True, excluded=excluded)

    before_reach = _reachable(before_available, device_name) if target_present \
        else set()
    after_reach = _reachable(after_available, device_name) if target_present \
        else set()
    lost_reach = sorted(before_reach - after_reach)
    gained_reach = sorted(after_reach - before_reach)

    before_isolated = {n for n in before_available.nodes
                       if before_available.degree(n) == 0}
    after_isolated = {n for n in after_available.nodes
                      if after_available.degree(n) == 0}
    newly_isolated = sorted(after_isolated - before_isolated)

    split_components: list[dict] = []
    lost_pairs = 0
    for comp in _components(before_available):
        sub = after_available.subgraph(comp)
        parts = [sorted(part) for part in nx.connected_components(sub)]
        parts.sort(key=lambda part: (len(part), part))
        if len(parts) > 1:
            sizes = [len(part) for part in parts]
            lost_pairs += (sum(sizes) ** 2 - sum(size * size
                                                 for size in sizes)) // 2
            split_components.append({"before": comp, "after": parts})

    # ---- Redundancy ------------------------------------------------------
    # ``before_available`` is a simple graph, so a pair with genuinely
    # parallel documented links collapses to one edge and networkx would
    # report it as a bridge. Filter those pairs out: a parallel link is
    # redundancy, not a single point of failure.
    available_pair_counts = Counter(
        _pair_key(link["device_a"], link["device_b"])
        for link in link_rows
        if link["status"] == "up"
        and device_status.get(link["device_a"]) == "up"
        and device_status.get(link["device_b"]) == "up")
    bridge_pairs = sorted(
        tuple(sorted(pair)) for pair in nx.bridges(before_available)
        if available_pair_counts.get(tuple(sorted(pair)), 0) <= 1) \
        if before_available.number_of_edges() else []
    articulation = sorted(nx.articulation_points(before_available)) \
        if before_available.number_of_nodes() else []
    alternate_paths: list[dict] = []
    for key in sorted(excluded):
        u, v = key[0], key[1]
        parallel = [link for link in link_rows
                    if _pair_key(link["device_a"], link["device_b"])
                    == _pair_key(u, v)]
        available_parallel = [
            link for link in parallel
            if link["status"] == "up"
            and device_status.get(link["device_a"]) == "up"
            and device_status.get(link["device_b"]) == "up"]
        path = None
        if u in after_available and v in after_available:
            try:
                path = list(nx.shortest_path(after_available, u, v))
            except (nx.NetworkXNoPath, nx.NodeNotFound):
                path = None
        alternate_paths.append({
            "link": f"{u}-{v}",
            "parallel_links": len(parallel),
            "available_parallel_links": len(available_parallel),
            "alternate_path_exists": path is not None,
            "alternate_path": path,
            "note": ("Structural only; an alternate path is not a live "
                     "availability guarantee."),
        })

    # ---- Invariants ------------------------------------------------------
    invariant_checks: list[dict] = []
    for invariant in invariants:
        name = invariant.strip().lower()
        if name not in _SUPPORTED_INVARIANTS:
            detail = ("routing protocol semantics are not modeled; only "
                      "structural reachability can be evaluated"
                      if name == "preserve_lab_routing"
                      else "the engine cannot evaluate this invariant")
            invariant_checks.append({
                "invariant": invariant, "status": "unsupported",
                "detail": detail})
            continue
        if not target_present or not target_has_links or target_is_down:
            invariant_checks.append({
                "invariant": invariant, "status": "unknown",
                "detail": "topology evidence is insufficient to evaluate it"})
        elif unknown_links or unknown_devices or unsupported:
            invariant_checks.append({
                "invariant": invariant, "status": "unknown",
                "detail": "the simulation is partial; the invariant cannot be "
                          "conclusively evaluated"})
        elif lost_reach:
            invariant_checks.append({
                "invariant": invariant, "status": "failed",
                "detail": "devices reachable before the change are no longer "
                          "reachable in the simulated topology: "
                          + ", ".join(lost_reach)})
        else:
            invariant_checks.append({
                "invariant": invariant, "status": "passed",
                "detail": "no documented reachable device is lost in the "
                          "simulated topology (structural only)"})

    # ---- Status ----------------------------------------------------------
    if structurally_invalid:
        status = STATUS_INVALID
        reasons.append("the topology snapshot is not a usable sequence")
    elif not device_name:
        status = STATUS_INVALID
        reasons.append("the proposed change has no target device")
    elif not device_rows or not link_rows:
        status = STATUS_INSUFFICIENT
        reasons.append("the topology snapshot has no usable devices/links")
    elif not target_present:
        status = STATUS_INVALID
        reasons.append(f"target device {device_name!r} is not present in the "
                       "topology snapshot")
    elif not target_has_links:
        status = STATUS_INSUFFICIENT
        reasons.append(f"target device {device_name!r} has no documented "
                       "links; reachability cannot be modeled")
    elif not proposed:
        status = STATUS_NOT_SIMULATED
        reasons.append("the proposed change has no configuration lines")
    elif not supported:
        status = STATUS_NOT_SIMULATED
        reasons.append("no proposed operation is supported for topology "
                       "simulation")
    elif not topology_ops and not metadata_ops and not unsupported:
        status = STATUS_NOT_SIMULATED
        reasons.append("the proposed change contains only interface context "
                       "lines; there is no state change to simulate")
    elif unsupported:
        status = STATUS_PARTIAL
        reasons.append(f"{len(unsupported)} proposed operation(s) are "
                       "unsupported for reachability modeling")
    elif unknown_links or unknown_devices:
        status = STATUS_PARTIAL
        reasons.append(
            f"{len(unknown_links)} link(s) and {len(unknown_devices)} "
            "device(s) have unknown status; reachability is uncertain")
    elif any(c["kind"] in ("malformed_device_row", "malformed_link_row",
                           "blank_device_name", "blank_link_endpoint",
                           "self_loop_link", "blank_interface",
                           "duplicate_device", "duplicate_link",
                           "duplicate_link_metadata_conflict",
                           "invalid_device_status", "invalid_link_status",
                           "link_endpoint_without_device",
                           "undocumented_interface", "cannot_infer_recovery",
                           "target_device_down")
             for c in conflicts):
        status = STATUS_PARTIAL
        reasons.append("topology/interface conflicts make the simulation "
                       "partial")
    elif any(c["status"] == "unsupported" for c in invariant_checks):
        status = STATUS_PARTIAL
        reasons.append("at least one requested invariant cannot be evaluated "
                       "by this engine")
    else:
        status = STATUS_SIMULATED

    # ---- Snapshot integrity gate (Phase D3) -----------------------------
    # Integrity validation can only make the result more conservative. A
    # stale/mismatched/malformed binding never upgrades a status, and an
    # unverifiable freshness policy downgrades a clean simulation to partial.
    if integrity_report is not None:
        status = _more_severe_status(
            status, integrity_report.get("recommended_simulation_status"))
        for conflict in integrity_report.get("conflicts") or []:
            if conflict not in conflicts:
                conflicts.append(conflict)
        reasons.extend(integrity_report.get("reasons") or [])

    if target_is_down:
        reasons.append("target device is stored as down; availability cannot "
                       "be established")
    if duplicates:
        reasons.append(f"{duplicates} duplicate link row(s) were collapsed")
    if not topology_ops and metadata_ops and status == STATUS_SIMULATED:
        reasons.append("no topology-affecting operation was proposed; the "
                       "modeled topology effect is none")
    if lost_reach:
        reasons.append(f"{len(lost_reach)} device(s) lose documented "
                       "reachability from the target")
    elif topology_ops and status == STATUS_SIMULATED:
        reasons.append("no documented reachability change was modeled")
    if invariant_checks:
        failed = [c["invariant"] for c in invariant_checks
                  if c["status"] == "failed"]
        unknown = [c["invariant"] for c in invariant_checks
                   if c["status"] == "unknown"]
        if failed:
            reasons.append("invariant(s) failed: " + ", ".join(failed))
        if unknown:
            reasons.append("invariant(s) could not be evaluated: "
                           + ", ".join(unknown))
        unsupported_invariants = [c["invariant"] for c in invariant_checks
                                  if c["status"] == "unsupported"]
        if unsupported_invariants:
            reasons.append("invariant(s) unsupported by this engine: "
                           + ", ".join(unsupported_invariants))

    checks = [
        _check("topology_snapshot_usable",
               not structurally_invalid and bool(device_rows),
               f"{len(device_rows)} device row(s), {len(link_rows)} link "
               f"row(s)"),
        _check("target_present", target_present,
               f"target {device_name or '(blank)'}"),
        _check("target_has_documented_links", target_has_links,
               f"{len(target_links)} link(s) touch the target"),
        _check("proposed_operations_supported", not unsupported,
               f"{len(supported)} supported, {len(unsupported)} unsupported"),
        _check("reachability_modeled",
               None if (not target_has_links or unknown_links
                        or unknown_devices) else True,
               "documented 'up' links only; unknown status is uncertainty"),
        _check("config_diff_available", config_diff is not None,
               "base configuration supplied" if config_diff is not None
               else "no base configuration was supplied"),
        _check("not_proof_of_safety", True,
               "a simulated result never proves a real change is safe"),
    ]
    if integrity_report is not None:
        checks.append(_check(
            "snapshot_integrity",
            integrity_report.get("status") == INTEGRITY_VERIFIED,
            f"integrity status is {integrity_report.get('status')!r}; "
            f"freshness is "
            f"{(integrity_report.get('freshness') or {}).get('status')!r}"))

    evidence = [
        f"topology snapshot: {len(device_rows)} device row(s), "
        f"{len(link_rows)} link row(s) (source={source}, "
        f"fingerprint={topology_fp})",
        f"proposed change: device={device_name or '(blank)'}, "
        f"{len(proposed)} line(s), fingerprint={change_fp}",
        "reachability: documented links with status 'up' and known-up "
        "endpoints; no packets sent and no device contacted",
        f"simulated unavailable link(s): {len(unavailable)}",
    ]
    if integrity_report is not None:
        evidence.append(
            "snapshot integrity: "
            f"status={integrity_report.get('status')}, "
            f"topology_ref={integrity_report.get('topology_ref') or '-'}, "
            "expected_topology_fingerprint="
            f"{integrity_report.get('expected_topology_fingerprint') or '-'}, "
            "actual_topology_fingerprint="
            f"{integrity_report.get('actual_topology_fingerprint') or '-'}, "
            "freshness="
            f"{(integrity_report.get('freshness') or {}).get('status')}")

    report = {
        "simulator": SIMULATOR,
        "simulator_version": SIMULATOR_VERSION,
        "status": status,
        "status_reasons": sorted(set(reasons)),
        "topology_ref": topology_ref,
        "topology_source": source,
        "topology_fingerprint": topology_fp,
        "snapshot_integrity_status": (
            integrity_report.get("status")
            if integrity_report is not None else None),
        "integrity": integrity_report,
        "change_ref": {
            "request_uuid": request_uuid,
            "request_status": request_status,
            "data_source": data_source,
            "device": device_name,
            "line_count": len(proposed),
            "fingerprint": change_fp,
            "scenario": scenario,
        },
        "config_diff": config_diff,
        "config_diff_available": config_diff is not None,
        "operations": [op.as_dict() for op in ops],
        "unsupported_operations": [op.as_dict() for op in unsupported],
        "topology_effect": ("none" if not topology_ops and metadata_ops
                            else "link_availability" if topology_ops
                            else "none"),
        "interface_effects": interface_effects,
        "affected_components": {
            "lost_reachability_from_target": lost_reach,
            "gained_reachability_from_target": gained_reach,
            "newly_isolated_devices": newly_isolated,
            "split_components": split_components,
            "lost_pairs": lost_pairs,
            "unavailable_links": sorted(
                unavailable, key=lambda item: item["link"]),
        },
        "before": {
            **_summary(before_available, device_name),
            "possible_edge_count": before_possible.number_of_edges(),
            "possible_reachable_from_target": sorted(
                _reachable(before_possible, device_name)),
        },
        "after": {
            **_summary(after_available, device_name),
            "possible_edge_count": after_possible.number_of_edges(),
            "possible_reachable_from_target": sorted(
                _reachable(after_possible, device_name)),
        },
        "redundancy": {
            "target_is_articulation_point": device_name in set(articulation),
            "bridge_links": [list(pair) for pair in bridge_pairs],
            "alternate_paths": alternate_paths,
            "note": ("Structural analysis of the documented topology only; an "
                     "alternate path is not a live availability guarantee."),
        },
        "invariant_checks": invariant_checks,
        "assumptions": assumptions,
        "limitations": limitations,
        "conflicts": _sorted_conflicts(conflicts),
        "checks": checks,
        "evidence": evidence,
        "not_proof_of_safety": True,
        "routing_modeled": False,
        "live_network_tested": False,
        "configuration_applied": False,
    }
    return report


# ---------------------------------------------------------------------------
# Read-only session wrapper (the only place that reads the database)
# ---------------------------------------------------------------------------
def _json_list(value):
    """Return ``(list_or_None, error_or_None)`` for a stored JSON list."""
    if value is None:
        return None, None
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value], None
    try:
        data = json.loads(value)
    except (TypeError, ValueError) as exc:
        return None, f"stored config is not valid JSON ({type(exc).__name__})"
    if not isinstance(data, list):
        return None, "stored config is not a JSON list"
    return [str(item) for item in data], None


def _topology_source_label(explicit: str | None) -> str:
    if explicit is not None:
        return (TOPOLOGY_DEMO if _text(explicit).lower() == TOPOLOGY_DEMO
                else TOPOLOGY_STORED)
    from config import get_settings
    return TOPOLOGY_DEMO if get_settings().demo_mode else TOPOLOGY_STORED


def _configured_max_age_seconds() -> int | None:
    """Read the optional freshness policy; invalid/absent means unknown."""
    try:
        from config import get_settings
        raw = get_settings().digital_twin_snapshot_max_age_seconds
    except Exception:
        return None
    policy, error = _positive_int(raw)
    return None if error else policy


def capture_request_snapshot(
    session,
    request,
    *,
    topology_source: str | None = None,
    captured_at=None,
    max_age_seconds: int | None = None,
) -> SnapshotBinding:
    """Read the stored topology and capture a binding for one request.

    Read-only: it never writes, refreshes, approves or executes anything. The
    returned binding is *not* persisted by D3; a future integration must store
    it with the change request/approval and validate it again before use.
    """
    from database import repository

    device_name = _text(_get(request, "device_name", ""))
    request_uuid = _text(_get(request, "request_uuid", "")) or None
    source = _topology_source_label(topology_source)
    proposed_lines, proposed_error = _json_list(
        _get(request, "proposed_config_json", None))
    base_lines, base_error = _json_list(
        _get(request, "base_config_json", None))
    errors = [error for error in (proposed_error, base_error) if error]
    if errors:
        raise ValueError("; ".join(errors))

    devices = repository.get_devices(session)
    links = repository.get_links(session)
    return capture_snapshot(
        devices=devices,
        links=links,
        topology_ref=f"{source}:{request_uuid or 'request'}",
        topology_source=source,
        device_name=device_name,
        proposed_lines=proposed_lines,
        base_lines=base_lines,
        request_uuid=request_uuid,
        captured_at=captured_at,
        max_age_seconds=max_age_seconds,
    )


def simulate_request(session, request, *,
                     topology_source: str | None = None,
                     snapshot_binding=None,
                     integrity_now=None,
                     max_age_seconds: int | None = None) -> dict:
    """Read stored topology and simulate one ChangeRequest. Never writes.

    When ``snapshot_binding`` is supplied, the current stored topology and
    proposal are re-fingerprinted and the integrity gate is applied. The
    snapshot is never refreshed silently: a mismatch/staleness returns a
    conservative status instead.
    """
    from database import repository

    device_name = _text(_get(request, "device_name", ""))
    request_uuid = _text(_get(request, "request_uuid", "")) or None
    status = _text(_get(request, "status", "")) or None
    data_source = _text(_get(request, "data_source", "live")) or "live"
    source = _topology_source_label(topology_source)

    proposed_lines, proposed_error = _json_list(
        _get(request, "proposed_config_json", None))
    base_lines, base_error = _json_list(
        _get(request, "base_config_json", None))
    input_errors = [error for error in (proposed_error, base_error) if error]

    if snapshot_binding is not None and max_age_seconds is None:
        bound_policy = _get(snapshot_binding, "max_age_seconds", None)
        if bound_policy is None:
            max_age_seconds = _configured_max_age_seconds()

    try:
        devices = repository.get_devices(session)
        links = repository.get_links(session)
        report = simulate_change(
            devices=devices, links=links, device_name=device_name,
            proposed_lines=proposed_lines, base_lines=base_lines,
            topology_ref=f"{source}:{request_uuid or 'request'}",
            topology_source=source, request_uuid=request_uuid,
            request_status=status, data_source=data_source,
            snapshot_binding=snapshot_binding, integrity_now=integrity_now,
            max_age_seconds=max_age_seconds)
    except Exception as exc:  # fail closed as an explicit error report
        report = {
            "simulator": SIMULATOR,
            "simulator_version": SIMULATOR_VERSION,
            "status": STATUS_ERROR,
            "status_reasons": [
                f"simulation could not run ({type(exc).__name__}); the "
                "approval and execution gates are unchanged"],
            "topology_ref": f"{source}:{request_uuid or 'request'}",
            "topology_source": source,
            "topology_fingerprint": None,
            "snapshot_integrity_status": None,
            "integrity": None,
            "change_ref": {"request_uuid": request_uuid, "device": device_name,
                           "line_count": len(proposed_lines or [])},
            "config_diff": None,
            "config_diff_available": False,
            "operations": [],
            "unsupported_operations": [],
            "topology_effect": "none",
            "interface_effects": [],
            "affected_components": {},
            "before": {},
            "after": {},
            "redundancy": {},
            "invariant_checks": [],
            "assumptions": [],
            "limitations": ["simulation failed; no topology conclusion is "
                            "available"],
            "conflicts": [_conflict(
                "simulation_error", device_name or "unknown",
                f"simulation raised {type(exc).__name__}")],
            "checks": [],
            "evidence": [],
            "not_proof_of_safety": True,
            "routing_modeled": False,
            "live_network_tested": False,
            "configuration_applied": False,
        }

    if input_errors:
        report["conflicts"] = _sorted_conflicts(
            list(report.get("conflicts") or []) + [
                _conflict("input_error", device_name or "request", error)
                for error in input_errors])
        if report.get("status") == STATUS_SIMULATED:
            report["status"] = STATUS_PARTIAL
        report["status_reasons"] = sorted(set(
            list(report.get("status_reasons") or []) + input_errors))
    return report


# ---------------------------------------------------------------------------
# Adapter to the existing DigitalTwin ABC
# ---------------------------------------------------------------------------
class TopologyDigitalTwin(DigitalTwin):
    """Adapter that exposes the deterministic engine behind the ABC.

    The topology snapshot is supplied at construction time. The adapter does
    not read the database, connect to a device, or touch approval/execution.
    """

    def __init__(self, *, devices: Sequence = (), links: Sequence = (),
                 topology_ref: str | None = None,
                 topology_source: str = TOPOLOGY_SNAPSHOT,
                 snapshot_binding=None,
                 max_age_seconds: int | None = None,
                 integrity_now=None) -> None:
        self.devices = list(devices)
        self.links = list(links)
        self.topology_ref = topology_ref
        self.topology_source = topology_source
        self.snapshot_binding = snapshot_binding
        self.max_age_seconds = max_age_seconds
        self.integrity_now = integrity_now

    def simulate(self, *, topology_ref=None, proposed_change=None,
                 invariants=None, scenario=None) -> SimulationResult:
        if isinstance(proposed_change, Mapping):
            proposal = proposed_change
        elif proposed_change is not None and hasattr(proposed_change,
                                                    "model_dump"):
            # Accept the Pydantic ``ConfigProposal`` used by the AI workflow.
            dumped = proposed_change.model_dump()
            proposal = dumped if isinstance(dumped, Mapping) else {}
        else:
            proposal = {}
        if not proposal:
            return SimulationResult(
                status=STATUS_NOT_SIMULATED, simulator_version=SIMULATOR_VERSION,
                reasons=["no proposed change was supplied; nothing was "
                         "simulated"])
        if proposal.get("status") not in (None, "ok"):
            return SimulationResult(
                status=STATUS_NOT_SIMULATED, simulator_version=SIMULATOR_VERSION,
                reasons=[f"proposal status is {proposal.get('status')!r}; "
                         "there is no valid proposal to simulate"])
        # The adapter is bound to one snapshot at construction time. A caller
        # that supplies a different topology reference would otherwise get a
        # report labelled with a snapshot it did not use.
        if (topology_ref is not None and self.topology_ref is not None
                and topology_ref != self.topology_ref):
            return SimulationResult(
                status=STATUS_INVALID, simulator_version=SIMULATOR_VERSION,
                reasons=["the requested topology_ref does not match the "
                         "snapshot bound to this adapter; nothing was "
                         "simulated"],
                topology_ref=topology_ref,
                conflicts=[_conflict(
                    "topology_ref_mismatch", str(topology_ref),
                    f"adapter snapshot is {self.topology_ref!r}")],
                not_proof_of_safety=True)
        device = _text(proposal.get("device") or proposal.get("device_name"))
        lines = (proposal.get("config_lines")
                 or proposal.get("proposed_lines")
                 or proposal.get("lines") or [])
        base = (proposal.get("base_lines")
                or proposal.get("base_config_lines"))
        try:
            report = simulate_change(
                devices=self.devices, links=self.links, device_name=device,
                proposed_lines=list(lines), base_lines=base,
                topology_ref=topology_ref or self.topology_ref,
                topology_source=self.topology_source,
                invariants=invariants, scenario=scenario,
                snapshot_binding=self.snapshot_binding,
                integrity_now=self.integrity_now,
                max_age_seconds=self.max_age_seconds)
        except Exception as exc:  # fail closed: never raise into the caller
            return SimulationResult(
                status=STATUS_ERROR,
                simulator_version=SIMULATOR_VERSION,
                reasons=[f"simulation could not run ({type(exc).__name__}); "
                         "no topology conclusion is available"],
                topology_ref=topology_ref or self.topology_ref,
                change_ref={"device": device, "line_count": len(lines)},
                limitations=["simulation failed; no topology conclusion is "
                             "available"],
                conflicts=[_conflict(
                    "simulation_error", device or "unknown",
                    f"simulation raised {type(exc).__name__}")],
                not_proof_of_safety=True,
                details={},
            )
        return SimulationResult(
            status=report["status"],
            checks=report["checks"],
            evidence=report["evidence"],
            simulator_version=SIMULATOR_VERSION,
            reasons=report["status_reasons"],
            assumptions=report["assumptions"],
            limitations=report["limitations"],
            conflicts=report["conflicts"],
            affected_components=report["affected_components"],
            topology_ref=report["topology_ref"],
            change_ref=report["change_ref"],
            before=report["before"],
            after=report["after"],
            not_proof_of_safety=True,
            details=report,
        )


# ---------------------------------------------------------------------------
# Integration boundary (deliberately not wired in D1)
# ---------------------------------------------------------------------------
# ``ai/graph.py`` still constructs ``UnavailableDigitalTwin``. Wiring this
# engine into the AI workflow would change Phase 3/B4 safety expectations
# (``digital_twin_status == "not_run"``) and requires passing a topology
# snapshot into the graph runner. That integration is a separate, reviewed
# step; D1 ships the engine, its tests, and the read-only UI preview only.
__all__ = [
    "SIMULATOR", "SIMULATOR_VERSION", "SNAPSHOT_BINDING_VERSION",
    "STATUS_SIMULATED", "STATUS_PARTIAL", "STATUS_INSUFFICIENT",
    "STATUS_INVALID", "STATUS_NOT_SIMULATED", "STATUS_ERROR",
    "INTEGRITY_VERIFIED", "INTEGRITY_UNKNOWN_FRESHNESS", "INTEGRITY_STALE",
    "INTEGRITY_MISMATCH", "INTEGRITY_INSUFFICIENT", "INTEGRITY_INVALID",
    "INTEGRITY_ERROR",
    "FRESHNESS_VERIFIED", "FRESHNESS_UNKNOWN", "FRESHNESS_STALE",
    "FRESHNESS_NOT_CONFIGURED", "FRESHNESS_INVALID",
    "TOPOLOGY_STORED", "TOPOLOGY_DEMO", "TOPOLOGY_SNAPSHOT",
    "SnapshotBinding", "capture_snapshot", "validate_snapshot",
    "capture_request_snapshot", "parse_utc", "integrity_failure_report",
    "TopologyDigitalTwin", "simulate_change", "simulate_request",
]
