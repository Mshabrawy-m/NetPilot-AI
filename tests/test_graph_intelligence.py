"""Tests for the v2 topology graph intelligence (analytics.graph).

These build small topologies straight from Device/Link ORM objects (no DB
required for the pure algorithms) and one test through the repository to
prove ``build_from_session`` wires up correctly.
"""
from __future__ import annotations

from types import SimpleNamespace

from analytics import graph


def _dev(name, status="up"):
    return SimpleNamespace(name=name, status=status, type="router")


def _link(a, b, if_a="eth1", if_b="eth1", subnet="10.0.0.0/30", status="up"):
    return SimpleNamespace(device_a=a, device_b=b, if_a=if_a, if_b=if_b,
                           subnet=subnet, status=status)


# A star: R1 in the middle, R2 and R3 leaves, plus PC1 hanging off R3.
STAR_DEVICES = [_dev("R1"), _dev("R2"), _dev("R3"), _dev("PC1")]
STAR_LINKS = [
    _link("R1", "R2"),
    _link("R1", "R3"),
    _link("R3", "PC1"),
]


def _star():
    return graph.build_graph(STAR_DEVICES, STAR_LINKS)


def test_build_graph_nodes_and_edges():
    g = _star()
    assert set(g.nodes) == {"R1", "R2", "R3", "PC1"}
    assert g.number_of_edges() == 3


def test_reachable_from_covers_whole_component():
    g = _star()
    assert graph.reachable_from(g, "PC1") == {"R1", "R2", "R3"}
    # Unknown node yields an honest empty set, never an exception.
    assert graph.reachable_from(g, "GHOST") == set()


def test_shortest_path_is_hop_list_or_none():
    g = _star()
    assert graph.shortest_path(g, "PC1", "R2") == ["PC1", "R3", "R1", "R2"]
    assert graph.shortest_path(g, "PC1", "GHOST") is None


def test_are_reachable_and_disconnected_case():
    g = graph.build_graph([_dev("A"), _dev("B")], [])
    assert graph.are_reachable(g, "A", "B") is False
    g2 = graph.build_graph([_dev("A"), _dev("B")], [_link("A", "B")])
    assert graph.are_reachable(g2, "A", "B") is True


def test_star_center_is_single_point_of_failure():
    g = _star()
    spof = graph.single_points_of_failure(g)
    assert "R1" in spof["articulation_nodes"]
    assert "R3" in spof["articulation_nodes"]  # its removal isolates PC1
    # Every edge of a tree is a bridge.
    assert len(spof["bridge_links"]) == 3


def test_blast_radius_node_disconnects_peers():
    g = _star()
    impact = graph.blast_radius_node(g, "R1")
    assert impact["changed"] is True
    assert impact["lost_pairs"] > 0
    # Removing R1 splits {R2} from {R3, PC1}: both groups are affected.
    assert set(impact["affected"]) >= {"R2", "R3", "PC1"}


def test_blast_radius_link_isolates_downstream_host():
    g = _star()
    impact = graph.blast_radius_link(g, "R3", "PC1")
    assert impact["changed"] is True
    assert "PC1" in impact["isolated"]


def test_blast_radius_redundant_link_has_no_impact():
    # Triangle R1-R2-R3-R1: removing any single link keeps everything reachable.
    g = graph.build_graph(
        [_dev("R1"), _dev("R2"), _dev("R3")],
        [_link("R1", "R2"), _link("R2", "R3"), _link("R3", "R1")],
    )
    impact = graph.blast_radius_link(g, "R1", "R2")
    assert impact["changed"] is False
    assert impact["lost_pairs"] == 0
    spof = graph.single_points_of_failure(g)
    assert spof["articulation_nodes"] == []
    assert spof["bridge_links"] == []


def test_connectivity_summary_detects_isolated_node():
    g = graph.build_graph(
        [_dev("R1"), _dev("R2"), _dev("ISOLATED")],
        [_link("R1", "R2")],
    )
    summary = graph.connectivity_summary(g)
    assert summary["nodes"] == 3
    assert summary["components"] == 2
    assert summary["is_connected"] is False
    assert "ISOLATED" in summary["isolated_nodes"]


def test_link_endpoint_without_device_row_still_present():
    # A documented link to an unregistered device must not be dropped.
    g = graph.build_graph([_dev("R1")], [_link("R1", "R9")])
    assert "R9" in g.nodes
    assert graph.are_reachable(g, "R1", "R9") is True


def test_build_from_session_reads_repository(session):
    from database import repository
    repository.upsert_device(session, name="R1", type_="router", status="up")
    repository.upsert_device(session, name="R2", type_="router", status="up")
    repository.upsert_link(session, device_a="R1", if_a="eth1",
                           device_b="R2", if_b="eth1", subnet="10.0.0.0/30",
                           status="up")
    session.commit()
    g = graph.build_from_session(session)
    assert graph.are_reachable(g, "R1", "R2") is True
    assert graph.shortest_path(g, "R1", "R2") == ["R1", "R2"]
