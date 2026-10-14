"""Investigation Agent - separates observed facts from stale/missing data."""
from __future__ import annotations

from ai import prompts, schemas
from ai.groq_client import GroqAssistant


def evidence_view(evidence: list[dict]) -> list[dict]:
    """Compact, already-bounded projection of evidence for the model."""
    return [{
        "uid": e.get("uid"),
        "kind": e.get("kind"),
        "device": e.get("device_name"),
        "observed_at": e.get("observed_at"),
        "unit": e.get("unit"),
        "content": e.get("content"),
        "stale": e.get("stale"),
        "missing": e.get("missing"),
    } for e in evidence]


def investigate(assistant: GroqAssistant, request_text: str,
                evidence: list[dict]) -> schemas.Investigation:
    payload = {
        "request": request_text,
        # Retrieved evidence is embedded as explicitly untrusted DATA.
        "evidence": prompts.untrusted_block(
            "read_only_tool_output", str(evidence_view(evidence))),
    }
    inv: schemas.Investigation = assistant.complete_json(
        prompts.INVESTIGATION_SYSTEM,
        prompts.user_payload("Summarise the evidence.", payload),
        schemas.Investigation,
    )
    # Code guarantees honesty: any record flagged stale/missing is surfaced,
    # whether or not the model mentioned it.
    flagged = [e["uid"] for e in evidence
               if (e.get("stale") or e.get("missing")) and e.get("uid")]
    for uid in flagged:
        if uid not in inv.stale_or_missing:
            inv.stale_or_missing.append(f"{uid} (tool reported stale/missing data)")
    valid = {e.get("uid") for e in evidence if e.get("uid")}
    inv.evidence_refs = [r for r in inv.evidence_refs if r in valid]
    return inv
