"""Diagnosis Agent - evidence-grounded hypotheses with honest uncertainty."""
from __future__ import annotations

from ai import prompts, schemas
from ai.agents.investigation_agent import evidence_view
from ai.groq_client import GroqAssistant


def diagnose(assistant: GroqAssistant, request_text: str, intent: dict,
             evidence: list[dict], investigation: dict) -> schemas.Diagnosis:
    payload = {
        "request": request_text,
        "intent": intent,
        "investigation": investigation,
        "evidence": prompts.untrusted_block(
            "read_only_tool_output", str(evidence_view(evidence))),
    }
    diag: schemas.Diagnosis = assistant.complete_json(
        prompts.DIAGNOSIS_SYSTEM,
        prompts.user_payload("Propose evidence-grounded causes.", payload),
        schemas.Diagnosis,
    )
    valid = {e.get("uid") for e in evidence if e.get("uid")}
    for cause in diag.causes:
        cause.supporting_evidence = [r for r in cause.supporting_evidence
                                     if r in valid]
        cause.contradicting_evidence = [r for r in cause.contradicting_evidence
                                        if r in valid]
        # A cause with no surviving evidence reference is an INFERENCE only.
        if not cause.supporting_evidence:
            cause.is_inference = True
            cause.confidence = "low"

    # Missing/stale evidence must be disclosed, never papered over.
    flagged = sorted({e["uid"] for e in evidence
                      if (e.get("stale") or e.get("missing")) and e.get("uid")})
    for uid in flagged:
        if uid not in diag.missing_evidence:
            diag.missing_evidence.append(uid)
    return diag
