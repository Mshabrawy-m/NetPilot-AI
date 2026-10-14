"""Topology service - graph data + documented status rules.

Reads devices/links from the database (which is populated from real lab
data by the collector, or from clearly-labelled demo fixtures). Contains
no SQL itself - all data access goes through database.repository.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import networkx as nx

from database import repository
from database.models import DEVICE_STATUSES, LINK_STATUSES  # noqa: F401 (doc)

#: ---------------------------------------------------------------------------
#: Network status rules (documented, used verbatim by UI and tests):
#:   healthy  - every device status is 'up'            (up == total > 0)
#:   degraded - at least 50% of devices are 'up'       (ceil(total/2) <= up < total)
#:   down     - fewer than 50% of devices are 'up'     (up < ceil(total/2))
#:   unknown  - no devices registered yet (total == 0)
#: ---------------------------------------------------------------------------

STATUS_COLORS = {
    "up": "#2e7d32", "down": "#c62828", "unknown": "#9e9e9e",
    "healthy": "#2e7d32", "degraded": "#f9a825",
}


@dataclass
class NetworkStatus:
    total: int
    up: int
    down: int
    unknown: int
    overall: str  # one of NETWORK_STATUSES values: healthy/degraded/down/unknown

    def summary(self) -> str:
        return (f"{self.up}/{self.total} devices up "
                f"({self.down} down, {self.unknown} unknown) -> {self.overall.upper()}")


def compute_network_status(devices: list) -> NetworkStatus:
    """Pure function implementing the documented rules above."""
    total = len(devices)
    up = sum(1 for d in devices if d.status == "up")
    down = sum(1 for d in devices if d.status == "down")
    unknown = total - up - down
    if total == 0:
        overall = "unknown"
    elif up == total:
        overall = "healthy"
    elif up >= math.ceil(total / 2):
        overall = "degraded"
    else:
        overall = "down"
    return NetworkStatus(total=total, up=up, down=down, unknown=unknown,
                         overall=overall)


def get_topology(session) -> dict:
    """Nodes/edges for the Topology page, generated from stored lab data."""
    devices = repository.get_devices(session)
    links = repository.get_links(session)
    nodes = [
        {"id": d.name, "label": d.name, "type": d.type, "status": d.status,
         "ip": d.lab_ip or d.mgmt_ip, "color": STATUS_COLORS.get(d.status, "#9e9e9e")}
        for d in devices
    ]
    edges = [
        {"source": l.device_a, "target": l.device_b,
         "label": f"{l.if_a} ↔ {l.if_b}", "status": l.status,
         "subnet": l.subnet,
         "color": STATUS_COLORS.get(l.status, "#9e9e9e")}
        for l in links
    ]
    return {"nodes": nodes, "edges": edges,
            "status": compute_network_status(list(devices))}


# ---------------------------------------------------------------------------
# Layout engines (Slice: multi-type topology views).
#   Pure functions over the node/edge dicts returned by ``get_topology`` so
#   they are unit-testable without a DB or UI. Every layout is DETERMINISTIC
#   (fixed seeds / anchors) so the same lab always renders identically.
# ---------------------------------------------------------------------------
LAYOUT_KINDS = ("layered", "force", "circular", "grid")


def degree_map(nodes: list, edges: list) -> dict:
    """Node id -> link count (isolated nodes are 0). Used to size markers."""
    deg = {n["id"]: 0 for n in nodes}
    for e in edges:
        deg[e["source"]] = deg.get(e["source"], 0) + 1
        deg[e["target"]] = deg.get(e["target"], 0) + 1
    return deg


def _to_graph(nodes: list, edges: list) -> nx.Graph:
    g = nx.Graph()
    g.add_nodes_from(n["id"] for n in nodes)
    g.add_edges_from((e["source"], e["target"]) for e in edges)
    return g


def _layered_positions(nodes: list, edges: list) -> dict:
    """Deterministic BFS depth tree: routers first, hosts fanning downward.
    The most-connected router is the root so backbone nodes sit on top."""
    g = _to_graph(nodes, edges)
    deg = dict(g.degree())
    routers = [n["id"] for n in nodes if n.get("type") == "router"]
    if routers:
        root = max(routers, key=lambda n: (deg.get(n, 0), n))
    elif nodes:
        root = max(nodes, key=lambda n: (deg.get(n["id"], 0), n["id"]))["id"]
    else:
        return {}
    depths: dict[str, int] = {root: 0}
    queue = [root]
    while queue:
        cur = queue.pop(0)
        for nbr in g.neighbors(cur):
            if nbr not in depths:
                depths[nbr] = depths[cur] + 1
                queue.append(nbr)
    # Disconnected or isolated nodes get a level below the deepest tier.
    floor = max(depths.values(), default=0) + 1
    for n in nodes:
        depths.setdefault(n["id"], floor)
    by_depth: dict[int, list[str]] = {}
    for name, depth in depths.items():
        by_depth.setdefault(depth, []).append(name)
    pos: dict[str, tuple[float, float]] = {}
    for depth, members in by_depth.items():
        members.sort()
        spread = len(members)
        for idx, member in enumerate(members):
            pos[member] = (idx - (spread - 1) / 2.0, -float(depth))
    return pos


def compute_layout(nodes: list, edges: list,
                   kind: str = "layered") -> dict:
    """Return ``{node_id: (x, y)}`` for the requested layout ``kind``.

    layered  - BFS tree (routers on top, hosts below)
    force    - spring/embedded (networkx, seeded)
    circular - every node on one ring (reveals adjacencies)
    grid     - tidy rows (best for very large fabrics)
    """
    if kind == "layered":
        return _layered_positions(nodes, edges)
    g = _to_graph(nodes, edges)
    if not g.number_of_nodes():
        return {}
    n = g.number_of_nodes()
    if kind == "grid":
        cols = math.ceil(math.sqrt(n)) or 1
        ids = sorted(g.nodes)
        return {name: (i % cols - (cols - 1) / 2.0,
                       -(i // cols) + (n // cols) / 2.0)
                for i, name in enumerate(ids)}
    if kind == "force":
        raw = nx.spring_layout(g, seed=7, k=1.2 / math.sqrt(max(n, 1)))
    elif kind == "circular":
        raw = nx.circular_layout(g)
    else:
        raise ValueError(f"unknown layout kind '{kind}'")
    return {k: (float(v[0]), float(v[1])) for k, v in raw.items()}
