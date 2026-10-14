"""Planner Agent - ordered plan for an EXPLICIT change request (no execution)."""
from __future__ import annotations

from ai import prompts, schemas
from ai.agents.investigation_agent import evidence_view
from ai.groq_client import GroqAssistant


def plan(assistant: GroqAssistant, intent: dict, evidence: list[dict],
         diagnosis: dict) -> schemas.Plan:
    payload = {
        "intent": intent,
        "diagnosis": diagnosis,
        "evidence": prompts.untrusted_block(
            "read_only_tool_output", str(evidence_view(evidence))),
    }
    result: schemas.Plan = assistant.complete_json(
        prompts.PLANNER_SYSTEM,
        prompts.user_payload("Create a review-only change plan.", payload),
        schemas.Plan,
    )
    # Planning is never authorisation - remind downstream consumers explicitly.
    if "Phase 4" not in " ".join(result.rollback_considerations):
        result.rollback_considerations.append(
            "Rollback execution is out of Phase-3 scope (Phase 4); nothing "
            "here is applied automatically.")
    return result
