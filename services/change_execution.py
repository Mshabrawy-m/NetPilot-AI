"""Gated, lab-only execution of an approved configuration change.

``services/change_service.py`` owns the *state machine* of a change request
(``draft -> proposed -> validated -> approved -> applied -> failed /
rolled_back / cancelled``) and refuses to apply anything without an injected
executor. What it did **not** own - and what Gap G3-2 was about - is the
runtime half of the loop: proving the lab identity, proving the device is
still in its expected pre-change state, verifying that the change actually
took effect, and rolling back (and proving the restoration) when it did not.

This module is that runtime half. It is deliberately executor-agnostic: the
executor is injected exactly like in ``change_service``, so the whole
pipeline can be exercised offline with a fake executor and no lab.

The pipeline, in order - every step is a gate that can only *stop* the
change, never start it:

1. the change loop must be enabled;
2. the request must exist and its approval window must still be open;
3. the request must be in status ``approved``;
4. the approval must be a real, authenticated, named, non-self approval
   bound to a verified requester identity;
5. an executor must be supplied;
6. the **rollback plan** must be computable and valid (fail closed when it
   is not - an unrollbackable change is never applied);
7. the **lab identity** of the target must be provable;
8. the **precondition** - the device's current running configuration must
   still contain the recorded base configuration - must hold;
9. the change is applied;
10. the result is **verified** by reading the device back;
11. if verification fails (or the apply only partially landed), the change
    is **rolled back** and the **restoration is verified** too.

Nothing here can reach a production device: the executor resolves the target
against the lab topology and refuses anything that is not a lab router.
"""
from __future__ import annotations

import datetime as dt
import json

from ai.verification import config_validation
from config import get_settings
from database import repository
from database.models import db_utcnow
from network.lab_config_ops import (build_rollback_ops, normalize_config,
                                    normalize_line, unrollbackable_lines)
from network.lab_verifier import verify_applied_config, verify_restored_config
from services import change_service

#: Upper bound for the redacted audit detail stored on the request.
_MAX_DETAIL_LEN = 500


def _dump(obj):
    return None if obj is None else json.dumps(obj, ensure_ascii=False,
                                               default=str, sort_keys=True)


def _load(x):
    try:
        return json.loads(x) if x else None
    except (TypeError, ValueError):
        return None


def _detail(*parts: str) -> str:
    return " | ".join(p for p in parts if p)[:_MAX_DETAIL_LEN]


def verify_rollback_plan(base_lines: list[str] | None,
                         proposed_lines: list[str] | None
                         ) -> tuple[bool, str]:
    """Check that a rollback really exists and is itself safe to apply.

    A change whose rollback cannot be computed, whose rollback would contain
    a prohibited command, or which touches a command FRR gives us no removal
    command for, must never be applied. This is the "verified rollback plan"
    precondition, and it fails closed.
    """
    base = list(base_lines or [])
    proposed = list(proposed_lines or [])
    if not base:
        return False, ("no base configuration recorded - a rollback could not "
                       "be verified")
    findings = config_validation.validate_lines(base)
    bad = [f for f in findings if not f["syntax_ok"] or f["prohibited"]]
    if bad:
        detail = "; ".join(f"{f['line'][:40]} ({f['reason']})" for f in bad[:5])
        return False, f"the recorded rollback configuration is not safe: {detail}"
    ops = build_rollback_ops(proposed, base)
    if not ops:
        return False, ("the rollback plan is empty - the change cannot be "
                       "undone deterministically")
    unrollbackable = unrollbackable_lines(proposed, base)
    if unrollbackable:
        return False, ("the change cannot be undone deterministically - FRR "
                       f"has no removal command for: {unrollbackable[:5]}")
    for op in ops:
        for line in op.lines:
            ok, reason = config_validation.syntax_ok(line)
            if not ok:
                return False, f"the rollback plan contains an unsafe line: {reason}"
            prohibited, matched = config_validation.prohibited_present(line)
            if prohibited:
                return False, (f"the rollback plan contains a prohibited "
                               f"command: {matched}")
    return True, f"rollback plan verified ({len(ops)} operation(s))"


def execute_change(session, request_uuid: str, *, executor=None,
                   now: dt.datetime | None = None) -> dict:
    """Apply an approved change to the isolated lab, with verification.

    Returns a dict describing every step. The keys are stable so the caller
    (CLI, test, or a future UI) can assert on them:

    ``ok``               - the change was applied AND verified;
    ``applied``          - the executor reported success;
    ``verified``         - post-change verification passed;
    ``rolled_back``      - a rollback was executed;
    ``restored``         - the restoration was verified;
    ``refused``          - a gate stopped the change before any device I/O;
    ``reason``           - why it was refused or failed;
    ``status``           - the resulting change-request status;
    ``verification``     - the verification detail (missing/unexpected lines).
    """
    settings = get_settings()
    now = now or db_utcnow()

    def refuse(reason: str, status: str | None = None) -> dict:
        return {"ok": False, "applied": False, "verified": False,
                "rolled_back": False, "restored": False, "refused": True,
                "reason": reason, "status": status, "verification": {}}

    # -- 1. the change loop must be explicitly enabled ---------------------
    if not settings.change_loop_enabled:
        return refuse("change loop disabled (set NETPILOT_CHANGE_LOOP_ENABLED "
                      "and provide a lab executor to apply)")

    req = repository.get_change_request(session, request_uuid)
    if req is None:
        return refuse("not found")

    # -- 2. the approval window must still be open -------------------------
    if change_service.expire_if_due(session, req, now):
        return refuse("change request expired; application refused",
                      req.status)

    # -- 3. the request must be approved -----------------------------------
    if req.status != "approved":
        return refuse(f"requires an approved request, current status "
                      f"'{req.status}'", req.status)

    # -- 4. explicit, non-self human approval ------------------------------
    if not req.approved_by or not req.approved_at:
        return refuse("no recorded human approval for this request", req.status)
    if not req.proposed_by or not req.proposed_by_authenticated:
        return refuse("the approval is not bound to a verified requester "
                      "identity", req.status)
    if req.proposed_by == req.approved_by:
        return refuse("self-approval is not allowed", req.status)

    # -- 5. an executor must be supplied -----------------------------------
    if executor is None:
        return refuse("no lab executor provided; refusing to apply", req.status)

    proposed = _load(req.proposed_config_json) or []
    base = _load(req.rollback_config_json) or _load(req.base_config_json) or []

    # -- 6. the rollback plan must be verifiable ----------------------------
    ok, reason = verify_rollback_plan(base, proposed)
    if not ok:
        return refuse(f"rollback plan not verified: {reason}", req.status)

    # -- 7. the lab identity of the target must be provable ------------------
    preflight = getattr(executor, "preflight", None)
    if callable(preflight):
        identity_ok, identity_reason = preflight(req.device_name)
        if not identity_ok:
            return refuse(f"lab identity not verified: {identity_reason}",
                          req.status)

    # -- 8. deterministic precondition: the device must still be as recorded -
    read_config = getattr(executor, "read_config", None)
    if not callable(read_config):
        return refuse("the executor cannot read the device state - a "
                      "precondition could not be verified", req.status)
    try:
        current = read_config(req.device_name)
    except Exception as exc:  # noqa: BLE001 - unreadable is not "unchanged"
        return refuse(f"could not read the current state of "
                      f"{req.device_name!r}: {type(exc).__name__}: {exc}",
                      req.status)
    expected_current = set(normalize_config(base))
    actual_current = set(normalize_config(current or []))
    drift = sorted(expected_current - actual_current)
    if drift:
        return refuse(f"precondition failed: {req.device_name!r} no longer "
                      f"matches the recorded base configuration (missing "
                      f"{drift[:5]}); the change was not applied", req.status)

    # -- 9. apply ----------------------------------------------------------
    result = change_service.apply(session, request_uuid, executor=executor,
                                   now=now)
    outcome = result.get("outcome")
    applied = bool(result.get("applied"))
    partial = bool(getattr(outcome, "partial", False))
    timed_out = bool(getattr(outcome, "timed_out", False))

    if not applied:
        reason = result.get("reason") or result.get("detail") or "apply failed"
        if result.get("expired"):
            # The window closed between our pre-check and the apply call.
            # Never overwrite the service's ``expired`` state with ``failed``.
            return refuse(reason, result.get("status", "expired"))
        if partial:
            # Some lines landed before the failure: undo them.
            rb = _rollback(session, req, executor, proposed, base, now)
            return {"ok": False, "applied": False, "verified": False,
                    "rolled_back": rb["rolled_back"], "restored": rb["restored"],
                    "refused": False,
                    "reason": f"{reason} (partial application - "
                              f"{rb['reason']})",
                    "status": rb["status"], "verification": rb["verification"],
                    "timed_out": timed_out}

        repository.update_change_request(
            session, req, status="failed", applied_at=now,
            apply_detail=_detail("apply failed", reason))
        session.commit()
        return {"ok": False, "applied": False, "verified": False,
                "rolled_back": False, "restored": False, "refused": False,
                "reason": reason, "status": "failed", "verification": {},
                "timed_out": timed_out}

    # -- 10. post-change verification --------------------------------------
    verification = verify_applied_config(req.device_name, proposed, read_config)
    if verification.passed:
        repository.update_change_request(
            session, req, status="applied", applied_at=now,
            apply_detail=_detail("applied and verified", verification.detail))
        session.commit()
        return {"ok": True, "applied": True, "verified": True,
                "rolled_back": False, "restored": False, "refused": False,
                "reason": verification.detail, "status": "applied",
                "verification": verification.as_dict(), "timed_out": False}

    # -- 11. verification failed -> roll back and verify the restoration ----
    rb = _rollback(session, req, executor, proposed, base, now)
    return {"ok": False, "applied": True, "verified": False,
            "rolled_back": rb["rolled_back"], "restored": rb["restored"],
            "refused": False,
            "reason": (f"post-change verification failed: {verification.detail} "
                       f"| rollback: {rb['reason']}"),
            "status": rb["status"],
            "verification": {**verification.as_dict(), "rollback": rb["verification"]},
            "timed_out": timed_out}


def _rollback(session, req, executor, proposed: list[str], base: list[str],
              now: dt.datetime) -> dict:
    """Undo the change and verify that the device was really restored."""
    rollback_config = getattr(executor, "rollback_config", None)
    if callable(rollback_config):
        outcome = rollback_config(req.device_name, proposed, base)
    else:
        # A plain executor protocol: restore the base configuration.
        outcome = executor(req.device_name, base)
    ok = bool(getattr(outcome, "ok", outcome))
    error = getattr(outcome, "error", None)

    restored = False
    verification: dict = {}
    if ok:
        read_config = getattr(executor, "read_config", None)
        if callable(read_config):
            # The lines the change added must be gone again - keeping the
            # base while the addition is still active is NOT a restoration.
            base_set = set(normalize_config(base))
            removed = [ln for ln in proposed
                       if normalize_line(ln) not in base_set]
            comparison = verify_restored_config(
                req.device_name, base, read_config, removed_lines=removed)
            verification = comparison.as_dict()
            restored = comparison.passed

    if ok and restored:
        status = "rolled_back"
        reason = "the change was rolled back and the restoration verified"
    elif ok:
        status = "failed"
        reason = ("the change was rolled back but the restoration could not "
                  "be verified")
    else:
        status = "failed"
        reason = f"the rollback failed: {error or 'executor reported failure'}"

    repository.update_change_request(
        session, req, status=status, applied_at=now,
        apply_detail=_detail("verification failed - rollback", reason))
    session.commit()
    return {"rolled_back": ok, "restored": restored, "status": status,
            "reason": reason, "verification": verification}


def apply_and_verify(session, request_uuid: str, *, executor=None,
                     now: dt.datetime | None = None) -> dict:
    """Alias kept for readability at call sites."""
    return execute_change(session, request_uuid, executor=executor, now=now)


__all__ = ["execute_change", "apply_and_verify", "verify_rollback_plan"]
