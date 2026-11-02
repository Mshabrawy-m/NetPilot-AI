"""Groq adapter tests - fully offline (injected fake client, no key/network).

Covers spec §11.4: missing key, timeout, rate limit, malformed output, bounded
retries, and schema-failure handling. Nothing here calls the real API.
"""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from ai.groq_client import (GroqAssistant, GroqBadResponse, GroqRateLimit,
                            GroqSchemaError, GroqTimeout, MissingGroqKey,
                            _classify)
from ai.schemas import Intent
from config import get_settings
from tests.ai_fakes import FakeClient, intent_json


def _assistant(responder, max_retries=2):
    a = GroqAssistant(get_settings(), client=FakeClient(responder),
                      sleeper=lambda _s: None)
    a.max_retries = max_retries
    return a


def test_missing_key(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "")
    a = GroqAssistant(get_settings(), client=None)
    assert a.available is False
    with pytest.raises(MissingGroqKey):
        a.complete_json("s", "u", Intent)


def test_sdk_retry_layer_is_disabled_so_adapter_owns_the_budget(monkeypatch):
    captured = {}

    class SDKClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setitem(sys.modules, "groq", SimpleNamespace(Groq=SDKClient))
    settings = get_settings()
    monkeypatch.setattr(settings, "groq_api_key", "test-key")
    assistant = GroqAssistant(settings)
    assert assistant.available is True
    assert captured["max_retries"] == 0
    assert captured["timeout"] == settings.groq_timeout_seconds


def test_happy_path_returns_validated_schema():
    a = _assistant(lambda sys: intent_json(request_type="investigation"))
    result = a.complete_json("classify the user request", "hi", Intent)
    assert isinstance(result, Intent)
    assert result.request_type == "investigation"


def test_rate_limit_is_transient_and_bounded():
    def responder(_sys):
        return GroqRateLimit("boom")
    a = _assistant(responder, max_retries=2)
    with pytest.raises(GroqRateLimit):
        a.complete_json("classify the user request", "hi", Intent)
    # attempts == max_retries + 1 (bounded, not unbounded)
    assert a._client.chat.completions.calls == 3


def test_transient_then_success_retries_once():
    state = {"n": 0}

    def responder(_sys):
        state["n"] += 1
        if state["n"] == 1:
            return GroqTimeout("temp")
        return intent_json()
    a = _assistant(responder, max_retries=3)
    result = a.complete_json("classify the user request", "hi", Intent)
    assert isinstance(result, Intent)
    assert a._client.chat.completions.calls == 2


def test_malformed_json_fails_after_bounded_retries():
    a = _assistant(lambda _sys: "this is not json", max_retries=1)
    with pytest.raises(GroqBadResponse):
        a.complete_json("classify the user request", "hi", Intent)
    assert a._client.chat.completions.calls == 2


@pytest.mark.parametrize("content", [None, 42, ["not", "text"]])
def test_non_text_model_content_is_a_typed_bad_response(content):
    with pytest.raises(GroqBadResponse, match="content must be text"):
        GroqAssistant._parse_json(content)


def test_malformed_completion_shape_is_a_typed_bad_response():
    class MalformedCompletions:
        def create(self, **_kwargs):
            return type("Completion", (), {"choices": None})()

    client = type("Client", (), {
        "chat": type("Chat", (), {"completions": MalformedCompletions()})()
    })()
    assistant = GroqAssistant(get_settings(), client=client)
    with pytest.raises(GroqBadResponse, match="malformed completion"):
        assistant._raw_chat([])


def test_schema_failure_never_fabricates():
    bad = json.dumps({"request_type": "not-a-valid-type"})
    a = _assistant(lambda _sys: bad, max_retries=1)
    with pytest.raises(GroqSchemaError):
        a.complete_json("classify the user request", "hi", Intent)


@pytest.mark.parametrize("exc,expected", [
    (type("RateLimitError", (Exception,), {}), GroqRateLimit),
    (type("APITimeoutError", (Exception,), {}), GroqTimeout),
    (type("APIConnectionError", (Exception,), {}), GroqTimeout),
])
def test_classify_by_name(exc, expected):
    assert isinstance(_classify(exc()), expected)


def test_classify_by_status_code():
    exc = type("APIError", (Exception,), {})(
        "server exploded")
    exc.status_code = 503
    assert isinstance(_classify(exc), GroqTimeout)
