"""Intent Agent (Phase 3 spec §5).

Classifies a request as question / investigation / change_request and extracts
structured intent. Language detection and the clarification rule are decided
**in code**, not trusted to the model, so an informational question can never
slip into a change path because of a misclassification, and Arabic input is
labelled correctly regardless of what the model returns.
"""
from __future__ import annotations

import re

from ai import prompts, schemas
from ai.groq_client import GroqAssistant

_ARABIC = re.compile(r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff\ufb50-\ufdff\ufe70-\ufeff]")


def detect_language(text: str) -> str:
    """Authoritative script detection (Arabic vs English vs other)."""
    if _ARABIC.search(text or ""):
        return "ar"
    if re.search(r"[A-Za-z]", text or ""):
        return "en"
    return "other"


def parse_intent(assistant: GroqAssistant, request_text: str) -> schemas.Intent:
    intent: schemas.Intent = assistant.complete_json(
        prompts.INTENT_SYSTEM,
        prompts.user_payload("Classify this network request.",
                             {"request": request_text}),
        schemas.Intent,
    )
    # Code owns the language label and the clarification guard.
    intent.source_language = detect_language(request_text)

    if intent.request_type == "change_request" and (
            not intent.targets or intent.missing_fields):
        intent.needs_clarification = True
        if not intent.clarifying_questions:
            intent.clarifying_questions.append(
                "Please specify the exact target device(s)/subnet(s) and the "
                "required/allowed services before a proposal can be made.")
    # An ambiguous question/investigation may still legitimately ask to clarify;
    # we keep whatever the model flagged but never let it default to a change.
    return intent
