"""Offline tests for the gated change-loop service (Slice 7).

These prove the safety guarantees: proposals are validated, application is
refused unless the loop is enabled AND the request is approved AND a lab
executor is injected - and the executor is only ever called with the stored
config. Nothing here touches a real device.

Phase B1 additionally pins the identity/approval rules: approval needs a
distinct *authenticated* principal and a proposal from an authenticated
requester; open mode fails closed without inventing an identity; reasons and
expiry are persisted and audited; invalid/repeated transitions are refused.
"""
import datetime as dt
import types

import pytest

from database import repository
from database.models import db_utcnow
from services import change_service

GOOD = ["hostname R1", "interface eth0", "ip address 10.0.0.1/24"]


def _propose_ok(session, proposed_by="alice", authenticated=True, **kwargs):
    return change_service.propose(
        session, device_name="R1", base_lines=["hostname R1"],
        proposed_lines=GOOD, proposed_by=proposed_by,
        proposer_authenticated=authenticated, **kwargs)


def _approve_ok(session, req, approver="bob", reason=None):
    return change_service.approve(
        session, req.request_uuid, approved_by=approver,
        authenticated=True, reason=reason)


def test_valid_proposal_is_validated(session):
    req = _propose_ok(session)
    assert req.status == "validated"
    assert "@@" in (req.diff or "")


def test_malicious_proposal_is_rejected(session):
    req = change_service.propose(
        session, device_name="R1", base_lines=[],
        proposed_lines=["interface eth0; rm -rf /"])
    assert req.status == "rejected"
    assert req.rejection_reason
    events = repository.get_change_audit_events(session, req.id)
    assert [e.action for e in events] == ["proposed", "rejected"]


def test_rejected_request_cannot_be_approved(session):
    req = change_service.propose(session, device_name="R1", base_lines=[],
                                 proposed_lines=["reload now"])
    out, msg = change_service.approve(session, req.request_uuid,
                                      approved_by="bob",
                                      authenticated=True)
    assert out.status == "rejected"
    assert "cannot approve" in msg


def test_self_approval_is_forbidden(session):
    req = _propose_ok(session, proposed_by="alice")
    out, msg = change_service.approve(session, req.request_uuid,
                                      approved_by="alice",
                                      authenticated=True)
    assert out.status == "validated"
    assert "self-approval" in msg
    assert out.approved_by is None


def test_apply_refused_when_loop_disabled(session):
    req = _propose_ok(session)
    _approve_ok(session, req)
    res = change_service.apply(session, req.request_uuid,
                               executor=lambda d, c: True)
    assert res["applied"] is False and "disabled" in res["reason"]
    assert req.status == "approved"  # untouched


def test_apply_refused_without_executor(session, monkeypatch):
    monkeypatch.setenv("NETPILOT_CHANGE_LOOP_ENABLED", "true")
    req = _propose_ok(session)
    _approve_ok(session, req)
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
    _approve_ok(session, req)
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
    _approve_ok(session, req)

    def boom(device, lines):
        raise RuntimeError("device unreachable")

    res = change_service.apply(session, req.request_uuid, executor=boom)
    assert res["applied"] is False
    assert req.status == "failed"


def test_rollback_restores_base_config(session, monkeypatch):
    monkeypatch.setenv("NETPILOT_CHANGE_LOOP_ENABLED", "true")
    req = _propose_ok(session)
    _approve_ok(session, req)
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
    _approve_ok(session, req)
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
    _approve_ok(session, req)
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


# ==========================================================================
# Phase B1 - identity separation, reasons, expiry, audit and transitions
# ==========================================================================
def test_auth_enabled_distinct_principal_can_approve(session):
    req = _propose_ok(session, proposed_by="alice")
    out, msg = change_service.approve(
        session, req.request_uuid, approved_by="bob", authenticated=True,
        reason="reviewed in the change window")
    assert msg == "approved"
    assert out.status == "approved"
    assert out.approved_by == "bob"
    assert out.approval_reason == "reviewed in the change window"


def test_auth_enabled_self_approval_is_rejected(session):
    req = _propose_ok(session, proposed_by="alice")
    out, msg = change_service.approve(
        session, req.request_uuid, approved_by="alice", authenticated=True)
    assert out.status == "validated"
    assert "self-approval" in msg
    assert out.approved_by is None


def test_open_mode_proposal_does_not_invent_anonymous_identity(session):
    req = _propose_ok(session, proposed_by=None, authenticated=False)
    assert req.proposed_by is None
    assert req.proposed_by_authenticated is False
    events = repository.get_change_audit_events(session, req.id)
    assert events[0].action == "proposed"
    assert events[0].actor is None
    assert events[0].actor_authenticated is False


def test_open_mode_anonymous_approval_is_refused_not_called_self_approval(
        session):
    req = _propose_ok(session, proposed_by=None, authenticated=False)
    out, msg = change_service.approve(
        session, req.request_uuid, approved_by="anonymous",
        authenticated=False)
    assert out.status == "validated"
    assert "authenticated" in msg
    assert "self-approval" not in msg
    assert out.approved_by is None


def test_authenticated_approver_cannot_approve_unverified_requester(session):
    """Fail closed: without a verified requester, separation cannot be proven."""
    req = _propose_ok(session, proposed_by=None, authenticated=False)
    out, msg = change_service.approve(
        session, req.request_uuid, approved_by="bob", authenticated=True)
    assert out.status == "validated"
    assert "authenticated requester" in msg
    assert out.approved_by is None


def test_approval_reason_is_persisted_and_audited(session):
    req = _propose_ok(session, proposed_by="alice")
    out, msg = change_service.approve(
        session, req.request_uuid, approved_by="bob", authenticated=True,
        reason="peer review complete")
    assert msg == "approved"
    assert out.approval_reason == "peer review complete"
    events = repository.get_change_audit_events(session, req.id)
    approved = [e for e in events if e.action == "approved"]
    assert len(approved) == 1
    assert approved[0].actor == "bob"
    assert approved[0].actor_authenticated is True
    assert approved[0].from_status == "validated"
    assert approved[0].to_status == "approved"
    assert approved[0].reason == "peer review complete"


def test_rejection_reason_is_required_persisted_and_audited(session):
    req = _propose_ok(session)
    out, msg = change_service.reject(
        session, req.request_uuid, rejected_by="bob", authenticated=True,
        reason="   ")
    assert out.status == "validated"
    assert "requires a reason" in msg

    out, msg = change_service.reject(
        session, req.request_uuid, rejected_by="bob", authenticated=True,
        reason="unsafe in the current maintenance window")
    assert msg == "rejected"
    assert out.status == "rejected"
    assert out.rejected_by == "bob"
    assert out.rejection_reason == "unsafe in the current maintenance window"
    events = repository.get_change_audit_events(session, req.id)
    rejected = [e for e in events if e.action == "rejected"]
    assert len(rejected) == 1
    assert rejected[0].actor == "bob"
    assert rejected[0].actor_authenticated is True
    assert rejected[0].reason == "unsafe in the current maintenance window"


def test_open_mode_rejection_is_allowed_and_audited_without_identity(session):
    req = _propose_ok(session, proposed_by=None, authenticated=False)
    out, msg = change_service.reject(
        session, req.request_uuid, rejected_by=None, authenticated=False,
        reason="not needed for this window")
    assert msg == "rejected"
    assert out.status == "rejected"
    assert out.rejected_by is None
    events = repository.get_change_audit_events(session, req.id)
    rejected = [e for e in events if e.action == "rejected"]
    assert rejected[0].actor is None
    assert rejected[0].actor_authenticated is False


def test_expired_request_cannot_be_approved(session):
    past = db_utcnow() - dt.timedelta(hours=2)
    req = _propose_ok(session, now=past, ttl_minutes=30)
    assert req.expires_at is not None
    out, msg = change_service.approve(
        session, req.request_uuid, approved_by="bob", authenticated=True)
    assert out.status == "expired"
    assert "expired" in msg
    assert out.approved_by is None
    events = repository.get_change_audit_events(session, req.id)
    expired = [e for e in events if e.action == "expired"]
    assert len(expired) == 1
    assert expired[0].actor is None
    assert expired[0].actor_authenticated is False
    assert expired[0].to_status == "expired"


def test_expired_approved_request_is_not_applied(session, monkeypatch):
    monkeypatch.setenv("NETPILOT_CHANGE_LOOP_ENABLED", "true")
    req = _propose_ok(session)
    _approve_ok(session, req)
    req.expires_at = db_utcnow() - dt.timedelta(minutes=1)
    session.commit()
    calls = []

    def executor(device, lines):
        calls.append((device, lines))
        return True

    res = change_service.apply(session, req.request_uuid, executor=executor)
    assert res["applied"] is False
    assert res.get("expired") is True
    assert "expired" in res["reason"]
    assert req.status == "expired"
    assert calls == []          # no device I/O after expiry


def test_expire_due_requests_marks_pending_requests(session):
    req = _propose_ok(session, now=db_utcnow() - dt.timedelta(days=1),
                      ttl_minutes=1)
    assert change_service.expire_due_requests(session) == 1
    assert req.status == "expired"


def test_invalid_and_repeated_transitions_are_rejected(session, monkeypatch):
    monkeypatch.setenv("NETPILOT_CHANGE_LOOP_ENABLED", "true")
    req = _propose_ok(session)
    out, msg = _approve_ok(session, req)
    assert msg == "approved"

    # repeated approval
    out, msg = _approve_ok(session, req)
    assert out.status == "approved"
    assert "cannot approve from status 'approved'" in msg

    # rejecting an already-approved request
    out, msg = change_service.reject(
        session, req.request_uuid, rejected_by="bob", authenticated=True,
        reason="too late")
    assert out.status == "approved"
    assert "cannot reject" in msg

    # applying a merely validated request
    req2 = _propose_ok(session)
    res = change_service.apply(session, req2.request_uuid,
                               executor=lambda d, c: True)
    assert res["applied"] is False and "approved" in res["reason"]

    # rolling back a request that was never applied
    res = change_service.rollback(session, req2.request_uuid,
                                  executor=lambda d, c: True)
    assert res["rolled_back"] is False and "only an applied" in res["reason"]

    # cancelling twice
    res = change_service.cancel(
        session, req2.request_uuid, cancelled_by="bob", authenticated=True)
    assert res["cancelled"] is True
    res = change_service.cancel(
        session, req2.request_uuid, cancelled_by="bob", authenticated=True)
    assert res["cancelled"] is False
    assert "cannot cancel from status 'cancelled'" in res["reason"]


def test_cancellation_records_actor_reason_and_audit(session):
    req = _propose_ok(session)
    res = change_service.cancel(
        session, req.request_uuid, cancelled_by="bob", authenticated=True,
        reason="superseded by a newer proposal")
    assert res["cancelled"] is True
    assert req.cancelled_by == "bob"
    assert req.cancellation_reason == "superseded by a newer proposal"
    events = repository.get_change_audit_events(session, req.id)
    cancelled = [e for e in events if e.action == "cancelled"]
    assert len(cancelled) == 1
    assert cancelled[0].actor == "bob"
    assert cancelled[0].actor_authenticated is True
    assert cancelled[0].reason == "superseded by a newer proposal"


def test_approval_performs_no_configuration_application_or_device_io(
        session, monkeypatch):
    from network import lab_executor

    def boom(*args, **kwargs):
        raise AssertionError("device I/O must not happen during approval")

    monkeypatch.setattr(lab_executor.LabExecutor, "__call__", boom)
    monkeypatch.setattr(lab_executor.LabExecutor, "read_config", boom)
    req = _propose_ok(session)
    out, msg = _approve_ok(session, req)
    assert msg == "approved"
    assert out.applied_at is None
    assert out.status == "approved"
