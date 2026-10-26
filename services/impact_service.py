"""Topology-aware change impact analysis (Phase B2).

This module is **read-only**. It builds the documented device/link graph from
the stored ``devices`` / ``links`` rows, walks it from the change target, and
classifies every finding as ``confirmed``, ``potential`` or ``unknown``. It
never connects to a device, never writes to the database, and never invokes the
change executor.

Evidence rules (deterministic, documented):

* ``confirmed`` - the change request itself proves the element is targeted
  (the named device, or an interface explicitly configured/removed in the
  proposed lines).
* ``potential`` - the element is reachable in the *documented* topology, but no
  telemetry or failure observation confirms the change will affect it.
* ``unknown`` - the evidence needed to decide is absent, stale, malformed or
  contradictory: empty topology, missing target, a path blocked by a down
  link/device, or a route/service that is not represented at all.

The stored ``Link`` schema has no direction field, so the graph is undirected.
"Upstream/downstream" is therefore reported as "reachable dependency" and that
assumption is included in every report. Device/link status values are the last
stored observations, not live probes. Demo topology is always labelled as demo
and is never presented as live production topology.
"""
from __future__ import annotations

import datetime as dt
import json
from collections import deque
from collections.abc import Mapping, Sequence
from types import SimpleNamespace

from analytics import graph
from config import get_settings
from database import repository
from database.models import (CHANGE_RISK_LEVELS, DEVICE_STATUSES,
                             LINK_STATUSES, db_utcnow)
from network.lab_config_ops import normalize_line

#: Classification values. These are evidence classes, not risk scores.
CONFIRMED = "confirmed"
POTENTIAL = "potential"
UNKNOWN = "unknown"

#: Analysis completeness values.
ANALYSIS_COMPLETE = "complete"
ANALYSIS_PARTIAL = "partial"
ANALYSIS_INSUFFICIENT = "insufficient_evidence"
ANALYSIS_ERROR = "error"

#: Topology provenance labels. There is deliberately no "live" value: stored
#: topology is never a live reachability observation.
TOPOLOGY_DEMO = "demo"
TOPOLOGY_STORED = "stored"

#: Telemetry older than this is reported as stale (uncertainty), never as a
#: confirmed impact. It is a presentation threshold, not a risk score.
DEFAULT_STALE_AFTER = dt.timedelta(hours=24)

_CLASS_RANK = {CONFIRMED: 0, POTENTIAL: 1, UNKNOWN: 2}


# ---------------------------------------------------------------------------
# Small normalisation helpers
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


def _device_status(value) -> str:
    status = _text(value).lower() or "unknown"
    return status if status in DEVICE_STATUSES else "unknown"


def _link_status(value) -> str:
    status = _text(value).lower() or "unknown"
    return status if status in LINK_STATUSES else "unknown"


def _naive_utc(value):
    if not isinstance(value, dt.datetime):
        return None
    if value.tzinfo is not None:
        return value.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return value


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


def topology_source_label(demo_mode: bool) -> str:
    """Map the application's demo flag to the only two honest labels."""
    return TOPOLOGY_DEMO if demo_mode else TOPOLOGY_STORED


def _source_detail(source: str) -> str:
    if source == TOPOLOGY_DEMO:
        return ("demo (synthetic fixtures in the separate demo database; "
                "NOT live production topology)")
    return ("stored (collected/lab database; not a live reachability test)")


def _unknown(kind: str, element: str, explanation: str,
             evidence: str) -> dict:
    return {"kind": kind, "element": element, "classification": UNKNOWN,
            "explanation": explanation, "evidence": evidence}


# ---------------------------------------------------------------------------
# Topology normalisation
# ---------------------------------------------------------------------------
def _pair_key(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


def _link_key(link: dict) -> tuple:
    a, b = link["device_a"], link["device_b"]
    if a <= b:
        return (a, b, link["if_a"], link["if_b"])
    return (b, a, link["if_b"], link["if_a"])


def _link_evidence(link: dict) -> str:
    return (f"{link['device_a']}:{link['if_a'] or '?'} ↔ "
            f"{link['device_b']}:{link['if_b'] or '?'} "
            f"(status={link['status']}, "
            f"subnet={link['subnet'] or 'not recorded'})")


def _normalise_topology(devices: Sequence, links: Sequence):
    """Return cleaned rows, malformed-row notes and duplicate count.

    Bad rows are skipped, never guessed at. Identical duplicate link rows are
    collapsed so they cannot create duplicate impact entries; genuinely
    parallel links (same pair, different interfaces) are kept as redundancy
    evidence.
    """
    malformed: list[str] = []
    device_rows: dict[str, dict] = {}

    def _device_sort_key(row):
        return _text(_get(row, "name", ""))

    for raw in sorted(list(devices or []), key=_device_sort_key):
        name = _text(_get(raw, "name", ""))
        if not name:
            malformed.append("device row with a blank name was skipped")
            continue
        if name in device_rows:
            malformed.append(f"duplicate device row for '{name}' was skipped")
            continue
        status = _device_status(_get(raw, "status", "unknown"))
        if status != (_text(_get(raw, "status", "unknown")).lower() or
                      "unknown"):
            malformed.append(
                f"device '{name}' had an invalid status; treated as unknown")
        device_rows[name] = {
            "name": name,
            "status": status,
            "type": _text(_get(raw, "type", "")) or None,
        }

    clean_links: list[dict] = []
    seen: set[tuple] = set()
    duplicates = 0
    for idx, raw in enumerate(list(links or [])):
        a = _text(_get(raw, "device_a", ""))
        b = _text(_get(raw, "device_b", ""))
        if not a or not b:
            malformed.append(f"link row {idx} has a blank endpoint; skipped")
            continue
        if a == b:
            malformed.append(f"link row {idx} is a self-loop on '{a}'; skipped")
            continue
        raw_status = _text(_get(raw, "status", "unknown")).lower() or "unknown"
        status = _link_status(raw_status)
        if status != raw_status:
            malformed.append(
                f"link {a}-{b} had an invalid status; treated as unknown")
        link = {
            "device_a": a,
            "if_a": _text(_get(raw, "if_a", "")),
            "device_b": b,
            "if_b": _text(_get(raw, "if_b", "")),
            "subnet": _text(_get(raw, "subnet", "")) or None,
            "status": status,
        }
        key = _link_key(link)
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        clean_links.append(link)

    clean_links.sort(key=lambda link: (
        min(link["device_a"], link["device_b"]),
        max(link["device_a"], link["device_b"]),
        link["if_a"], link["if_b"], link["device_a"], link["device_b"]))
    return device_rows, clean_links, sorted(set(malformed)), duplicates


def _build_graph(device_rows: dict[str, dict], clean_links: list[dict]):
    devices = [SimpleNamespace(name=row["name"], status=row["status"],
                               type=row["type"])
               for _name, row in sorted(device_rows.items())]
    links = [SimpleNamespace(**link) for link in clean_links]
    return graph.build_graph(devices, links)


def _pair_links(clean_links: list[dict]) -> dict[tuple[str, str], list[dict]]:
    pairs: dict[tuple[str, str], list[dict]] = {}
    for link in clean_links:
        pairs.setdefault(
            _pair_key(link["device_a"], link["device_b"]), []).append(link)
    for key in pairs:
        pairs[key].sort(key=lambda link: (
            link["device_a"], link["if_a"], link["device_b"], link["if_b"]))
    return pairs


# ---------------------------------------------------------------------------
# Traversal
# ---------------------------------------------------------------------------
def _bfs_paths(g, source: str) -> dict[str, list[str]]:
    """Deterministic BFS path map; a visited set makes cycles terminate."""
    if source not in g:
        return {}
    paths: dict[str, list[str]] = {source: [source]}
    queue = deque([source])
    while queue:
        current = queue.popleft()
        for neighbour in sorted(g.neighbors(current)):
            if neighbour in paths:
                continue
            paths[neighbour] = paths[current] + [neighbour]
            queue.append(neighbour)
    return paths


def _path_state(path: list[str], device_status: dict[str, str],
                pairs: dict[tuple[str, str], list[dict]]) -> tuple[str, list[str]]:
    """Classify a documented path as up / uncertain / blocked.

    Only link status and non-target device status can block a path. A missing
    or unknown observation is uncertainty, never a confirmed failure.
    """
    blocks: list[str] = []
    uncertainties: list[str] = []
    for u, v in zip(path, path[1:]):
        links = pairs.get(_pair_key(u, v), [])
        if not links:
            blocks.append(f"no documented link between {u} and {v}")
            continue
        statuses = {link["status"] for link in links}
        if "up" in statuses:
            continue
        if "unknown" in statuses:
            uncertainties.append(
                f"link {u}-{v} has no 'up' observation (status unknown)")
        else:
            blocks.append(f"every documented link between {u} and {v} is down")
    for node in path[1:]:
        status = device_status.get(node, "unknown")
        if status == "down":
            blocks.append(f"device {node} is down in stored data")
        elif status != "up":
            uncertainties.append(f"device {node} status is {status}")
    if blocks:
        return "blocked", blocks + uncertainties
    if uncertainties:
        return "uncertain", uncertainties
    return "up", []


def _path_links(path: list[str],
                pairs: dict[tuple[str, str], list[dict]]) -> list[dict]:
    found: list[dict] = []
    seen: set[tuple] = set()
    for u, v in zip(path, path[1:]):
        for link in pairs.get(_pair_key(u, v), []):
            key = _link_key(link)
            if key not in seen:
                seen.add(key)
                found.append(link)
    found.sort(key=lambda link: (
        link["device_a"], link["if_a"], link["device_b"], link["if_b"]))
    return found


def _telemetry_entry(name: str, telemetry_times: dict, now: dt.datetime,
                     stale_after: dt.timedelta) -> dict:
    latest = _naive_utc((telemetry_times or {}).get(name))
    if latest is None:
        return {"state": "missing", "latest_at": None,
                "note": "no stored telemetry sample for this device/source"}
    if latest > now:
        return {"state": "uncertain",
                "latest_at": latest.isoformat(sep=" "),
                "note": "latest stored sample timestamp is after the "
                        "analysis time"}
    if now - latest > stale_after:
        hours = max(1, int(stale_after.total_seconds() // 3600))
        return {"state": "stale", "latest_at": latest.isoformat(sep=" "),
                "note": f"latest stored sample is older than {hours}h"}
    return {"state": "fresh", "latest_at": latest.isoformat(sep=" "),
            "note": "latest stored sample is within the freshness window"}


def _telemetry_summary(telemetry: dict) -> dict:
    summary = {"fresh": 0, "stale": 0, "missing": 0, "uncertain": 0}
    for entry in telemetry.values():
        state = entry.get("state", "missing")
        summary[state] = summary.get(state, 0) + 1
    summary["total"] = len(telemetry)
    return summary


# ---------------------------------------------------------------------------
# Interface extraction
# ---------------------------------------------------------------------------
def _extract_interfaces(proposed_lines: list[str] | None) -> dict[str, list[str]]:
    """Map explicitly targeted interface names to configured/removed actions."""
    targets: dict[str, set[str]] = {}
    for raw in proposed_lines or []:
        line = normalize_line(raw)
        if not line:
            continue
        parts = line.split()
        head = parts[0].lower()
        if head == "interface" and len(parts) >= 2:
            targets.setdefault(parts[1], set()).add("configured")
        elif (head == "no" and len(parts) >= 3
              and parts[1].lower() == "interface"):
            targets.setdefault(parts[2], set()).add("removed")
    return {name: sorted(actions) for name, actions in sorted(targets.items())}


# ---------------------------------------------------------------------------
# Report skeleton
# ---------------------------------------------------------------------------
def _base_report(*, request_uuid, device_name, status, data_source,
                 risk_level, description, source, proposed_lines,
                 base_lines, diff=None) -> dict:
    declared_risk = (risk_level if risk_level in CHANGE_RISK_LEVELS
                     else "unknown")
    return {
        "request_uuid": request_uuid or None,
        "device": device_name or "",
        "status": status or "unknown",
        "data_source": data_source or "live",
        "description": description or None,
        "diff": diff or None,
        "declared_risk": declared_risk,
        "declared_risk_basis": (
            "risk level recorded on the change request (human-assigned; "
            "not a measured operational score)"),
        "topology_source": source,
        "topology_source_label": _source_detail(source),
        "topology_is_demo": source == TOPOLOGY_DEMO,
        "topology_is_live": False,
        "analysis_status": ANALYSIS_COMPLETE,
        "analysis_notes": [],
        "scope": {
            "target_device": device_name or "",
            "interfaces": [],
            "scope_kind": "device",
            "proposed_line_count": len(proposed_lines or []),
            "base_line_count": len(base_lines or []),
            "proposed_config_available": proposed_lines is not None,
            "declared_risk": declared_risk,
            "reachable_device_count": 0,
            "direct_neighbor_count": 0,
        },
        "direct_targets": [],
        "interfaces": [],
        "dependencies": [],
        "unknown_findings": [],
        "missing_information": [],
        "assumptions": [
            "The stored Link schema has no direction field, so links are "
            "treated as undirected; upstream/downstream cannot be "
            "distinguished.",
            "Device and link status values are the last stored observations, "
            "not live probes.",
            "Reachability is computed from documented Link rows only; no "
            "packets were sent and no device was contacted.",
            "An alternate path in the graph is structural evidence, not a "
            "guarantee that the path is available or carrying traffic.",
            "Routes, services and traffic flows are not represented in the "
            "stored topology, so service-level impact is unknown.",
            ("Demo topology is synthetic and must not be read as live "
             "production topology." if source == TOPOLOGY_DEMO else
             "Stored topology is not live production topology and may be "
             "stale relative to the lab."),
        ],
        "redundancy": {
            "target_is_articulation_point": False,
            "alternate_paths": [],
            "note": ("Structural analysis of the stored topology only; an "
                     "alternate path is not a live availability guarantee."),
        },
        "telemetry": {},
        "telemetry_summary": {"fresh": 0, "stale": 0, "missing": 0,
                              "uncertain": 0, "total": 0},
        "routes_and_services": {
            "represented": False,
            "note": ("The stored topology contains devices, links and link "
                     "subnets only. No route table, service inventory or "
                     "traffic flow is represented, so route/service impact "
                     "is unknown."),
        },
        "affected_subnets": [],
        "classification_counts": {"confirmed": 0, "potential": 0,
                                  "unknown": 0},
        "duplicate_links": 0,
        "malformed_rows": [],
    }


def _error_report(*, request_uuid, device_name, status, data_source,
                  risk_level, description, source, proposed_lines,
                  base_lines, diff, exc: Exception) -> dict:
    report = _base_report(
        request_uuid=request_uuid, device_name=device_name, status=status,
        data_source=data_source, risk_level=risk_level,
        description=description, source=source,
        proposed_lines=proposed_lines, base_lines=base_lines, diff=diff)
    report["analysis_status"] = ANALYSIS_ERROR
    report["analysis_notes"] = [
        f"Impact analysis could not run ({type(exc).__name__}); the approval "
        "and execution gates are unchanged."]
    report["unknown_findings"] = [_unknown(
        "analysis_error", device_name or "unknown",
        "The impact analysis raised an error, so no topology conclusion is "
        "available.", f"analysis error ({type(exc).__name__})")]
    report["missing_information"] = ["Impact analysis did not complete."]
    if device_name:
        report["direct_targets"] = [{
            "kind": "device", "element": device_name,
            "classification": CONFIRMED,
            "explanation": ("The change request names this device; the "
                            "topology impact could not be analyzed."),
            "evidence": "change request device_name",
            "path": [device_name],
            "uncertainty": ["impact analysis failed"],
        }]
        report["classification_counts"]["confirmed"] = 1
    report["classification_counts"]["unknown"] = 1
    return report


# ---------------------------------------------------------------------------
# Public pure analysis function
# ---------------------------------------------------------------------------
def analyze_change(
    *,
    device_name: str,
    proposed_lines: list[str] | None = None,
    base_lines: list[str] | None = None,
    request_uuid: str | None = None,
    status: str | None = None,
    data_source: str = "live",
    risk_level: str = "low",
    description: str | None = None,
    diff: str | None = None,
    devices: Sequence = (),
    links: Sequence = (),
    topology_source: str = TOPOLOGY_STORED,
    telemetry_times: dict[str, dt.datetime] | None = None,
    now: dt.datetime | None = None,
    stale_after: dt.timedelta = DEFAULT_STALE_AFTER,
    input_errors: Sequence[str] = (),
) -> dict:
    """Analyze one proposed change against stored Device/Link rows.

    This function is pure with respect to the database and the network: it
    receives already-read rows and returns a deterministic, JSON-serialisable
    report. It never writes, applies or connects.
    """
    device_name = _text(device_name)
    source = (TOPOLOGY_DEMO if _text(topology_source).lower() == TOPOLOGY_DEMO
              else TOPOLOGY_STORED)
    now = _naive_utc(now) or db_utcnow()
    stale_after = stale_after or DEFAULT_STALE_AFTER
    report = _base_report(
        request_uuid=request_uuid, device_name=device_name, status=status,
        data_source=data_source, risk_level=risk_level,
        description=description, source=source,
        proposed_lines=proposed_lines, base_lines=base_lines, diff=diff)

    device_rows, clean_links, malformed, duplicates = _normalise_topology(
        devices, links)
    g = _build_graph(device_rows, clean_links)
    pairs = _pair_links(clean_links)

    device_status = {name: row["status"]
                     for name, row in device_rows.items()}
    for link in clean_links:
        for name in (link["device_a"], link["device_b"]):
            device_status.setdefault(name, "unknown")

    report["malformed_rows"] = malformed
    report["duplicate_links"] = duplicates
    report["unknown_findings"].extend(
        _unknown("malformed_topology", "topology", note,
                 "stored device/link rows")
        for note in malformed)
    if duplicates:
        report["missing_information"].append(
            f"{duplicates} duplicate link row(s) were collapsed so impact "
            "entries are not duplicated.")
    if malformed:
        report["missing_information"].append(
            f"{len(malformed)} malformed topology row(s) were skipped.")

    # Provenance and input limitations are recorded before any early return,
    # so an insufficient-evidence report still states what is missing.
    if source == TOPOLOGY_DEMO:
        report["missing_information"].append(
            "Topology source is demo/synthetic; it is not live production "
            "topology.")
    else:
        report["missing_information"].append(
            "Topology is stored data; no live reachability probe was run.")
    if report["data_source"] == "demo" and source != TOPOLOGY_DEMO:
        report["missing_information"].append(
            "The change request uses demo data but the topology source is "
            "stored; provenance may not match.")
    if report["data_source"] != "demo" and source == TOPOLOGY_DEMO:
        report["missing_information"].append(
            "The change request data source is live but the topology source "
            "is demo; the impact evidence is demo-only.")
    for error in input_errors:
        report["unknown_findings"].append(_unknown(
            "input_error", device_name or "request", str(error),
            "stored change request config"))
        report["missing_information"].append(str(error))

    interface_targets = _extract_interfaces(proposed_lines)
    report["interfaces"] = sorted(interface_targets)
    report["scope"]["interfaces"] = sorted(interface_targets)
    report["scope"]["scope_kind"] = (
        "interface" if interface_targets else "device")

    # ---- Direct targets: the change request itself is the evidence ---------
    target_uncertainty: list[str] = []
    if device_name and device_name not in device_rows:
        target_uncertainty.append(
            "no Device row for the target in the stored topology")
    target_status = device_status.get(device_name, "unknown")
    if target_status == "down":
        target_uncertainty.append(
            "target device status is down in stored data")
    elif target_status != "up":
        target_uncertainty.append(
            f"target device status is {target_status}")

    if device_name:
        report["direct_targets"].append({
            "kind": "device", "element": device_name,
            "classification": CONFIRMED,
            "explanation": ("The change request applies the proposed "
                            f"configuration to {device_name}."),
            "evidence": "change request device_name",
            "path": [device_name],
            "uncertainty": sorted(set(target_uncertainty)),
        })

    for name, actions in interface_targets.items():
        linked = [
            link for link in clean_links
            if (link["device_a"] == device_name and link["if_a"] == name)
            or (link["device_b"] == device_name and link["if_b"] == name)
        ]
        uncertainty = []
        evidence = f"proposed config explicitly targets interface {name} " \
                   f"({', '.join(actions)})"
        if linked:
            evidence += "; stored link(s): " + "; ".join(
                _link_evidence(link) for link in linked)
        else:
            uncertainty.append(
                f"interface {name} on {device_name} is not referenced by any "
                "stored link; topology support is unknown")
        report["direct_targets"].append({
            "kind": "interface", "element": name,
            "classification": CONFIRMED,
            "explanation": (
                f"The proposed configuration explicitly targets interface "
                f"{name} on {device_name}."),
            "evidence": evidence,
            "path": [device_name],
            "uncertainty": uncertainty,
        })

    if proposed_lines is None:
        report["unknown_findings"].append(_unknown(
            "proposed_config_missing", device_name or "target",
            "The proposed configuration is unavailable, so interface-level "
            "targets cannot be determined.",
            "change request proposed_config_json"))
    elif not interface_targets:
        report["missing_information"].append(
            "The proposed configuration has no interface context; the scope "
            "is device-level and interface impact cannot be scoped.")

    # ---- Topology availability --------------------------------------------
    if not device_rows and not clean_links:
        report["analysis_status"] = ANALYSIS_INSUFFICIENT
        report["unknown_findings"].append(_unknown(
            "empty_topology", device_name or "topology",
            "No stored devices or links are available, so no dependency can "
            "be established. This is an insufficient-evidence result, not an "
            "empty impact result.",
            "stored devices/links tables"))
        report["missing_information"].append(
            "No stored topology is available (devices and links are empty).")
        report["analysis_notes"] = [
            "Impact analysis has insufficient evidence: no stored topology.",
            "No dependent device is claimed because no topology exists.",
        ]
        report["classification_counts"]["confirmed"] = sum(
            1 for item in report["direct_targets"]
            if item["classification"] == CONFIRMED)
        report["classification_counts"]["unknown"] = len(
            report["unknown_findings"])
        report["missing_information"] = sorted(set(report["missing_information"]))
        return report

    if device_name not in g:
        report["analysis_status"] = ANALYSIS_INSUFFICIENT
        report["unknown_findings"].append(_unknown(
            "target_missing", device_name or "target",
            f"Device {device_name or '(blank)'} is not present in the stored "
            "topology, so its dependencies cannot be established.",
            "stored devices/links tables"))
        report["missing_information"].append(
            f"Device {device_name or '(blank)'} is not present in the stored "
            "topology.")
        report["analysis_notes"] = [
            "Impact analysis has insufficient evidence: the target is not in "
            "the stored topology.",
            "No dependent device is claimed for a target that has no "
            "topology node.",
        ]
        report["classification_counts"]["confirmed"] = sum(
            1 for item in report["direct_targets"]
            if item["classification"] == CONFIRMED)
        report["classification_counts"]["unknown"] = len(
            report["unknown_findings"])
        report["missing_information"] = sorted(set(report["missing_information"]))
        return report

    # ---- Neighbours and reachable dependencies ---------------------------
    neighbours = sorted(g.neighbors(device_name))
    paths = _bfs_paths(g, device_name)
    dependencies: list[dict] = []

    for node in sorted(paths, key=lambda name: (len(paths[name]), name)):
        if node == device_name:
            continue
        path = paths[node]
        hop = len(path) - 1
        path_state, path_notes = _path_state(path, device_status, pairs)
        classification = (POTENTIAL if path_state in ("up", "uncertain")
                          else UNKNOWN)
        telemetry = _telemetry_entry(
            node, telemetry_times or {}, now, stale_after)
        uncertainty = list(path_notes)
        if telemetry["state"] != "fresh":
            uncertainty.append(telemetry["note"])
        if device_status.get(node, "unknown") not in ("up", "down"):
            uncertainty.append(
                f"device {node} has no 'up'/'down' observation")

        links = _path_links(path, pairs)
        if hop == 1:
            relationship = "direct neighbor"
            explanation = (
                f"{node} is directly connected to {device_name} by a "
                "documented link. A change on the target may affect this "
                "neighbor; no failure is observed or inferred.")
            evidence = "stored link(s): " + (
                "; ".join(_link_evidence(link) for link in links)
                if links else "none")
        else:
            relationship = "reachable dependency"
            explanation = (
                f"{node} is connected to {device_name} in {hop} hop(s) in "
                "the documented topology. The change may affect this "
                "dependency if the path is disrupted; no failure is observed "
                "or inferred.")
            evidence = (f"documented path {' → '.join(path)}; link(s): " +
                        ("; ".join(_link_evidence(link) for link in links)
                         if links else "none"))

        if classification == UNKNOWN:
            explanation += (" The documented path is not currently usable or "
                            "the evidence is insufficient, so the effect is "
                            "unknown, not confirmed.")

        dependencies.append({
            "element": node,
            "kind": "device",
            "classification": classification,
            "hop": hop,
            "relationship": relationship,
            "path": path,
            "path_state": path_state,
            "explanation": explanation,
            "evidence": evidence,
            "uncertainty": sorted(set(uncertainty)),
            "telemetry": telemetry,
        })

    dependencies.sort(key=lambda item: (
        _CLASS_RANK.get(item["classification"], 9), item["hop"],
        item["element"]))
    report["dependencies"] = dependencies
    report["scope"]["reachable_device_count"] = len(dependencies)
    report["scope"]["direct_neighbor_count"] = len(neighbours)

    if not neighbours:
        report["missing_information"].append(
            f"Target {device_name} has no documented links; neighbor and "
            "dependency impact is unknown.")
        report["unknown_findings"].append(_unknown(
            "target_isolated", device_name,
            "The target has no documented links, so no neighbor or "
            "dependency relationship can be established.",
            "stored links table"))

    blocked = [item for item in dependencies
               if item["classification"] == UNKNOWN]
    if blocked:
        report["unknown_findings"].append(_unknown(
            "blocked_paths", device_name,
            f"{len(blocked)} documented path(s) from {device_name} are "
            "blocked by a down link/device or missing link evidence; those "
            "devices are reported as unknown, not reachable.",
            "stored link/device status"))

    # ---- Telemetry -------------------------------------------------------
    telemetry_names = [device_name] + [item["element"]
                                       for item in dependencies]
    telemetry = {name: _telemetry_entry(
        name, telemetry_times or {}, now, stale_after)
        for name in sorted(set(telemetry_names))}
    report["telemetry"] = telemetry
    report["telemetry_summary"] = _telemetry_summary(telemetry)

    if telemetry[device_name]["state"] != "fresh":
        report["unknown_findings"].append(_unknown(
            "target_telemetry", device_name,
            f"Telemetry for the target is {telemetry[device_name]['state']}; "
            "its current operational state is not established.",
            f"stored telemetry for source '{report['data_source']}'"))
    if report["telemetry_summary"]["missing"]:
        report["missing_information"].append(
            f"No stored telemetry for {report['telemetry_summary']['missing']} "
            f"analyzed device(s) from source '{report['data_source']}'.")
    if report["telemetry_summary"]["stale"]:
        report["missing_information"].append(
            f"Stored telemetry is stale for "
            f"{report['telemetry_summary']['stale']} analyzed device(s).")
    if report["telemetry_summary"]["uncertain"]:
        report["missing_information"].append(
            f"Stored telemetry has an uncertain timestamp for "
            f"{report['telemetry_summary']['uncertain']} analyzed device(s).")

    # ---- Redundancy ------------------------------------------------------
    spof = graph.single_points_of_failure(g)
    bridge_pairs = {tuple(sorted(pair)) for pair in spof["bridge_links"]}
    alternate_paths = []
    for neighbour in neighbours:
        pair = _pair_key(device_name, neighbour)
        links = pairs.get(pair, [])
        if len(links) > 1:
            alternate = True
            basis = f"{len(links)} parallel documented links"
        elif pair in bridge_pairs:
            alternate = False
            basis = "the documented link is a bridge (single path)"
        else:
            alternate = True
            basis = ("the documented topology contains a cycle around this "
                     "link")
        alternate_paths.append({
            "neighbor": neighbour,
            "parallel_links": len(links),
            "alternate_path": alternate,
            "basis": basis,
            "link_statuses": sorted({link["status"] for link in links}),
            "note": ("Structural only; not a live availability guarantee."
                     if alternate else
                     "No alternate path is documented for this link."),
        })
    report["redundancy"] = {
        "target_is_articulation_point": (
            device_name in set(spof["articulation_nodes"])),
        "alternate_paths": alternate_paths,
        "note": ("Structural analysis of the stored topology only; an "
                 "alternate path is not a live availability guarantee."),
    }

    # ---- Subnets / routes / services ------------------------------------
    subnets = sorted({link["subnet"] for neighbour in neighbours
                      for link in pairs.get(
                          _pair_key(device_name, neighbour), [])
                      if link["subnet"]})
    report["affected_subnets"] = subnets
    report["unknown_findings"].append(_unknown(
        "service_impact", "routes/services",
        "Route tables, service inventory and traffic flows are not "
        "represented in the stored topology; service-level impact is unknown.",
        "stored topology schema (devices, links, subnets only)"))
    report["missing_information"].append(report["routes_and_services"]["note"])

    # ---- Classification counts ------------------------------------------
    report["classification_counts"] = {
        "confirmed": sum(1 for item in report["direct_targets"]
                         if item["classification"] == CONFIRMED),
        "potential": sum(1 for item in dependencies
                         if item["classification"] == POTENTIAL),
        "unknown": (sum(1 for item in dependencies
                        if item["classification"] == UNKNOWN)
                    + len(report["unknown_findings"])),
    }

    # ---- Analysis completeness ------------------------------------------
    data_unknown = [finding for finding in report["unknown_findings"]
                    if finding["kind"] != "service_impact"]
    reasons: list[str] = []
    if malformed:
        reasons.append(f"{len(malformed)} malformed row(s) skipped")
    if proposed_lines is None:
        reasons.append("proposed configuration unavailable")
    if not neighbours:
        reasons.append("target has no documented links")
    if blocked:
        reasons.append(f"{len(blocked)} documented path(s) blocked")
    if data_unknown:
        reasons.append(f"{len(data_unknown)} unknown-impact finding(s)")
    if report["telemetry_summary"]["stale"]:
        reasons.append("telemetry stale for at least one device")
    if report["telemetry_summary"]["missing"]:
        reasons.append("telemetry missing for at least one device")
    if report["telemetry_summary"]["uncertain"]:
        reasons.append("telemetry timestamp uncertain for at least one device")

    if reasons:
        report["analysis_status"] = ANALYSIS_PARTIAL
        report["analysis_notes"] = [
            "Impact analysis is partial: " + "; ".join(reasons) + ".",
            f"{len(dependencies)} documented reachable device(s) analyzed "
            f"from {device_name}.",
            f"Classification counts: confirmed="
            f"{report['classification_counts']['confirmed']}, potential="
            f"{report['classification_counts']['potential']}, unknown="
            f"{report['classification_counts']['unknown']}.",
        ]
    else:
        report["analysis_status"] = ANALYSIS_COMPLETE
        report["analysis_notes"] = [
            "Impact analysis completed against the stored topology.",
            f"{len(dependencies)} documented reachable device(s) analyzed "
            f"from {device_name}.",
            f"Classification counts: confirmed="
            f"{report['classification_counts']['confirmed']}, potential="
            f"{report['classification_counts']['potential']}, unknown="
            f"{report['classification_counts']['unknown']}.",
        ]
    if duplicates:
        report["analysis_notes"].append(
            f"{duplicates} duplicate link row(s) collapsed.")
    if malformed:
        report["analysis_notes"].append(
            f"{len(malformed)} malformed topology row(s) skipped.")

    report["missing_information"] = sorted(set(report["missing_information"]))
    report["unknown_findings"].sort(key=lambda finding: (
        finding["kind"], finding["element"], finding["explanation"]))
    return report


# ---------------------------------------------------------------------------
# Session-backed wrapper (read-only)
# ---------------------------------------------------------------------------
def analyze_request(session, request, *, topology_source: str | None = None,
                    now: dt.datetime | None = None,
                    stale_after: dt.timedelta = DEFAULT_STALE_AFTER) -> dict:
    """Read stored topology/telemetry and analyze one ChangeRequest.

    Any failure becomes an explicit ``error`` report. It never changes the
    request, never bypasses approval and never touches the executor.
    """
    device_name = _text(_get(request, "device_name", ""))
    request_uuid = _text(_get(request, "request_uuid", "")) or None
    status = _text(_get(request, "status", "")) or None
    data_source = _text(_get(request, "data_source", "live")) or "live"
    risk_level = _text(_get(request, "risk_level", "low")) or "low"
    description = _text(_get(request, "description", "")) or None
    diff = _text(_get(request, "diff", "")) or None
    source = (TOPOLOGY_DEMO if _text(topology_source).lower() == TOPOLOGY_DEMO
              else TOPOLOGY_STORED) if topology_source is not None else \
        topology_source_label(get_settings().demo_mode)

    proposed_lines, proposed_error = _json_list(
        _get(request, "proposed_config_json", None))
    base_lines, base_error = _json_list(
        _get(request, "base_config_json", None))
    input_errors = [error for error in (proposed_error, base_error) if error]

    try:
        devices = repository.get_devices(session)
        links = repository.get_links(session)
        telemetry_times = repository.get_latest_metric_times(
            session, data_source=data_source)
        return analyze_change(
            device_name=device_name, proposed_lines=proposed_lines,
            base_lines=base_lines, request_uuid=request_uuid, status=status,
            data_source=data_source, risk_level=risk_level,
            description=description, diff=diff, devices=devices, links=links,
            topology_source=source, telemetry_times=telemetry_times,
            now=now, stale_after=stale_after, input_errors=input_errors)
    except Exception as exc:  # fail closed as an explicit error report
        return _error_report(
            request_uuid=request_uuid, device_name=device_name, status=status,
            data_source=data_source, risk_level=risk_level,
            description=description, source=source,
            proposed_lines=proposed_lines, base_lines=base_lines, diff=diff,
            exc=exc)
