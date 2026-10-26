"""Testable action layer for the Ops Center (Phase B3).

Every state-changing button on ``pages/11_Ops_Center.py`` delegates here.
This module owns the short transaction, maps service refusals to user
feedback, and turns unexpected exceptions into a structured error result
instead of a Streamlit red box.

Safety properties:

* It never injects an executor, opens a device connection, runs a ping or
  traceroute, or changes ``change_loop_enabled`` / ``lab_execution_enabled``.
* Change approvals/rejections/cancellations still go through
  ``services.change_service``; this module only supplies the session and
  formats the outcome.
* A refusal is reported as ``ok=False``. It is never converted into a
  success message.
* Alert state changes are re-read inside the session, so a stale detached
  row cannot silently overwrite a newer status.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from database import repository
from database.connection import get_session
from database.models import AlertEvent, AlertRule, db_utcnow
from services import alert_service, auth as auth_service, change_service

_LEVELS = ("success", "info", "warning", "error")


@dataclass(frozen=True)
class ActionResult:
    """One user-facing outcome from an Ops Center action."""

    ok: bool
    message: str
    level: str = "success"
    data: Any = None

    def __post_init__(self) -> None:
        if self.level not in _LEVELS:
            raise ValueError(f"unsupported action level: {self.level!r}")


def _prefix(value: str | None) -> str:
    return (value or "?")[:8]


def _error_message(prefix: str, exc: Exception) -> str:
    detail = f"{type(exc).__name__}: {str(exc)}".strip()
    return f"{prefix}: {detail[:240]}"


def _execute(fn: Callable[[Any], ActionResult], *,
             failure_prefix: str,
             include_exception_detail: bool = True) -> ActionResult:
    """Run one action in its own session and never leak an exception."""
    session = get_session()
    try:
        outcome = fn(session)
        if not isinstance(outcome, ActionResult):
            raise TypeError("action returned an unexpected result")
        session.commit()
        return outcome
    except Exception as exc:  # noqa: BLE001 - UI boundary must not crash
        session.rollback()
        if include_exception_detail:
            return ActionResult(False, _error_message(failure_prefix, exc),
                                "error")
        return ActionResult(False, failure_prefix, "error")
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Change-loop actions
# ---------------------------------------------------------------------------
def approve_request(request_uuid: str, *, approved_by: str | None,
                    authenticated: bool, reason: str | None = None
                    ) -> ActionResult:
    prefix = _prefix(request_uuid)

    def _do(session):
        req, msg = change_service.approve(
            session, request_uuid, approved_by=approved_by,
            authenticated=authenticated, reason=reason)
        if msg == "approved":
            return ActionResult(
                True, f"Change request {prefix} approved.",
                data={"status": req.status, "request_uuid": request_uuid})
        return ActionResult(
            False, msg, "warning",
            data={"status": getattr(req, "status", None),
                  "request_uuid": request_uuid})

    return _execute(_do, failure_prefix=f"Approval failed for {prefix}")


def reject_request(request_uuid: str, *, rejected_by: str | None,
                   authenticated: bool, reason: str | None = None
                   ) -> ActionResult:
    prefix = _prefix(request_uuid)
    if not (reason or "").strip():
        return ActionResult(False, "Rejection requires a reason.", "warning")

    def _do(session):
        req, msg = change_service.reject(
            session, request_uuid, rejected_by=rejected_by,
            authenticated=authenticated, reason=reason)
        if msg == "rejected":
            return ActionResult(
                True, f"Change request {prefix} rejected.",
                data={"status": req.status, "request_uuid": request_uuid})
        return ActionResult(
            False, msg, "warning",
            data={"status": getattr(req, "status", None),
                  "request_uuid": request_uuid})

    return _execute(_do, failure_prefix=f"Rejection failed for {prefix}")


def cancel_request(request_uuid: str, *, cancelled_by: str | None,
                   authenticated: bool, reason: str | None = None
                   ) -> ActionResult:
    prefix = _prefix(request_uuid)

    def _do(session):
        res = change_service.cancel(
            session, request_uuid, cancelled_by=cancelled_by,
            authenticated=authenticated, reason=reason)
        if res.get("cancelled"):
            return ActionResult(
                True, f"Change request {prefix} cancelled.",
                data={"status": res.get("status"),
                      "request_uuid": request_uuid})
        return ActionResult(
            False, res.get("reason") or "cancellation refused", "warning",
            data={"request_uuid": request_uuid})

    return _execute(_do, failure_prefix=f"Cancellation failed for {prefix}")


def apply_request(request_uuid: str) -> ActionResult:
    """Attempt the guarded UI apply path.

    No executor is passed, so the service must refuse. This function never
    creates a lab executor and therefore cannot perform device I/O.
    """
    prefix = _prefix(request_uuid)

    def _do(session):
        res = change_service.apply(session, request_uuid)  # no executor
        if res.get("applied"):
            return ActionResult(
                True, f"Change request {prefix} applied.",
                data={"status": res.get("status"),
                      "request_uuid": request_uuid})
        return ActionResult(
            False, res.get("reason") or "application refused", "warning",
            data={"status": res.get("status"),
                  "request_uuid": request_uuid})

    return _execute(_do, failure_prefix=f"Application failed for {prefix}")


def propose_change(*, device_name: str, base_lines: list[str],
                   proposed_lines: list[str], data_source: str,
                   description: str | None = None,
                   proposed_by: str | None = None,
                   proposer_authenticated: bool = False,
                   risk_level: str = "low") -> ActionResult:
    if not (device_name or "").strip():
        return ActionResult(False, "Select a device first.", "warning")

    def _do(session):
        req = change_service.propose(
            session, device_name=device_name.strip(),
            base_lines=base_lines, proposed_lines=proposed_lines,
            data_source=data_source, description=description,
            proposed_by=proposed_by,
            proposer_authenticated=proposer_authenticated,
            risk_level=risk_level)
        status = req.status
        return ActionResult(
            status == "validated",
            f"Proposal recorded as {status}.",
            "success" if status == "validated" else "error",
            data={"status": status, "request_uuid": req.request_uuid})

    return _execute(_do, failure_prefix="Could not record the proposal")


# ---------------------------------------------------------------------------
# Alert-rule and alert-event actions
# ---------------------------------------------------------------------------
def create_alert_rule(*, name: str, metric: str,
                      device_name: str | None, condition: str,
                      threshold: float, severity: str) -> ActionResult:
    name = (name or "").strip()
    metric = (metric or "").strip()
    if not name or not metric:
        return ActionResult(False, "Rule name and metric are required.",
                            "warning")

    def _do(session):
        rule = repository.create_alert_rule(
            session, name=name, metric=metric, device_name=device_name,
            condition=condition, threshold=float(threshold),
            severity=severity)
        return ActionResult(True, f"Rule '{rule.name}' created",
                            data={"rule_id": rule.id})

    return _execute(_do, failure_prefix="Could not create the rule")


def toggle_alert_rule(rule_id: int | None) -> ActionResult:
    def _do(session):
        if rule_id is None:
            return ActionResult(False, "Rule no longer exists.", "warning")
        rule = session.get(AlertRule, rule_id)
        if rule is None:
            return ActionResult(False, "Rule no longer exists.", "warning")
        was_enabled = bool(rule.enabled)
        repository.set_alert_rule_enabled(session, rule, not was_enabled)
        return ActionResult(
            True, f"Rule '{rule.name}' "
                  f"{'disabled' if was_enabled else 'enabled'}.",
            data={"rule_id": rule.id, "enabled": not was_enabled})

    return _execute(_do, failure_prefix="Could not update the rule")


def run_alert_cycle(data_source: str) -> ActionResult:
    def _do(session):
        touched = alert_service.run_cycle(session, data_source=data_source)
        return ActionResult(
            True, f"Cycle complete - {len(touched)} series in breach.",
            data={"touched": len(touched)})

    return _execute(_do, failure_prefix="Alert cycle failed")


def acknowledge_alert(alert_id: int | None) -> ActionResult:
    def _do(session):
        if alert_id is None:
            return ActionResult(False, "Alert no longer exists.", "warning")
        event = session.get(AlertEvent, alert_id)
        if event is None:
            return ActionResult(False, "Alert no longer exists.", "warning")
        if event.status != "open":
            return ActionResult(
                False, f"Alert #{alert_id} is already {event.status}; "
                       "acknowledge refused.", "warning")
        repository.acknowledge_alert_event(session, event, db_utcnow())
        return ActionResult(True, f"Alert #{alert_id} acknowledged.",
                            data={"alert_id": alert_id,
                                  "status": "acknowledged"})

    return _execute(_do, failure_prefix=f"Could not acknowledge alert "
                                       f"#{alert_id}")


def resolve_alert(alert_id: int | None) -> ActionResult:
    def _do(session):
        if alert_id is None:
            return ActionResult(False, "Alert no longer exists.", "warning")
        event = session.get(AlertEvent, alert_id)
        if event is None:
            return ActionResult(False, "Alert no longer exists.", "warning")
        if event.status not in ("open", "acknowledged"):
            return ActionResult(
                False, f"Alert #{alert_id} is already {event.status}; "
                       "resolve refused.", "warning")
        repository.resolve_alert_event(session, event, db_utcnow())
        return ActionResult(True, f"Alert #{alert_id} resolved.",
                            data={"alert_id": alert_id, "status": "resolved"})

    return _execute(_do, failure_prefix=f"Could not resolve alert #{alert_id}")


# ---------------------------------------------------------------------------
# Admin user action
# ---------------------------------------------------------------------------
def create_user(*, username: str, password: str, role: str,
                iterations: int) -> ActionResult:
    username = (username or "").strip()
    if not username:
        return ActionResult(False, "Username is required.", "warning")
    try:
        auth_service.validate_role(role)
    except ValueError as exc:
        return ActionResult(False, str(exc), "warning")
    ok, msg = auth_service.password_policy(password)
    if not ok:
        return ActionResult(False, msg, "warning")

    def _do(session):
        repository.create_user(
            session, username=username,
            password_hash=auth_service.hash_password(password, iterations),
            role=role)
        return ActionResult(True, f"User '{username}' created as {role}.")

    return _execute(
        _do, failure_prefix="Could not create user (name may already exist).",
        include_exception_detail=False)


# ---------------------------------------------------------------------------
# Safe display helper
# ---------------------------------------------------------------------------
def parse_stored_json(text: str | None) -> tuple[Any, str | None]:
    """Parse a stored JSON field without crashing the audit panel.

    Returns ``(value, error)``. On malformed data the error is explicit and
    the caller can show the raw stored text instead of inventing a structure.
    """
    if not text:
        return None, None
    try:
        return json.loads(text), None
    except (TypeError, ValueError) as exc:
        return None, (f"Stored JSON is invalid ({type(exc).__name__}); "
                      "showing the raw value.")
