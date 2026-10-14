"""Tests for the multi-layout topology views and the demo fabric generator.

Layout functions are pure (operate on node/edge dicts) so they need no DB;
the fabric test uses the demo-mode fixture and never touches a live lab.
"""
from __future__ import annotations

import math

import pytest

from services import topology_service as ts

# A tiny but connected lab: R1 (router) backbone, S1 switch, H1/H2 hosts.
NODES = [
    {"id": "R1", "label": "R1", "type": "router", "status": "up", "ip": "1", "color": "c"},
    {"id": "R2", "label": "R2", "type": "router", "status": "up", "ip": "2", "color": "c"},
    {"id": "S1", "label": "S1", "type": "switch", "status": "up", "ip": "3", "color": "c"},
    {"id": "H1", "label": "H1", "type": "host", "status": "up", "ip": "4", "color": "c"},
    {"id": "H2", "label": "H2", "type": "host", "status": "down", "ip": "5", "color": "c"},
]
EDGES = [
    {"source": "R1", "target": "R2", "label": "l", "status": "up", "subnet": "s", "color": "c"},
    {"source": "R1", "target": "S1", "label": "l", "status": "up", "subnet": "s", "color": "c"},
    {"source": "S1", "target": "H1", "label": "l", "status": "up", "subnet": "s", "color": "c"},
    {"source": "S1", "target": "H2", "label": "l", "status": "down", "subnet": "s", "color": "c"},
]


# ---------------------------------------------------------------- layouts
@pytest.mark.parametrize("kind", ts.LAYOUT_KINDS)
def test_every_layout_positions_all_nodes(kind):
    pos = ts.compute_layout(NODES, EDGES, kind=kind)
    assert set(pos) == {n["id"] for n in NODES}
    for x, y in pos.values():
        assert math.isfinite(x) and math.isfinite(y)


@pytest.mark.parametrize("kind", ts.LAYOUT_KINDS)
def test_layouts_are_deterministic(kind):
    a = ts.compute_layout(NODES, EDGES, kind=kind)
    b = ts.compute_layout(NODES, EDGES, kind=kind)
    assert a == b


def test_layered_puts_backbone_on_top():
    pos = ts.compute_layout(NODES, EDGES, kind="layered")
    # The root router has the highest y; hosts sit lower than it.
    assert pos["R1"][1] == max(p[1] for p in pos.values())
    assert pos["H1"][1] < pos["R1"][1]


def test_circular_lays_nodes_on_a_ring():
    pos = ts.compute_layout(NODES, EDGES, kind="circular")
    cx = sum(p[0] for p in pos.values()) / len(pos)
    cy = sum(p[1] for p in pos.values()) / len(pos)
    radii = [math.hypot(p[0] - cx, p[1] - cy) for p in pos.values()]
    assert max(radii) - min(radii) < 1e-6      # all equidistant from centre


def test_grid_uses_distinct_cells():
    pos = ts.compute_layout(NODES, EDGES, kind="grid")
    assert len(set(pos.values())) == len(NODES)


def test_unknown_layout_raises():
    with pytest.raises(ValueError):
        ts.compute_layout(NODES, EDGES, kind="spiral")


def test_empty_graph_returns_no_positions():
    assert ts.compute_layout([], [], kind="force") == {}


def test_degree_map_counts_links():
    deg = ts.degree_map(NODES, EDGES)
    assert deg["R1"] == 2 and deg["S1"] == 3 and deg["H1"] == 1


# ----------------------------------------------------------------- fabric
@pytest.fixture()
def demo_env(tmp_path, monkeypatch):
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "true")
    monkeypatch.setenv("DATABASE_URL",
                       f"sqlite:///{(tmp_path / 'fabric.db').as_posix()}")
    return tmp_path


def test_fabric_refused_outside_demo_mode(db_url):
    from services.demo_data import seed_demo_fabric
    with pytest.raises(RuntimeError):
        seed_demo_fabric()


def test_fabric_invalid_args(demo_env):
    from services.demo_data import seed_demo_fabric
    with pytest.raises(ValueError):
        seed_demo_fabric(core=0)


def test_fabric_builds_large_connected_demo(demo_env):
    from database import repository
    from database.connection import get_session, init_db
    from services.demo_data import seed_demo_fabric
    from analytics import graph

    init_db()
    counts = seed_demo_fabric(core=2, dist=3, access=4, hosts_per_access=2)
    # 2 + 3 + 4 + 8 hosts = 17 devices; every tier above has uplinks.
    assert counts["devices"] == 17
    assert counts["links"] > 0 and counts["samples"] > 0

    session = get_session()
    try:
        devices = repository.get_devices(session)
        assert {d.type for d in devices} == {"router", "switch", "host"}
        # A down device is always present so degraded/down rendering is real.
        assert any(d.status == "down" for d in devices)
        # Every stored metric sample is labelled demo (provenance kept).
        metrics = repository.get_available_metrics(session)
        assert metrics and all(src == "demo" for *_x, src in metrics)
        # Backbone (core+dist) is reachable - not a scatter of islands.
        g = graph.build_from_session(session)
        assert g.has_edge("CORE1", "DIST1")
    finally:
        session.close()


def test_fabric_is_deterministic(demo_env):
    from services.demo_data import seed_demo_fabric
    from database.connection import init_db
    init_db()
    a = seed_demo_fabric(core=2, dist=3, access=4, hosts_per_access=2)
    b = seed_demo_fabric(core=2, dist=3, access=4, hosts_per_access=2)
    assert a == b        # idempotent upserts, same seed -> same counts
