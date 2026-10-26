"""Focused Phase B2 tests for topology-aware change impact analysis.

The pure tests build Device/Link-like rows directly (no DB), and the wrapper
tests use the isolated ``session`` fixture. No test here touches a device, a
lab executor or the network. The analysis must be deterministic, read-only and
honest about missing/stale evidence.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from database import repository
from services import change_service, impact_service

NOW = dt.datetime(2026, 1, 1, 12, 0, 0)


def _dev(name, status="up", type_="router"):
    return SimpleNamespace(name=name, status=status, type=type_)


def _link(a, b, if_a="eth1", if_b="eth1", status="up",
          subnet="10.0.0.0/30"):
    return SimpleNamespace(device_a=a, device_b=b, if_a=if_a, if_b=if_b,
                           status=status, subnet=subnet)


def _fresh(devices):
    return {d.name: NOW - dt.timedelta(hours=1) for d in devices}


def _analyze(devices, links, **overrides):
    """Analyze R1 with fresh telemetry unless a test overrides it."""
    devices = list(devices)
    if "telemetry_times" not in overrides:
        overrides["telemetry_times"] = _fresh(devices)
    kwargs = dict(
        device_name="R1",
        proposed_lines=["hostname R1", "interface eth1"],
        base_lines=["hostname R1"],
        topology_source=impact_service.TOPOLOGY_STORED,
        now=NOW,
        devices=devices,
        links=list(links),
    )
    kwargs.update(overrides)
    return impact_service.analyze_change(**kwargs)


def _chain():
    devices = [_dev("R1"), _dev("R2"), _dev("R3"), _dev("PC1")]
    links = [_link("R1", "R2"), _link("R2", "R3"), _link("R3", "PC1")]
    return devices, links


def _dep(report, name):
    return next(item for item in report["dependencies"]
                if item["element"] == name)


# 1 -------------------------------------------------------------------------
def test_direct_target_and_interface_are_confirmed():
    devices, links = _chain()
    report = _analyze(devices, links)
    assert report["analysis_status"] == impact_service.ANALYSIS_COMPLETE

    device_target = report["direct_targets"][0]
    assert device_target["element"] == "R1"
    assert device_target["kind"] == "device"
    assert device_target["classification"] == impact_service.CONFIRMED

    assert report["interfaces"] == ["eth1"]
    interface_target = next(
        item for item in report["direct_targets"]
        if item["kind"] == "interface")
    assert interface_target["element"] == "eth1"
    assert interface_target["classification"] == impact_service.CONFIRMED
    assert "eth1" in interface_target["evidence"]


# 2 -------------------------------------------------------------------------
def test_connected_neighbor_is_identified_as_potential():
    devices, links = _chain()
    report = _analyze(devices, links)
    r2 = _dep(report, "R2")
    assert r2["classification"] == impact_service.POTENTIAL
    assert r2["classification"] != impact_service.CONFIRMED
    assert r2["hop"] == 1
    assert r2["path"] == ["R1", "R2"]
    assert r2["relationship"] == "direct neighbor"
    assert "eth1" in r2["evidence"]


# 3 -------------------------------------------------------------------------
def test_multi_hop_dependency_is_discovered():
    devices, links = _chain()
    report = _analyze(devices, links)
    r3 = _dep(report, "R3")
    assert r3["classification"] == impact_service.POTENTIAL
    assert r3["hop"] == 2
    assert r3["path"] == ["R1", "R2", "R3"]
    assert "documented path" in r3["evidence"]


# 4 -------------------------------------------------------------------------
def test_cycles_terminate_and_do_not_duplicate_entries():
    devices = [_dev("R1"), _dev("R2"), _dev("R3"), _dev("R4")]
    links = [_link("R1", "R2"), _link("R2", "R3"),
             _link("R3", "R1"), _link("R3", "R4")]
    report = _analyze(devices, links)
    assert {item["element"] for item in report["dependencies"]} == {
        "R2", "R3", "R4"}
    assert len(report["dependencies"]) == 3
    assert all(item["path"][0] == "R1" for item in report["dependencies"])


# 5 -------------------------------------------------------------------------
def test_disconnected_devices_are_not_reported_as_reachable():
    devices = [_dev("R1"), _dev("R2"), _dev("D1"),
               _dev("D2"), _dev("D3")]
    links = [_link("R1", "R2"), _link("D2", "D3")]
    report = _analyze(devices, links)
    reachable = {item["element"] for item in report["dependencies"]}
    assert reachable == {"R2"}
    assert not ({"D1", "D2", "D3"} & reachable)


# 6 -------------------------------------------------------------------------
def test_missing_and_malformed_topology_is_handled_safely():
    devices = [
        _dev("R1"), _dev("R2"),
        SimpleNamespace(name="", status="up", type="router"),
        SimpleNamespace(name="R3", status="not-a-status", type="router"),
    ]
    links = [
        _link("R1", "R2"),
        SimpleNamespace(device_a="", if_a="", device_b="R2",
                        if_b="eth2", status="up", subnet=None),
        SimpleNamespace(device_a="R2", if_a="eth3", device_b="R2",
                        if_b="eth3", status="up", subnet=None),
        _link("R1", "GHOST"),
    ]
    report = _analyze(devices, links)
    assert report["analysis_status"] == impact_service.ANALYSIS_PARTIAL
    assert report["malformed_rows"]
    assert any(finding["kind"] == "malformed_topology"
               for finding in report["unknown_findings"])
    # The analysis still returns a usable dependency for the valid rows.
    assert _dep(report, "R2")["classification"] == impact_service.POTENTIAL


def test_missing_target_is_insufficient_evidence_not_invented():
    devices = [_dev("R2")]
    links = [_link("R2", "R3")]
    report = _analyze(devices, links, device_name="GHOST")
    assert report["analysis_status"] == impact_service.ANALYSIS_INSUFFICIENT
    assert report["dependencies"] == []
    assert any(finding["kind"] == "target_missing"
               for finding in report["unknown_findings"])


# 7 -------------------------------------------------------------------------
def test_duplicate_links_do_not_duplicate_impact_entries():
    devices = [_dev("R1"), _dev("R2")]
    links = [_link("R1", "R2"), _link("R1", "R2")]
    report = _analyze(devices, links)
    entries = [item for item in report["dependencies"]
               if item["element"] == "R2"]
    assert len(entries) == 1
    assert report["duplicate_links"] == 1


def test_parallel_links_are_redundancy_not_duplicate_entries():
    devices = [_dev("R1"), _dev("R2")]
    links = [_link("R1", "R2", if_a="eth1", if_b="eth1"),
             _link("R1", "R2", if_a="eth2", if_b="eth2")]
    report = _analyze(devices, links)
    entries = [item for item in report["dependencies"]
               if item["element"] == "R2"]
    assert len(entries) == 1
    assert report["duplicate_links"] == 0
    alternate = report["redundancy"]["alternate_paths"][0]
    assert alternate["parallel_links"] == 2
    assert alternate["alternate_path"] is True


# 8 -------------------------------------------------------------------------
def test_same_inputs_produce_deterministic_output():
    devices, links = _chain()
    first = _analyze(devices, links)
    second = _analyze(list(reversed(devices)), list(reversed(links)))
    assert first == second


# 9 -------------------------------------------------------------------------
def test_empty_topology_is_explicitly_insufficient():
    report = _analyze([], [], telemetry_times={})
    assert report["analysis_status"] == impact_service.ANALYSIS_INSUFFICIENT
    assert report["dependencies"] == []
    assert any(finding["kind"] == "empty_topology"
               for finding in report["unknown_findings"])
    assert report["direct_targets"][0]["element"] == "R1"
    assert "no stored topology" in " ".join(
        report["analysis_notes"]).lower()


# 10 ------------------------------------------------------------------------
def test_demo_topology_is_labelled_and_never_called_live():
    devices, links = _chain()
    demo = _analyze(devices, links, topology_source="demo")
    assert demo["topology_source"] == impact_service.TOPOLOGY_DEMO
    assert demo["topology_is_demo"] is True
    assert demo["topology_is_live"] is False
    assert "demo" in demo["topology_source_label"].lower()
    assert any("demo" in item.lower() for item in demo["assumptions"])

    stored = _analyze(devices, links, topology_source="stored")
    assert stored["topology_source"] == impact_service.TOPOLOGY_STORED
    assert stored["topology_is_demo"] is False
    assert stored["topology_is_live"] is False
    assert stored["topology_source"] != "live"
    assert "not a live reachability test" in stored["topology_source_label"]


# 11 ------------------------------------------------------------------------
def test_missing_or_stale_telemetry_is_uncertainty_not_confirmed():
    devices, links = _chain()
    telemetry = {
        "R1": NOW - dt.timedelta(hours=48),   # stale
        "R2": NOW - dt.timedelta(hours=1),    # fresh
        # R3 and PC1 have no samples
    }
    report = _analyze(devices, links, telemetry_times=telemetry)
    assert report["analysis_status"] == impact_service.ANALYSIS_PARTIAL
    assert report["telemetry_summary"]["stale"] >= 1
    assert report["telemetry_summary"]["missing"] >= 1
    assert all(item["classification"] != impact_service.CONFIRMED
               for item in report["dependencies"])
    r3 = _dep(report, "R3")
    assert r3["classification"] == impact_service.POTENTIAL
    assert any("no stored telemetry" in note for note in r3["uncertainty"])
    assert any(finding["kind"] == "target_telemetry"
               for finding in report["unknown_findings"])


# 12 ------------------------------------------------------------------------
def test_analysis_error_does_not_bypass_approval_or_execution(
        session, monkeypatch):
    req = change_service.propose(
        session, device_name="R1", base_lines=["hostname R1"],
        proposed_lines=["hostname R1", "interface eth0"],
        proposed_by="alice", proposer_authenticated=True)
    before_events = len(repository.get_change_audit_events(session, req.id))

    def boom(*_args, **_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(repository, "get_devices", boom)
    report = impact_service.analyze_request(
        session, req, topology_source="stored", now=NOW)
    assert report["analysis_status"] == impact_service.ANALYSIS_ERROR
    assert any(finding["kind"] == "analysis_error"
               for finding in report["unknown_findings"])

    session.refresh(req)
    assert req.status == "validated"
    assert len(repository.get_change_audit_events(session, req.id)) == \
        before_events

    out, msg = change_service.approve(
        session, req.request_uuid, approved_by=None, authenticated=False)
    assert out.status == "validated"
    assert "authenticated" in msg

    out, msg = change_service.approve(
        session, req.request_uuid, approved_by="bob", authenticated=True)
    assert msg == "approved"

    res = change_service.apply(
        session, req.request_uuid, executor=lambda _d, _c: True)
    assert res["applied"] is False
    assert "disabled" in res["reason"]


def test_b1_self_approval_is_still_refused_after_analysis(session):
    req = change_service.propose(
        session, device_name="R1", base_lines=["hostname R1"],
        proposed_lines=["hostname R1", "interface eth0"],
        proposed_by="alice", proposer_authenticated=True)
    impact_service.analyze_request(session, req, topology_source="stored",
                                   now=NOW)
    out, msg = change_service.approve(
        session, req.request_uuid, approved_by="alice", authenticated=True)
    assert out.status == "validated"
    assert "self-approval" in msg
    assert out.approved_by is None


# 13 is exercised by tests/ui_smoke_check.py (report visible before approval).


# 14 ------------------------------------------------------------------------
def test_analysis_is_read_only_and_performs_no_device_io(session, monkeypatch):
    repository.upsert_device(session, "R1", type_="router", status="up")
    repository.upsert_device(session, "R2", type_="router", status="up")
    repository.upsert_link(session, "R1", "eth0", "R2", "eth0", status="up")
    session.commit()

    req = change_service.propose(
        session, device_name="R1", base_lines=["hostname R1"],
        proposed_lines=["hostname R1", "interface eth0"],
        proposed_by="alice", proposer_authenticated=True)
    before_status = req.status
    before_events = len(repository.get_change_audit_events(session, req.id))

    calls = []

    def fail(*_args, **_kwargs):
        calls.append("called")
        raise AssertionError("device I/O attempted during impact analysis")

    monkeypatch.setattr("network.lab_executor.LabExecutor.__init__", fail)
    monkeypatch.setattr("network.connectivity.run_ping", fail)
    monkeypatch.setattr("network.connectivity.run_traceroute", fail)

    report = impact_service.analyze_request(
        session, req, topology_source="stored", now=NOW)
    assert report["dependencies"]
    assert calls == []

    session.refresh(req)
    assert req.status == before_status
    assert len(repository.get_change_audit_events(session, req.id)) == \
        before_events


# 15 is covered by the existing B1 suites; these focused checks pin the
# approval/execution interactions above.


# ---------------------------------------------------------------------------
# Additional edge cases that keep the report honest
# ---------------------------------------------------------------------------
def test_interface_removal_is_extracted_from_proposed_config():
    devices, links = _chain()
    report = _analyze(devices, links,
                      proposed_lines=["no interface eth1"])
    interface = next(item for item in report["direct_targets"]
                     if item["kind"] == "interface")
    assert interface["element"] == "eth1"
    assert "removed" in interface["evidence"]


def test_target_without_links_is_partial_and_explicitly_unknown():
    devices = [_dev("R1"), _dev("R2")]
    report = _analyze(devices, [])
    assert report["analysis_status"] == impact_service.ANALYSIS_PARTIAL
    assert report["dependencies"] == []
    assert any(finding["kind"] == "target_isolated"
               for finding in report["unknown_findings"])


def test_routes_and_services_are_reported_as_unrepresented():
    devices, links = _chain()
    report = _analyze(devices, links)
    assert report["routes_and_services"]["represented"] is False
    assert any(finding["kind"] == "service_impact"
               for finding in report["unknown_findings"])
    assert "route" in report["routes_and_services"]["note"].lower()


def test_redundant_cycle_reports_an_alternate_path():
    devices = [_dev("R1"), _dev("R2"), _dev("R3")]
    links = [_link("R1", "R2"), _link("R2", "R3"), _link("R3", "R1")]
    report = _analyze(devices, links)
    alternate = next(item for item in report["redundancy"]["alternate_paths"]
                     if item["neighbor"] == "R2")
    assert alternate["alternate_path"] is True
    assert alternate["parallel_links"] == 1


def test_stored_rows_are_read_through_the_repository(session):
    repository.upsert_device(session, "R1", type_="router", status="up")
    repository.upsert_device(session, "R2", type_="router", status="up")
    repository.upsert_link(session, "R1", "eth0", "R2", "eth0", status="up")
    session.commit()
    req = change_service.propose(
        session, device_name="R1", base_lines=["hostname R1"],
        proposed_lines=["hostname R1", "interface eth0"],
        proposed_by="alice", proposer_authenticated=True)

    report = impact_service.analyze_request(
        session, req, topology_source="stored", now=NOW)
    assert _dep(report, "R2")["classification"] == impact_service.POTENTIAL
    assert report["topology_source"] == impact_service.TOPOLOGY_STORED
    assert "interface eth0" in (report["diff"] or "")
    session.refresh(req)
    assert req.status == "validated"


def test_invalid_stored_config_is_reported_not_crashing(session):
    request = SimpleNamespace(
        device_name="R1", request_uuid="bad-config", status="validated",
        data_source="demo", risk_level="low", description=None,
        proposed_config_json="not-json", base_config_json=None, diff=None)
    report = impact_service.analyze_request(
        session, request, topology_source="demo", now=NOW)
    assert report["analysis_status"] == impact_service.ANALYSIS_INSUFFICIENT
    assert any(finding["kind"] == "input_error"
               for finding in report["unknown_findings"])
    assert report["scope"]["proposed_config_available"] is False


@pytest.mark.parametrize("source", ["demo", "stored"])
def test_topology_source_never_becomes_live(source):
    devices, links = _chain()
    report = _analyze(devices, links, topology_source=source)
    assert report["topology_source"] in (
        impact_service.TOPOLOGY_DEMO, impact_service.TOPOLOGY_STORED)
    assert report["topology_is_live"] is False
