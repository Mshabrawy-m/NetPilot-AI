"""Workflow routing + safety-transition tests (spec §11.2, §11.3, §11.11)."""
from __future__ import annotations

import json

import pytest

from ai.agents import intent_agent  # noqa: F401
from ai.groq_client import GroqAssistant
from ai.graph import WorkflowRunner
from config import get_settings
from database import repository
from services import ai_workflow_service as ai_svc
from tests.ai_fakes import intent_json, make_assistant


INVESTIGATION = json.dumps({"facts": ["R2 up [ev-1]"], "observations": [],
                            "stale_or_missing": [], "evidence_refs": ["ev-1"]})
DIAGNOSIS = json.dumps({"causes": [
    {"cause": "no anomaly in evidence", "supporting_evidence": ["ev-1"],
     "contradicting_evidence": [], "confidence": "low", "is_inference": False}],
    "next_read_only_checks": [], "missing_evidence": []})
PLAN = json.dumps({"objective": "isolate", "scope": ["R2"], "preconditions": [],
                   "proposed_steps": ["add acl"], "expected_outcomes": [],
                   "required_checks": [], "risks": ["outage"],
                   "side_effects": [], "rollback_considerations": []})
CONFIG_OK = json.dumps({"status": "ok", "device": "R2", "intent": "isolate",
                        "assumptions": [],
                        "config_lines": ["interface eth1",
                                         " ip access-group 100 in"],
                        "diff": "-x", "blocked_reason": None})


def _responder(intent_payload, *, with_plan=False):
    def r(sys):
        if "classify the user request" in sys:
            return intent_payload
        if "summarise the provided evidence" in sys:
            return INVESTIGATION
        if "propose likely causes" in sys:
            return DIAGNOSIS
        if with_plan:
            if "EXPLICIT change request only" in sys:
                return PLAN
            if "propose configuration lines" in sys:
                return CONFIG_OK
        return "{}"
    return r


def _seed(session):
    for name, type_ in (("R1", "router"), ("R2", "router"), ("PC1", "host")):
        repository.upsert_device(session, name, type_, lab_ip="10.1.1.1",
                                 status="up")
    session.commit()


@pytest.mark.parametrize("forbidden",
                         ["deploy", "apply", "commit", "write_config",
                          "execute", "run_command"])
def test_runner_has_no_deployment_method(forbidden):
    """No graph node may deploy or apply configuration in Phase 3."""
    assert not hasattr(WorkflowRunner, forbidden)


def test_question_never_reaches_config(session):
    _seed(session)
    a = make_assistant(_responder(intent_json(request_type="question")))
    res = ai_svc.run_workflow(session, request_text="Is R2 up?",
                              data_source="demo", assistant=a)
    assert res["status"] == "completed"
    detail = ai_svc.get_run_detail(session, res["run_uuid"])
    assert detail["config_proposal"] is None
    assert detail["plan"] is None
    assert detail["verifier_report"] is None


def test_change_request_reaches_verifier_and_digital_twin(session):
    _seed(session)
    a = make_assistant(_responder(
        intent_json(request_type="change_request", targets=["R2"],
                    services_or_reachability=[]), with_plan=True))
    res = ai_svc.run_workflow(session, request_text="Isolate R2 from HR",
                              data_source="demo", assistant=a)
    assert res["status"] == "awaiting_human_review"
    assert res["digital_twin_status"] == "not_run"
    detail = ai_svc.get_run_detail(session, res["run_uuid"])
    assert detail["verifier_report"]["status"] in ("passed", "failed",
                                                   "inconclusive")
    assert detail["config_proposal"]["status"] == "ok"


def test_ambiguous_change_stops_for_clarification(session):
    _seed(session)
    a = make_assistant(_responder(
        intent_json(request_type="change_request", targets=[],
                    missing_fields=["subnet"])))
    res = ai_svc.run_workflow(session, request_text="Block traffic",
                              data_source="demo", assistant=a)
    assert res["needs_clarification"] is True
    assert res["status"] == "awaiting_human_review"
    detail = ai_svc.get_run_detail(session, res["run_uuid"])
    # Clarification short-circuits before evidence/planning/config.
    assert detail["plan"] is None
    assert detail["config_proposal"] is None


def test_missing_key_fails_safe(session):
    _seed(session)
    # No injected client + empty key -> assistant is unavailable, so the
    # intent node raises MissingGroqKey and the workflow must fail safe
    # (never fabricating an answer).
    offline = GroqAssistant(get_settings(), client=None)
    offline.api_key = ""
    res = ai_svc.run_workflow(session, request_text="Anything",
                              data_source="demo", assistant=offline)
    assert res["status"] == "failed_safe"
