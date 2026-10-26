"""Offline tests for the gated change-loop pipeline.

``services/change_execution.execute_change`` is the runtime half of the
change loop: it proves the lab identity, proves the device is still in its
expected pre-change state, applies the change, verifies it by reading the
device back, and rolls back (and verifies the restoration) when the
verification fails.

Every gate is exercised here, and every outcome is asserted with an
in-memory fake executor - no device, no Docker, no network.
"""
from __future__ import annotations

import datetime as dt

import pytest

from database.models import db_utcnow
from services import change_execution, change_service
from tests.lab_fakes import (BASE_R2, PROPOSED_R2, FakeLab, FakeOutcome,
                             lab_inventory)


class FakeExecutor:
    """A change-loop executor backed by an in-memory device.

    It implements the whole protocol the pipeline relies on:
    ``preflight``, ``read_config``, ``__call__`` (apply) and
    ``rollback_config``.
    """

    def __init__(self, config: list[str] | None = None,
                 identity_ok: bool = True,
                 fail_apply: bool = False,
                 timeout_apply: bool = False,
                 partial_apply: bool = False,
                 verify_fails: bool = False,
                 rollback_fails: bool = False):
        self.config = list(config if config is not None else BASE_R2)
        self.identity_ok = identity_ok
        self.fail_apply = fail_apply
        self.timeout_apply = timeout_apply
        self.partial_apply = partial_apply
        self.verify_fails = verify_fails
        self.rollback_fails = rollback_fails
        self.applied: list[list[str]] = []
        self.rolled_back = False

    # -- gates ------------------------------------------------------------
    def preflight(self, device_name: str) -> tuple[bool, str]:
        if self.identity_ok:
            return True, f"lab node {device_name!r} verified"
        return False, "lab identity not verified (fake)"

    def read_config(self, device_name: str) -> list[str]:
        return list(self.config)

    # -- execution --------------------------------------------------------
    def __call__(self, device_name: str, config_lines: list[str]) -> FakeOutcome:
        self.applied.append(list(config_lines))
        if self.timeout_apply:
            return FakeOutcome(ok=False, timed_out=True,
                               error="simulated timeout")
        if self.fail_apply:
            return FakeOutcome(ok=False, error="simulated apply failure")
        if self.partial_apply:
            # only part of the change landed
            self.config = list(self.config) + list(config_lines[:2])
            return FakeOutcome(ok=False, partial=True,
                               applied_lines=list(config_lines[:2]),
                               missing_lines=list(config_lines[2:]),
                               error="simulated partial application")
        self.config = list(self.config) + [
            ln for ln in config_lines if ln not in self.config]
        if self.verify_fails:
            # the change "applied" but the device does not actually show it
            self.config = [ln for ln in self.config
                           if ln not in config_lines]
        return FakeOutcome(ok=True, applied_lines=list(config_lines))

    def rollback_config(self, device_name: str, proposed: list[str],
                        base: list[str]) -> FakeOutcome:
        if self.rollback_fails:
            return FakeOutcome(ok=False, error="simulated rollback failure")
        self.rolled_back = True
        self.config = list(base)
        return FakeOutcome(ok=True, detail="rolled back")


@pytest.fixture()
def live(monkeypatch):
    monkeypatch.setenv("NETPILOT_CHANGE_LOOP_ENABLED", "true")
    monkeypatch.setenv("NETPILOT_LAB_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "false")


def _propose(session, base=None, proposed=None, proposed_by="alice"):
    return change_service.propose(
        session, device_name="R2",
        base_lines=list(base if base is not None else BASE_R2),
        proposed_lines=list(proposed if proposed is not None else PROPOSED_R2),
        proposed_by=proposed_by, proposer_authenticated=True)


def _approved(session, base=None, proposed=None, approver="bob"):
    req = _propose(session, base, proposed)
    change_service.approve(session, req.request_uuid, approved_by=approver,
                           authenticated=True)
    return req.request_uuid


# --------------------------------------------------------------------------
# Gates
# --------------------------------------------------------------------------
def test_refused_when_the_change_loop_is_disabled(session, monkeypatch):
    monkeypatch.setenv("NETPILOT_CHANGE_LOOP_ENABLED", "false")
    uuid = _approved(session)
    res = change_execution.execute_change(session, uuid,
                                          executor=FakeExecutor())
    assert res["refused"] is True
    assert res["applied"] is False
    assert "change loop disabled" in res["reason"]


def test_refused_when_the_request_is_not_approved(session, live):
    req = _propose(session)
    res = change_execution.execute_change(session, req.request_uuid,
                                          executor=FakeExecutor())
    assert res["refused"] is True
    assert "requires an approved request" in res["reason"]
    assert res["status"] == "validated"


def test_refused_without_an_executor(session, live):
    uuid = _approved(session)
    res = change_execution.execute_change(session, uuid, executor=None)
    assert res["refused"] is True
    assert "no lab executor provided" in res["reason"]


def test_refused_when_the_approval_window_has_expired(session, live):
    uuid = _approved(session)
    from database import repository
    req = repository.get_change_request(session, uuid)
    req.expires_at = db_utcnow() - dt.timedelta(minutes=1)
    session.commit()
    executor = FakeExecutor()
    res = change_execution.execute_change(session, uuid, executor=executor)
    assert res["refused"] is True
    assert "expired" in res["reason"]
    assert executor.applied == []       # nothing was applied


def test_expiry_during_apply_is_not_overwritten_as_failed(session, live,
                                                          monkeypatch):
    """A window closing between the pre-check and apply must stay expired."""
    uuid = _approved(session)
    monkeypatch.setattr(
        change_service, "apply",
        lambda *a, **k: {"applied": False, "expired": True,
                         "status": "expired",
                         "reason": "change request expired; application "
                                   "refused"})
    executor = FakeExecutor()
    res = change_execution.execute_change(session, uuid, executor=executor)
    assert res["refused"] is True
    assert res["status"] == "expired"
    assert executor.applied == []


def test_refused_when_approval_has_no_verified_requester(session, live):
    req = change_service.propose(
        session, device_name="R2", base_lines=list(BASE_R2),
        proposed_lines=list(PROPOSED_R2), proposed_by=None,
        proposer_authenticated=False)
    # forge an approval that the service would never have produced
    req.approved_by = "bob"
    req.approved_at = req.created_at
    req.status = "approved"
    session.commit()
    executor = FakeExecutor()
    res = change_execution.execute_change(session, req.request_uuid,
                                          executor=executor)
    assert res["refused"] is True
    assert "verified requester" in res["reason"]
    assert executor.applied == []


def test_refused_when_the_approval_is_a_self_approval(session, live):
    req = _propose(session, proposed_by="alice")
    # forge the self-approval that change_service would have refused
    req.approved_by = "alice"
    req.approved_at = req.created_at
    req.status = "approved"
    session.commit()
    res = change_execution.execute_change(session, req.request_uuid,
                                          executor=FakeExecutor())
    assert res["refused"] is True
    assert "self-approval" in res["reason"]


def test_refused_when_no_rollback_plan_can_be_verified(session, live):
    # an empty base config means the rollback could not be verified
    uuid = _approved(session, base=[])
    res = change_execution.execute_change(session, uuid,
                                          executor=FakeExecutor())
    assert res["refused"] is True
    assert "rollback plan not verified" in res["reason"]
    assert "rollback" in res["reason"]


def test_refused_when_the_rollback_config_is_unsafe(session, live):
    uuid = _approved(session, base=["hostname R2", "write memory"])
    res = change_execution.execute_change(session, uuid,
                                          executor=FakeExecutor())
    assert res["refused"] is True
    assert "rollback plan not verified" in res["reason"]


def test_refused_when_the_change_cannot_be_undone(session, live):
    """Fail closed: a change FRR gives us no removal command for is refused."""
    uuid = _approved(session, proposed=BASE_R2 + ["log filter-text secret"])
    res = change_execution.execute_change(session, uuid,
                                          executor=FakeExecutor())
    assert res["refused"] is True
    assert "rollback plan not verified" in res["reason"]
    assert "cannot be undone deterministically" in res["reason"]


def test_refused_when_the_change_is_a_no_op(session, live):
    """A change that changes nothing has no rollback plan -> it is refused."""
    uuid = _approved(session, proposed=list(BASE_R2))
    executor = FakeExecutor()
    res = change_execution.execute_change(session, uuid, executor=executor)
    assert res["refused"] is True
    assert "rollback plan not verified" in res["reason"]
    assert executor.applied == []      # nothing was applied


def test_the_rollback_plan_removes_what_the_change_added(session, live):
    """The plan the pipeline verifies must be a real undo."""
    proposed = list(BASE_R2) + ["banner motd default"]
    uuid = _approved(session, proposed=proposed)
    res = change_execution.execute_change(session, uuid,
                                          executor=FakeExecutor())
    assert res["ok"] is True
    # the executor's rollback received a plan that removes the banner
    plan = change_execution.build_rollback_ops(proposed, BASE_R2)
    flat = [ln for op in plan for ln in op.lines]
    assert "no banner motd" in flat


def test_refused_when_the_lab_identity_cannot_be_verified(session, live):
    uuid = _approved(session)
    res = change_execution.execute_change(
        session, uuid, executor=FakeExecutor(identity_ok=False))
    assert res["refused"] is True
    assert "lab identity not verified" in res["reason"]


def test_refused_when_the_device_has_drifted_from_the_recorded_base(session,
                                                                     live):
    uuid = _approved(session)
    drifted = FakeExecutor(config=[ln for ln in BASE_R2
                                   if ln != "network 10.0.12.0/24 area 0"])
    res = change_execution.execute_change(session, uuid, executor=drifted)
    assert res["refused"] is True
    assert "precondition failed" in res["reason"]
    assert drifted.applied == []          # nothing was applied


def test_refused_when_the_device_state_cannot_be_read(session, live):
    uuid = _approved(session)

    class Broken(FakeExecutor):
        def read_config(self, device_name):
            raise RuntimeError("device unreachable")

    res = change_execution.execute_change(session, uuid, executor=Broken())
    assert res["refused"] is True
    assert "could not read the current state" in res["reason"]


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------
def test_success_applies_and_verifies_the_change(session, live):
    uuid = _approved(session)
    executor = FakeExecutor()
    res = change_execution.execute_change(session, uuid, executor=executor)
    assert res["ok"] is True
    assert res["applied"] is True
    assert res["verified"] is True
    assert res["rolled_back"] is False
    assert res["status"] == "applied"
    assert executor.applied == [PROPOSED_R2]


def test_a_successful_change_is_persisted_as_applied(session, live):
    uuid = _approved(session)
    change_execution.execute_change(session, uuid, executor=FakeExecutor())
    from database import repository
    req = repository.get_change_request(session, uuid)
    assert req.status == "applied"
    assert "verified" in (req.apply_detail or "")


# --------------------------------------------------------------------------
# Failure paths
# --------------------------------------------------------------------------
def test_a_failed_apply_is_recorded_as_failed(session, live):
    uuid = _approved(session)
    res = change_execution.execute_change(
        session, uuid, executor=FakeExecutor(fail_apply=True))
    assert res["ok"] is False
    assert res["applied"] is False
    assert res["status"] == "failed"
    assert "simulated apply failure" in res["reason"]


def test_a_timeout_is_never_claimed_as_success(session, live):
    uuid = _approved(session)
    res = change_execution.execute_change(
        session, uuid, executor=FakeExecutor(timeout_apply=True))
    assert res["ok"] is False
    assert res["verified"] is False
    assert res["timed_out"] is True
    assert res["status"] == "failed"


def test_partial_application_triggers_a_rollback(session, live):
    uuid = _approved(session)
    executor = FakeExecutor(partial_apply=True)
    res = change_execution.execute_change(session, uuid, executor=executor)
    assert res["ok"] is False
    assert res["applied"] is False
    assert res["rolled_back"] is True
    assert res["restored"] is True
    assert res["status"] == "rolled_back"
    assert executor.rolled_back is True


def test_verification_failure_triggers_a_rollback_and_verifies_restoration(
        session, live):
    uuid = _approved(session)
    executor = FakeExecutor(verify_fails=True)
    res = change_execution.execute_change(session, uuid, executor=executor)
    assert res["ok"] is False
    assert res["applied"] is True
    assert res["verified"] is False
    assert res["rolled_back"] is True
    assert res["restored"] is True
    assert res["status"] == "rolled_back"
    assert "post-change verification failed" in res["reason"]
    assert executor.rolled_back is True


def test_a_failed_rollback_is_recorded_as_failed_not_rolled_back(session, live):
    uuid = _approved(session)
    executor = FakeExecutor(verify_fails=True, rollback_fails=True)
    res = change_execution.execute_change(session, uuid, executor=executor)
    assert res["ok"] is False
    assert res["rolled_back"] is False
    assert res["restored"] is False
    assert res["status"] == "failed"
    assert "rollback failed" in res["reason"]


def test_a_restoration_that_cannot_be_verified_is_not_claimed(session, live):
    uuid = _approved(session)

    class Unverifiable(FakeExecutor):
        def rollback_config(self, device_name, proposed, base):
            # the rollback "ran" but the change is still active on the device
            self.rolled_back = True
            self.config = list(base) + ["ip address 10.0.23.2/24"]
            return FakeOutcome(ok=True)

    res = change_execution.execute_change(
        session, uuid, executor=Unverifiable(verify_fails=True))
    assert res["ok"] is False
    assert res["rolled_back"] is True
    assert res["restored"] is False
    assert res["status"] == "failed"
    assert "restoration could not be verified" in res["reason"]


def test_a_restoration_that_lost_a_base_line_is_not_claimed(session, live):
    uuid = _approved(session)

    class Incomplete(FakeExecutor):
        def rollback_config(self, device_name, proposed, base):
            # the rollback "ran" but a base line is gone again
            self.rolled_back = True
            self.config = [ln for ln in base
                           if ln != "ip address 10.0.12.2/24"]
            return FakeOutcome(ok=True)

    res = change_execution.execute_change(
        session, uuid, executor=Incomplete(verify_fails=True))
    assert res["ok"] is False
    assert res["rolled_back"] is True
    assert res["restored"] is False
    assert res["status"] == "failed"


# --------------------------------------------------------------------------
# The rollback plan itself
# --------------------------------------------------------------------------
def test_rollback_plan_removes_what_the_change_added():
    ok, reason = change_execution.verify_rollback_plan(BASE_R2, PROPOSED_R2)
    assert ok is True, reason


def test_rollback_plan_is_refused_without_a_base_configuration():
    ok, reason = change_execution.verify_rollback_plan([], PROPOSED_R2)
    assert ok is False
    assert "no base configuration" in reason


def test_rollback_plan_is_refused_when_it_would_contain_a_prohibited_command():
    ok, reason = change_execution.verify_rollback_plan(
        ["hostname R2", "reload now"], ["hostname R2"])
    assert ok is False
    assert "not safe" in reason


def test_the_rollback_plan_of_a_bare_block_removal_is_complete():
    """The "verified rollback plan" gate must not pass an incomplete plan.

    A change that removes a block without sub-lines (``no interface eth3``)
    must have a plan that restores that block - otherwise the change is
    applied and the device is left in a state that is not the pre-change
    state.
    """
    base = BASE_R2 + ["interface eth3"]
    proposed = ["no interface eth3", "hostname R3"]
    ok, reason = change_execution.verify_rollback_plan(base, proposed)
    assert ok is True, reason
    from network.lab_config_ops import build_rollback_ops
    flat = [(op.context, tuple(op.lines))
            for op in build_rollback_ops(proposed, base)]
    assert ("interface eth3", ()) in flat


def test_a_change_that_removes_a_line_is_applied_and_verified(session, live,
                                                               monkeypatch):
    """End to end, with the real executor and the FRR-ish fake lab.

    A removal is proven by the **absence** of its target: the read-back can
    never contain the removal command itself. A verification that looked for
    the proposal's own lines would report this change as a partial
    application and roll it back again.
    """
    from network import containerlab_manager
    from network.lab_executor import LabExecutor

    lab = FakeLab(config={"R2": list(BASE_R2)})
    monkeypatch.setattr(containerlab_manager, "docker_exec", lab.docker_exec)
    monkeypatch.setattr(containerlab_manager, "docker_inspect_labels",
                        lab.docker_inspect_labels)
    executor = LabExecutor(inventory=lab_inventory("R2"))

    proposed = ["no interface eth2"]
    uuid = _approved(session, base=BASE_R2, proposed=proposed)
    res = change_execution.execute_change(session, uuid, executor=executor)
    assert res["ok"] is True, res["reason"]
    assert res["applied"] is True
    assert res["verified"] is True
    assert res["status"] == "applied"
    assert "interface eth2" not in lab.config["R2"]
