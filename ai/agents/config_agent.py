"""Config Agent - *proposal only*, hard-gated in code (Phase 3 spec §5).

Configuration text is produced only when, in code, we can confirm:
  * the request is an explicit change_request (never an informational question),
  * the target device exists in the lab inventory, and
  * the device is actually FRR-configurable in this lab (a router, not a host).

Any generated content is a proposal for review; it is never written to an
active configuration file and there is no apply path in Phase 3.
"""
from __future__ import annotations

from ai import prompts, schemas
from ai.agents.investigation_agent import evidence_view
from ai.groq_client import GroqAssistant


def _pick_target(intent: dict, known: set[str]) -> str | None:
    for t in intent.get("targets", []):
        if isinstance(t, str) and t in known:
            return t
    return None


def propose(assistant: GroqAssistant, intent: dict, evidence: list[dict],
             *, known_devices: set[str],
             configurable_devices: set[str]) -> schemas.ConfigProposal:
    # 1. Gate on request type (defence-in-depth: routing already enforces this).
    if intent.get("request_type") != "change_request":
        return schemas.ConfigProposal(
            status="blocked",
            blocked_reason="Not an explicit change request; configuration "
                           "generation is not permitted for questions.")

    # 2. Gate on a known target.
    device = _pick_target(intent, known_devices)
    if device is None:
        return schemas.ConfigProposal(
            status="blocked",
            blocked_reason="Target device is unknown or absent from the lab "
                           "inventory; refusing to invent one.")

    # 3. Gate on a supported capability (only FRR routers here).
    if device not in configurable_devices:
        return schemas.ConfigProposal(
            status="blocked", device=device,
            blocked_reason=f"{device!r} is not an FRR-configurable lab device "
                           f"(hosts/unknown types are not proposed for).")

    # Ask the model to draft a structured proposal for the confirmed device.
    payload = {
        "intent": intent,
        "confirmed_device": device,
        "baseline_evidence": prompts.untrusted_block(
            "read_only_tool_output", str(evidence_view(evidence))),
    }
    prop: schemas.ConfigProposal = assistant.complete_json(
        prompts.CONFIG_SYSTEM,
        prompts.user_payload("Draft a review-only configuration proposal.",
                             payload),
        schemas.ConfigProposal,
    )

    # 4. Re-enforce the gate on whatever the model returned - never trust it.
    if prop.status == "ok":
        if not prop.config_lines:
            prop.status = "blocked"
            prop.blocked_reason = "Model returned no concrete configuration."
        elif prop.device not in configurable_devices:
            prop.status = "blocked"
            prop.blocked_reason = (f"Model targeted {prop.device!r}, which is "
                                   f"not a configurable lab device.")
        else:
            prop.device = device  # pin to the code-confirmed target
    return prop
