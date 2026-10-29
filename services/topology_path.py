"""Read-only structural path inspection with provenance-scoped hop telemetry."""
from __future__ import annotations

from analytics import graph
from database import repository


class PathInspectionError(ValueError):
    """The requested path inspection has an invalid scope or target."""


def inspect_path(session, source: str, destination: str, *,
                 data_source: str, max_hops: int = 32) -> dict:
    """Return a shortest structural path and available per-hop observations.

    Reachability is calculated from recorded topology links. Metrics are read
    from only the requested ``demo`` or ``live`` provenance and are never
    treated as a synthetic connectivity probe.
    """
    if data_source not in {"demo", "live"}:
        raise PathInspectionError("data_source must be demo or live")
    if source == destination:
        raise PathInspectionError("choose two different devices")
    topology = graph.build_from_session(session)
    if source not in topology or destination not in topology:
        missing = [name for name in (source, destination)
                   if name not in topology]
        raise PathInspectionError("unknown device(s): " + ", ".join(missing))
    path = graph.shortest_path(topology, source, destination)
    if path is None:
        return {"status": "no_documented_path", "source": source,
                "destination": destination, "data_source": data_source,
                "path": [], "hops": [],
                "message": "No path exists in the stored topology graph."}
    if len(path) - 1 > max_hops:
        raise PathInspectionError(
            f"path has {len(path) - 1} hops; display bound is {max_hops}")

    hops = []
    for index, name in enumerate(path):
        attrs = topology.nodes[name]
        next_link = None
        if index + 1 < len(path):
            edge = topology.get_edge_data(name, path[index + 1]) or {}
            next_link = {"to": path[index + 1],
                         "status": edge.get("status", "unknown"),
                         "interface": (f"{edge.get('if_a') or '?'} ↔ "
                                       f"{edge.get('if_b') or '?'}")}
        measurements = repository.latest_metric_values_for_source(
            session, name, data_source)
        measurements = sorted(
            measurements,
            key=lambda row: (row.metric not in {
                "ping.avg_rtt_ms", "ping.loss_pct", "if.rx_bps",
                "if.tx_bps", "cpu.usage_pct", "memory.usage_pct"},
                             row.metric, row.interface_name or ""))
        latest = [{"metric": row.metric, "value": row.value,
                   "unit": row.unit, "interface": row.interface_name,
                   "collected_at": row.collected_at.isoformat()
                   if row.collected_at else None,
                   "data_source": row.data_source}
                  for row in measurements[:8]]
        hops.append({"index": index, "device": name,
                     "device_status": attrs.get("status", "unknown"),
                     "type": attrs.get("type"), "next_link": next_link,
                     "measurements": latest,
                     "omitted_metric_count": max(0, len(measurements) - 8)})
    return {"status": "structural_path_found", "source": source,
            "destination": destination, "data_source": data_source,
            "path": path, "hop_count": len(path) - 1, "hops": hops,
            "caveat": ("This is the shortest path in the stored topology, "
                       "not a verified packet route. Link status and hop "
                       "measurements are recorded evidence; timestamps show "
                       "freshness, and absent metrics are not inferred.")}
