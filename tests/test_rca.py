"""Tests for v2 Slice 4b root-cause correlation (analytics.rca)."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from analytics import graph, rca


def _dev(name):
    return SimpleNamespace(name=name, status="up", type="router")


def _link(a, b):
    return SimpleNamespace(device_a=a, device_b=b, if_a="e1", if_b="e1",
                           subnet=None, status="up")


def _ev(dev, metric, minutes, eid):
    return {"device_name": dev, "metric": metric, "id": eid,
            "first_seen_at": dt.datetime(2026, 10, 13, 12, 0, 0)
            + dt.timedelta(minutes=minutes)}


STAR = graph.build_graph([_dev("R1"), _dev("R2"), _dev("R3")],
                         [_link("R1", "R2"), _link("R1", "R3")])


def test_star_outbreak_blames_central_earliest_device():
    events = [_ev("R1", "ping.loss_pct", 0, 1),
              _ev("R2", "ping.loss_pct", 4, 2),
              _ev("R3", "ping.loss_pct", 7, 3)]
    reports = rca.correlate_root_causes(events, STAR, window_minutes=15)
    assert len(reports) == 1
    rep = reports[0]
    assert rep["probable_cause"] == "R1"          # earliest + highest betweenness
    assert rep["candidates"][0]["device"] == "R1"
    assert set(rep["event_ids"]) == {1, 2, 3}


def test_far_apart_events_do_not_cluster():
    events = [_ev("R1", "m", 0, 1), _ev("R2", "m", 90, 2)]
    reports = rca.correlate_root_causes(events, STAR, window_minutes=15)
    assert reports == []                            # no connected pair in-window


def test_disconnected_components_yield_separate_reports():
    two = graph.build_graph(
        [_dev("R1"), _dev("R2"), _dev("A"), _dev("B")],
        [_link("R1", "R2"), _link("A", "B")])
    events = [_ev("R1", "m", 0, 1), _ev("R2", "m", 3, 2),
              _ev("A", "m", 1, 3), _ev("B", "m", 5, 4)]
    reports = rca.correlate_root_causes(events, two, window_minutes=15)
    assert len(reports) == 2
    causes = {r["probable_cause"] for r in reports}
    assert "R1" in causes and "A" in causes        # each comp's earliest


def test_single_anomaly_is_not_reported_as_correlated():
    events = [_ev("R1", "m", 0, 1)]
    assert rca.correlate_root_causes(events, STAR) == []


def test_device_missing_from_graph_is_kept_as_singleton_not_merged():
    # GHOST is not a graph node; it must not be attributed into R1's cluster.
    events = [_ev("R1", "m", 0, 1), _ev("R2", "m", 2, 2),
              _ev("GHOST", "m", 1, 3)]
    reports = rca.correlate_root_causes(events, STAR, window_minutes=15)
    assert len(reports) == 1
    assert set(reports[0]["devices"]) == {"R1", "R2"}


def test_rationale_says_most_central_when_it_actually_is():
    events = [_ev("R1", "m", 0, 1), _ev("R2", "m", 4, 2), _ev("R3", "m", 7, 3)]
    rep = rca.correlate_root_causes(events, STAR, window_minutes=15)[0]
    assert rep["probable_cause"] == "R1"
    assert "earliest anomaly" in rep["rationale"]
    assert "most central" in rep["rationale"]


def test_rationale_never_claims_centrality_it_does_not_have():
    # R2 (a leaf, betweenness 0) is earliest and wins on precedence alone.
    events = [_ev("R2", "m", 0, 1), _ev("R3", "m", 4, 2), _ev("R1", "m", 7, 3)]
    rep = rca.correlate_root_causes(events, STAR, window_minutes=15)[0]
    assert rep["probable_cause"] == "R2"
    assert "not the most central" in rep["rationale"]
    assert "earliest anomaly" in rep["rationale"]


def test_scores_are_ordered_descending():
    events = [_ev("R1", "m", 0, 1), _ev("R2", "m", 4, 2), _ev("R3", "m", 6, 3)]
    rep = rca.correlate_root_causes(events, STAR)[0]
    scores = [c["score"] for c in rep["candidates"]]
    assert scores == sorted(scores, reverse=True)
