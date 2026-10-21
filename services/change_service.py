"""Gated change-loop service (Slice 7).

Safety model (lab-only, approval-bound, fail-closed):

* Proposals are validated by the SAME deterministic FRR validator the Phase-3
  verifier uses - shell metacharacters and execution/persistence commands are
  rejected, so a "config" can never smuggle a command.
* ``approve`` records a human principal and forbids self-approval.
* ``apply`` changes nothing unless ALL THREE hold:
    (1) ``NETPILOT_CHANGE_LOOP_ENABLED`` is set,
    (2) the request is in status ``approved``,
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
#: Extra wall-clock guard: even when enabled, apply is refused without one.
_MAX_DETAIL_LEN = 500


def _dump(obj):
    return None if obj is None else json.dumps(obj, ensure_ascii=False,
                                               default=str, sort_keys=True)


def _load(x):
    return json.loads(x) if x else None


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
            risk_level: str = "low"):
    """Create a validated (or rejected) change request. Never applies."""
    base_lines = base_lines or []
    ok, findings = validate_proposal(proposed_lines)
    req = repository.create_change_request(
        session, request_uuid=str(uuid.uuid4()), device_name=device_name,
        data_source=data_source, description=description,
        base_config=base_lines, proposed_config=proposed_lines,
        rollback_config=base_lines, diff=make_diff(base_lines, proposed_lines),
        validation=findings, risk_level=risk_level,
        status="validated" if ok else "rejected", proposed_by=proposed_by)
    session.commit()
    return req


def approve(session, request_uuid: str, *, approved_by: str):
    req = repository.get_change_request(session, request_uuid)
    if req is None:
        return None, "not found"
    if req.status not in _APPROVABLE:
        return req, f"cannot approve from status '{req.status}'"
    if not approved_by:
        return req, "approval requires a principal"
    if req.proposed_by and req.proposed_by == approved_by:
        return req, "self-approval is not allowed"
    req = repository.update_change_request(
        session, req, status="approved", approved_by=approved_by,
        approved_at=db_utcnow())
    session.commit()
    return req, "approved"


def apply(session, request_uuid: str, *, executor=None,
          now: dt.datetime | None = None) -> dict:
    """Apply an approved change through an injected lab executor - if, and only
    if, the loop is enabled and every gate passes. Otherwise refuse, no-op."""
    settings = get_settings()
    req = repository.get_change_request(session, request_uuid)
    if req is None:
        return {"applied": False, "reason": "not found"}
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

    now = now or db_utcnow()
    proposed = _load(req.proposed_config_json) or []
    try:
        result = executor(req.device_name, proposed)
    except Exception as exc:  # executor failure is recorded, never a crash
        detail = f"{type(exc).__name__}: {exc}"[:_MAX_DETAIL_LEN]
        repository.update_change_request(session, req, status="failed",
                                         applied_at=now, apply_detail=detail)
        session.commit()
        return {"applied": False, "reason": f"executor raised: {detail}"}

    ok, detail = _outcome(result)
    detail = (detail or "")[:_MAX_DETAIL_LEN]
    if ok:
        repository.update_change_request(
            session, req, status="applied", applied_at=now,
            apply_detail=detail or "applied via lab executor")
    else:
        repository.update_change_request(
            session, req, status="failed", applied_at=now,
            apply_detail=detail or "executor reported failure")
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
        detail = f"{type(exc).__name__}: {exc}"[:_MAX_DETAIL_LEN]
        repository.update_change_request(session, req, status="failed",
                                         apply_detail=f"rollback raised: {detail}")
        session.commit()
        return {"rolled_back": False, "restored": False, "reason": detail}
    ok, detail = _outcome(result)
    if not ok:
        repository.update_change_request(session, req, status="failed",
                                         apply_detail=f"rollback failed: {detail}")
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

    repository.update_change_request(
        session, req, status="rolled_back" if restored else "failed",
        apply_detail=(detail or "rolled back") if restored
        else "rolled back but the restoration could not be verified")
    session.commit()
    return {"rolled_back": True, "restored": restored, "status": req.status,
            "verification": verification,
            "reason": (verification.get("detail") or detail or "rolled back")}


def cancel(session, request_uuid: str) -> dict:
    """Cancel a not-yet-applied request (audit-friendly, applies nothing)."""
    req = repository.get_change_request(session, request_uuid)
    if req is None:
        return {"cancelled": False, "reason": "not found"}
    if req.status in ("applied", "failed"):
        return {"cancelled": False,
                "reason": f"cannot cancel from status '{req.status}'"}
    repository.update_change_request(session, req, status="cancelled")
    session.commit()
    return {"cancelled": True, "status": "cancelled"}
