"""Root-cause correlation (v2 Slice 4b).

Given a batch of anomaly events and the topology graph (``analytics.graph``),
this groups events that fire at nearly the same time on *network-connected*
devices into an incident cluster and ranks the most probable origin.

Ranking is a transparent heuristic, deliberately conservative:
* **precedence** - the earliest-seen anomaly in a cluster is the prime
  suspect (faults usually propagate outward from an origin in time);
* **centrality** - betweenness ties the score toward devices that sit on
  many paths (their failure fans out to more peers).

The output is a set of *probable* causes with reasons and the event ids that
support them. It is correlation, NOT proven causation - the deterministic
verifier and human review remain authoritative.
"""
from __future__ import annotations

import datetime as dt
from typing import Sequence

import networkx as nx


def _cluster_by_time(events: Sequence[dict], *, window_minutes: int) -> list[list[dict]]:
    """Greedy chain clustering of events sorted by first_seen_at.

    Each event dict needs ``first_seen_at`` (datetime) and ``device_name``.
    """
    ordered = sorted(events, key=lambda e: e["first_seen_at"])
    clusters: list[list[dict]] = []
    current: list[dict] = []
    window = dt.timedelta(minutes=window_minutes)
    for ev in ordered:
        if not current:
            current = [ev]
            continue
        if ev["first_seen_at"] - current[-1]["first_seen_at"] <= window:
            current.append(ev)
        else:
            clusters.append(current)
            current = [ev]
    if current:
        clusters.append(current)
    return clusters


def _split_by_component(cluster: list[dict], graph: nx.Graph) -> list[list[dict]]:
    """Split a time cluster into connected sub-groups (network locality)."""
    nodes = [e["device_name"] for e in cluster]
    present = [n for n in nodes if n in graph]
    groups: list[list[dict]] = []
    seen: set[str] = set()
    for n in present:
        if n in seen:
            continue
        comp = set(nx.node_connected_component(graph, n))
        members = [e for e in cluster
                   if e["device_name"] in comp and e["device_name"] not in seen]
        for e in members:
            seen.add(e["device_name"])
        if members:
            groups.append(members)
    # Devices with no graph node: keep as their own singleton groups so no
    # event is silently dropped.
    for e in cluster:
        if e["device_name"] not in present and e["device_name"] not in seen:
            groups.append([e])
            seen.add(e["device_name"])
    return groups


def correlate_root_causes(
    events: Sequence[dict],
    graph: nx.Graph,
    *,
    window_minutes: int = 10,
    precedence_weight: float = 0.6,
    centrality_weight: float = 0.4,
) -> list[dict]:
    """Rank probable root causes for correlated anomaly clusters.

    Returns one report per connected, multi-device time cluster (single,
    isolated anomalies are not "correlated" and are omitted). Each report:
    ``{devices, probable_cause, candidates, rationale, event_ids}``.
    """
    usable = [e for e in events if e.get("first_seen_at") and e.get("device_name")]
    betweenness = (nx.betweenness_centrality(graph)
                   if graph.number_of_nodes() else {})

    reports: list[dict] = []
    for tcluster in _cluster_by_time(usable, window_minutes=window_minutes):
        for group in _split_by_component(tcluster, graph):
            if len(group) < 2:
                continue
            by_dev = {}
            for e in group:
                by_dev.setdefault(e["device_name"], []).append(e)
            # earliest first-seen + best centrality per device
            scored = []
            for dev, evs in by_dev.items():
                earliest = min(ev["first_seen_at"] for ev in evs)
                cent = float(betweenness.get(dev, 0.0))
                scored.append({
                    "device": dev, "earliest": earliest, "centrality": cent,
                    "event_ids": [ev.get("id") for ev in evs],
                    "metrics": sorted({ev.get("metric") for ev in evs
                                       if ev.get("metric")}),
                })
            earliest_all = min(s["earliest"] for s in scored)
            latest_all = max(s["earliest"] for s in scored)
            spread = (latest_all - earliest_all).total_seconds() or 1.0
            for s in scored:
                prec = 1.0 - (s["earliest"] - earliest_all).total_seconds() / spread
                s["score"] = round(precedence_weight * prec
                                   + centrality_weight * s["centrality"], 4)
            scored.sort(key=lambda s: (-s["score"], s["earliest"]))
            top = scored[0]
            # Describe what actually drove the ranking - the winner is not
            # necessarily both earliest and most central.
            earliest = min(scored, key=lambda s: s["earliest"])
            max_centrality = max(s["centrality"] for s in scored)
            if top["device"] == earliest["device"]:
                timing_claim = (f"the earliest anomaly "
                                f"({top['earliest']:%H:%M:%S})")
            else:
                timing_claim = (f"not the earliest anomaly - "
                                f"{earliest['device']} was first at "
                                f"{earliest['earliest']:%H:%M:%S}")
            if max_centrality <= 0:
                centrality_claim = ("centrality is not decisive (all "
                                    "betweenness values are zero)")
            elif top["centrality"] >= max_centrality:
                centrality_claim = "the most central device in this cluster"
            else:
                centrality_claim = (
                    f"not the most central device (betweenness "
                    f"{top['centrality']:.3f} vs highest "
                    f"{max_centrality:.3f})")
            reports.append({
                "devices": sorted(by_dev),
                "probable_cause": top["device"],
                "candidates": scored,
                "rationale": (
                    f"{top['device']} is {timing_claim} and "
                    f"{centrality_claim}; ranked by precedence + "
                    "betweenness."),
                "event_ids": [i for s in scored for i in s["event_ids"]
                              if i is not None],
            })
    return reports
