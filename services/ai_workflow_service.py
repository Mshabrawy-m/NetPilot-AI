"""AI workflow service - orchestrates a run and persists the audit trail.

Keeps the UI thin: it creates the ``AIRun`` row, drives the LangGraph workflow,
then writes evidence references, status transitions and every structured
output back to the database. No secrets are ever stored. No deploy path exists.
"""
from __future__ import annotations

import datetime as dt
import json
import uuid

from ai.graph import WorkflowRunner, build_graph
from ai.groq_client import GroqAssistant
from ai.prompts import PROMPT_VERSION
from config import get_settings
from database import repository


def _dump(obj) -> str | None:
    if obj is None:
        return None
    return json.dumps(obj, ensure_ascii=False, default=str, sort_keys=True)


def run_workflow(session, *, request_text: str, data_source: str,
                 assistant: GroqAssistant | None = None,
                 now: dt.datetime | None = None) -> dict:
    settings = get_settings()
    now = now or dt.datetime.utcnow()
    # Bound request size inside the service (defence, not just a prompt).
    request_text = (request_text or "").strip()[: settings.ai_request_max_chars]
    data_source = data_source if data_source in ("live", "demo") else "demo"

    assistant = assistant or GroqAssistant(settings)
    run_uuid = str(uuid.uuid4())
    run = repository.create_ai_run(
        session, run_uuid=run_uuid, request_text=request_text,
        data_source=data_source, model=assistant.model,
        prompt_version=PROMPT_VERSION)
    repository.add_status_transition(session, run, "received",
                                     "request intake")

    runner = WorkflowRunner(assistant=assistant, session=session,
                            data_source=data_source, now=now)
    graph = build_graph(runner)
    state = graph.invoke({
        "run_uuid": run_uuid, "request_text": request_text,
        "data_source": data_source, "status_log": [], "step_count": 0,
    })

    # ---- persist structured outputs ------------------------------------
    intent = state.get("intent") or {}
    # A proposal that never ran must persist as NULL, not an empty object.
    prop = state.get("config_proposal") or None
    fields = {
        "request_type": intent.get("request_type"),
        "risk_level": intent.get("risk_level"),
        "source_language": intent.get("source_language"),
        "intent_json": _dump(intent),
        "investigation_json": _dump(state.get("investigation")),
        "diagnosis_json": _dump(state.get("diagnosis")),
        "plan_json": _dump(state.get("plan")),
        "config_proposal_json": _dump(prop),
        "config_diff": prop.get("diff") if prop and prop.get("status") == "ok" else None,
        "verifier_report_json": _dump(state.get("verifier_report")),
        "digital_twin_status": state.get("digital_twin", {}).get(
            "status", "not_run"),
        "digital_twin_report_json": _dump(state.get("digital_twin")),
        "final_answer": state.get("final_answer"),
        "status": state.get("status", "completed"),
        "failure_reason": state.get("failure_reason"),
    }
    repository.update_ai_run(session, run, **fields)

    # ---- evidence references (bounded, provenance) ---------------------
    for ev in state.get("evidence", []):
        repository.add_ai_evidence(
            session, run,
            evidence_uid=ev.get("uid"), kind=ev.get("kind"),
            tool_name=ev.get("tool"), device_name=ev.get("device_name"),
            data_source=ev.get("data_source", data_source),
            observed_at=_parse_dt(ev.get("observed_at")), unit=ev.get("unit"),
            content=ev.get("content"),
            source_ref_json=_dump(ev.get("source_ref")),
            stale=bool(ev.get("stale")), missing=bool(ev.get("missing")))

    # ---- status-transition audit --------------------------------------
    for entry in state.get("status_log", [])[1:]:  # 'received' already added
        repository.add_status_transition(
            session, run, entry.get("status", "completed"),
            entry.get("detail", ""))

    session.commit()
    return {
        "run_uuid": run_uuid,
        "status": fields["status"],
        "request_type": fields["request_type"],
        "verifier_status": (state.get("verifier_report") or {}).get("status"),
        "digital_twin_status": fields["digital_twin_status"],
        "final_answer": fields["final_answer"],
        "needs_clarification": state.get("needs_clarification", False),
        "clarifying_questions": state.get("clarifying_questions", []),
    }


def _parse_dt(value) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value))
    except ValueError:
        return None


def list_runs(session, *, data_source: str | None = None,
              limit: int = 25) -> list:
    return repository.get_ai_runs(session, data_source=data_source,
                                  limit=limit)


def get_run_detail(session, run_uuid: str) -> dict | None:
    run = repository.get_ai_run_by_uuid(session, run_uuid)
    if run is None:
        return None
    evidence = repository.get_ai_evidence(session, run.id)
    transitions = repository.get_status_transitions(session, run.id)

    def _load(x):
        return json.loads(x) if x else None

    return {
        "run": run,
        "intent": _load(run.intent_json),
        "investigation": _load(run.investigation_json),
        "diagnosis": _load(run.diagnosis_json),
        "plan": _load(run.plan_json),
        "config_proposal": _load(run.config_proposal_json),
        "verifier_report": _load(run.verifier_report_json),
        "digital_twin": _load(run.digital_twin_report_json),
        "evidence": evidence,
        "transitions": transitions,
    }


def submit_human_review(session, run_uuid: str, outcome: str) -> bool:
    run = repository.get_ai_run_by_uuid(session, run_uuid)
    if run is None:
        return False
    repository.set_human_review(session, run, outcome)
    session.commit()
    return True
