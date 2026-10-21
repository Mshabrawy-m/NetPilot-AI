"""Offline tests for the gated change-loop service (Slice 7).

These prove the safety guarantees: proposals are validated, application is
refused unless the loop is enabled AND the request is approved AND a lab
executor is injected - and the executor is only ever called with the stored
config. Nothing here touches a real device.
"""
import types

import pytest

from services import change_service

GOOD = ["hostname R1", "interface eth0", "ip address 10.0.0.1/24"]


def _propose_ok(session, proposed_by=None):
    return change_service.propose(
        session, device_name="R1", base_lines=["hostname R1"],
        proposed_lines=GOOD, proposed_by=proposed_by)


def test_valid_proposal_is_validated(session):
    req = _propose_ok(session)
    assert req.status == "validated"
    assert "@@" in (req.diff or "")


def test_malicious_proposal_is_rejected(session):
    req = change_service.propose(
        session, device_name="R1", base_lines=[],
        proposed_lines=["interface eth0; rm -rf /"])
    assert req.status == "rejected"


def test_rejected_request_cannot_be_approved(session):
    req = change_service.propose(session, device_name="R1", base_lines=[],
                                 proposed_lines=["reload now"])
    out, msg = change_service.approve(session, req.request_uuid,
                                      approved_by="bob")
    assert out.status == "rejected"
    assert "cannot approve" in msg


def test_self_approval_is_forbidden(session):
    req = _propose_ok(session, proposed_by="alice")
    out, msg = change_service.approve(session, req.request_uuid,
                                      approved_by="alice")
    assert out.status == "validated"
    assert "self-approval" in msg


def test_apply_refused_when_loop_disabled(session):
    req = _propose_ok(session)
    change_service.approve(session, req.request_uuid, approved_by="bob")
    res = change_service.apply(session, req.request_uuid,
                               executor=lambda d, c: True)
    assert res["applied"] is False and "disabled" in res["reason"]
    assert req.status == "approved"  # untouched


def test_apply_refused_without_executor(session, monkeypatch):
    monkeypatch.setenv("NETPILOT_CHANGE_LOOP_ENABLED", "true")
    req = _propose_ok(session)
    change_service.approve(session, req.request_uuid, approved_by="bob")
    res = change_service.apply(session, req.request_uuid, executor=None)
    assert res["applied"] is False and "executor" in res["reason"]


def test_apply_refused_when_not_approved(session, monkeypatch):
    monkeypatch.setenv("NETPILOT_CHANGE_LOOP_ENABLED", "true")
    req = _propose_ok(session)  # only 'validated', not approved
    res = change_service.apply(session, req.request_uuid,
                               executor=lambda d, c: True)
    assert res["applied"] is False and "approved" in res["reason"]


def test_apply_success_calls_executor_with_proposed_config(session, monkeypatch):
    monkeypatch.setenv("NETPILOT_CHANGE_LOOP_ENABLED", "true")
    req = _propose_ok(session)
    change_service.approve(session, req.request_uuid, approved_by="bob")
    calls = []

    def executor(device, lines):
        calls.append((device, list(lines)))
        return types.SimpleNamespace(ok=True, error=None)

    res = change_service.apply(session, req.request_uuid, executor=executor)
    assert res["applied"] is True
    assert req.status == "applied"
    assert calls == [("R1", GOOD)]


def test_apply_records_failure_when_executor_raises(session, monkeypatch):
    monkeypatch.setenv("NETPILOT_CHANGE_LOOP_ENABLED", "true")
    req = _propose_ok(session)
    change_service.approve(session, req.request_uuid, approved_by="bob")

    def boom(device, lines):
        raise RuntimeError("device unreachable")

    res = change_service.apply(session, req.request_uuid, executor=boom)
    assert res["applied"] is False
    assert req.status == "failed"


def test_rollback_restores_base_config(session, monkeypatch):
    monkeypatch.setenv("NETPILOT_CHANGE_LOOP_ENABLED", "true")
    req = _propose_ok(session)
    change_service.approve(session, req.request_uuid, approved_by="bob")
    change_service.apply(session, req.request_uuid,
                         executor=lambda d, c: types.SimpleNamespace(ok=True))
    seen = []

    def executor(device, lines):
        seen.append(list(lines))
        return types.SimpleNamespace(ok=True)

    res = change_service.rollback(session, req.request_uuid, executor=executor)
    assert res["rolled_back"] is True
    assert seen == [["hostname R1"]]  # base config restored
    # the executor cannot read the device back, so the restoration could NOT
    # be verified - and it is not claimed as such
    assert res["restored"] is False
    assert req.status == "failed"
    assert "restoration could not be verified" in (req.apply_detail or "")


def test_rollback_prefers_the_executors_real_undo(session, monkeypatch):
    """A rollback must UNDO the change, not merge the base on top of it."""
    monkeypatch.setenv("NETPILOT_CHANGE_LOOP_ENABLED", "true")
    req = _propose_ok(session)
    change_service.approve(session, req.request_uuid, approved_by="bob")
    change_service.apply(session, req.request_uuid,
                         executor=lambda d, c: types.SimpleNamespace(ok=True))
    calls = []

    class UndoExecutor:
        def rollback_config(self, device, proposed, base):
            calls.append(("rollback_config", device, list(proposed),
                          list(base)))
            return types.SimpleNamespace(ok=True, detail="undone")

        def read_config(self, device):
            return ["hostname R1"]

    res = change_service.rollback(session, req.request_uuid,
                                  executor=UndoExecutor())
    assert res["rolled_back"] is True
    assert res["restored"] is True
    assert req.status == "rolled_back"
    # the executor's real undo was used, not a re-apply of the base config
    assert calls and calls[0][0] == "rollback_config"
    assert calls[0][2] == GOOD          # what to undo
    assert calls[0][3] == ["hostname R1"]  # what to restore


def test_a_rollback_that_leaves_the_change_active_is_not_claimed(session,
                                                                 monkeypatch):
    monkeypatch.setenv("NETPILOT_CHANGE_LOOP_ENABLED", "true")
    req = _propose_ok(session)
    change_service.approve(session, req.request_uuid, approved_by="bob")
    change_service.apply(session, req.request_uuid,
                         executor=lambda d, c: types.SimpleNamespace(ok=True))

    class StillThere:
        def rollback_config(self, device, proposed, base):
            return types.SimpleNamespace(ok=True, detail="undone")

        def read_config(self, device):
            # the change is still active on the device
            return ["hostname R1"] + list(GOOD)

    res = change_service.rollback(session, req.request_uuid,
                                  executor=StillThere())
    assert res["rolled_back"] is True
    assert res["restored"] is False
    assert req.status == "failed"


def test_cancel_before_apply(session):
    req = _propose_ok(session)
    res = change_service.cancel(session, req.request_uuid)
    assert res["cancelled"] is True and req.status == "cancelled"
