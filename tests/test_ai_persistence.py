"""AI run persistence & audit trail (spec §11.4 / §12).

Each run is persisted with its intent, evidence references, ordered status
transitions and verdict, keeping demo/live provenance separate and never
storing the provider secret.
"""
from __future__ import annotations

import json

from database import repository
from services import ai_workflow_service as ai_svc
from tests.ai_fakes import intent_json, make_assistant


CHANGE_INTENT = intent_json(request_type="change_request", targets=["R2"],
                            services_or_reachability=[])
RESPONSES = {
    "classify the user request": CHANGE_INTENT,
    "summarise the provided evidence": json.dumps(
        {"facts": ["R2 up [ev-2]"], "observations": [], "stale_or_missing": [],
         "evidence_refs": ["ev-2"]}),
    "propose likely causes": json.dumps(
        {"causes": [{"cause": "no anomaly", "supporting_evidence": ["ev-2"],
                     "contradicting_evidence": [], "confidence": "low",
                     "is_inference": False}],
         "next_read_only_checks": [], "missing_evidence": []}),
    "EXPLICIT change request only": json.dumps(
        {"objective": "isolate", "scope": ["R2"], "proposed_steps": ["acl"]}),
    "propose configuration lines": json.dumps(
        {"status": "ok", "device": "R2", "intent": "isolate", "assumptions": [],
         "config_lines": ["interface eth1", " ip access-group 100 in"],
         "diff": "-x", "blocked_reason": None}),
}


def _responder(sys):
    for key, value in RESPONSES.items():
        if key in sys:
            return value
    return "{}"


def _seed(session):
    for name in ("R1", "R2"):
        repository.upsert_device(session, name, "router", status="up")
    session.commit()


def _row_dump(run) -> str:
    parts = []
    for col in run.__table__.columns:
        val = getattr(run, col.name, None)
        parts.append(str(val))
    return "\n".join(parts)


def test_run_persists_full_audit_trail(session):
    _seed(session)
    a = make_assistant(_responder)
    res = ai_svc.run_workflow(session, request_text="Isolate R2",
                              data_source="demo", assistant=a)

    run = repository.get_ai_run_by_uuid(session, res["run_uuid"])
    assert run is not None
    assert run.request_text == "Isolate R2"
    assert run.data_source == "demo"
    assert run.request_type == "change_request"
    assert json.loads(run.intent_json)["targets"] == ["R2"]
    assert run.config_proposal_json  # stored for review
    assert run.status == res["status"]
    assert run.prompt_version


def test_status_transitions_are_ordered_history(session):
    _seed(session)
    res = ai_svc.run_workflow(session, request_text="Isolate R2",
                              data_source="demo",
                              assistant=make_assistant(_responder))
    run = repository.get_ai_run_by_uuid(session, res["run_uuid"])
    transitions = repository.get_status_transitions(session, run.id)
    orders = [t.step_order for t in transitions]
    assert orders == sorted(orders)
    assert transitions[0].status == "received"
    assert transitions[-1].status == res["status"]


def test_evidence_references_are_persisted(session):
    _seed(session)
    res = ai_svc.run_workflow(session, request_text="Isolate R2",
                              data_source="demo",
                              assistant=make_assistant(_responder))
    run = repository.get_ai_run_by_uuid(session, res["run_uuid"])
    evidence = repository.get_ai_evidence(session, run.id)
    assert evidence, "a change run must persist at least one evidence ref"
    # Stable uid + provenance retained for every row.
    assert all(e.evidence_uid and e.data_source == "demo" for e in evidence)
    assert {e.kind for e in evidence} & {"topology", "device", "link",
                                          "connectivity", "metric"}


def test_demo_and_live_runs_are_kept_separate(session):
    _seed(session)
    r_demo = ai_svc.run_workflow(session, request_text="Is R2 up?",
                                 data_source="demo",
                                 assistant=make_assistant(_responder))
    ai_svc.run_workflow(session, request_text="Isolate R2", data_source="live",
                        assistant=make_assistant(_responder))
    demo_runs = repository.get_ai_runs(session, data_source="demo")
    live_runs = repository.get_ai_runs(session, data_source="live")
    assert all(x.data_source == "demo" for x in demo_runs)
    assert all(x.data_source == "live" for x in live_runs)
    assert r_demo["run_uuid"] in {x.run_uuid for x in demo_runs}


def test_human_review_records_outcome_only(session):
    _seed(session)
    res = ai_svc.run_workflow(session, request_text="Isolate R2",
                              data_source="demo",
                              assistant=make_assistant(_responder))
    ok = ai_svc.submit_human_review(session, res["run_uuid"], "rejected")
    assert ok is True
    run = repository.get_ai_run_by_uuid(session, res["run_uuid"])
    assert run.human_review_outcome == "rejected"
    # Reviewing never mutates the verdict or advances past human review.
    assert run.status == "awaiting_human_review"


def test_provider_secret_is_never_stored(session):
    _seed(session)
    assistant = make_assistant(_responder)
    assistant.api_key = "gsk_SENTINEL_SHOULD_NEVER_BE_PERSISTED"
    res = ai_svc.run_workflow(session, request_text="Isolate R2",
                              data_source="demo", assistant=assistant)
    run = repository.get_ai_run_by_uuid(session, res["run_uuid"])
    blob = _row_dump(run)
    for ev in repository.get_ai_evidence(session, run.id):
        blob += _row_dump(ev)
    for t in repository.get_status_transitions(session, run.id):
        blob += _row_dump(t)
    assert "SENTINEL_SHOULD_NEVER_BE_PERSISTED" not in blob
