"""Per-hop topology inspection uses stored graph and source-scoped telemetry."""
from __future__ import annotations

import datetime as dt

import pytest

from database import repository
from services.topology_path import PathInspectionError, inspect_path


def _path_fixture(session):
    for name in ("R1", "R2", "R3", "R4"):
        repository.upsert_device(session, name, type_="router", status="up")
    repository.upsert_link(session, "R1", "eth0", "R2", "eth0", status="up")
    repository.upsert_link(session, "R2", "eth1", "R3", "eth0", status="down")
    repository.add_metric_sample(
        session, "R2", "ping.avg_rtt_ms", 14.2, unit="ms",
        data_source="demo", collected_at=dt.datetime(2026, 10, 1, 0, 0))
    repository.add_metric_sample(
        session, "R2", "ping.avg_rtt_ms", 8.6, unit="ms",
        data_source="live", collected_at=dt.datetime(2026, 10, 1, 0, 0))
    session.commit()


def test_path_reports_per_hop_status_link_status_and_source_scoped_metrics(session):
    _path_fixture(session)
    result = inspect_path(session, "R1", "R3", data_source="live")
    assert result["status"] == "structural_path_found"
    assert result["path"] == ["R1", "R2", "R3"]
    middle = result["hops"][1]
    assert middle["device_status"] == "up"
    assert middle["next_link"]["status"] == "down"
    assert middle["measurements"][0]["value"] == 8.6
    assert middle["measurements"][0]["data_source"] == "live"
    assert "not a verified packet route" in result["caveat"]


def test_no_path_is_explicit_and_never_fabricates_hops(session):
    _path_fixture(session)
    result = inspect_path(session, "R1", "R4", data_source="demo")
    assert result["status"] == "no_documented_path"
    assert result["path"] == []
    assert result["hops"] == []


@pytest.mark.parametrize("source", ["mixed", "synthetic", ""])
def test_path_requires_an_explicit_supported_provenance(session, source):
    with pytest.raises(PathInspectionError, match="data_source"):
        inspect_path(session, "R1", "R2", data_source=source)


def test_unknown_targets_and_display_bound_refuse(session):
    _path_fixture(session)
    with pytest.raises(PathInspectionError, match="unknown device"):
        inspect_path(session, "R1", "NOT-A-DEVICE", data_source="live")
    with pytest.raises(PathInspectionError, match="display bound"):
        inspect_path(session, "R1", "R3", data_source="live", max_hops=1)
