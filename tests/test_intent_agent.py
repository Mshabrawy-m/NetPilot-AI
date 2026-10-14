"""Intent Agent tests - Arabic/English parsing via mocked LLM outputs (§11.1)."""
from __future__ import annotations

from ai.agents import intent_agent
from tests.ai_fakes import intent_json, make_assistant


def _assistant_for(intent_payload):
    return make_assistant(
        lambda sys: intent_json() if "classify" not in sys else intent_payload)


def test_english_question():
    a = _assistant_for(intent_json(request_type="question",
                                    goal="why pc1 cannot reach pc2"))
    intent = intent_agent.parse_intent(a, "Why can't PC1 reach PC2?")
    assert intent.request_type == "question"
    assert intent.source_language == "en"


def test_arabic_language_detected_in_code():
    # Even if the model claims English, code detection wins for Arabic script.
    a = _assistant_for(intent_json(request_type="change_request",
                                    source_language="en", targets=["R2"],
                                    goal="isolate"))
    intent = intent_agent.parse_intent(a, "اعزل شبكة HR عن الخوادم")
    assert intent.source_language == "ar"


def test_ambiguous_change_request_asks_clarification():
    a = _assistant_for(intent_json(request_type="change_request",
                                   targets=[], missing_fields=["subnet"]))
    intent = intent_agent.parse_intent(a, "Block some traffic please")
    assert intent.request_type == "change_request"
    assert intent.needs_clarification is True
    assert intent.clarifying_questions  # non-empty


def test_informational_never_flagged_for_clarification_by_default():
    a = _assistant_for(intent_json(request_type="question"))
    intent = intent_agent.parse_intent(a, "What is the link status of R1?")
    assert intent.needs_clarification is False
