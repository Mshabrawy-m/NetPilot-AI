"""Gated change-loop service (Slice 7).

Safety model (lab-only, approval-bound, fail-closed):

* Proposals are validated by the SAME deterministic FRR validator the Phase-3
  verifier uses - shell metacharacters and execution/persistence commands are
  rejected, so a "config" can never smuggle a command.
* A proposal only records a requester when that requester is authenticated.
  Open mode (auth disabled) records **no** identity rather than inventing
  ``anonymous`` as if it were a person.
* ``approve`` requires an authenticated approver AND a proposal from an
  authenticated requester, and forbids self-approval. When those identities
  cannot be established (open mode) approval fails closed; it is never
  weakened by treating an anonymous click as a second person.
* Approval windows are enforced here, not only in the UI. An expired request
  is moved to ``expired`` and can no longer be approved or applied.
* Every proposal, approval, rejection, cancellation and expiry is written to
  the append-only ``change_audit_events`` trail with its actor (if verified),
  the status transition and the bounded reason.
* ``apply`` changes nothing unless ALL THREE hold:
    (1) ``NETPILOT_CHANGE_LOOP_ENABLED`` is set,
    (2) the request is in status ``approved`` and has not expired,
    (3) a lab ``executor`` callable is injected by the caller.
  With any missing it refuses and leaves the request untouched. The dashboard
  never injects an executor, so from the UI ``apply`` is always a no-op; a real
  device is never reachable through this path.

Only the virtual lab is ever in scope; this is not a production deployment tool.
"""
from __future__ import annotations

import datetime as dt
import difflib
import json
import uuid

from ai.verification.config_validation import validate_lines
from config import get_settings
from database import repository
from database.models import db_utcnow
from network.lab_config_ops import normalize_config, normalize_line
from network.lab_verifier import verify_restored_config

_APPROVABLE = ("validated",)
#: States that can still expire or be cancelled.
_PENDING = ("draft", "proposed", "validated", "approved")
_CANCELLABLE = ("draft", "proposed", "validated", "approved")
#: Explicit state machine. A service action may only move along these edges;
#: repeated or out-of-order actions are refused before any write.
_TRANSITIONS = {
    "draft": {"proposed", "validated", "rejected", "cancelled", "expired"},
    "proposed": {"validated", "rejected", "cancelled", "expired"},
    "validated": {"approved", "rejected", "cancelled", "expired"},
    "approved": {"applied", "failed", "cancelled", "expired"},
    "applied": {"rolled_back", "failed"},
    "failed": set(),
    "rolled_back": set(),
    "rejected": set(),
    "cancelled": set(),
    "expired": set(),
}
_MAX_REASON_LEN = 500


def _dump(obj):
    return None if obj is None else json.dumps(obj, ensure_ascii=False,
                                               default=str, sort_keys=True)


def _load(x):
    return json.loads(x) if x else None


def _principal(name: str | None, authenticated: bool) -> str | None:
    """Return a verified principal name, or None when none was verified.

    Open mode must not fabricate a person. A caller-supplied name is only
    recorded when the auth layer marked it as authenticated.
    """
    if not authenticated or not name:
        return None
    return str(name).strip()[:64] or None


def _clean_reason(reason: str | None) -> str | None:
    if reason is None:
        return None
    text = str(reason).strip()
    return text[:_MAX_REASON_LEN] if text else None


def _audit(session, req, action: str, *, actor: str | None = None,
           from_status: str | None = None, to_status: str | None = None,
           reason: str | None = None, at: dt.datetime | None = None) -> None:
    repository.add_change_audit_event(
        session, req, action=action, actor=actor,
        actor_authenticated=bool(actor), from_status=from_status,
        to_status=to_status, reason=_clean_reason(reason), at=at)


def _transition_error(req, to_status: str) -> str | None:
    if to_status not in _TRANSITIONS.get(req.status, set()):
        return f"invalid transition '{req.status}' -> '{to_status}'"
    return None


def _expire_if_due(session, req, now: dt.datetime) -> bool:
    """Move a pending request past its deadline to ``expired`` (once)."""
    if req.status not in _PENDING or not req.expires_at:
        return False
    if now < req.expires_at:
        return False
    if _transition_error(req, "expired"):
        return False
    old_status = req.status
    reason = (f"approval window expired at "
              f"{req.expires_at.isoformat(sep=' ')} UTC")
    req = repository.update_change_request(
        session, req, status="expired", expired_at=now)
    _audit(session, req, "expired", from_status=old_status,
           to_status="expired", reason=reason, at=now)
    session.commit()
    return True


def expire_if_due(session, req, now: dt.datetime | None = None) -> bool:
    """Public expiry gate used by the runtime execution pipeline."""
    return _expire_if_due(session, req, now or db_utcnow())


def expire_due_requests(session, now: dt.datetime | None = None) -> int:
    """Expire every pending request whose approval window has passed."""
    now = now or db_utcnow()
    expired = 0
    for req in repository.list_change_requests(session, limit=1000):
        if _expire_if_due(session, req, now):
            expired += 1
    return expired


def make_diff(base: list[str], proposed: list[str]) -> str:
    return "\n".join(difflib.unified_diff(base or [], proposed or [],
                                          "current", "proposed", lineterm=""))


def validate_proposal(lines: list[str]) -> tuple[bool, list[dict]]:
    """Deterministic gate. Empty proposals are never 'valid'."""
    findings = validate_lines(lines or [])
    ok = bool(lines) and all(f["syntax_ok"] and not f["prohibited"]
                             for f in findings)
    return ok, findings


def _outcome(result) -> tuple[bool, str]:
    """Normalise an executor return (bool / CommandResult / dict) to (ok, detail)."""
    if isinstance(result, bool):
        return result, "" if result else "executor returned False"
    if isinstance(result, dict):
        return bool(result.get("ok")), str(result.get("detail")
                                            or result.get("error") or "")
    ok = getattr(result, "ok", None)
    if ok is None:
        return bool(result), ""
    err = getattr(result, "error", None)
    return bool(ok), (str(err) if err else "")


def propose(session, *, device_name: str, proposed_lines: list[str],
            base_lines: list[str] | None = None, data_source: str = "live",
            description: str | None = None, proposed_by: str | None = None,
            proposer_authenticated: bool = False,
            risk_level: str = "low", now: dt.datetime | None = None,
            ttl_minutes: int | None = None):
    """Create a validated (or rejected) change request. Never applies.

    ``proposed_by`` is recorded only when ``proposer_authenticated`` is true.
    Open mode therefore stores no requester identity instead of a fake one.
    """
    base_lines = base_lines or []
    now = now or db_utcnow()
    ttl = (get_settings().change_request_ttl_minutes
           if ttl_minutes is None else int(ttl_minutes))
    expires_at = now + dt.timedelta(minutes=ttl) if ttl > 0 else None

    ok, findings = validate_proposal(proposed_lines)
    status = "validated" if ok else "rejected"
    actor = _principal(proposed_by, proposer_authenticated)

    if ok:
        reason = "proposal passed deterministic validation"
    else:
        bad = [f for f in findings
               if not f.get("syntax_ok") or f.get("prohibited")]
        detail = "; ".join(
            f"{str(f.get('line', ''))[:60]}: {f.get('reason', 'invalid')}"
            for f in bad[:5])
        reason = ("deterministic validation rejected the proposal"
                  + (f": {detail}" if detail else ""))

    req = repository.create_change_request(
        session, request_uuid=str(uuid.uuid4()), device_name=device_name,
        data_source=data_source, description=description,
        base_config=base_lines, proposed_config=proposed_lines,
        rollback_config=base_lines, diff=make_diff(base_lines, proposed_lines),
        validation=findings, risk_level=risk_level, status=status,
        proposed_by=actor, proposed_by_authenticated=bool(actor),
        expires_at=expires_at,
        rejection_reason=None if ok else _clean_reason(reason),
        rejected_at=None if ok else now)
    _audit(session, req, "proposed", actor=actor, from_status=None,
           to_status=status, reason=reason, at=now)
    if not ok:
        # The deterministic validator - not a person - rejected this proposal.
        _audit(session, req, "rejected", actor=None, from_status=None,
               to_status="rejected", reason=reason, at=now)
    session.commit()
    return req


def approve(session, request_uuid: str, *, approved_by: str | None,
            authenticated: bool = False, reason: str | None = None,
            now: dt.datetime | None = None):
    """Approve a validated request by a distinct authenticated principal.

    Fails closed when either identity cannot be verified. In particular, an
    open-mode (unauthenticated) click is never treated as a second person.
    """
    req = repository.get_change_request(session, request_uuid)
    if req is None:
        return None, "not found"
    now = now or db_utcnow()
    if _expire_if_due(session, req, now):
        return req, "change request expired; approval refused"
    if req.status not in _APPROVABLE:
        return req, f"cannot approve from status '{req.status}'"

    actor = _principal(approved_by, authenticated)
    if actor is None:
        return req, ("approval requires an authenticated principal; open mode "
                     "cannot prove a distinct approver")
    if not req.proposed_by or not req.proposed_by_authenticated:
        return req, ("approval requires a proposal from an authenticated "
                     "requester; this request has no verified requester "
                     "identity")
    if req.proposed_by == actor:
        return req, "self-approval is not allowed"

    reason = _clean_reason(reason)
    req = repository.update_change_request(
        session, req, status="approved", approved_by=actor,
        approved_at=now, approval_reason=reason)
    _audit(session, req, "approved", actor=actor, from_status="validated",
           to_status="approved",
           reason=reason or "approved by a distinct authenticated principal",
           at=now)
    session.commit()
    return req, "approved"


def reject(session, request_uuid: str, *, rejected_by: str | None = None,
           authenticated: bool = False, reason: str | None = None,
           now: dt.datetime | None = None):
    """Reject a validated request with a required, persisted reason."""
    req = repository.get_change_request(session, request_uuid)
    if req is None:
        return None, "not found"
    now = now or db_utcnow()
    if _expire_if_due(session, req, now):
        return req, "change request expired; rejection refused"
    if req.status != "validated":
        return req, f"cannot reject from status '{req.status}'"

    reason = _clean_reason(reason)
    if not reason:
        return req, "rejection requires a reason"

    actor = _principal(rejected_by, authenticated)
    req = repository.update_change_request(
        session, req, status="rejected", rejected_by=actor,
        rejected_at=now, rejection_reason=reason)
    _audit(session, req, "rejected", actor=actor, from_status="validated",
           to_status="rejected", reason=reason, at=now)
    session.commit()
    return req, "rejected"


def apply(session, request_uuid: str, *, executor=None,
          now: dt.datetime | None = None) -> dict:
    """Apply an approved change through an injected lab executor - if, and only
    if, the loop is enabled, the request is unexpired and every gate passes.
    Otherwise refuse, no-op."""
    req = repository.get_change_request(session, request_uuid)
    if req is None:
        return {"applied": False, "reason": "not found"}
    now = now or db_utcnow()
    if _expire_if_due(session, req, now):
        return {"applied": False, "expired": True, "status": req.status,
                "reason": "change request expired; application refused"}

    settings = get_settings()
    if not settings.change_loop_enabled:
        return {"applied": False,
                "reason": "change loop disabled (set NETPILOT_CHANGE_LOOP_ENABLED "
                          "and provide a lab executor to apply)"}
    if req.status != "approved":
        return {"applied": False,
                "reason": f"requires an approved request, current status "
                          f"'{req.status}'"}
    if executor is None:
        return {"applied": False,
                "reason": "no lab executor provided; refusing to apply"}

    proposed = _load(req.proposed_config_json) or []
    try:
        result = executor(req.device_name, proposed)
    except Exception as exc:  # executor failure is recorded, never a crash
        detail = f"{type(exc).__name__}: {exc}"[:_MAX_REASON_LEN]
        req = repository.update_change_request(
            session, req, status="failed", applied_at=now,
            apply_detail=detail)
        _audit(session, req, "failed", from_status="approved",
               to_status="failed", reason=f"executor raised: {detail}", at=now)
        session.commit()
        return {"applied": False, "reason": f"executor raised: {detail}"}

    ok, detail = _outcome(result)
    detail = (detail or "")[:_MAX_REASON_LEN]
    if ok:
        req = repository.update_change_request(
            session, req, status="applied", applied_at=now,
            apply_detail=detail or "applied via lab executor")
        _audit(session, req, "applied", from_status="approved",
               to_status="applied",
               reason=detail or "applied via lab executor", at=now)
    else:
        req = repository.update_change_request(
            session, req, status="failed", applied_at=now,
            apply_detail=detail or "executor reported failure")
        _audit(session, req, "failed", from_status="approved",
               to_status="failed",
               reason=detail or "executor reported failure", at=now)
    session.commit()
    # The raw executor outcome is returned as well: the service layer needs
    # to distinguish a clean failure from a *partial* application and from a
    # timeout, because those three need three different responses.
    return {"applied": bool(ok), "status": req.status, "detail": detail,
            "outcome": result}


def rollback(session, request_uuid: str, *, executor=None,
             now: dt.datetime | None = None) -> dict:
    """Undo an applied change (same gates) and verify the restoration.

    The rollback is a **real undo**, not a merge: when the executor offers a
    ``rollback_config(device, proposed, base)`` entry point (the lab
    executor does), that is used - it removes the lines the change added and
    re-applies the base values for the lines it changed. Re-applying the
    base configuration on top of the device would leave an added line in
    place, which is not a restoration.

    When the executor cannot read the device back, the restoration cannot be
    proven and is reported as unverified - never as a success.
    """
    settings = get_settings()
    req = repository.get_change_request(session, request_uuid)
    if req is None:
        return {"rolled_back": False, "reason": "not found"}
    if not settings.change_loop_enabled:
        return {"rolled_back": False, "reason": "change loop disabled"}
    if req.status != "applied":
        return {"rolled_back": False,
                "reason": f"only an applied change can roll back, current "
                          f"status '{req.status}'"}
    if executor is None:
        return {"rolled_back": False, "reason": "no lab executor provided"}
    now = now or db_utcnow()
    restore = _load(req.rollback_config_json) or _load(req.base_config_json) or []
    proposed = _load(req.proposed_config_json) or []

    rollback_config = getattr(executor, "rollback_config", None)
    try:
        if callable(rollback_config):
            result = rollback_config(req.device_name, proposed, restore)
        else:
            # A plain executor protocol: restore the base configuration.
            result = executor(req.device_name, restore)
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"[:_MAX_REASON_LEN]
        req = repository.update_change_request(
            session, req, status="failed",
            apply_detail=f"rollback raised: {detail}")
        _audit(session, req, "failed", from_status="applied",
               to_status="failed", reason=f"rollback raised: {detail}", at=now)
        session.commit()
        return {"rolled_back": False, "restored": False, "reason": detail}
    ok, detail = _outcome(result)
    if not ok:
        req = repository.update_change_request(
            session, req, status="failed",
            apply_detail=f"rollback failed: {detail}")
        _audit(session, req, "failed", from_status="applied",
               to_status="failed", reason=f"rollback failed: {detail}", at=now)
        session.commit()
        return {"rolled_back": False, "restored": False, "reason": detail}

    # The restoration is only claimed when the device was actually read back.
    restored = False
    verification: dict = {}
    read_config = getattr(executor, "read_config", None)
    if callable(read_config):
        removed = [ln for ln in proposed
                   if normalize_line(ln) not in set(normalize_config(restore))]
        comparison = verify_restored_config(req.device_name, restore,
                                            read_config,
                                            removed_lines=removed)
        verification = comparison.as_dict()
        restored = comparison.passed

    req = repository.update_change_request(
        session, req, status="rolled_back" if restored else "failed",
        apply_detail=(detail or "rolled back") if restored
        else "rolled back but the restoration could not be verified")
    _audit(session, req, "rolled_back" if restored else "failed",
           from_status="applied", to_status=req.status,
           reason=(verification.get("detail") or detail or "rolled back")
           if restored else "rolled back but the restoration could not be verified",
           at=now)
    session.commit()
    return {"rolled_back": True, "restored": restored, "status": req.status,
            "verification": verification,
            "reason": (verification.get("detail") or detail or "rolled back")}


def cancel(session, request_uuid: str, *, cancelled_by: str | None = None,
           authenticated: bool = False, reason: str | None = None,
           now: dt.datetime | None = None) -> dict:
    """Cancel a not-yet-applied request (audit-friendly, applies nothing)."""
    req = repository.get_change_request(session, request_uuid)
    if req is None:
        return {"cancelled": False, "reason": "not found"}
    now = now or db_utcnow()
    if _expire_if_due(session, req, now):
        return {"cancelled": False, "reason": "change request expired"}
    if req.status not in _CANCELLABLE:
        return {"cancelled": False,
                "reason": f"cannot cancel from status '{req.status}'"}

    actor = _principal(cancelled_by, authenticated)
    reason = _clean_reason(reason)
    old_status = req.status
    req = repository.update_change_request(
        session, req, status="cancelled", cancelled_by=actor,
        cancelled_at=now, cancellation_reason=reason)
    _audit(session, req, "cancelled", actor=actor, from_status=old_status,
           to_status="cancelled",
           reason=reason or "cancelled by operator", at=now)
    session.commit()
    return {"cancelled": True, "status": "cancelled"}
