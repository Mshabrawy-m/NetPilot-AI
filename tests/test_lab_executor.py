"""Offline tests for the isolated-lab executor (network.lab_executor).

Every gate is exercised: the executor must refuse when the lab identity,
the authorization or the static rules cannot be proven, and it must report
success, failure, timeout and partial application honestly.

Nothing here touches a real device: ``network.containerlab_manager`` is
replaced by an in-memory fake lab (``tests/lab_fakes.py``).
"""
from __future__ import annotations

import pytest

from network import containerlab_manager
from network.containerlab_manager import CommandResult
from network.lab_executor import (ALLOWED_CONFIG_HEADS, LabExecutor,
                                   static_gate)
from tests.lab_fakes import BASE_R2, PROPOSED_R2, FakeLab, lab_inventory


@pytest.fixture()
def lab(monkeypatch):
    fake = FakeLab(config={"R2": list(BASE_R2), "R1": ["hostname R1"],
                           "PC1": []})
    monkeypatch.setattr(containerlab_manager, "docker_exec", fake.docker_exec)
    monkeypatch.setattr(containerlab_manager, "docker_inspect_labels",
                        fake.docker_inspect_labels)
    return fake


@pytest.fixture()
def executor(lab):
    return LabExecutor(inventory=lab_inventory("R1", "R2", "PC1",
                                                types={"PC1": "host"}))


@pytest.fixture()
def live(monkeypatch):
    """Both execution gates open, live mode."""
    monkeypatch.setenv("NETPILOT_LAB_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("NETPILOT_CHANGE_LOOP_ENABLED", "true")
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "false")


# --------------------------------------------------------------------------
# Gates: the executor must fail closed
# --------------------------------------------------------------------------
def test_execution_refused_when_lab_execution_is_disabled(executor, monkeypatch):
    monkeypatch.setenv("NETPILOT_LAB_EXECUTION_ENABLED", "false")
    monkeypatch.setenv("NETPILOT_CHANGE_LOOP_ENABLED", "true")
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "false")
    ok, reason = executor.preflight("R2")
    assert ok is False
    assert "lab execution disabled" in reason


def test_execution_refused_when_change_loop_is_disabled(executor, monkeypatch):
    monkeypatch.setenv("NETPILOT_LAB_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("NETPILOT_CHANGE_LOOP_ENABLED", "false")
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "false")
    ok, reason = executor.preflight("R2")
    assert ok is False
    assert "change loop disabled" in reason


def test_execution_refused_in_demo_mode(executor, live, monkeypatch):
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "true")
    ok, reason = executor.preflight("R2")
    assert ok is False
    assert "demo mode" in reason


def test_unknown_device_is_refused(executor, live):
    ok, reason = executor.preflight("R9")
    assert ok is False
    assert "not a node of the configured lab topology" in reason


def test_the_allowlist_only_contains_commands_frr_accepts():
    """``mtu`` is not an FRR 8.4 configuration command.

    The interface MTU is a kernel property; FRR only *reports* it. A
    proposal using ``mtu`` would have been rejected by the device itself,
    so it is refused at the static gate instead.
    """
    assert "mtu" not in ALLOWED_CONFIG_HEADS
    assert "interface" in ALLOWED_CONFIG_HEADS


def test_the_routing_table_is_read_with_a_read_only_command(executor, live,
                                                            lab):
    lab.routes["R2"] = ["O* 10.0.12.0/24 [110/10] via 10.0.12.1"]
    routes = executor.read_routing_table("R2")
    assert routes == ["O* 10.0.12.0/24 [110/10] via 10.0.12.1"]
    # the command is a read-only "show", never a configuration change
    argv = lab.calls[-1][1]
    assert argv[:3] == ["vtysh", "-c", "show ip route"]
    assert "configure" not in " ".join(argv)


def test_an_unreadable_routing_table_is_never_reported_as_empty(executor,
                                                               live, lab,
                                                               monkeypatch):
    lab.routes["R2"] = []
    # the fake returns an empty table for a device it knows -> that IS empty
    assert executor.read_routing_table("R2") == []

    # ... but a failing command raises instead of pretending
    def boom(container, argv, timeout=20):
        return CommandResult(False, 1, "", "vtysh failed")

    monkeypatch.setattr(containerlab_manager, "docker_exec", boom)
    with pytest.raises(RuntimeError):
        executor.read_routing_table("R2")


def test_host_nodes_can_never_be_configured(executor, live):
    ok, reason = executor.preflight("PC1")
    assert ok is False
    assert "only routers can be configured" in reason


def test_container_without_containerlab_label_is_refused(executor, live,
                                                          monkeypatch):
    monkeypatch.setattr(containerlab_manager, "docker_inspect_labels",
                        lambda c, timeout=15: {})
    ok, reason = executor.preflight("R2")
    assert ok is False
    assert "lab identity unverified" in reason


def test_container_label_mismatch_is_refused(executor, live, monkeypatch):
    monkeypatch.setattr(containerlab_manager, "docker_inspect_labels",
                        lambda c, timeout=15: {"clab_node_name": "SOMETHINGELSE"})
    ok, reason = executor.preflight("R2")
    assert ok is False
    assert "refusing" in reason


def test_labels_cannot_be_read_at_all_is_refused(executor, live, monkeypatch):
    monkeypatch.setattr(containerlab_manager, "docker_inspect_labels",
                        lambda c, timeout=15: None)
    ok, reason = executor.preflight("R2")
    assert ok is False
    assert "lab identity unverified" in reason


# --------------------------------------------------------------------------
# The static gate (no device access)
# --------------------------------------------------------------------------
def test_static_gate_rejects_prohibited_commands():
    ok, reason = static_gate("R2", ["hostname R2", "write memory"],
                              {"R2"})
    assert ok is False
    assert "prohibited" in reason


def test_static_gate_rejects_shell_injection():
    ok, reason = static_gate("R2", ["interface eth2; rm -rf /"], {"R2"})
    assert ok is False
    assert "invalid/prohibited" in reason


def test_static_gate_rejects_non_config_command_heads():
    # A read-only FRR command is not a configuration change; it must not be
    # accepted as one (the LLM cannot use the executor to run commands).
    ok, reason = static_gate("R2", ["show running-config"], {"R2"})
    assert ok is False
    assert "outside the allowed config set" in reason


def test_static_gate_rejects_out_of_scope_device():
    ok, reason = static_gate("R2", ["interface eth1", "ip address 10.0.12.9/24",
                                     "description link to R1"], {"R1", "R2"})
    assert ok is False
    assert "scope check failed" in reason


def test_static_gate_rejects_removing_core_routing():
    ok, reason = static_gate("R2", ["no ip routing"], {"R2"})
    assert ok is False
    assert "lab invariant check failed" in reason


def test_static_gate_accepts_a_valid_proposal():
    ok, reason = static_gate("R2", PROPOSED_R2, {"R1", "R2", "PC1"})
    assert ok is True, reason


def test_static_gate_rejects_an_empty_proposal():
    ok, reason = static_gate("R2", [], {"R2"})
    assert ok is False
    assert "empty configuration" in reason


# --------------------------------------------------------------------------
# Apply: success, failure, timeout, partial application
# --------------------------------------------------------------------------
def test_apply_success_reports_every_line_applied(executor, lab, live):
    outcome = executor.apply_config("R2", PROPOSED_R2)
    assert outcome.ok is True, outcome.error
    assert outcome.missing_lines == []
    assert outcome.applied_lines
    assert "interface eth3" in lab.config["R2"]


def test_apply_uses_a_fixed_vtysh_argv_with_no_shell(executor, lab, live):
    executor.apply_config("R2", PROPOSED_R2)
    applies = [argv for _device, argv in lab.calls if "configure terminal" in argv]
    assert len(applies) == 1
    argv = applies[0]
    assert argv[:2] == ["vtysh", "-c"]
    commands = [argv[i + 1] for i, v in enumerate(argv) if v == "-c"]
    assert commands[0] == "configure terminal"
    assert commands[-1] == "end"
    # every configuration line is its own argument - no shell involved
    assert "interface eth3" in commands
    assert "ip address 10.0.23.2/24" in commands


def test_apply_failure_is_reported_and_never_claimed_as_success(executor, lab,
                                                                 live):
    lab.fail_next = "simulated vtysh error"
    outcome = executor.apply_config("R2", PROPOSED_R2)
    assert outcome.ok is False
    assert "simulated vtysh error" in outcome.error


def test_apply_timeout_is_reported_with_unknown_state(executor, lab, live):
    lab.timeout_next = True
    outcome = executor.apply_config("R2", PROPOSED_R2)
    assert outcome.ok is False
    assert outcome.timed_out is True
    assert "state is unknown" in outcome.error


def test_partial_application_is_reported_as_partial_not_as_success(executor, lab,
                                                                    live):
    lab.partial_next = 3          # only the first commands land
    outcome = executor.apply_config("R2", PROPOSED_R2)
    assert outcome.ok is False
    assert outcome.partial is True
    assert outcome.missing_lines
    assert outcome.applied_lines


def test_apply_refuses_when_the_static_gate_fails(executor, lab, live):
    outcome = executor.apply_config("R2", ["reload now"])
    assert outcome.ok is False
    assert outcome.applied_lines == []
    # nothing was sent to the device at all
    assert lab.calls == []


def test_apply_refuses_an_unknown_device_before_any_device_io(executor, lab,
                                                               live):
    outcome = executor.apply_config("R9", PROPOSED_R2)
    assert outcome.ok is False
    assert lab.calls == []


def test_read_config_uses_the_read_only_vtysh_command(executor, lab, live):
    lines = executor.read_config("R2")
    assert lines == BASE_R2
    assert lab.calls[-1][1] == ["vtysh", "-c", "show running-config"]


def test_read_config_raises_when_the_device_cannot_be_read(executor, lab, live):
    lab.unreadable = True
    with pytest.raises(Exception):
        executor.read_config("R2")


# --------------------------------------------------------------------------
# Rollback: a real undo, not a merge
# --------------------------------------------------------------------------
def test_rollback_removes_added_blocks_and_restores_the_base(executor, lab,
                                                              live):
    executor.apply_config("R2", PROPOSED_R2)
    assert "interface eth3" in lab.config["R2"]

    outcome = executor.rollback_config("R2", PROPOSED_R2, BASE_R2)
    assert outcome.ok is True, outcome.error
    assert "interface eth3" not in lab.config["R2"]
    for line in BASE_R2:
        assert line in lab.config["R2"]


def test_rollback_failure_is_reported(executor, lab, live):
    lab.fail_next = "simulated rollback error"
    outcome = executor.rollback_config("R2", PROPOSED_R2, BASE_R2)
    assert outcome.ok is False
    assert "simulated rollback error" in outcome.error


def test_rollback_of_an_unchanged_change_is_a_no_op(executor, lab, live):
    outcome = executor.rollback_config("R2", BASE_R2, BASE_R2)
    assert outcome.ok is True
    assert "no rollback required" in outcome.detail


def test_a_proposal_that_removes_a_line_applies_cleanly(executor, lab, live):
    """A removal is proven by the **absence** of its target.

    A removal can never appear in the read-back, so a verification that only
    looked for the proposal's own lines would report every removal as a
    partial application - even when it applied perfectly.
    """
    outcome = executor.apply_config("R2", ["no interface eth2"])
    assert outcome.ok is True, outcome.error
    assert outcome.partial is False
    assert outcome.missing_lines == []
    assert "interface eth2" not in lab.config["R2"]


def test_a_removal_that_did_not_land_is_reported_as_missing(executor, lab,
                                                            live):
    """A removal that did not happen is never reported as applied."""
    lab.partial_next = 0            # nothing lands
    outcome = executor.apply_config("R2", ["no interface eth2"])
    assert outcome.ok is False
    assert "no interface eth2" in outcome.missing_lines


def test_a_removal_that_did_not_stick_is_a_partial_application(executor, lab,
                                                              live,
                                                              monkeypatch):
    """The transaction ran, but the line is still there: partial application."""
    monkeypatch.setattr(lab, "_remove",
                        lambda lines, norm, context: list(lines))
    outcome = executor.apply_config("R2", ["no interface eth2"])
    assert outcome.ok is False
    assert outcome.partial is True
    assert "no interface eth2" in outcome.missing_lines


def test_a_rollback_restores_a_block_that_has_no_sub_lines(executor, lab,
                                                          live):
    """A removed bare block is really brought back.

    ``no interface eth3`` deletes the whole interface, so the rollback has to
    re-enter the empty context. If it did not, the device would keep the
    change after the rollback and the restoration check would fail.
    """
    base = BASE_R2 + ["interface eth3"]
    proposed = ["no interface eth3", "hostname R3"]
    lab.config["R2"] = list(base)

    assert executor.apply_config("R2", proposed).ok is True
    assert "interface eth3" not in lab.config["R2"]

    outcome = executor.rollback_config("R2", proposed, base)
    assert outcome.ok is True, outcome.error
    assert "interface eth3" in lab.config["R2"]
    assert "hostname R2" in lab.config["R2"]

    from network.lab_verifier import verify_restored_config
    restored = verify_restored_config("R2", base, executor.read_config,
                                       removed_lines=["interface eth3"])
    assert restored.passed is True, restored.detail


# --------------------------------------------------------------------------
# The executor protocol used by services.change_service
# --------------------------------------------------------------------------
def test_executor_satisfies_the_change_service_protocol(executor, live):
    outcome = executor("R2", PROPOSED_R2)
    assert hasattr(outcome, "ok")
    assert hasattr(outcome, "error")
    assert outcome.ok is True
