"""Offline contracts for the bounded tool-using analytics agent."""
from __future__ import annotations

import json

import pytest

from analytics import lab_dataset
from database.models import AgenticAnalysisRun
from services import agentic_analytics
from pydantic import ValidationError
from tests.ai_lab_fixtures import unlabelled_dataset


class _Planner:
    def __init__(self, payload):
        self.payload = payload

    def complete_json(self, _system, _user, schema):
        return schema.model_validate(self.payload)


def test_plan_rejects_unknown_or_unregistered_series(sim):
    with pytest.raises(ValidationError):
        agentic_analytics.AgentPlan.model_validate({
            "objective": "x", "extra": "no", "steps": []})
    with pytest.raises(ValueError, match="outside the dataset inventory"):
        agentic_analytics.plan_tools(_Planner({
            "objective": "inspect", "steps": [{"tool": "run_forecast",
                "series_ids": ["s999"], "horizon": 5, "purpose": "forecast"}]
        }), "forecast", sim)


@pytest.fixture
def sim():
    return unlabelled_dataset(n=80)


def test_agent_runs_real_statistics_and_forecast_and_persists_audit(session, sim):
    plan = agentic_analytics.AgentPlan.model_validate({
        "objective": "Summarize and forecast observed latency",
        "steps": [
            {"tool": "run_statistics", "series_ids": ["s0"],
             "horizon": 10, "purpose": "Compute descriptive statistics"},
            {"tool": "run_forecast", "series_ids": ["s0"],
             "horizon": 5, "purpose": "Backtest and forecast"},
        ]})
    result = agentic_analytics.execute_plan(
        session, sim, plan,
        request_text="Summarize latency; api_key=sk-abcdefghijklmnop")
    assert result["status"] == "completed"
    assert [step["tool"] for step in result["steps"]] == [
        "inspect_dataset", "run_statistics", "run_forecast"]
    assert all(step["status"] == "ok" for step in result["steps"])
    assert result["steps"][1]["output"]["table"]
    assert result["steps"][2]["output"]["table"]
    assert "Evidence:" in result["report"]
    audit = session.query(AgenticAnalysisRun).filter_by(
        run_uuid=result["run_uuid"]).one()
    assert audit.source_type == lab_dataset.SOURCE_HISTORICAL
    assert audit.dataset_fingerprint == sim.fingerprint
    assert "sk-abcdefghijklmnop" not in audit.request_text
    assert "[REDACTED]" in audit.request_text
    assert json.loads(audit.steps_json)[-1]["experiment_id"] is not None
    history = agentic_analytics.list_runs(session)
    assert history[0]["run_uuid"] == result["run_uuid"]
    assert len(history[0]["steps"]) == 3


def test_repeated_calls_are_bounded_and_failed_tool_is_not_success(
        session, sim, monkeypatch):
    def fail(*_args, **_kwargs):
        raise RuntimeError("diagnostic detail is not exposed")
    monkeypatch.setattr(agentic_analytics.lab_experiment, "run_experiment", fail)
    plan = agentic_analytics.AgentPlan.model_validate({
        "objective": "Compute statistics",
        "steps": [
            {"tool": "run_statistics", "series_ids": ["s0"],
             "horizon": 10, "purpose": "compute"},
            {"tool": "run_statistics", "series_ids": ["s0"],
             "horizon": 10, "purpose": "repeat"},
        ]})
    result = agentic_analytics.execute_plan(session, sim, plan)
    assert result["status"] == "failed"
    assert result["steps"][1]["status"] == "failed"
    assert result["steps"][1]["error"] == "RuntimeError"
    assert "diagnostic detail" not in result["report"]
    assert result["steps"][2]["error"] == "ValueError"


def test_agent_refuses_invalid_arguments_and_safety_flags_remain_off(
        session, sim):
    with pytest.raises(Exception):
        agentic_analytics.PlannedTool.model_validate({
            "tool": "run_forecast", "series_ids": ["s0"],
            "horizon": 5000, "purpose": "unbounded"})
    from config import get_settings
    settings = get_settings()
    assert settings.change_loop_enabled is False
    assert settings.lab_execution_enabled is False
