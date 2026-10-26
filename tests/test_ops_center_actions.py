"""Focused Phase B3 tests for the Ops Center action layer.

Every state-changing button on ``pages/11_Ops_Center.py`` delegates to
``services.ops_center_actions``. These tests pin the behaviour that layer must
provide:

* a success is reported only when the change was persisted;
* a refusal (validation, identity, expiry, stale selection) is reported as a
  refusal and leaves the database untouched;
* an unexpected service exception becomes structured error feedback instead of
  an uncaught Streamlit exception;
* repeated clicks cannot duplicate a transition;
* an action only ever touches the request/rule/alert ID it was given.

The tests are completely offline: an autouse fixture makes any socket,
subprocess or change-execution call fail immediately. No device is contacted
and no configuration is applied.
"""
from __future__ import annotations

import datetime as dt
import os
import socket
import subprocess
import types

import pytest

from database import repository
from database.connection import get_session
from database.models import db_utcnow
from services import change_service
from services import ops_center_actions as actions

GOOD = ["hostname R1", "interface eth0", "ip address 10.0.0.1/24"]
PASSWORD = "long-enough-password-123"


# ---------------------------------------------------------------------------
# Safety net: fail fast on any unintended I/O
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def no_device_or_network_io(monkeypatch, db_url):
    """Any socket, shell or executor call during these tests is a defect.

    Depending on ``db_url`` also gives every test its own temporary database,
    so no test can see (or mutate) the developer's local data.
    """
    def fail(*_args, **_kwargs):
        raise AssertionError(
            "device/network I/O attempted by an Ops Center action test")

    monkeypatch.setattr(socket.socket, "connect", fail)
    monkeypatch.setattr(socket, "create_connection", fail)
    monkeypatch.setattr(subprocess, "run", fail)
    monkeypatch.setattr(subprocess, "Popen", fail)
    monkeypatch.setattr(os, "system", fail)

    from services import change_execution
    monkeypatch.setattr(change_execution, "execute_change", fail)
    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _propose(*, proposed_by="alice", authenticated=True, ttl_minutes=None,
             now=None, data_source="demo", lines=None, device="R1"):
    session = get_session()
    try:
        req = change_service.propose(
            session, device_name=device, base_lines=["hostname R1"],
            proposed_lines=list(lines if lines is not None else GOOD),
            data_source=data_source, proposed_by=proposed_by,
            proposer_authenticated=authenticated,
            ttl_minutes=ttl_minutes, now=now)
        return req.request_uuid
    finally:
        session.close()


def _approve(uuid, *, by="bob", now=None):
    session = get_session()
    try:
        req, msg = change_service.approve(
            session, uuid, approved_by=by, authenticated=True, now=now)
        assert msg == "approved", msg
        return req.request_uuid
    finally:
        session.close()


def _snapshot(uuid):
    session = get_session()
    try:
        req = repository.get_change_request(session, uuid)
        assert req is not None
        return {
            "id": req.id,
            "status": req.status,
            "approved_by": req.approved_by,
            "approval_reason": req.approval_reason,
            "rejection_reason": req.rejection_reason,
            "cancellation_reason": req.cancellation_reason,
            "cancelled_by": req.cancelled_by,
            "applied_at": req.applied_at,
            "expired_at": req.expired_at,
            "validation_json": req.validation_json,
        }
    finally:
        session.close()


def _audit(uuid):
    session = get_session()
    try:
        req = repository.get_change_request(session, uuid)
        assert req is not None
        return [{
            "action": event.action,
            "actor": event.actor,
            "authenticated": event.actor_authenticated,
            "from_status": event.from_status,
            "to_status": event.to_status,
            "reason": event.reason,
        } for event in repository.get_change_audit_events(session, req.id)]
    finally:
        session.close()


def _make_alert(*, device="R1", metric="ping.avg_rtt_ms", value=150.0):
    session = get_session()
    try:
        rule = repository.create_alert_rule(
            session, name=f"rule-{device}-{metric}", metric=metric,
            device_name=device, condition="above", threshold=100.0,
            severity="warning")
        sample = types.SimpleNamespace(
            device_name=device, interface_name=None, metric=metric,
            value=value, data_source="demo")
        event = repository.record_alert_firing(
            session, rule, sample, fired_at=db_utcnow())
        session.commit()
        return event.id
    finally:
        session.close()


def _alert_snapshot(alert_id):
    session = get_session()
    try:
        event = session.get(repository.AlertEvent, alert_id)
        assert event is not None
        return {
            "status": event.status,
            "acknowledged_at": event.acknowledged_at,
            "resolved_at": event.resolved_at,
        }
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Change-loop actions
# ---------------------------------------------------------------------------
def test_approve_success_persists_reason_and_audit():
    uuid = _propose()
    outcome = actions.approve_request(
        uuid, approved_by="bob", authenticated=True,
        reason="reviewed the impact report")

    assert outcome.ok is True
    assert outcome.level == "success"
    assert "approved" in outcome.message

    snap = _snapshot(uuid)
    assert snap["status"] == "approved"
    assert snap["approved_by"] == "bob"
    assert snap["approval_reason"] == "reviewed the impact report"

    audit = _audit(uuid)
    assert [event["action"] for event in audit] == ["proposed", "approved"]
    assert audit[-1]["actor"] == "bob"
    assert audit[-1]["authenticated"] is True
    assert audit[-1]["from_status"] == "validated"
    assert audit[-1]["to_status"] == "approved"
    assert audit[-1]["reason"] == "reviewed the impact report"


def test_approve_open_mode_fails_closed_and_reports_refusal():
    uuid = _propose(proposed_by=None, authenticated=False)

    outcome = actions.approve_request(
        uuid, approved_by=None, authenticated=False)

    assert outcome.ok is False
    assert outcome.level == "warning"
    assert "authenticated principal" in outcome.message
    snap = _snapshot(uuid)
    assert snap["status"] == "validated"
    assert snap["approved_by"] is None
    assert "approved" not in [event["action"] for event in _audit(uuid)]


def test_self_approval_is_refused_without_changing_the_request():
    uuid = _propose(proposed_by="alice", authenticated=True)

    outcome = actions.approve_request(
        uuid, approved_by="alice", authenticated=True)

    assert outcome.ok is False
    assert "self-approval" in outcome.message
    assert _snapshot(uuid)["status"] == "validated"


def test_approve_missing_request_is_a_warning_not_a_crash():
    outcome = actions.approve_request(
        "does-not-exist", approved_by="bob", authenticated=True)

    assert outcome.ok is False
    assert outcome.level == "warning"
    assert outcome.message == "not found"


def test_repeated_approval_does_not_duplicate_the_transition():
    uuid = _propose()

    first = actions.approve_request(
        uuid, approved_by="bob", authenticated=True)
    second = actions.approve_request(
        uuid, approved_by="bob", authenticated=True)

    assert first.ok is True
    assert second.ok is False
    assert "cannot approve from status 'approved'" in second.message
    assert [event["action"] for event in _audit(uuid)].count("approved") == 1


def test_approval_only_touches_the_selected_request():
    selected = _propose()
    other = _propose()

    outcome = actions.approve_request(
        selected, approved_by="bob", authenticated=True)

    assert outcome.ok is True
    assert _snapshot(selected)["status"] == "approved"
    assert _snapshot(other)["status"] == "validated"


def test_expired_request_is_transitioned_and_approval_refused():
    uuid = _propose(ttl_minutes=1, now=dt.datetime(2020, 1, 1, 0, 0, 0))

    outcome = actions.approve_request(
        uuid, approved_by="bob", authenticated=True)

    assert outcome.ok is False
    assert "expired" in outcome.message
    snap = _snapshot(uuid)
    assert snap["status"] == "expired"
    assert snap["approved_by"] is None
    actions_audit = [event["action"] for event in _audit(uuid)]
    assert "expired" in actions_audit
    assert "approved" not in actions_audit


def test_reject_requires_a_reason_and_persists_it():
    uuid = _propose()

    missing = actions.reject_request(
        uuid, rejected_by="bob", authenticated=True, reason="   ")
    assert missing.ok is False
    assert missing.level == "warning"
    assert "requires a reason" in missing.message
    assert _snapshot(uuid)["status"] == "validated"

    outcome = actions.reject_request(
        uuid, rejected_by="bob", authenticated=True,
        reason="breaks the management path")
    assert outcome.ok is True
    snap = _snapshot(uuid)
    assert snap["status"] == "rejected"
    assert snap["rejection_reason"] == "breaks the management path"

    audit = _audit(uuid)
    assert [event["action"] for event in audit] == ["proposed", "rejected"]
    assert audit[-1]["actor"] == "bob"
    assert audit[-1]["reason"] == "breaks the management path"


def test_repeated_rejection_is_refused():
    uuid = _propose()
    assert actions.reject_request(
        uuid, rejected_by="bob", authenticated=True,
        reason="first decision").ok

    second = actions.reject_request(
        uuid, rejected_by="bob", authenticated=True,
        reason="second decision")

    assert second.ok is False
    assert "cannot reject from status 'rejected'" in second.message
    assert _snapshot(uuid)["rejection_reason"] == "first decision"


def test_cancel_success_is_persisted_and_repeated_cancel_refused():
    uuid = _propose()

    outcome = actions.cancel_request(
        uuid, cancelled_by="bob", authenticated=True,
        reason="superseded")
    assert outcome.ok is True
    snap = _snapshot(uuid)
    assert snap["status"] == "cancelled"
    assert snap["cancellation_reason"] == "superseded"
    assert snap["cancelled_by"] == "bob"

    audit = _audit(uuid)
    assert [event["action"] for event in audit] == ["proposed", "cancelled"]
    assert audit[-1]["from_status"] == "validated"
    assert audit[-1]["to_status"] == "cancelled"

    second = actions.cancel_request(
        uuid, cancelled_by="bob", authenticated=True, reason="again")
    assert second.ok is False
    assert "cannot cancel from status 'cancelled'" in second.message


def test_cancel_missing_request_is_a_warning():
    outcome = actions.cancel_request(
        "missing", cancelled_by="bob", authenticated=True)

    assert outcome.ok is False
    assert outcome.level == "warning"
    assert outcome.message == "not found"


def test_apply_injects_no_executor_and_never_applies(monkeypatch):
    """The UI action must never supply an executor.

    The change-loop flag is deliberately left at its safe default (off), so
    this test does not enable execution. The spy proves the action layer still
    calls the service without an executor, and the request is untouched.
    """
    uuid = _approve(_propose())
    calls = []
    original = change_service.apply

    def spy(session, request_uuid, **kwargs):
        calls.append(kwargs)
        return original(session, request_uuid, **kwargs)

    monkeypatch.setattr(change_service, "apply", spy)

    outcome = actions.apply_request(uuid)

    assert outcome.ok is False
    assert outcome.level == "warning"
    assert calls
    assert calls[0].get("executor") is None
    snap = _snapshot(uuid)
    assert snap["status"] == "approved"
    assert snap["applied_at"] is None


def test_apply_refuses_when_the_change_loop_is_disabled():
    uuid = _approve(_propose())

    outcome = actions.apply_request(uuid)

    assert outcome.ok is False
    assert "change loop disabled" in outcome.message
    assert _snapshot(uuid)["status"] == "approved"


def test_apply_refuses_an_expired_approved_request():
    uuid = _propose(ttl_minutes=1, now=dt.datetime(2020, 1, 1, 0, 0, 0))
    _approve(uuid, now=dt.datetime(2020, 1, 1, 0, 0, 0))

    outcome = actions.apply_request(uuid)

    assert outcome.ok is False
    assert "expired" in outcome.message
    assert _snapshot(uuid)["status"] == "expired"


def test_propose_change_reports_validation_outcome():
    outcome = actions.propose_change(
        device_name="R1", base_lines=["hostname R1"],
        proposed_lines=GOOD, data_source="demo",
        proposed_by="alice", proposer_authenticated=True)

    assert outcome.ok is True
    assert outcome.level == "success"
    assert outcome.data["status"] == "validated"
    assert _snapshot(outcome.data["request_uuid"])["status"] == "validated"


def test_propose_change_does_not_claim_success_for_invalid_config():
    outcome = actions.propose_change(
        device_name="R1", base_lines=[],
        proposed_lines=["interface eth0; rm -rf /"], data_source="demo")

    assert outcome.ok is False
    assert outcome.level == "error"
    assert "rejected" in outcome.message
    assert _snapshot(outcome.data["request_uuid"])["status"] == "rejected"


def test_propose_change_requires_a_device():
    outcome = actions.propose_change(
        device_name="   ", base_lines=[], proposed_lines=GOOD,
        data_source="demo")

    assert outcome.ok is False
    assert outcome.level == "warning"
    assert "Select a device" in outcome.message


# ---------------------------------------------------------------------------
# Alert-rule and alert-event actions
# ---------------------------------------------------------------------------
def test_create_alert_rule_persists_and_reports_success():
    outcome = actions.create_alert_rule(
        name="high latency", metric="ping.avg_rtt_ms",
        device_name="R1", condition="above", threshold=100.0,
        severity="warning")

    assert outcome.ok is True
    assert outcome.data["rule_id"]
    session = get_session()
    try:
        names = [rule.name for rule in repository.get_alert_rules(session)]
    finally:
        session.close()
    assert "high latency" in names


def test_create_alert_rule_validates_required_fields():
    outcome = actions.create_alert_rule(
        name="  ", metric="ping.avg_rtt_ms", device_name=None,
        condition="above", threshold=1.0, severity="warning")

    assert outcome.ok is False
    assert outcome.level == "warning"
    assert "required" in outcome.message


def test_create_alert_rule_duplicate_is_reported_as_an_error():
    first = actions.create_alert_rule(
        name="duplicate", metric="ping.avg_rtt_ms", device_name=None,
        condition="above", threshold=1.0, severity="warning")
    assert first.ok is True

    second = actions.create_alert_rule(
        name="duplicate", metric="ping.avg_rtt_ms", device_name=None,
        condition="above", threshold=2.0, severity="warning")

    assert second.ok is False
    assert second.level == "error"
    assert "Could not create the rule" in second.message


def test_toggle_alert_rule_persists_and_stale_id_is_refused():
    created = actions.create_alert_rule(
        name="toggle-me", metric="ping.avg_rtt_ms", device_name=None,
        condition="above", threshold=1.0, severity="warning")
    rule_id = created.data["rule_id"]

    disabled = actions.toggle_alert_rule(rule_id)
    assert disabled.ok is True
    assert disabled.data["enabled"] is False

    enabled = actions.toggle_alert_rule(rule_id)
    assert enabled.ok is True
    assert enabled.data["enabled"] is True

    missing = actions.toggle_alert_rule(999_999)
    assert missing.ok is False
    assert missing.level == "warning"
    assert "no longer exists" in missing.message

    none_id = actions.toggle_alert_rule(None)
    assert none_id.ok is False
    assert "no longer exists" in none_id.message


def test_run_alert_cycle_reports_the_persisted_result():
    outcome = actions.run_alert_cycle("demo")

    assert outcome.ok is True
    assert "series in breach" in outcome.message
    assert outcome.data["touched"] == 0


def test_acknowledge_alert_persists_and_repeated_click_is_refused():
    alert_id = _make_alert(device="R1")

    outcome = actions.acknowledge_alert(alert_id)
    assert outcome.ok is True
    snap = _alert_snapshot(alert_id)
    assert snap["status"] == "acknowledged"
    assert snap["acknowledged_at"] is not None

    second = actions.acknowledge_alert(alert_id)
    assert second.ok is False
    assert "already acknowledged" in second.message


def test_resolve_alert_persists_and_repeated_click_is_refused():
    alert_id = _make_alert(device="R2")

    outcome = actions.resolve_alert(alert_id)
    assert outcome.ok is True
    snap = _alert_snapshot(alert_id)
    assert snap["status"] == "resolved"
    assert snap["resolved_at"] is not None

    second = actions.resolve_alert(alert_id)
    assert second.ok is False
    assert "already resolved" in second.message


def test_alert_actions_handle_missing_ids():
    for outcome in (actions.acknowledge_alert(999_999),
                    actions.resolve_alert(999_999),
                    actions.acknowledge_alert(None),
                    actions.resolve_alert(None)):
        assert outcome.ok is False
        assert outcome.level == "warning"
        assert "no longer exists" in outcome.message


# ---------------------------------------------------------------------------
# Admin action
# ---------------------------------------------------------------------------
def test_create_user_validates_password_and_persists_a_hash():
    weak = actions.create_user(
        username="carol", password="short", role="viewer",
        iterations=50_000)
    assert weak.ok is False
    assert weak.level == "warning"
    assert "at least" in weak.message

    blank = actions.create_user(
        username="   ", password=PASSWORD, role="viewer",
        iterations=50_000)
    assert blank.ok is False
    assert "Username is required" in blank.message

    outcome = actions.create_user(
        username="carol", password=PASSWORD, role="viewer",
        iterations=50_000)
    assert outcome.ok is True

    session = get_session()
    try:
        user = repository.get_user_by_name(session, "carol")
        assert user is not None
        assert user.role == "viewer"
        assert user.password_hash != PASSWORD
    finally:
        session.close()


def test_create_user_rejects_an_unknown_role_before_writing():
    outcome = actions.create_user(
        username="eve", password=PASSWORD, role="superuser",
        iterations=50_000)

    assert outcome.ok is False
    assert outcome.level == "warning"
    assert "role must be one of" in outcome.message

    session = get_session()
    try:
        assert repository.get_user_by_name(session, "eve") is None
    finally:
        session.close()


def test_create_user_duplicate_reports_generic_error_without_details():
    assert actions.create_user(
        username="dave", password=PASSWORD, role="viewer",
        iterations=50_000).ok

    duplicate = actions.create_user(
        username="dave", password=PASSWORD, role="viewer",
        iterations=50_000)

    assert duplicate.ok is False
    assert duplicate.level == "error"
    assert duplicate.message == (
        "Could not create user (name may already exist).")


# ---------------------------------------------------------------------------
# Error boundary and display helper
# ---------------------------------------------------------------------------
def test_unexpected_service_exception_becomes_error_feedback(monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(repository, "create_alert_rule", boom)

    outcome = actions.create_alert_rule(
        name="boom", metric="ping.avg_rtt_ms", device_name=None,
        condition="above", threshold=1.0, severity="warning")

    assert outcome.ok is False
    assert outcome.level == "error"
    assert "Could not create the rule" in outcome.message
    assert "RuntimeError: storage unavailable" in outcome.message


def test_parse_stored_json_is_safe_for_invalid_and_empty_values():
    assert actions.parse_stored_json(None) == (None, None)
    assert actions.parse_stored_json("") == (None, None)
    assert actions.parse_stored_json('{"ok": true}') == ({"ok": True}, None)

    value, error = actions.parse_stored_json("{not valid json")
    assert value is None
    assert error is not None
    assert "invalid" in error.lower()


def test_action_result_rejects_unknown_feedback_levels():
    with pytest.raises(ValueError):
        actions.ActionResult(True, "message", "not-a-level")
