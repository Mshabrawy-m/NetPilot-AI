"""Topology graph intelligence (v2 Slice 1).

Pure graph algorithms over the lab topology already stored in the
``devices`` / ``links`` tables. This module does **no SQL** itself - it is
fed plain sequences of ORM rows (``Device`` / ``Link``) so the logic is
unit-testable with the standard ``session`` fixture. ``build_from_session``
is the thin convenience bridge that pulls rows through
``database.repository`` (the only place allowed to touch the DB).

Capabilities
------------
* ``reachable_from``        - which devices are reachable from one node.
* ``shortest_path``         - hop-by-hop path between two devices.
* ``blast_radius_node``/ ``blast_radius_link`` - impact of a failure.
* ``single_points_of_failure`` - articulation nodes + bridge links.

Everything is computed from the *documented* topology (Link rows), never
invented. If the graph is empty the functions return honest empty results.
"""
from __future__ import annotations

from itertools import combinations
from typing import Sequence

import networkx as nx


# --------------------------------------------------------------------------
# Graph construction
# --------------------------------------------------------------------------
def build_graph(devices: Sequence, links: Sequence) -> nx.Graph:
    """Build an undirected graph from Device/Link rows.

    Nodes are device names (carrying ``status``/``type``); edges connect the
    two endpoints of every link (carrying ``if_a``/``if_b``/``status``/
    ``subnet``). A link whose endpoint has no Device row still contributes a
    node, so reachability never silently drops a documented endpoint.
    """
    g = nx.Graph()
    for d in devices:
        g.add_node(d.name, status=getattr(d, "status", "unknown"),
                   type=getattr(d, "type", None))
    for l in links:
        for name in (l.device_a, l.device_b):
            if name not in g:
                g.add_node(name, status="unknown", type=None)
        g.add_edge(l.device_a, l.device_b, if_a=l.if_a, if_b=l.if_b,
                   status=getattr(l, "status", "unknown"), subnet=l.subnet)
    return g


def build_from_session(session) -> nx.Graph:
    """Convenience bridge: read devices/links through the repository."""
    from database import repository
    return build_graph(repository.get_devices(session),
                       repository.get_links(session))


# --------------------------------------------------------------------------
# Reachability & paths
# --------------------------------------------------------------------------
def _connected_sets(g: nx.Graph) -> dict[str, frozenset[str]]:
    """Map each node to the frozenset of nodes in its connected component."""
    comp_of: dict[str, frozenset[str]] = {}
    for comp in nx.connected_components(g):
        frozen = frozenset(comp)
        for n in comp:
            comp_of[n] = frozen
    return comp_of


def reachable_from(g: nx.Graph, source: str) -> set[str]:
    """Devices reachable from ``source`` (excluding itself). Empty if unknown."""
    if source not in g:
        return set()
    return set(nx.node_connected_component(g, source)) - {source}


def shortest_path(g: nx.Graph, a: str, b: str) -> list[str] | None:
    """Unweighted hop list from ``a`` to ``b``; ``None`` if not connected."""
    try:
        return list(nx.shortest_path(g, a, b))
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return None


def are_reachable(g: nx.Graph, a: str, b: str) -> bool:
    if a not in g or b not in g:
        return False
    return nx.has_path(g, a, b)


# --------------------------------------------------------------------------
# Failure impact (blast radius)
# --------------------------------------------------------------------------
def _impact(g: nx.Graph, *, removed_node: str | None = None,
            removed_edge: tuple[str, str] | None = None) -> dict:
    """Compute disconnected pairs after removing a node or an edge.

    Returns ``{lost_pairs, affected, isolated, reachable_from}`` where
    ``affected`` are the endpoints that lose connectivity to at least one
    peer and ``isolated`` are nodes left with no reachable neighbour.
    """
    before = _connected_sets(g)
    h = g.copy()
    if removed_node is not None:
        if removed_node not in h:
            return {"target": removed_node, "lost_pairs": 0,
                    "affected": [], "isolated": [], "changed": False}
        h.remove_node(removed_node)
    elif removed_edge is not None:
        u, v = removed_edge
        if not h.has_edge(u, v):
            return {"target": removed_edge, "lost_pairs": 0,
                    "affected": [], "isolated": [], "changed": False}
        h.remove_edge(u, v)
    else:
        raise ValueError("either removed_node or removed_edge must be given")

    after = _connected_sets(h)
    nodes = [n for n in before if n != removed_node]
    lost_pairs = 0
    affected: set[str] = set()
    for a, b in combinations(nodes, 2):
        connected_before = b in before.get(a, frozenset())
        connected_after = b in after.get(a, frozenset())
        if connected_before and not connected_after:
            lost_pairs += 1
            affected.add(a)
            affected.add(b)
    isolated = sorted(
        n for n in nodes if len(after.get(n, frozenset([n]))) <= 1)
    return {"target": removed_node if removed_node is not None else removed_edge,
            "lost_pairs": lost_pairs,
            "affected": sorted(affected),
            "isolated": isolated,
            "changed": lost_pairs > 0}


def blast_radius_node(g: nx.Graph, node: str) -> dict:
    """Impact of losing ``node`` (device failure) on reachability."""
    return _impact(g, removed_node=node)


def blast_radius_link(g: nx.Graph, a: str, b: str) -> dict:
    """Impact of losing the ``a``-``b`` link (cable/interface failure)."""
    return _impact(g, removed_edge=(a, b))


# --------------------------------------------------------------------------
# Structural risk
# --------------------------------------------------------------------------
def single_points_of_failure(g: nx.Graph) -> dict:
    """Articulation nodes and bridge links whose removal disconnects the lab.

    ``articulation_nodes`` are devices that are a SPOF for some pair;
    ``bridge_links`` are (unordered) device pairs joined by a single edge.
    """
    artics = sorted(nx.articulation_points(g)) if g.number_of_nodes() else []
    bridges = sorted(tuple(sorted(e)) for e in nx.bridges(g)) if g.number_of_edges() else []
    return {"articulation_nodes": artics, "bridge_links": list(bridges)}


def connectivity_summary(g: nx.Graph) -> dict:
    """High-level posture: node/edge counts, components, isolated nodes."""
    components = [sorted(c) for c in nx.connected_components(g)]
    isolated = [n for n in g.nodes if g.degree(n) == 0]
    return {
        "nodes": g.number_of_nodes(),
        "edges": g.number_of_edges(),
        "components": len(components),
        "isolated_nodes": isolated,
        "is_connected": g.number_of_nodes() <= 1 or nx.is_connected(g),
    }
