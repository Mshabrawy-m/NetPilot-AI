"""Headless UI smoke test using Streamlit's official AppTest harness.

Executes app.py and each page script the same way the Streamlit runtime
would, failing on any uncaught exception (rendered red box in the
browser), then exercises the Phase 2 action buttons (run detection,
run forecast comparison) against the isolated demo database.

It also guards two bugs that only surfaced in the hosted app:

* page 8 - running detection with a DEVICE-LEVEL metric. ``options`` then
  contains ``None`` (the "(device-level)" scope), so the default-index
  lookup used to raise ``KeyError`` the first time the button was pressed.
* page 10 - the SAME AI run visible in the "New request" and "Run history"
  tabs at once. ``st.tabs`` renders every panel in a single run, so the
  shared widget key raised ``StreamlitDuplicateElementKey``.

Run: .venv/Scripts/python tests/ui_smoke_check.py
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Demo mode with an isolated throwaway database so the smoke test never
# touches live lab data.
os.environ["NETPILOT_DEMO_MODE"] = "true"
os.environ["DATABASE_URL"] = f"sqlite:///{(ROOT / 'data' / 'uitest.db').as_posix()}"

from streamlit.testing.v1 import AppTest  # noqa: E402

TARGETS = ["app.py"] + sorted(str(p.relative_to(ROOT))
                              for p in (ROOT / "pages").glob("*.py"))

failed = False
for target in TARGETS:
    at = AppTest.from_file(str(ROOT / target), default_timeout=120)
    at.run()
    exceptions = [e.value for e in at.exception]
    if exceptions:
        failed = True
        print(f"[FAIL] {target}")
        for exc in exceptions:
            print("       ", str(exc)[:400])
    else:
        print(f"[ OK ] {target} "
              f"(warnings={len(at.warning)}, infos={len(at.info)})")

# ------------------------------------------------- Phase 2 action buttons


def press(page: str, label: str, *, select_metric: str | None = None) -> bool:
    """Open a page, optionally pick a metric, click `label`, rerun, check."""
    at = AppTest.from_file(str(ROOT / page), default_timeout=180)
    at.run()
    if select_metric is not None:
        picked = False
        for box in at.selectbox:
            if select_metric in list(box.options or []):
                box.select(select_metric)
                picked = True
                break
        if not picked:
            print(f"[FAIL] {page}: no selectbox offered metric "
                  f"'{select_metric}'")
            return False
        at.run()
    btn = next((b for b in at.button if label in (b.label or "")), None)
    if btn is None:
        print(f"[FAIL] {page}: button '{label}' not rendered")
        return False
    btn.click()
    at.run()
    if at.exception:
        print(f"[FAIL] {page}: after clicking '{label}'")
        for exc in at.exception:
            print("       ", str(exc)[:400])
        return False
    print(f"[ OK ] {page}: '{label}' executed without exceptions")
    return True


def page10_same_run_in_two_tabs() -> bool:
    """Regression: one run rendered in the 'New request' AND 'Run history'
    tabs in the same script run must not collide on a widget key."""
    from database import repository
    from database.connection import get_session, init_db

    init_db()
    run_uuid = "smoke-dupkey-run"
    session = get_session()
    try:
        if repository.get_ai_run_by_uuid(session, run_uuid) is None:
            repository.create_ai_run(
                session, run_uuid=run_uuid, request_text="smoke regression",
                data_source="demo", model="smoke", prompt_version="smoke")
            session.commit()
    finally:
        session.close()

    at = AppTest.from_file(
        str(ROOT / "pages/10_AI_Network_Assistant.py"), default_timeout=180)
    at.session_state["last_ai_run"] = run_uuid
    at.run()
    if at.exception:
        print("[FAIL] pages/10_AI_Network_Assistant.py: same run in two tabs "
              "(duplicate element key)")
        for exc in at.exception:
            print("       ", str(exc)[:400])
        return False
    print("[ OK ] pages/10_AI_Network_Assistant.py: same run in two tabs")
    return True


def page11_flash_survives_rerun() -> bool:
    """Regression: confirmations rendered just before ``st.rerun()`` were
    discarded, so creating a rule persisted it but showed no feedback."""
    from database import repository
    from database.connection import get_session, init_db

    init_db()
    rule_name = "smoke-flash-rule"
    session = get_session()
    try:
        for rule in repository.get_alert_rules(session):
            if rule.name == rule_name:
                session.delete(rule)
        session.commit()
    finally:
        session.close()

    try:
        at = AppTest.from_file(
            str(ROOT / "pages/11_Ops_Center.py"), default_timeout=180)
        at.run()
        field = next((t for t in at.text_input
                      if "Rule name" in (t.label or "")), None)
        if field is None:
            print("[FAIL] pages/11_Ops_Center.py: rule-name field not rendered")
            return False
        field.set_value(rule_name)
        button = next((b for b in at.button
                       if "Create rule" in (b.label or "")), None)
        if button is None:
            print("[FAIL] pages/11_Ops_Center.py: 'Create rule' not rendered")
            return False
        button.click()
        at.run()
        if at.exception:
            print("[FAIL] pages/11_Ops_Center.py: after creating a rule")
            for exc in at.exception:
                print("       ", str(exc)[:400])
            return False
        expected = f"Rule '{rule_name}' created"
        if not any(expected in s.value for s in at.success):
            print("[FAIL] pages/11_Ops_Center.py: confirmation lost after "
                  "st.rerun()")
            return False
        session = get_session()
        try:
            persisted = any(r.name == rule_name
                            for r in repository.get_alert_rules(session))
        finally:
            session.close()
        if not persisted:
            print("[FAIL] pages/11_Ops_Center.py: rule was not persisted")
            return False
        print("[ OK ] pages/11_Ops_Center.py: confirmation survives st.rerun()")
        return True
    finally:
        session = get_session()
        try:
            for rule in repository.get_alert_rules(session):
                if rule.name == rule_name:
                    session.delete(rule)
            session.commit()
        finally:
            session.close()


def _delete_change_requests(uuids: list[str]) -> None:
    """Remove smoke-test change requests and their audit rows."""
    from sqlalchemy import delete

    from database import repository
    from database.connection import get_session
    from database.models import ChangeAuditEvent

    session = get_session()
    try:
        for uuid in uuids:
            req = repository.get_change_request(session, uuid)
            if req is None:
                continue
            session.execute(delete(ChangeAuditEvent).where(
                ChangeAuditEvent.request_id == req.id))
            session.delete(req)
        session.commit()
    finally:
        session.close()


def _create_smoke_change(proposed_by, authenticated: bool, *,
                         device: str = "R1", ttl_minutes: int | None = None,
                         now: dt.datetime | None = None) -> str:
    from database.connection import get_session, init_db
    from services import change_service

    init_db()
    session = get_session()
    try:
        req = change_service.propose(
            session, device_name=device, base_lines=["hostname R1"],
            proposed_lines=["hostname R1", "interface eth0"],
            data_source="demo", description="UI smoke change",
            proposed_by=proposed_by,
            proposer_authenticated=authenticated,
            ttl_minutes=ttl_minutes, now=now)
        return req.request_uuid
    finally:
        session.close()


def _approve_smoke_change(uuid: str, *, approved_by: str = "bob") -> None:
    from database.connection import get_session
    from services import change_service

    session = get_session()
    try:
        _req, msg = change_service.approve(
            session, uuid, approved_by=approved_by, authenticated=True)
        assert msg == "approved", msg
    finally:
        session.close()


def _corrupt_validation_json(uuid: str) -> None:
    from database import repository
    from database.connection import get_session

    session = get_session()
    try:
        req = repository.get_change_request(session, uuid)
        req.validation_json = "{this is not valid json"
        session.commit()
    finally:
        session.close()


def _audit_actions(uuid: str) -> list[str]:
    from database import repository
    from database.connection import get_session

    session = get_session()
    try:
        req = repository.get_change_request(session, uuid)
        return [event.action for event in
                repository.get_change_audit_events(session, req.id)]
    finally:
        session.close()


def _change_snapshot(uuid: str) -> dict:
    from database import repository
    from database.connection import get_session

    session = get_session()
    try:
        req = repository.get_change_request(session, uuid)
        return {
            "status": req.status,
            "approved_by": req.approved_by,
            "approval_reason": req.approval_reason,
            "rejection_reason": req.rejection_reason,
            "cancellation_reason": req.cancellation_reason,
            "applied_at": req.applied_at,
            "expired_at": req.expired_at,
        }
    finally:
        session.close()


def _selectbox(at, label: str):
    return next((s for s in at.selectbox if (s.label or "") == label), None)


def _text_input(at, label: str):
    return next((t for t in at.text_input if (t.label or "") == label), None)


def _button(at, label: str):
    return next((b for b in at.button if label in (b.label or "")), None)


def page11_open_mode_approval_feedback() -> bool:
    """Open mode must fail closed with durable feedback, not fabricate a
    second person or report a misleading self-approval."""
    uuid = _create_smoke_change(None, False)
    try:
        at = AppTest.from_file(
            str(ROOT / "pages/11_Ops_Center.py"), default_timeout=180)
        at.run()
        impact = _selectbox(at, "Impact analysis request")
        if impact is None:
            print("[FAIL] pages/11_Ops_Center.py: impact selector not rendered")
            return False
        impact.select(uuid)
        at.run()
        box = _selectbox(at, "Approve (validated)")
        if box is None:
            print("[FAIL] pages/11_Ops_Center.py: approve selector not rendered")
            return False
        box.select(uuid)
        at.run()
        button = _button(at, "✅ Approve")
        if button is None:
            print("[FAIL] pages/11_Ops_Center.py: approve button not rendered")
            return False
        button.click()
        at.run()
        if at.exception:
            print("[FAIL] pages/11_Ops_Center.py: open-mode approval click")
            for exc in at.exception:
                print("       ", str(exc)[:400])
            return False
        if not any("authenticated principal" in w.value for w in at.warning):
            print("[FAIL] pages/11_Ops_Center.py: open-mode approval feedback "
                  "lost after st.rerun()")
            return False

        from database import repository
        from database.connection import get_session
        session = get_session()
        try:
            req = repository.get_change_request(session, uuid)
            status, approved_by = req.status, req.approved_by
        finally:
            session.close()
        if status != "validated" or approved_by is not None:
            print("[FAIL] pages/11_Ops_Center.py: open-mode approval changed "
                  f"the request (status={status}, approved_by={approved_by})")
            return False
        print("[ OK ] pages/11_Ops_Center.py: open-mode approval fails closed "
              "with durable feedback")
        return True
    finally:
        _delete_change_requests([uuid])


def page11_authenticated_decision_feedback() -> bool:
    """Distinct authenticated approver can approve, and rejection feedback
    survives the rerun. Both decisions are persisted with their reasons."""
    os.environ["NETPILOT_AUTH_ENABLED"] = "true"
    uuids = [_create_smoke_change("alice", True),
             _create_smoke_change("alice", True)]
    try:
        at = AppTest.from_file(
            str(ROOT / "pages/11_Ops_Center.py"), default_timeout=180)
        at.session_state["_np_user"] = {"username": "bob",
                                        "role": "operator"}
        at.run()

        impact = _selectbox(at, "Impact analysis request")
        if impact is None:
            print("[FAIL] pages/11_Ops_Center.py: impact selector missing "
                  "with auth enabled")
            return False
        impact.select(uuids[0])
        at.run()
        box = _selectbox(at, "Approve (validated)")
        if box is None:
            print("[FAIL] pages/11_Ops_Center.py: approve selector not "
                  "rendered with auth enabled")
            return False
        box.select(uuids[0])
        at.run()
        reason = _text_input(at, "Approval reason (optional)")
        if reason is None:
            print("[FAIL] pages/11_Ops_Center.py: approval reason missing")
            return False
        reason.set_value("UI smoke approval reason")
        button = _button(at, "✅ Approve")
        if button is None:
            print("[FAIL] pages/11_Ops_Center.py: approve button missing")
            return False
        button.click()
        at.run()
        if at.exception:
            print("[FAIL] pages/11_Ops_Center.py: authenticated approval")
            for exc in at.exception:
                print("       ", str(exc)[:400])
            return False
        if not any("approved." in s.value for s in at.success):
            print("[FAIL] pages/11_Ops_Center.py: approval confirmation lost "
                  "after st.rerun()")
            return False
        approve_box = _selectbox(at, "Approve (validated)")
        if approve_box is not None and uuids[0] in list(approve_box.options):
            print("[FAIL] pages/11_Ops_Center.py: an approved request is still "
                  "offered for a second approval")
            return False

        impact = _selectbox(at, "Impact analysis request")
        if impact is None:
            print("[FAIL] pages/11_Ops_Center.py: impact selector missing "
                  "before rejection")
            return False
        impact.select(uuids[1])
        at.run()
        box = _selectbox(at, "Reject (validated)")
        if box is None:
            print("[FAIL] pages/11_Ops_Center.py: reject selector missing")
            return False
        box.select(uuids[1])
        at.run()
        reason = _text_input(at, "Rejection reason (required)")
        if reason is None:
            print("[FAIL] pages/11_Ops_Center.py: rejection reason missing")
            return False
        reason.set_value("UI smoke rejection reason")
        button = _button(at, "❌ Reject")
        if button is None:
            print("[FAIL] pages/11_Ops_Center.py: reject button missing")
            return False
        button.click()
        at.run()
        if at.exception:
            print("[FAIL] pages/11_Ops_Center.py: authenticated rejection")
            for exc in at.exception:
                print("       ", str(exc)[:400])
            return False
        if not any("rejected." in s.value for s in at.success):
            print("[FAIL] pages/11_Ops_Center.py: rejection confirmation lost "
                  "after st.rerun()")
            return False

        from database import repository
        from database.connection import get_session
        session = get_session()
        try:
            approved = repository.get_change_request(session, uuids[0])
            rejected = repository.get_change_request(session, uuids[1])
            persisted = (
                approved.status == "approved"
                and approved.approved_by == "bob"
                and approved.approval_reason == "UI smoke approval reason"
                and rejected.status == "rejected"
                and rejected.rejection_reason == "UI smoke rejection reason")
        finally:
            session.close()
        if not persisted:
            print("[FAIL] pages/11_Ops_Center.py: decisions/reasons not "
                  "persisted")
            return False
        print("[ OK ] pages/11_Ops_Center.py: authenticated approval/rejection "
              "feedback survives st.rerun()")
        return True
    finally:
        os.environ.pop("NETPILOT_AUTH_ENABLED", None)
        _delete_change_requests(uuids)


def page11_impact_report_visible_before_approval() -> bool:
    """B2: the read-only impact report is rendered on the change tab before
    any approval action, and it clearly labels the demo topology."""
    uuid = _create_smoke_change(None, False)
    try:
        at = AppTest.from_file(
            str(ROOT / "pages/11_Ops_Center.py"), default_timeout=180)
        at.run()
        box = _selectbox(at, "Impact analysis request")
        if box is None:
            print("[FAIL] pages/11_Ops_Center.py: impact-analysis selector "
                  "not rendered before approval")
            return False
        box.select(uuid)
        at.run()
        if at.exception:
            print("[FAIL] pages/11_Ops_Center.py: after selecting an impact "
                  "request")
            for exc in at.exception:
                print("       ", str(exc)[:400])
            return False
        markdown = " ".join(str(m.value) for m in at.markdown)
        warnings = " ".join(str(w.value) for w in at.warning)
        if "Topology impact analysis" not in markdown:
            print("[FAIL] pages/11_Ops_Center.py: impact report header not "
                  "rendered")
            return False
        if "Directly targeted (confirmed)" not in markdown:
            print("[FAIL] pages/11_Ops_Center.py: direct-target section not "
                  "rendered")
            return False
        if "Digital Twin simulation" not in markdown:
            print("[FAIL] pages/11_Ops_Center.py: Digital Twin panel not "
                  "rendered")
            return False
        captions = " ".join(str(c.value) for c in at.caption)
        if "not approval" not in captions.lower():
            print("[FAIL] pages/11_Ops_Center.py: Digital Twin panel does not "
                  "disclaim approval/execution")
            return False
        if "demo" not in warnings.lower():
            print("[FAIL] pages/11_Ops_Center.py: demo topology not labelled "
                  "in the impact report")
            return False
        if _button(at, "✅ Approve") is None:
            print("[FAIL] pages/11_Ops_Center.py: approval control missing "
                  "after the impact report")
            return False
        labels = [box.label for box in at.selectbox]
        if ("Impact analysis request" in labels
                and "Approve (validated)" in labels
                and labels.index("Impact analysis request")
                > labels.index("Approve (validated)")):
            print("[FAIL] pages/11_Ops_Center.py: approval control rendered "
                  "before the impact report")
            return False
        print("[ OK ] pages/11_Ops_Center.py: impact report visible before "
              "approval and demo-labelled")
        return True
    finally:
        _delete_change_requests([uuid])


def page11_impact_mismatch_guard() -> bool:
    """An action must never run against a request other than the one whose
    impact report is on screen. Mismatching selections disable the button."""
    uuids = [_create_smoke_change(None, False),
             _create_smoke_change(None, False)]
    try:
        at = AppTest.from_file(
            str(ROOT / "pages/11_Ops_Center.py"), default_timeout=180)
        at.run()
        impact = _selectbox(at, "Impact analysis request")
        if impact is None:
            print("[FAIL] pages/11_Ops_Center.py: impact selector missing")
            return False
        impact.select(uuids[0])
        at.run()

        approve = _selectbox(at, "Approve (validated)")
        if approve is None:
            print("[FAIL] pages/11_Ops_Center.py: approve selector missing")
            return False
        approve.select(uuids[1])
        at.run()

        button = _button(at, "✅ Approve")
        if button is None:
            print("[FAIL] pages/11_Ops_Center.py: approve button missing")
            return False
        if not button.proto.disabled:
            print("[FAIL] pages/11_Ops_Center.py: approve enabled while the "
                  "impact report belongs to another request")
            return False
        if not any("different request" in w.value for w in at.warning):
            print("[FAIL] pages/11_Ops_Center.py: mismatch warning missing")
            return False

        impact = _selectbox(at, "Impact analysis request")
        impact.select(uuids[1])
        at.run()
        button = _button(at, "✅ Approve")
        if button is None or button.proto.disabled:
            print("[FAIL] pages/11_Ops_Center.py: matching impact selection "
                  "did not re-enable approval")
            return False

        if (_change_snapshot(uuids[0])["status"] != "validated"
                or _change_snapshot(uuids[1])["status"] != "validated"):
            print("[FAIL] pages/11_Ops_Center.py: mismatch guard mutated a "
                  "request")
            return False
        print("[ OK ] pages/11_Ops_Center.py: impact/action mismatch is "
              "blocked and no request was mutated")
        return True
    finally:
        _delete_change_requests(uuids)


def page11_insufficient_impact_evidence() -> bool:
    """Missing topology evidence is shown as INSUFFICIENT, not as safety, and
    approval remains an explicit human decision."""
    uuid = _create_smoke_change(None, False, device="GHOST")
    try:
        at = AppTest.from_file(
            str(ROOT / "pages/11_Ops_Center.py"), default_timeout=180)
        at.run()
        impact = _selectbox(at, "Impact analysis request")
        if impact is None:
            print("[FAIL] pages/11_Ops_Center.py: impact selector missing")
            return False
        impact.select(uuid)
        at.run()
        approve = _selectbox(at, "Approve (validated)")
        if approve is not None:
            approve.select(uuid)
            at.run()

        warnings = " ".join(str(w.value) for w in at.warning)
        if "INSUFFICIENT EVIDENCE" not in warnings:
            print("[FAIL] pages/11_Ops_Center.py: insufficient evidence not "
                  "reported for an unknown target")
            return False
        if "demo" not in warnings.lower():
            print("[FAIL] pages/11_Ops_Center.py: demo topology not labelled "
                  "for an insufficient-evidence report")
            return False
        button = _button(at, "✅ Approve")
        if button is None or button.proto.disabled:
            print("[FAIL] pages/11_Ops_Center.py: impact evidence incorrectly "
                  "blocks the explicit human decision")
            return False
        if _change_snapshot(uuid)["status"] != "validated":
            print("[FAIL] pages/11_Ops_Center.py: analysis changed the request")
            return False
        print("[ OK ] pages/11_Ops_Center.py: insufficient impact evidence is "
              "reported honestly")
        return True
    finally:
        _delete_change_requests([uuid])


def page11_apply_gating_feedback() -> bool:
    """Apply must refuse with durable feedback while the loop flag is off.

    This test never enables ``NETPILOT_CHANGE_LOOP_ENABLED``: the safe default
    must be enough to prevent every configuration application.
    """
    uuid = _create_smoke_change("alice", True)
    _approve_smoke_change(uuid)
    try:
        at = AppTest.from_file(
            str(ROOT / "pages/11_Ops_Center.py"), default_timeout=180)
        at.run()
        impact = _selectbox(at, "Impact analysis request")
        if impact is None:
            print("[FAIL] pages/11_Ops_Center.py: impact selector missing")
            return False
        impact.select(uuid)
        at.run()
        apply_box = _selectbox(at, "Apply (approved)")
        if apply_box is None:
            print("[FAIL] pages/11_Ops_Center.py: apply selector missing")
            return False
        apply_box.select(uuid)
        at.run()
        button = _button(at, "🚀 Apply")
        if button is None or button.proto.disabled:
            print("[FAIL] pages/11_Ops_Center.py: apply button missing or "
                  "disabled for an approved request")
            return False
        button.click()
        at.run()
        if at.exception:
            print("[FAIL] pages/11_Ops_Center.py: apply click raised")
            for exc in at.exception:
                print("       ", str(exc)[:400])
            return False
        warnings = " ".join(str(w.value) for w in at.warning)
        if "change loop disabled" not in warnings:
            print("[FAIL] pages/11_Ops_Center.py: disabled-loop refusal "
                  "feedback missing")
            return False
        if any("applied." in s.value for s in at.success):
            print("[FAIL] pages/11_Ops_Center.py: apply reported success "
                  "while the loop was disabled")
            return False
        snap = _change_snapshot(uuid)
        if snap["status"] != "approved" or snap["applied_at"] is not None:
            print("[FAIL] pages/11_Ops_Center.py: disabled loop still mutated "
                  "the request")
            return False
        print("[ OK ] pages/11_Ops_Center.py: apply is gated by the disabled "
              "loop and reports the refusal")
        return True
    finally:
        _delete_change_requests([uuid])


def page11_expiry_feedback() -> bool:
    """An expired request is transitioned by the service and the UI reports
    the refusal instead of approving it."""
    uuid = _create_smoke_change(
        None, False, ttl_minutes=1, now=dt.datetime(2020, 1, 1, 0, 0, 0))
    try:
        at = AppTest.from_file(
            str(ROOT / "pages/11_Ops_Center.py"), default_timeout=180)
        at.run()
        impact = _selectbox(at, "Impact analysis request")
        if impact is None:
            print("[FAIL] pages/11_Ops_Center.py: impact selector missing")
            return False
        impact.select(uuid)
        at.run()
        approve = _selectbox(at, "Approve (validated)")
        if approve is None:
            print("[FAIL] pages/11_Ops_Center.py: approve selector missing")
            return False
        approve.select(uuid)
        at.run()
        button = _button(at, "✅ Approve")
        if button is None or button.proto.disabled:
            print("[FAIL] pages/11_Ops_Center.py: expired request approve "
                  "control unavailable")
            return False
        button.click()
        at.run()
        warnings = " ".join(str(w.value) for w in at.warning)
        if "expired" not in warnings:
            print("[FAIL] pages/11_Ops_Center.py: expiry refusal feedback "
                  "missing")
            return False
        snap = _change_snapshot(uuid)
        if snap["status"] != "expired" or snap["approved_by"] is not None:
            print("[FAIL] pages/11_Ops_Center.py: expired request was approved")
            return False
        print("[ OK ] pages/11_Ops_Center.py: expiry refusal is durable and "
              "persisted")
        return True
    finally:
        _delete_change_requests([uuid])


def page11_cancel_and_audit_reload() -> bool:
    """Cancellation is persisted with its reason, and the audit trail shown
    after a reload matches the stored append-only events."""
    uuid = _create_smoke_change(None, False)
    try:
        at = AppTest.from_file(
            str(ROOT / "pages/11_Ops_Center.py"), default_timeout=180)
        at.run()
        cancel = _selectbox(at, "Cancel")
        if cancel is None:
            print("[FAIL] pages/11_Ops_Center.py: cancel selector missing")
            return False
        cancel.select(uuid)
        at.run()
        reason = _text_input(at, "Cancellation reason (optional)")
        if reason is None:
            print("[FAIL] pages/11_Ops_Center.py: cancel reason missing")
            return False
        reason.set_value("smoke cancellation reason")
        button = _button(at, "🚫 Cancel")
        if button is None or button.proto.disabled:
            print("[FAIL] pages/11_Ops_Center.py: cancel button missing")
            return False
        button.click()
        at.run()
        if at.exception:
            print("[FAIL] pages/11_Ops_Center.py: cancel click raised")
            for exc in at.exception:
                print("       ", str(exc)[:400])
            return False
        if not any("cancelled." in s.value for s in at.success):
            print("[FAIL] pages/11_Ops_Center.py: cancellation confirmation "
                  "lost after st.rerun()")
            return False
        snap = _change_snapshot(uuid)
        if (snap["status"] != "cancelled"
                or snap["cancellation_reason"] != "smoke cancellation reason"):
            print("[FAIL] pages/11_Ops_Center.py: cancellation not persisted")
            return False
        if _audit_actions(uuid) != ["proposed", "cancelled"]:
            print("[FAIL] pages/11_Ops_Center.py: cancellation audit events "
                  "do not match persisted state")
            return False

        inspect = _selectbox(at, "Inspect a request")
        if inspect is None:
            print("[FAIL] pages/11_Ops_Center.py: inspect selector missing")
            return False
        inspect.select(uuid)
        at.run()
        audit_rendered = any(
            "action" in getattr(df.value, "columns", [])
            and "cancelled" in set(df.value["action"].astype(str))
            for df in at.dataframe)
        if not audit_rendered:
            print("[FAIL] pages/11_Ops_Center.py: audit trail not reloaded "
                  "after cancellation")
            return False
        print("[ OK ] pages/11_Ops_Center.py: cancellation and audit reload "
              "match persisted state")
        return True
    finally:
        _delete_change_requests([uuid])


def page11_invalid_stored_json_is_safe() -> bool:
    """Malformed stored validation JSON must show a warning, not crash the
    audit/request inspector."""
    uuid = _create_smoke_change(None, False)
    _corrupt_validation_json(uuid)
    try:
        at = AppTest.from_file(
            str(ROOT / "pages/11_Ops_Center.py"), default_timeout=180)
        at.run()
        inspect = _selectbox(at, "Inspect a request")
        if inspect is None:
            print("[FAIL] pages/11_Ops_Center.py: inspect selector missing")
            return False
        inspect.select(uuid)
        at.run()
        if at.exception:
            print("[FAIL] pages/11_Ops_Center.py: invalid stored JSON crashed "
                  "the page")
            for exc in at.exception:
                print("       ", str(exc)[:400])
            return False
        warnings = " ".join(str(w.value) for w in at.warning)
        if "Stored JSON is invalid" not in warnings:
            print("[FAIL] pages/11_Ops_Center.py: invalid stored JSON warning "
                  "missing")
            return False
        codes = " ".join(str(c.value) for c in at.code)
        if "not valid json" not in codes:
            print("[FAIL] pages/11_Ops_Center.py: raw invalid JSON not shown")
            return False
        print("[ OK ] pages/11_Ops_Center.py: invalid stored JSON is handled "
              "safely")
        return True
    finally:
        _delete_change_requests([uuid])


def page2_flash_survives_rerun() -> bool:
    """Regression: the demo-fabric seed confirmation was also lost because
    the page reran immediately after ``st.success``."""
    at = AppTest.from_file(str(ROOT / "pages/2_Topology.py"),
                           default_timeout=180)
    at.run()
    button = next((b for b in at.button
                   if "Seed / refresh demo fabric" in (b.label or "")), None)
    if button is None:
        print("[FAIL] pages/2_Topology.py: seed button not rendered")
        return False
    button.click()
    at.run()
    if at.exception:
        print("[FAIL] pages/2_Topology.py: after seeding the demo fabric")
        for exc in at.exception:
            print("       ", str(exc)[:400])
        return False
    if not any("Fabric seeded:" in s.value for s in at.success):
        print("[FAIL] pages/2_Topology.py: confirmation lost after st.rerun()")
        return False
    print("[ OK ] pages/2_Topology.py: confirmation survives st.rerun()")
    return True


def _radio(at, label: str):
    return next((r for r in at.radio if (r.label or "") == label), None)


def page12_lab_roundtrip() -> bool:
    """AI Lab: a simulated dataset has to produce a REAL anomaly experiment.

    Guards the three things a placeholder UI would fake: a run that reports an
    ``ok`` status only after computing, supervised metrics that exist because
    the simulator supplied ground truth, and exports/history that reference
    that stored run.
    """
    page = "pages/12_AI_Lab.py"
    at = AppTest.from_file(str(ROOT / page), default_timeout=300)
    at.run()
    if at.exception:
        print(f"[FAIL] {page}: initial render")
        for exc in at.exception:
            print("        ", str(exc)[:400])
        return False
    build = next((b for b in at.button
                  if "Build simulated dataset" in (b.label or "")), None)
    if build is None:
        print(f"[FAIL] {page}: 'Build simulated dataset' not rendered")
        return False
    build.click()
    at.run()
    if at.exception:
        print(f"[FAIL] {page}: after building the simulated dataset")
        for exc in at.exception:
            print("        ", str(exc)[:400])
        return False
    task_radio = _radio(at, "Experiment task")
    if task_radio is None:
        print(f"[FAIL] {page}: task picker missing after loading a dataset")
        return False
    anomaly = next((o for o in task_radio.options
                    if str(o).startswith("anomaly")), None)
    if anomaly is None:
        print(f"[FAIL] {page}: anomaly task not offered")
        return False
    task_radio.set_value(anomaly)
    at.run()
    run = next((b for b in at.button
                if "Run experiment" in (b.label or "")), None)
    if run is None:
        print(f"[FAIL] {page}: 'Run experiment' not rendered")
        return False
    run.click()
    at.run()
    if at.exception:
        print(f"[FAIL] {page}: after running the anomaly experiment")
        for exc in at.exception:
            print("        ", str(exc)[:400])
        return False
    if not any("finished with status `ok`" in s.value for s in at.success):
        print(f"[FAIL] {page}: no completed-run confirmation after running")
        return False
    if not any("supervised" in (m.label or "").lower()
               and str(m.value).strip().lower() == "yes" for m in at.metric):
        print(f"[FAIL] {page}: ground-truth evaluation was not reported even "
              "though the simulator provides labels")
        return False
    downloads = [d.label or "" for d in at.get("download_button")]
    missing = [name for name in ("Table CSV", "Metrics CSV",
                                 "Markdown report", "HTML report")
               if name not in downloads]
    if missing:
        print(f"[FAIL] {page}: exports missing: {missing}")
        return False
    history = next((s for s in at.selectbox
                    if "Open a stored experiment" in (s.label or "")), None)
    if history is None or not list(history.options):
        print(f"[FAIL] {page}: the stored run is not inspectable in history")
        return False
    print(f"[ OK ] {page}: simulated dataset ran a real anomaly experiment, "
          f"reported supervised metrics and exported "
          f"({len(history.options)} stored run(s))")
    return True


def page12_lab_live_source_disabled() -> bool:
    """AI Lab: the optional live telemetry source must stay visibly disabled
    (and say why) when no endpoint is configured - never a fake success."""
    page = "pages/12_AI_Lab.py"
    at = AppTest.from_file(str(ROOT / page), default_timeout=300)
    at.run()
    source_radio = _radio(at, "Source type")
    if source_radio is None:
        print(f"[FAIL] {page}: source picker not rendered")
        return False
    live = next((o for o in source_radio.options if "Prometheus" in str(o)),
                None)
    if live is None:
        print(f"[FAIL] {page}: live source option not offered")
        return False
    source_radio.set_value(live)
    at.run()
    if at.exception:
        print(f"[FAIL] {page}: selecting the live source raised")
        for exc in at.exception:
            print("        ", str(exc)[:400])
        return False
    text = " ".join([w.value for w in at.warning] + [i.value for i in at.info])
    if "disabled" not in text or "configur" not in text.lower():
        print(f"[FAIL] {page}: unconfigured live source did not report the "
              "disabled boundary")
        return False
    if any("Loaded" in s.value or "fetched" in s.value for s in at.success):
        print(f"[FAIL] {page}: unconfigured live source claimed success")
        return False
    print(f"[ OK ] {page}: live source stays disabled and honest without an "
          "endpoint")
    return True


page8_ok = press("pages/8_Anomalies.py", "Run detection",
                 select_metric="ping.loss_pct")
page10_ok = page10_same_run_in_two_tabs()
page11_flash_ok = page11_flash_survives_rerun()
page11_open_approval_ok = page11_open_mode_approval_feedback()
page11_auth_decision_ok = page11_authenticated_decision_feedback()
page11_impact_ok = page11_impact_report_visible_before_approval()
page11_mismatch_ok = page11_impact_mismatch_guard()
page11_insufficient_ok = page11_insufficient_impact_evidence()
page11_apply_ok = page11_apply_gating_feedback()
page11_expiry_ok = page11_expiry_feedback()
page11_cancel_ok = page11_cancel_and_audit_reload()
page11_json_ok = page11_invalid_stored_json_is_safe()
page2_flash_ok = page2_flash_survives_rerun()
page12_lab_ok = page12_lab_roundtrip()
page12_live_ok = page12_lab_live_source_disabled()

actions_ok = (
    page8_ok
    and page10_ok
    and page11_flash_ok
    and page11_open_approval_ok
    and page11_auth_decision_ok
    and page11_impact_ok
    and page11_mismatch_ok
    and page11_insufficient_ok
    and page11_apply_ok
    and page11_expiry_ok
    and page11_cancel_ok
    and page11_json_ok
    and page2_flash_ok
    and page12_lab_ok
    and page12_live_ok
    and press("pages/9_Forecasting.py", "Run forecast comparison")
    # Read-only insight action: it must never raise or write.
    and press("pages/11_Ops_Center.py", "Run multivariate check")
    # Regression guard: submitting a change proposal used to raise sqlalchemy
    # DetachedInstanceError, because the page read an ORM attribute after the
    # committing session had closed. See pages/11_Ops_Center.py.
    and press("pages/11_Ops_Center.py", "Validate & record proposal")
)

# Demo banner must be present on the landing page (spec: clearly labelled).
at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120)
at.run()
banner = any("DEMO MODE" in w.value for w in at.warning)
print("demo banner shown:", "OK" if banner else "MISSING")
sys.exit(1 if (failed or not banner or not actions_ok) else 0)
