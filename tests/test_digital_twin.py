"""Phase D1 Digital Twin engine tests.

These tests pin the deterministic, isolated simulation contract:

* it models only documented interface admin state on a topology snapshot;
* it never contacts a device, sends a packet, runs a subprocess or applies
  configuration;
* unknown/unsupported evidence is reported honestly, never as success;
* a simulation result never changes approval, expiry, authorization or
  execution state.

The tests use plain in-memory rows, so they are fully offline.
"""
from __future__ import annotations

import ast
import copy
import json
import os
import socket
import subprocess
from pathlib import Path

import pytest

from services import digital_twin as dt

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
def _devices():
    return [
        {"name": "R1", "type": "router", "status": "up"},
        {"name": "R2", "type": "router", "status": "up"},
        {"name": "R3", "type": "router", "status": "up"},
        {"name": "PC1", "type": "host", "status": "up"},
    ]


def _links():
    return [
        {"device_a": "R1", "if_a": "eth1", "device_b": "R2", "if_b": "eth1",
         "subnet": "10.0.12.0/30", "status": "up"},
        {"device_a": "R2", "if_a": "eth2", "device_b": "R3", "if_b": "eth1",
         "subnet": "10.0.23.0/30", "status": "up"},
        {"device_a": "R2", "if_a": "eth3", "device_b": "PC1", "if_b": "eth0",
         "subnet": "10.0.2.0/24", "status": "up"},
    ]


def _simulate(*, device="R2", lines=None, devices=None, links=None,
              **kwargs):
    return dt.simulate_change(
        devices=_devices() if devices is None else devices,
        links=_links() if links is None else links,
        device_name=device,
        proposed_lines=["interface eth3", "shutdown"] if lines is None
        else lines,
        topology_ref="snapshot:test",
        **kwargs)


# ---------------------------------------------------------------------------
# Core simulation behavior
# ---------------------------------------------------------------------------
def test_valid_shutdown_disconnects_a_leaf():
    report = _simulate()

    assert report["status"] == dt.STATUS_SIMULATED
    assert report["simulator"] == dt.SIMULATOR
    assert report["not_proof_of_safety"] is True
    assert report["configuration_applied"] is False
    assert report["live_network_tested"] is False
    assert report["affected_components"]["lost_reachability_from_target"] == [
        "PC1"]
    assert report["affected_components"]["newly_isolated_devices"] == ["PC1"]
    assert report["before"]["reachable_from_target"] == ["PC1", "R1", "R3"]
    assert report["after"]["reachable_from_target"] == ["R1", "R3"]
    assert report["topology_ref"] == "snapshot:test"
    assert report["topology_fingerprint"].startswith("sha256:")
    assert report["change_ref"]["device"] == "R2"
    assert report["change_ref"]["fingerprint"].startswith("sha256:")
    assert report["operations"]
    assert report["unsupported_operations"] == []


def test_change_with_no_modeled_impact():
    links = _links()
    links[0]["status"] = "down"
    report = _simulate(device="R1", lines=["interface eth1", "shutdown"],
                       links=links)

    assert report["status"] == dt.STATUS_SIMULATED
    assert report["affected_components"]["lost_reachability_from_target"] == []
    assert report["affected_components"]["lost_pairs"] == 0
    assert any(c["kind"] == "link_already_down"
               for c in report["conflicts"])
    assert any("no documented reachability change" in reason
               for reason in report["status_reasons"])


def test_disconnects_a_previously_reachable_component():
    report = _simulate(device="R1", lines=["interface eth1", "shutdown"])

    assert report["status"] == dt.STATUS_SIMULATED
    lost = report["affected_components"]["lost_reachability_from_target"]
    assert lost == ["PC1", "R2", "R3"]
    assert report["affected_components"]["newly_isolated_devices"] == ["R1"]
    assert report["affected_components"]["lost_pairs"] > 0
    assert report["affected_components"]["split_components"]


def test_cycle_preserves_reachability_via_alternate_path():
    links = _links() + [
        {"device_a": "R1", "if_a": "eth9", "device_b": "R3", "if_b": "eth9",
         "subnet": "10.0.13.0/30", "status": "up"},
    ]
    report = _simulate(device="R1", lines=["interface eth1", "shutdown"],
                       links=links)

    assert report["status"] == dt.STATUS_SIMULATED
    assert report["affected_components"]["lost_reachability_from_target"] == []
    assert report["affected_components"]["lost_pairs"] == 0
    alternates = report["redundancy"]["alternate_paths"]
    assert alternates and alternates[0]["alternate_path_exists"] is True
    assert alternates[0]["alternate_path"] == ["R1", "R3", "R2"]


def test_missing_device_is_invalid():
    report = _simulate(device="GHOST")

    assert report["status"] == dt.STATUS_INVALID
    assert any("not present in the topology" in reason
               for reason in report["status_reasons"])
    assert report["affected_components"]["lost_reachability_from_target"] == []


def test_disconnected_device_is_insufficient_evidence():
    devices = _devices() + [{"name": "R4", "type": "router", "status": "up"}]
    report = _simulate(device="R4", lines=["interface eth1", "shutdown"],
                       devices=devices)

    assert report["status"] == dt.STATUS_INSUFFICIENT
    assert any("no documented links" in reason
               for reason in report["status_reasons"])
    assert report["checks"][2]["name"] == "target_has_documented_links"
    assert report["checks"][2]["passed"] is False


def test_undocumented_interface_is_partial_not_success():
    report = _simulate(device="R2", lines=["interface eth99", "shutdown"])

    assert report["status"] == dt.STATUS_PARTIAL
    assert any(c["kind"] == "undocumented_interface"
               for c in report["conflicts"])
    assert report["affected_components"]["lost_reachability_from_target"] == []
    assert report["interface_effects"][0]["effect"] == "unknown"


def test_unsupported_operation_is_not_simulated():
    report = _simulate(device="R2",
                       lines=["interface eth3", "ip address 10.9.9.1/24"])

    assert report["status"] == dt.STATUS_PARTIAL
    assert len(report["unsupported_operations"]) == 1
    assert report["unsupported_operations"][0]["line"].startswith("ip address")
    assert report["operations"]
    # The supported context is still reported, but no success is claimed.
    assert report["checks"][3]["passed"] is False


def test_only_unsupported_operations_are_not_simulated():
    report = _simulate(device="R2", lines=["ip address 10.9.9.1/24"])

    assert report["status"] == dt.STATUS_NOT_SIMULATED
    assert report["affected_components"]["lost_reachability_from_target"] == []


def test_duplicate_links_are_collapsed_and_parallel_links_kept():
    links = _links() + [
        # Exact duplicate: collapsed, reported.
        dict(_links()[0]),
        # Genuine parallel link: kept as redundancy.
        {"device_a": "R1", "if_a": "eth2", "device_b": "R2", "if_b": "eth2",
         "subnet": "10.0.12.4/30", "status": "up"},
    ]
    report = _simulate(device="R1", lines=["interface eth1", "shutdown"],
                       links=links)

    assert any(c["kind"] == "duplicate_link" for c in report["conflicts"])
    # The parallel link keeps R1 reachable, so no loss is claimed.
    assert report["status"] == dt.STATUS_PARTIAL
    assert report["affected_components"]["lost_reachability_from_target"] == []
    alt = report["redundancy"]["alternate_paths"][0]
    assert alt["parallel_links"] == 2  # two distinct links; duplicate collapsed
    assert alt["alternate_path_exists"] is True


def test_parallel_links_are_not_reported_as_bridges():
    links = _links() + [
        {"device_a": "R1", "if_a": "eth2", "device_b": "R2", "if_b": "eth2",
         "subnet": "10.0.12.4/30", "status": "up"},
    ]
    report = dt.simulate_change(
        devices=_devices(), links=links, device_name="R2",
        proposed_lines=["hostname R2"])
    bridge_links = [tuple(pair) for pair in
                    report["redundancy"]["bridge_links"]]

    # A pair with parallel links is redundant, not a single point of failure.
    assert ("R1", "R2") not in bridge_links
    assert ("PC1", "R2") in bridge_links


def test_malformed_topology_is_explicit():
    report = dt.simulate_change(
        devices="not-a-list", links=_links(), device_name="R2",
        proposed_lines=["interface eth3", "shutdown"])

    assert report["status"] == dt.STATUS_INVALID
    assert any(c["kind"] == "topology_not_sequence"
               for c in report["conflicts"])


def test_malformed_rows_are_skipped_and_reported():
    devices = _devices() + [{"name": "", "status": "up"}]
    links = _links() + [
        {"device_a": "", "if_a": "x", "device_b": "R2", "if_b": "y",
         "status": "up"},
        {"device_a": "R2", "if_a": "loop", "device_b": "R2", "if_b": "loop",
         "status": "up"},
        {"device_a": "R1", "if_a": "", "device_b": "R2", "if_b": "z",
         "status": "up"},
    ]
    report = _simulate(devices=devices, links=links)

    kinds = {c["kind"] for c in report["conflicts"]}
    assert {"blank_device_name", "blank_link_endpoint", "self_loop_link",
            "blank_interface"} <= kinds
    assert report["status"] == dt.STATUS_PARTIAL


def test_unknown_status_is_uncertainty_not_availability():
    links = [dict(link, status="unknown") for link in _links()]
    report = _simulate(links=links)

    assert report["status"] == dt.STATUS_PARTIAL
    assert report["before"]["available_edge_count"] == 0
    assert report["before"]["possible_edge_count"] == 3
    assert report["affected_components"]["lost_reachability_from_target"] == []
    assert any("unknown status" in reason for reason in report["status_reasons"])
    # An invariant cannot be conclusively evaluated on unknown evidence.
    assert report["invariant_checks"] == []


def test_unknown_status_invariant_is_unknown():
    links = [dict(link, status="unknown") for link in _links()]
    report = _simulate(links=links,
                       invariants=["preserve_reachability"])

    assert report["invariant_checks"][0]["status"] == "unknown"


def test_no_shutdown_does_not_invent_recovery():
    links = _links()
    links[0]["status"] = "down"
    report = _simulate(device="R1",
                       lines=["interface eth1", "no shutdown"], links=links)

    assert report["status"] == dt.STATUS_PARTIAL
    assert any(c["kind"] == "cannot_infer_recovery"
               for c in report["conflicts"])
    # No recovery was invented: the target remains isolated.
    assert report["after"]["reachable_from_target"] == []
    assert "R1" in report["after"]["isolated_devices"]


def test_invariant_failure_is_reported_not_enforced():
    report = _simulate(device="R2",
                       invariants=["preserve_reachability"])

    assert report["status"] == dt.STATUS_SIMULATED
    assert report["invariant_checks"][0]["status"] == "failed"
    assert any("invariant(s) failed" in reason
               for reason in report["status_reasons"])
    # Reporting a failed invariant never creates approval or execution.
    assert report["configuration_applied"] is False


def test_routing_invariant_is_reported_unsupported():
    report = _simulate(invariants=["preserve_lab_routing"])

    check = report["invariant_checks"][0]
    assert check["status"] == "unsupported"
    assert "routing protocol semantics are not modeled" in check["detail"]
    # Unsupported evidence never silently becomes a pass or a clean success.
    assert check["status"] != "passed"
    assert report["status"] == dt.STATUS_PARTIAL
    assert any("invariant(s) unsupported" in reason
               for reason in report["status_reasons"])


def test_repeatability_for_identical_inputs():
    first = _simulate()
    second = _simulate()
    assert json.dumps(first, sort_keys=True) == json.dumps(second,
                                                           sort_keys=True)


def test_original_inputs_and_topology_are_not_mutated():
    devices, links = _devices(), _links()
    before_devices = copy.deepcopy(devices)
    before_links = copy.deepcopy(links)

    report = _simulate(devices=devices, links=links)

    assert devices == before_devices
    assert links == before_links
    assert report["before"] != report["after"]
    assert report["before"]["available_edge_count"] == 3
    assert report["after"]["available_edge_count"] == 2


def test_config_diff_is_deterministic_and_optional():
    report = _simulate(base_lines=["hostname R2", "interface eth3"])
    assert report["config_diff_available"] is True
    assert "interface eth3" in report["config_diff"]

    no_base = _simulate()
    assert no_base["config_diff_available"] is False
    assert no_base["config_diff"] is None


# ---------------------------------------------------------------------------
# Safety: no device I/O / execution
# ---------------------------------------------------------------------------
def test_no_socket_subprocess_or_shell_calls(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("simulation attempted device I/O or execution")

    monkeypatch.setattr(socket, "socket", explode)
    monkeypatch.setattr(socket, "create_connection", explode)
    monkeypatch.setattr(subprocess, "run", explode)
    monkeypatch.setattr(subprocess, "Popen", explode)
    monkeypatch.setattr(os, "system", explode)
    monkeypatch.setattr(os, "popen", explode)

    report = _simulate()
    assert report["status"] == dt.STATUS_SIMULATED


def test_simulation_never_calls_executor_apply_or_probe(monkeypatch):
    from network import connectivity, lab_executor
    from services import change_execution, change_service

    def explode(*args, **kwargs):
        raise AssertionError("simulation invoked an execution/probe primitive")

    for module, name in (
            (connectivity, "run_ping"),
            (connectivity, "run_traceroute"),
            (lab_executor.LabExecutor, "apply_config"),
            (lab_executor.LabExecutor, "read_config"),
            (lab_executor.LabExecutor, "rollback_config"),
            (change_execution, "execute_change"),
            (change_execution, "apply_and_verify"),
            (change_service, "apply"),
            (change_service, "rollback")):
        monkeypatch.setattr(module, name, explode)

    report = _simulate()
    assert report["status"] == dt.STATUS_SIMULATED
    assert report["configuration_applied"] is False


def test_module_imports_no_execution_primitives():
    source = (ROOT / "services" / "digital_twin.py").read_text(
        encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    forbidden = {
        "socket", "subprocess", "os", "shutil", "requests", "urllib",
        "http", "paramiko", "docker", "network.lab_executor",
        "services.change_execution", "services.change_service",
        "services.ops_center_actions",
    }
    assert not (imported & forbidden), imported & forbidden


def test_engine_has_no_executor_or_apply_entry_point():
    for forbidden in ("apply", "execute", "execute_change", "run_command",
                      "connect", "deploy"):
        assert not hasattr(dt, forbidden)


# ---------------------------------------------------------------------------
# Session wrapper / approval boundary
# ---------------------------------------------------------------------------
def _seed_topology(session):
    from database import repository
    for name, type_ in (("R1", "router"), ("R2", "router"),
                        ("PC1", "host")):
        repository.upsert_device(session, name, type_, status="up")
    repository.upsert_link(session, "R1", "eth1", "R2", "eth1",
                           subnet="10.0.12.0/30", status="up")
    repository.upsert_link(session, "R2", "eth2", "PC1", "eth0",
                           subnet="10.0.2.0/24", status="up")
    session.commit()


def test_simulate_request_is_read_only(session):
    from database import repository
    from services import change_service

    _seed_topology(session)
    req = change_service.propose(
        session, device_name="R2", base_lines=["hostname R2"],
        proposed_lines=["interface eth2", "shutdown"], data_source="live")
    status_before = req.status
    device_count = len(repository.get_devices(session))
    link_count = len(repository.get_links(session))

    report = dt.simulate_request(session, req)

    assert report["status"] == dt.STATUS_SIMULATED
    assert report["affected_components"]["lost_reachability_from_target"] == [
        "PC1"]
    assert req.status == status_before
    assert len(repository.get_devices(session)) == device_count
    assert len(repository.get_links(session)) == link_count


def test_failure_does_not_enable_or_bypass_approval_or_apply(session):
    from services import change_service

    _seed_topology(session)
    req = change_service.propose(
        session, device_name="R2", base_lines=[],
        proposed_lines=["interface eth2", "shutdown"], data_source="live")
    uuid = req.request_uuid
    assert req.status == "validated"

    # Simulate against a target with no documented links: an honest
    # insufficient-evidence result.
    report = dt.simulate_change(
        devices=[{"name": "R2", "status": "up", "type": "router"}], links=[],
        device_name="R2", proposed_lines=["interface eth2", "shutdown"])
    assert report["status"] == dt.STATUS_INSUFFICIENT

    # The request is unchanged and application still refuses by design.
    assert req.status == "validated"
    result = change_service.apply(session, uuid)
    assert result["applied"] is False
    assert "change loop disabled" in result["reason"]
    assert req.status == "validated"


def test_simulate_request_reports_invalid_stored_json(session):
    from database import repository

    _seed_topology(session)
    device_count = len(repository.get_devices(session))
    link_count = len(repository.get_links(session))
    request = {
        "device_name": "R2",
        "request_uuid": "req-json",
        "status": "validated",
        "data_source": "live",
        "proposed_config_json": "not-json",
        "base_config_json": None,
    }

    report = dt.simulate_request(session, request)

    assert any(c["kind"] == "input_error" for c in report["conflicts"])
    assert report["status"] != dt.STATUS_SIMULATED
    assert report["configuration_applied"] is False
    assert len(repository.get_devices(session)) == device_count
    assert len(repository.get_links(session)) == link_count


def test_simulate_request_fails_closed_on_repository_error(session, monkeypatch):
    from database import repository

    _seed_topology(session)
    device_count = len(repository.get_devices(session))
    link_count = len(repository.get_links(session))

    def boom(*args, **kwargs):
        raise RuntimeError("database unavailable")

    original_get_devices = repository.get_devices
    monkeypatch.setattr(repository, "get_devices", boom)
    request = {
        "device_name": "R2",
        "request_uuid": "req-error",
        "status": "validated",
        "data_source": "live",
        "proposed_config_json": '["interface eth3", "shutdown"]',
        "base_config_json": None,
    }

    report = dt.simulate_request(session, request)

    assert report["status"] == dt.STATUS_ERROR
    assert any(c["kind"] == "simulation_error"
               for c in report["conflicts"])
    assert report["configuration_applied"] is False
    monkeypatch.setattr(repository, "get_devices", original_get_devices)
    assert len(repository.get_devices(session)) == device_count
    assert len(repository.get_links(session)) == link_count


# ---------------------------------------------------------------------------
# ABC adapter
# ---------------------------------------------------------------------------
def test_adapter_returns_structured_simulation_result():
    twin = dt.TopologyDigitalTwin(devices=_devices(), links=_links(),
                                  topology_ref="snapshot:adapter")
    result = twin.simulate(
        topology_ref="snapshot:adapter",
        proposed_change={"status": "ok", "device": "R2",
                         "config_lines": ["interface eth3", "shutdown"]},
        invariants=["preserve_reachability"], scenario=None)

    assert result.status == dt.STATUS_SIMULATED
    assert result.simulator_version == dt.SIMULATOR_VERSION
    assert result.not_proof_of_safety is True
    assert result.change_ref["device"] == "R2"
    assert result.before and result.after
    # The full engine report is exposed for a future integration step.
    assert result.details["status"] == dt.STATUS_SIMULATED
    assert result.details["affected_components"][
        "lost_reachability_from_target"] == ["PC1"]
    payload = result.as_dict()
    for key in ("status", "assumptions", "limitations", "conflicts",
                "affected_components", "before", "after",
                "not_proof_of_safety", "details"):
        assert key in payload


def test_adapter_accepts_pydantic_like_proposal():
    class _Proposal:
        def model_dump(self):
            return {"status": "ok", "device": "R2",
                    "config_lines": ["interface eth3", "shutdown"]}

    twin = dt.TopologyDigitalTwin(devices=_devices(), links=_links())
    result = twin.simulate(topology_ref=None, proposed_change=_Proposal(),
                           invariants=[], scenario=None)

    assert result.status == dt.STATUS_SIMULATED
    assert result.change_ref["device"] == "R2"


def test_adapter_refuses_mismatched_topology_ref():
    twin = dt.TopologyDigitalTwin(devices=_devices(), links=_links(),
                                  topology_ref="snapshot:bound")
    result = twin.simulate(
        topology_ref="snapshot:different",
        proposed_change={"status": "ok", "device": "R2",
                         "config_lines": ["interface eth3", "shutdown"]},
        invariants=[], scenario=None)

    assert result.status == dt.STATUS_INVALID
    assert result.topology_ref == "snapshot:different"
    assert result.conflicts[0]["kind"] == "topology_ref_mismatch"
    assert result.not_proof_of_safety is True


def test_adapter_refuses_non_ok_proposal():
    twin = dt.TopologyDigitalTwin(devices=_devices(), links=_links())
    result = twin.simulate(
        topology_ref=None,
        proposed_change={"status": "blocked", "device": "R2",
                         "config_lines": []},
        invariants=[], scenario=None)

    assert result.status == dt.STATUS_NOT_SIMULATED
    assert result.reasons
    assert result.not_proof_of_safety is True


def test_adapter_does_not_mutate_inputs():
    devices, links = _devices(), _links()
    twin = dt.TopologyDigitalTwin(devices=devices, links=links)
    before = copy.deepcopy((devices, links))
    twin.simulate(topology_ref=None,
                  proposed_change={"device": "R2",
                                   "config_lines": ["interface eth3",
                                                    "shutdown"]},
                  invariants=[], scenario=None)
    assert (devices, links) == before


# ---------------------------------------------------------------------------
# Unknown / unsupported statuses are explicit
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("status", [dt.STATUS_PARTIAL, dt.STATUS_INSUFFICIENT,
                                    dt.STATUS_INVALID, dt.STATUS_NOT_SIMULATED,
                                    dt.STATUS_ERROR])
def test_unknown_status_never_claims_safety(status):
    # A tiny guard: the status vocabulary never collapses to a boolean
    # "safe" value, and every non-simulated status is distinct from
    # "simulated".
    assert status != dt.STATUS_SIMULATED


# ---------------------------------------------------------------------------
# Phase D2 edge-case regressions
# ---------------------------------------------------------------------------
def test_empty_topology_is_insufficient_evidence():
    report = dt.simulate_change(
        devices=[], links=[], device_name="R1",
        proposed_lines=["interface eth1", "shutdown"])

    assert report["status"] == dt.STATUS_INSUFFICIENT
    assert any("no usable devices/links" in reason
               for reason in report["status_reasons"])
    assert report["before"]["reachable_from_target"] == []
    assert report["after"]["reachable_from_target"] == []


def test_target_device_down_is_partial_not_success():
    devices = _devices()
    devices[0]["status"] = "down"  # R1
    report = _simulate(device="R1", lines=["interface eth1", "shutdown"],
                       devices=devices,
                       invariants=["preserve_reachability"])

    assert report["status"] == dt.STATUS_PARTIAL
    assert any(c["kind"] == "target_device_down"
               for c in report["conflicts"])
    assert any("stored as down" in reason
               for reason in report["status_reasons"])
    # A down target has no documented reachability, so no loss is invented.
    assert report["affected_components"]["lost_reachability_from_target"] == []
    # And no invariant may be reported as passed on that evidence.
    assert report["invariant_checks"][0]["status"] == "unknown"


def test_last_interface_state_wins():
    down_last = _simulate(
        device="R1", lines=["interface eth1", "no shutdown", "shutdown"])
    assert down_last["status"] == dt.STATUS_SIMULATED
    assert down_last["affected_components"][
        "lost_reachability_from_target"] == ["PC1", "R2", "R3"]

    up_last = _simulate(
        device="R1", lines=["interface eth1", "shutdown", "no shutdown"])
    assert up_last["status"] == dt.STATUS_SIMULATED
    assert up_last["affected_components"][
        "lost_reachability_from_target"] == []


def test_conflicting_duplicate_devices_are_order_independent():
    up_row = {"name": "R2", "type": "router", "status": "up"}
    down_row = {"name": "R2", "type": "switch", "status": "down"}
    base = [device for device in _devices() if device["name"] != "R2"]

    first = _simulate(device="R1", lines=["interface eth1", "shutdown"],
                      devices=[up_row, down_row, *base])
    second = _simulate(device="R1", lines=["interface eth1", "shutdown"],
                       devices=[down_row, up_row, *base])

    assert first["status"] == dt.STATUS_PARTIAL
    assert second["status"] == dt.STATUS_PARTIAL
    assert first["topology_fingerprint"] == second["topology_fingerprint"]
    # R2 is unknown, so no link through it counts as available.
    assert first["before"]["available_edge_count"] == 0
    assert second["before"]["available_edge_count"] == 0
    assert any(c["kind"] == "duplicate_device"
               and "collapsed to unknown" in c["detail"]
               for c in first["conflicts"])


def test_unknown_device_status_is_not_available():
    devices = _devices()
    devices[1]["status"] = "unknown"  # R2
    report = _simulate(device="R1", lines=["interface eth1", "shutdown"],
                       devices=devices)

    assert report["status"] == dt.STATUS_PARTIAL
    assert report["before"]["available_edge_count"] == 0
    assert report["before"]["possible_edge_count"] == 3
    assert report["before"]["reachable_from_target"] == []
    assert report["before"]["possible_reachable_from_target"] == [
        "PC1", "R2", "R3"]
    assert any("unknown status" in reason
               for reason in report["status_reasons"])


def test_end_closes_interface_context():
    report = _simulate(device="R1",
                       lines=["interface eth1", "end", "shutdown"])

    assert report["status"] == dt.STATUS_PARTIAL
    shutdown = [op for op in report["operations"]
                if op["line"] == "shutdown"][0]
    assert shutdown["supported"] is False
    assert "outside an interface" in shutdown["reason"]
    # The command after ``end`` must not be modeled inside the interface.
    assert report["affected_components"]["lost_reachability_from_target"] == []


def test_unsupported_non_context_line_keeps_interface_context():
    report = _simulate(
        device="R1",
        lines=["interface eth1", "ip address 10.0.0.1/24", "shutdown"])

    # ``ip`` is unsupported, so the overall verdict stays partial...
    assert report["status"] == dt.STATUS_PARTIAL
    shutdown = [op for op in report["operations"]
                if op["line"] == "shutdown"][0]
    # ...but ``shutdown`` is still correctly modeled inside the block.
    assert shutdown["supported"] is True
    assert shutdown["operation"] == "interface_shutdown"
    assert report["affected_components"]["lost_reachability_from_target"] == [
        "PC1", "R2", "R3"]


@pytest.mark.parametrize("lines", [
    ["interface eth3 extra", "shutdown"],
    ["shutdown now"],
    ["interface eth3", "no shutdown now"],
    ["hostname"],
    ["interface eth3", "no interface"],
])
def test_malformed_supported_lines_are_unsupported(lines):
    report = _simulate(device="R2", lines=lines)

    assert report["status"] in (dt.STATUS_PARTIAL, dt.STATUS_NOT_SIMULATED)
    assert report["unsupported_operations"]
    assert report["status"] != dt.STATUS_SIMULATED


def test_malformed_context_end_does_not_leave_interface_open():
    report = _simulate(device="R1",
                       lines=["interface eth1", "end extra", "shutdown"])

    assert report["status"] == dt.STATUS_PARTIAL
    shutdown = [op for op in report["operations"]
                if op["line"] == "shutdown"][0]
    assert shutdown["supported"] is False
    assert report["affected_components"]["lost_reachability_from_target"] == []


def test_duplicate_link_metadata_conflict_is_reported():
    links = _links() + [
        dict(_links()[0], status="down", subnet="10.9.9.0/24"),
    ]
    report = _simulate(device="R1", lines=["interface eth1", "shutdown"],
                       links=links)

    conflicts = [c for c in report["conflicts"]
                 if c["kind"] == "duplicate_link_metadata_conflict"]
    assert conflicts
    assert "status" in conflicts[0]["detail"]
    assert "subnet" in conflicts[0]["detail"]
    assert report["status"] == dt.STATUS_PARTIAL


def test_conflicting_duplicate_links_are_order_independent():
    up_row = dict(_links()[0], status="up", subnet="10.0.12.0/30")
    down_row = dict(_links()[0], status="down", subnet="10.9.9.0/24")
    others = _links()[1:]

    first = _simulate(device="R1", lines=["interface eth1", "shutdown"],
                      links=[up_row, down_row, *others])
    second = _simulate(device="R1", lines=["interface eth1", "shutdown"],
                       links=[down_row, up_row, *others])

    assert first["status"] == dt.STATUS_PARTIAL
    assert second["status"] == dt.STATUS_PARTIAL
    assert (first["topology_fingerprint"]
            == second["topology_fingerprint"])
    assert (first["affected_components"]["lost_reachability_from_target"]
            == second["affected_components"]["lost_reachability_from_target"])
    # The conflicting link is not trusted as available in either order.
    assert first["before"]["available_edge_count"] == 2
    assert second["before"]["available_edge_count"] == 2
    assert any("collapsed to unknown" in c["detail"]
               for c in first["conflicts"]
               if c["kind"] == "duplicate_link_metadata_conflict")


def test_parallel_link_availability_is_counted_separately():
    links = _links() + [
        {"device_a": "R1", "if_a": "eth2", "device_b": "R2", "if_b": "eth2",
         "subnet": "10.0.12.4/30", "status": "down"},
        {"device_a": "R1", "if_a": "eth3", "device_b": "R2", "if_b": "eth3",
         "subnet": "10.0.12.8/30", "status": "up"},
    ]
    report = _simulate(device="R1", lines=["interface eth1", "shutdown"],
                       links=links)

    alt = report["redundancy"]["alternate_paths"][0]
    assert alt["parallel_links"] == 3
    assert alt["available_parallel_links"] == 2
    assert alt["alternate_path_exists"] is True


def test_parallel_link_through_unknown_device_is_not_available():
    devices = _devices()
    devices[1]["status"] = "unknown"  # R2
    links = _links() + [
        {"device_a": "R1", "if_a": "eth2", "device_b": "R2", "if_b": "eth2",
         "subnet": "10.0.12.4/30", "status": "up"},
    ]
    report = _simulate(device="R1", lines=["interface eth1", "shutdown"],
                       devices=devices, links=links)

    alt = report["redundancy"]["alternate_paths"][0]
    assert alt["parallel_links"] == 2
    assert alt["available_parallel_links"] == 0
    assert alt["alternate_path_exists"] is False
    assert report["status"] == dt.STATUS_PARTIAL


def test_metadata_only_change_is_not_a_topology_success():
    report = _simulate(device="R2", lines=["hostname R2"])

    assert report["status"] == dt.STATUS_SIMULATED
    assert report["topology_effect"] == "none"
    assert any("no topology-affecting operation" in reason
               for reason in report["status_reasons"])


def test_fingerprints_are_stable_and_input_sensitive():
    base = _simulate()
    again = _simulate()

    assert base["topology_fingerprint"] == again["topology_fingerprint"]
    assert (base["change_ref"]["fingerprint"]
            == again["change_ref"]["fingerprint"])

    # Row order must not change the topology fingerprint.
    reordered = _simulate(devices=list(reversed(_devices())),
                          links=list(reversed(_links())))
    assert reordered["topology_fingerprint"] == base["topology_fingerprint"]

    # Relevant topology changes must change the topology fingerprint.
    changed_status = _simulate(
        links=[dict(_links()[0], status="down"), *_links()[1:]])
    assert (changed_status["topology_fingerprint"]
            != base["topology_fingerprint"])
    changed_interface = _simulate(
        links=[dict(_links()[0], if_a="eth9"), *_links()[1:]])
    assert (changed_interface["topology_fingerprint"]
            != base["topology_fingerprint"])

    # Change-line changes must change the change fingerprint.
    changed_lines = _simulate(lines=["interface eth3", "shutdown",
                                     "hostname R2"])
    assert (changed_lines["change_ref"]["fingerprint"]
            != base["change_ref"]["fingerprint"])


def test_adapter_fails_closed_on_engine_exception(monkeypatch):
    twin = dt.TopologyDigitalTwin(devices=_devices(), links=_links())

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(dt, "simulate_change", boom)
    result = twin.simulate(
        topology_ref="snapshot:adapter",
        proposed_change={"status": "ok", "device": "R2",
                         "config_lines": ["hostname R2"]},
        invariants=[], scenario=None)

    assert result.status == dt.STATUS_ERROR
    assert result.not_proof_of_safety is True
    assert any("could not run" in reason for reason in result.reasons)
    assert result.conflicts[0]["kind"] == "simulation_error"
    assert result.details == {}


# ---------------------------------------------------------------------------
# Phase D3: snapshot identity, freshness and integrity
# ---------------------------------------------------------------------------
D3_CAPTURED_AT = "2026-10-26T10:00:00+00:00"
D3_NOW = "2026-10-26T10:30:00+00:00"
D3_LINES = ["interface eth3", "shutdown"]


def _binding(**kwargs):
    params = dict(
        devices=_devices(), links=_links(),
        topology_ref="snapshot:test",
        device_name="R2",
        proposed_lines=list(D3_LINES),
    )
    params.update(kwargs)
    return dt.capture_snapshot(**params)


def _validate(binding, **kwargs):
    params = dict(
        devices=_devices(), links=_links(),
        topology_ref="snapshot:test",
        device_name="R2",
        proposed_lines=list(D3_LINES),
        now=D3_NOW,
    )
    params.update(kwargs)
    return dt.validate_snapshot(binding, **params)


def _fresh_binding():
    return _binding(captured_at=D3_CAPTURED_AT, max_age_seconds=3600)


def test_capture_snapshot_binds_identity_and_change():
    devices, links = _devices(), _links()
    before = copy.deepcopy((devices, links))
    binding = dt.capture_snapshot(
        devices=devices, links=links, topology_ref="snapshot:test",
        device_name="R2", proposed_lines=list(D3_LINES),
        captured_at=D3_CAPTURED_AT, max_age_seconds=3600)

    assert (devices, links) == before
    assert binding.topology_ref == "snapshot:test"
    assert binding.topology_source == dt.TOPOLOGY_STORED
    assert binding.topology_fingerprint.startswith("sha256:")
    assert binding.change_fingerprint.startswith("sha256:")
    assert binding.device_count == 4
    assert binding.link_count == 3
    assert binding.binding_version == dt.SNAPSHOT_BINDING_VERSION

    payload = binding.as_dict()
    assert payload["captured_at"] == D3_CAPTURED_AT
    assert payload["max_age_seconds"] == 3600
    assert dt.SnapshotBinding.from_mapping(payload).as_dict() == payload


@pytest.mark.parametrize("kwargs", [
    {"topology_ref": ""},
    {"topology_ref": "snapshot:test", "captured_at": "not-a-date"},
    {"topology_ref": "snapshot:test", "max_age_seconds": 0},
    {"topology_ref": "snapshot:test", "max_age_seconds": -5},
])
def test_capture_snapshot_rejects_unusable_identity(kwargs):
    params = dict(devices=_devices(), links=_links(), device_name="R2",
                  proposed_lines=list(D3_LINES))
    params.update(kwargs)
    with pytest.raises(ValueError):
        dt.capture_snapshot(**params)


def test_validate_matching_snapshot_with_freshness_is_verified():
    report = _validate(_fresh_binding())

    assert report["status"] == dt.INTEGRITY_VERIFIED
    assert report["freshness"]["status"] == dt.FRESHNESS_VERIFIED
    assert report["recommended_simulation_status"] is None
    assert report["not_proof_of_safety"] is True
    assert report["read_only"] is True
    assert report["database_written"] is False
    assert report["device_contacted"] is False


def test_validate_matching_snapshot_without_freshness_is_explicitly_unknown():
    report = _validate(_binding())

    assert report["status"] == dt.INTEGRITY_UNKNOWN_FRESHNESS
    assert report["freshness"]["status"] == dt.FRESHNESS_NOT_CONFIGURED
    assert report["recommended_simulation_status"] == dt.STATUS_PARTIAL
    assert any("freshness cannot be established"
               in reason for reason in report["reasons"])


def test_validate_reordered_rows_are_equivalent():
    binding = _fresh_binding()
    report = _validate(binding, devices=list(reversed(_devices())),
                       links=list(reversed(_links())))

    assert report["status"] == dt.INTEGRITY_VERIFIED
    assert report["expected_topology_fingerprint"] == \
        report["actual_topology_fingerprint"]


@pytest.mark.parametrize("devices,links", [
    ([{"name": "R1", "type": "router", "status": "down"}, *_devices()[1:]],
     _links()),
    (_devices(), [dict(_links()[0], status="down"), *_links()[1:]]),
    (_devices(), [dict(_links()[0], if_a="eth9"), *_links()[1:]]),
])
def test_validate_device_and_link_changes_are_detected(devices, links):
    report = _validate(_fresh_binding(), devices=devices, links=links)

    assert report["status"] == dt.INTEGRITY_MISMATCH
    assert report["recommended_simulation_status"] == dt.STATUS_INSUFFICIENT
    assert any(c["kind"] == "topology_fingerprint_mismatch"
               for c in report["conflicts"])


def test_validate_changed_proposal_is_detected():
    report = _validate(_fresh_binding(),
                       proposed_lines=["interface eth3", "no shutdown"])

    assert report["status"] == dt.INTEGRITY_MISMATCH
    assert report["recommended_simulation_status"] == dt.STATUS_INVALID
    assert any(c["kind"] == "change_fingerprint_mismatch"
               for c in report["conflicts"])


def test_validate_ref_mismatch_is_invalid():
    report = _validate(_fresh_binding(), topology_ref="snapshot:different")

    assert report["status"] == dt.INTEGRITY_INVALID
    assert report["recommended_simulation_status"] == dt.STATUS_INVALID
    assert any(c["kind"] == "topology_ref_mismatch"
               for c in report["conflicts"])


def test_validate_missing_identity_is_insufficient_evidence():
    report = dt.validate_snapshot(
        {}, devices=_devices(), links=_links(), device_name="R2",
        proposed_lines=list(D3_LINES))

    assert report["status"] == dt.INTEGRITY_INSUFFICIENT
    assert report["recommended_simulation_status"] == dt.STATUS_INSUFFICIENT
    assert any(c["kind"] == "snapshot_identity_missing"
               for c in report["conflicts"])


@pytest.mark.parametrize("binding", [
    {"topology_ref": "", "topology_fingerprint": "sha256:" + "a" * 64,
     "change_fingerprint": "sha256:" + "b" * 64},
    {"topology_ref": 123, "topology_fingerprint": "sha256:" + "a" * 64,
     "change_fingerprint": "sha256:" + "b" * 64},
    {"topology_ref": "snapshot:test", "topology_fingerprint": "md5:abc",
     "change_fingerprint": "sha256:" + "b" * 64},
    {"topology_ref": "snapshot:test", "topology_fingerprint": "sha256:" + "a" * 64,
     "change_fingerprint": "sha256:" + "b" * 64,
     "captured_at": "not-a-date"},
    {"topology_ref": "snapshot:test", "topology_fingerprint": "sha256:" + "a" * 64,
     "change_fingerprint": "sha256:" + "b" * 64, "max_age_seconds": 0},
    {"topology_ref": "snapshot:test", "topology_fingerprint": "sha256:" + "a" * 64,
     "change_fingerprint": "sha256:" + "b" * 64,
     "binding_version": "d2-1"},
])
def test_validate_malformed_identity_is_invalid(binding):
    report = _validate(binding)

    assert report["status"] == dt.INTEGRITY_INVALID
    assert report["recommended_simulation_status"] == dt.STATUS_INVALID
    assert any(c["kind"] == "snapshot_identity_invalid"
               for c in report["conflicts"])


def test_validate_expired_snapshot_is_stale():
    binding = _binding(captured_at=D3_CAPTURED_AT, max_age_seconds=60)
    report = _validate(binding, now="2026-10-26T12:00:00+00:00")

    assert report["status"] == dt.INTEGRITY_STALE
    assert report["freshness"]["status"] == dt.FRESHNESS_STALE
    assert report["recommended_simulation_status"] == dt.STATUS_INSUFFICIENT
    assert any(c["kind"] == "stale_topology"
               for c in report["conflicts"])


def test_validate_future_capture_is_invalid():
    report = _validate(_fresh_binding(), now="2026-10-26T09:00:00+00:00")

    assert report["status"] == dt.INTEGRITY_INVALID
    assert report["freshness"]["status"] == dt.FRESHNESS_INVALID
    assert any("future" in reason for reason in report["reasons"])


def test_validate_policy_without_timestamp_is_unknown_freshness():
    report = _validate(_binding(max_age_seconds=3600))

    assert report["status"] == dt.INTEGRITY_UNKNOWN_FRESHNESS
    assert report["freshness"]["status"] == dt.FRESHNESS_UNKNOWN
    assert report["recommended_simulation_status"] == dt.STATUS_PARTIAL


def test_validate_timestamp_without_policy_is_unknown_freshness():
    report = _validate(_binding(captured_at=D3_CAPTURED_AT))

    assert report["status"] == dt.INTEGRITY_UNKNOWN_FRESHNESS
    assert report["freshness"]["status"] == dt.FRESHNESS_NOT_CONFIGURED
    assert report["recommended_simulation_status"] == dt.STATUS_PARTIAL


def test_topology_only_binding_can_be_verified():
    binding = dt.capture_snapshot(
        devices=_devices(), links=_links(), topology_ref="snapshot:topology",
        captured_at=D3_CAPTURED_AT, max_age_seconds=3600)
    report = dt.validate_snapshot(
        binding, devices=_devices(), links=_links(),
        topology_ref="snapshot:topology", now=D3_NOW)

    assert report["status"] == dt.INTEGRITY_VERIFIED
    assert report["expected_change_fingerprint"] is None


def test_simulate_change_verified_binding_stays_simulated():
    report = _simulate(snapshot_binding=_fresh_binding(),
                       integrity_now=D3_NOW)

    assert report["status"] == dt.STATUS_SIMULATED
    assert report["snapshot_integrity_status"] == dt.INTEGRITY_VERIFIED
    assert report["integrity"]["status"] == dt.INTEGRITY_VERIFIED
    assert report["not_proof_of_safety"] is True


def test_simulate_change_stale_binding_is_not_success():
    binding = _binding(captured_at=D3_CAPTURED_AT, max_age_seconds=60)
    report = _simulate(snapshot_binding=binding,
                       integrity_now="2026-10-26T12:00:00+00:00")

    assert report["status"] == dt.STATUS_INSUFFICIENT
    assert report["snapshot_integrity_status"] == dt.INTEGRITY_STALE
    assert any(c["kind"] == "stale_topology" for c in report["conflicts"])
    assert report["configuration_applied"] is False


def test_simulate_change_topology_mismatch_is_not_success():
    changed = [dict(_links()[0], status="down"), *_links()[1:]]
    report = _simulate(devices=_devices(), links=changed,
                       snapshot_binding=_fresh_binding(),
                       integrity_now=D3_NOW)

    assert report["status"] == dt.STATUS_INSUFFICIENT
    assert report["snapshot_integrity_status"] == dt.INTEGRITY_MISMATCH
    assert any(c["kind"] == "topology_fingerprint_mismatch"
               for c in report["conflicts"])


def test_simulate_change_proposal_mismatch_is_invalid():
    report = _simulate(lines=["interface eth3", "no shutdown"],
                       snapshot_binding=_fresh_binding(),
                       integrity_now=D3_NOW)

    assert report["status"] == dt.STATUS_INVALID
    assert report["snapshot_integrity_status"] == dt.INTEGRITY_MISMATCH
    assert any(c["kind"] == "change_fingerprint_mismatch"
               for c in report["conflicts"])


def test_simulate_change_unknown_freshness_is_partial():
    report = _simulate(snapshot_binding=_binding())

    assert report["status"] == dt.STATUS_PARTIAL
    assert report["snapshot_integrity_status"] == dt.INTEGRITY_UNKNOWN_FRESHNESS
    assert any("freshness" in reason for reason in report["status_reasons"])


def test_simulate_change_malformed_binding_fails_closed():
    report = _simulate(snapshot_binding={
        "topology_ref": "snapshot:test",
        "topology_fingerprint": "not-a-fingerprint",
    })

    assert report["status"] == dt.STATUS_INVALID
    assert report["snapshot_integrity_status"] == dt.INTEGRITY_INVALID
    assert report["configuration_applied"] is False


def test_simulate_change_without_binding_is_unchanged():
    report = _simulate()

    assert report["status"] == dt.STATUS_SIMULATED
    assert report["integrity"] is None
    assert report["snapshot_integrity_status"] is None


def test_integrity_report_is_deterministic():
    binding = _fresh_binding()
    first = _validate(binding)
    second = _validate(binding)

    assert json.dumps(first, sort_keys=True) == json.dumps(second,
                                                           sort_keys=True)


def test_integrity_functions_are_read_only(session, monkeypatch):
    from database import repository
    from network import connectivity, lab_executor
    from services import change_execution, change_service

    _seed_topology(session)
    devices = repository.get_devices(session)
    links = repository.get_links(session)
    before = (
        len(devices), len(links),
        [(device.name, device.status) for device in devices],
        [(link.device_a, link.device_b, link.status) for link in links],
    )

    def explode(*args, **kwargs):
        raise AssertionError("integrity check attempted I/O or execution")

    monkeypatch.setattr(socket, "socket", explode)
    monkeypatch.setattr(socket, "create_connection", explode)
    monkeypatch.setattr(subprocess, "run", explode)
    monkeypatch.setattr(subprocess, "Popen", explode)
    monkeypatch.setattr(os, "system", explode)
    monkeypatch.setattr(os, "popen", explode)
    for module, name in (
            (connectivity, "run_ping"),
            (connectivity, "run_traceroute"),
            (lab_executor.LabExecutor, "apply_config"),
            (lab_executor.LabExecutor, "read_config"),
            (lab_executor.LabExecutor, "rollback_config"),
            (change_execution, "execute_change"),
            (change_execution, "apply_and_verify"),
            (change_service, "apply"),
            (change_service, "rollback")):
        monkeypatch.setattr(module, name, explode)

    binding = dt.capture_snapshot(
        devices=devices, links=links, topology_ref="snapshot:test",
        device_name="R2", proposed_lines=["interface eth2", "shutdown"],
        captured_at=D3_CAPTURED_AT, max_age_seconds=3600)
    report = dt.validate_snapshot(
        binding, devices=devices, links=links,
        topology_ref="snapshot:test", device_name="R2",
        proposed_lines=["interface eth2", "shutdown"], now=D3_NOW)
    simulation = dt.simulate_change(
        devices=devices, links=links, device_name="R2",
        proposed_lines=["interface eth2", "shutdown"],
        topology_ref="snapshot:test", snapshot_binding=binding,
        integrity_now=D3_NOW)

    assert report["status"] == dt.INTEGRITY_VERIFIED
    assert simulation["status"] == dt.STATUS_SIMULATED
    assert simulation["configuration_applied"] is False

    after_devices = repository.get_devices(session)
    after_links = repository.get_links(session)
    after = (
        len(after_devices), len(after_links),
        [(device.name, device.status) for device in after_devices],
        [(link.device_a, link.device_b, link.status) for link in after_links],
    )
    assert after == before


def test_simulate_request_with_binding_is_verified_and_read_only(session):
    from database import repository
    from services import change_service

    _seed_topology(session)
    req = change_service.propose(
        session, device_name="R2", base_lines=["hostname R2"],
        proposed_lines=["interface eth2", "shutdown"], data_source="live")
    binding = dt.capture_request_snapshot(
        session, req, captured_at=D3_CAPTURED_AT, max_age_seconds=3600)
    request_before = (
        req.status, req.proposed_config_json, req.base_config_json,
        req.request_uuid, req.updated_at)
    device_count = len(repository.get_devices(session))
    link_count = len(repository.get_links(session))

    report = dt.simulate_request(
        session, req, snapshot_binding=binding, integrity_now=D3_NOW)

    assert report["status"] == dt.STATUS_SIMULATED
    assert report["snapshot_integrity_status"] == dt.INTEGRITY_VERIFIED
    assert (req.status, req.proposed_config_json, req.base_config_json,
            req.request_uuid, req.updated_at) == request_before
    assert len(repository.get_devices(session)) == device_count
    assert len(repository.get_links(session)) == link_count


def test_simulate_request_detects_topology_change_after_capture(session):
    from database import repository
    from services import change_service

    _seed_topology(session)
    req = change_service.propose(
        session, device_name="R2", base_lines=[],
        proposed_lines=["interface eth2", "shutdown"], data_source="live")
    binding = dt.capture_request_snapshot(
        session, req, captured_at=D3_CAPTURED_AT, max_age_seconds=3600)

    link = repository.get_links(session)[0]
    link.status = "down"
    session.commit()

    report = dt.simulate_request(
        session, req, snapshot_binding=binding, integrity_now=D3_NOW)

    assert report["status"] != dt.STATUS_SIMULATED
    assert report["snapshot_integrity_status"] == dt.INTEGRITY_MISMATCH
    assert any(c["kind"] == "topology_fingerprint_mismatch"
               for c in report["conflicts"])
    assert report["configuration_applied"] is False


def test_capture_request_snapshot_rejects_invalid_json(session):
    request = {
        "device_name": "R2",
        "request_uuid": "req-json",
        "proposed_config_json": "not-json",
        "base_config_json": None,
    }

    with pytest.raises(ValueError):
        dt.capture_request_snapshot(session, request)


def test_adapter_passes_binding_and_fails_closed():
    binding = _fresh_binding()
    twin = dt.TopologyDigitalTwin(
        devices=_devices(), links=_links(), topology_ref="snapshot:test",
        snapshot_binding=binding, integrity_now=D3_NOW)

    result = twin.simulate(
        topology_ref="snapshot:test",
        proposed_change={"status": "ok", "device": "R2",
                         "config_lines": list(D3_LINES)},
        invariants=[], scenario=None)
    assert result.status == dt.STATUS_SIMULATED
    assert result.details["snapshot_integrity_status"] == \
        dt.INTEGRITY_VERIFIED

    changed = twin.simulate(
        topology_ref="snapshot:test",
        proposed_change={"status": "ok", "device": "R2",
                         "config_lines": ["interface eth3", "no shutdown"]},
        invariants=[], scenario=None)
    assert changed.status == dt.STATUS_INVALID
    assert changed.details["snapshot_integrity_status"] == \
        dt.INTEGRITY_MISMATCH
    assert changed.not_proof_of_safety is True


@pytest.mark.parametrize("raw,expected", [
    ("120", 120),
    ("0", None),
    ("not-an-int", None),
    ("", None),
])
def test_freshness_policy_is_configurable(monkeypatch, raw, expected):
    monkeypatch.setenv("NETPILOT_DIGITAL_TWIN_MAX_AGE_SECONDS", raw)
    from config import get_settings

    assert get_settings().digital_twin_snapshot_max_age_seconds == expected


def test_simulate_request_uses_configured_freshness_policy(session,
                                                           monkeypatch):
    from services import change_service

    monkeypatch.setenv("NETPILOT_DIGITAL_TWIN_MAX_AGE_SECONDS", "3600")
    _seed_topology(session)
    req = change_service.propose(
        session, device_name="R2", base_lines=[],
        proposed_lines=["interface eth2", "shutdown"], data_source="live")
    binding = dt.capture_request_snapshot(
        session, req, captured_at=D3_CAPTURED_AT)

    report = dt.simulate_request(
        session, req, snapshot_binding=binding, integrity_now=D3_NOW)

    assert report["snapshot_integrity_status"] == dt.INTEGRITY_VERIFIED
    assert report["status"] == dt.STATUS_SIMULATED
