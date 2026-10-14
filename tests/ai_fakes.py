"""Offline fakes for Phase 3 tests (no API key, no network calls).

``FakeGroqClient`` mimics the tiny slice of the Groq SDK the adapter uses
(``client.chat.completions.create``) and returns canned JSON chosen by the
system-prompt task keyword, or raises injected provider errors.
"""
from __future__ import annotations

import json
from typing import Callable

from ai.groq_client import GroqAssistant
from config import get_settings


class _Message:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Message(content)


class _Completion:
    def __init__(self, content):
        self.choices = [_Choice(content)]


class FakeChat:
    def __init__(self, responder: Callable[[str], str]):
        self._responder = responder
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        system = kwargs["messages"][0]["content"]
        result = self._responder(system)
        if isinstance(result, Exception):
            raise result
        return _Completion(result)


class FakeClient:
    def __init__(self, responder: Callable[[str], str]):
        self.chat = type("C", (), {"completions": FakeChat(responder)})()


def make_assistant(responder: Callable[[str], str],
                   *, max_retries: int | None = None) -> GroqAssistant:
    assistant = GroqAssistant(get_settings(),
                              client=FakeClient(responder),
                              sleeper=lambda _s: None)
    if max_retries is not None:
        assistant.max_retries = max_retries
    return assistant


def intent_json(**over) -> str:
    base = {"request_type": "question", "goal": "why down",
            "source_language": "en", "targets": [], "constraints": [],
            "services_or_reachability": [], "risk_level": "low",
            "missing_fields": [], "clarifying_questions": []}
    base.update(over)
    return json.dumps(base)


# Task-keyword routers used by the agents' system prompts.
def route(**responses: str | Exception) -> Callable[[str], str]:
    """Map prompt substrings to canned content/exceptions."""
    def responder(system: str) -> str | Exception:
        for key, value in responses.items():
            if key in system:
                return value
        return "{}"
    return responder
