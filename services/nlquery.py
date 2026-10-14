"""Deterministic natural-language data query (offline) - Slice 5b.

Turns a plain operator question into a *structured read* over the repository
and the graph engine - no LLM, no fabrication. Every answer is derived from
real stored rows; if the intent is not recognised or the data is missing, the
result says so instead of inventing a number. This complements the Phase-3
Groq assistant: it gives instant, auditable answers to the common "what is the
state of the lab?" questions even with no API key configured.
"""
from __future__ import annotations

import re

from analytics import graph
from database import repository

#: Metric keyword -> substring to look for in stored metric names.
_METRIC_HINTS = {
    "latency": ("rtt", "latency"), "rtt": ("rtt", "latency"),
    "delay": ("rtt", "latency"), "loss": ("loss",),
    "packet loss": ("loss",), "cpu": ("cpu",),
    "memory": ("mem",), "mem": ("mem",),
    "bandwidth": ("rate", "bps", "throughput"), "throughput": ("rate", "bps"),
    "uptime": ("up", "uptime"),
}
#: Metrics for which a *higher* value is the *worse* one.
_HIGHER_IS_WORSE = ("rtt", "latency", "loss", "cpu", "mem", "delay")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _devices_in_text(names: list[str], text: str) -> list[str]:
    hits = [n for n in names if re.search(rf"(?<!\w){re.escape(n.lower())}(?!\w)", text)]
    # preserve the order they appear in the question
    hits.sort(key=lambda n: text.find(n.lower()))
    return hits


def _metric_token(text: str) -> str | None:
    for hint, subs in _METRIC_HINTS.items():
        if re.search(rf"(?<!\w){re.escape(hint)}(?!\w)", text):
            return hint
    return None


def _device_rows(devices) -> list[dict]:
    return [{"device": d.name, "type": d.type, "status": d.status,
             "lab_ip": d.lab_ip, "mgmt_ip": d.mgmt_ip} for d in devices]


def _latest_for(session, device_name, data_source):
    rows = repository.latest_metric_values_for_source(
        session, device_name, data_source)
    return [r for r in rows if r.value is not None]


def _rank_by_metric(session, device_names, token, data_source):
    subs = _METRIC_HINTS.get(token, (token,))
    ranked = []
    for name in device_names:
        best = None
        for r in _latest_for(session, name, data_source):
            m = r.metric.lower()
            if any(s in m for s in subs):
                if best is None or r.value > best.value:
                    best = r
        if best is not None:
            ranked.append({"device": name, "metric": best.metric,
                           "value": best.value, "unit": best.unit})
    return ranked


def answer(session, question: str, *, data_source: str = "live") -> dict:
    """Return ``{intent, understood, answer, rows, entities}`` for a question."""
    text = _norm(question)
    devices = list(repository.get_devices(session))
    names = [d.name for d in devices]
    result = {"intent": "unknown", "understood": False,
              "answer": "", "rows": [], "entities": []}

    if not text:
        result["answer"] = "No question given."
        return result

    named = _devices_in_text(names, text)
    result["entities"] = named

    # --- which devices are down / unreachable ------------------------------
    if any(w in text for w in ("down", "unreachable", "offline", "not up",
                               "dead", "lost")) and "count" not in text:
        result["intent"] = "devices_down"
        downs = [d for d in devices if d.status in ("down", "unknown")]
        result["rows"] = _device_rows(downs)
        result["understood"] = True
        result["answer"] = (
            f"{len(downs)} device(s) are down or of unknown state"
            if downs else "All devices are up.")
        return result

    # --- device count / inventory -----------------------------------------
    if ("how many device" in text or "count device" in text
            or "number of device" in text or text in ("devices", "inventory")):
        result["intent"] = "device_count"
        result["rows"] = _device_rows(devices)
        result["understood"] = True
        result["answer"] = f"{len(devices)} device(s) recorded."
        return result

    # --- alerts -------------------------------------------------------------
    if "alert" in text:
        result["intent"] = "alerts"
        events = repository.get_alert_events(
            session, status="open", data_source=data_source, limit=25)
        result["rows"] = [{
            "device": e.device_name, "metric": e.metric, "severity": e.severity,
            "value": e.value, "threshold": e.threshold,
            "condition": e.condition, "rule": e.rule_name,
            "last_fired (UTC)": e.last_fired_at} for e in events]
        result["understood"] = True
        result["answer"] = (f"{repository.count_open_alerts(session)} open/acked "
                            f"alert(s); showing {len(events)} for source "
                            f"'{data_source}'.")
        return result

    # --- single points of failure -----------------------------------------
    if ("single point" in text or "spof" in text or "articulation" in text
            or ("critical" in text and "node" in text)
            or "bridge" in text):
        result["intent"] = "spof"
        g = graph.build_from_session(session)
        spof = graph.single_points_of_failure(g)
        result["rows"] = ([{"kind": "device SPOF", "name": n}
                           for n in spof["articulation_nodes"]]
                          + [{"kind": "bridge link",
                              "name": "-".join(pair)}
                             for pair in spof["bridge_links"]])
        result["understood"] = True
        result["answer"] = (
            f"{len(spof['articulation_nodes'])} device SPOF(s), "
            f"{len(spof['bridge_links'])} bridge link(s).")
        return result

    # --- blast radius of a named device ------------------------------------
    if (("blast" in text or "impact" in text or "what breaks" in text
         or "if" in text and "fail" in text) and named):
        result["intent"] = "blast_radius"
        g = graph.build_from_session(session)
        impact = graph.blast_radius_node(g, named[0])
        result["rows"] = [{"device": d} for d in impact["affected"]]
        result["understood"] = True
        result["answer"] = (
            f"Losing {named[0]} disconnects {impact['lost_pairs']} reachable "
            f"pair(s); {len(impact['isolated'])} node(s) would be isolated.")
        return result

    # --- reachability between two named devices ----------------------------
    if (("reach" in text or "path" in text or "connect" in text
         or "talk to" in text) and len(named) >= 2):
        result["intent"] = "reachability"
        g = graph.build_from_session(session)
        path = graph.shortest_path(g, named[0], named[1])
        result["understood"] = True
        if path:
            result["rows"] = [{"hop": i + 1, "device": d}
                              for i, d in enumerate(path)]
            result["answer"] = (f"{named[0]} -> {named[1]} reachable via "
                                f"{' -> '.join(path)}.")
        else:
            result["answer"] = (f"No path between {named[0]} and {named[1]} in "
                                "the stored topology.")
        return result

    # --- extreme metric (worst / highest / lowest) -------------------------
    token = _metric_token(text)
    if token and any(w in text for w in ("worst", "highest", "max", "largest",
                                         "most", "best", "lowest", "min",
                                         "smallest", "least", "top", "which")):
        result["intent"] = "metric_extreme"
        ranked = _rank_by_metric(session, names, token, data_source)
        if ranked:
            higher_worse = token in _HIGHER_IS_WORSE
            want_highest = any(w in text for w in
                               ("worst", "highest", "max", "largest", "most",
                                "top"))
            reverse = want_highest if higher_worse else not want_highest
            ranked.sort(key=lambda r: r["value"], reverse=reverse)
            result["rows"] = ranked
            result["understood"] = True
            top = ranked[0]
            label = "worst" if (want_highest == higher_worse) else "best"
            result["answer"] = (f"{label} {token}: {top['device']} = "
                                f"{top['value']:.3g} {top['unit'] or ''}".strip())
        else:
            result["understood"] = True
            result["answer"] = (f"No stored '{token}' values for source "
                                f"'{data_source}'.")
        return result

    # --- status of one named device ----------------------------------------
    if named and ("status" in text or "how is" in text or "state" in text
                  or "info" in text):
        result["intent"] = "device_status"
        dev = repository.get_device_by_name(session, named[0])
        latest = _latest_for(session, named[0], data_source)
        result["rows"] = [{"metric": r.metric, "value": r.value,
                           "unit": r.unit} for r in latest]
        result["understood"] = True
        result["answer"] = (f"{named[0]} is {dev.status if dev else 'unknown'} "
                            f"with {len(latest)} recent metric(s).")
        return result

    # --- overall connectivity posture ---------------------------------------
    if ("connected" in text or "component" in text or "topology" in text
            or "partition" in text or "isolated" in text):
        result["intent"] = "connectivity"
        g = graph.build_from_session(session)
        summary = graph.connectivity_summary(g)
        result["rows"] = [{"device": n} for n in summary["isolated_nodes"]]
        result["understood"] = True
        result["answer"] = (
            f"{summary['nodes']} node(s), {summary['edges']} link(s), "
            f"{summary['components']} component(s); "
            f"{'fully connected' if summary['is_connected'] else 'PARTITIONED'}.")
        return result

    result["answer"] = ("Could not map this to a known data question. Try: "
                        "'which devices are down', 'how many devices', "
                        "'open alerts', 'single points of failure', "
                        "'can R1 reach R3', 'worst latency', 'status of R1', "
                        "'is the topology connected'.")
    return result
