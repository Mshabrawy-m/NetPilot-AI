"""Deterministic offline assistants used by the evaluation harness.

These are *fixtures*, not production provider code. They let the evaluation
exercise the real workflow/agents/verifier/evidence tools with no API key, no
network access and no real model call. Every response is fixed in the
version-controlled dataset, so a result is reproducible.

The only dynamic part is evidence-id token substitution: a dataset can write
``@device:R2``, ``@kind:connectivity`` or ``@contains:FAILED`` in a scripted
answer and the harness replaces it with the stable ``ev-N`` uid of the matching
evidence record. That keeps cases readable without hard-coding fragile uids.
"""
from __future__ import annotations

import json
import re
from typing import Any

from pydantic import ValidationError

from ai.groq_client import (GroqBadResponse, GroqSchemaError, MissingGroqKey)
from config import get_settings

#: Selector tokens are written inside ``[...]`` in a scripted answer. A
#: ``contains`` selector may span multiple words, so spaces are allowed inside
#: the captured value; device/kind selectors are single tokens by nature.
_TOKEN = re.compile(
    r"@(device|kind|contains):([A-Za-z0-9_.:/%\-]+(?: [A-Za-z0-9_.:/%\-]+)*)")

_TASKS = (
    ("classify the user request", "intent"),
    ("summarise the provided evidence", "investigation"),
    ("propose likely causes", "diagnosis"),
    ("EXPLICIT change request only", "plan"),
    ("propose configuration lines", "config"),
)


def task_for_prompt(system: str) -> str:
    """Map a system prompt to the dataset response key it belongs to."""
    text = system or ""
    for marker, task in _TASKS:
        if marker in text:
            return task
    return "unknown"


def build_evidence_map(records: list[dict]) -> dict:
    """Index evidence records by stable, human-readable selectors.

    ``@device:NAME`` selects the first record for a device, ``@kind:KIND`` the
    first record of a kind, and ``@contains:TEXT`` the first record whose
    content contains TEXT (case-insensitive). The generic content selector
    keeps dataset fixtures readable without hard-coding volatile ``ev-N``
    identifiers.
    """
    by_device: dict[str, str] = {}
    by_kind: dict[str, str] = {}
    uids: dict[str, dict] = {}
    ordered: list[dict] = []
    for rec in records or []:
        uid = rec.get("uid")
        if not uid:
            continue
        uids[uid] = rec
        ordered.append(rec)
        device = rec.get("device_name")
        if device and device not in by_device:
            by_device[device] = uid
        kind = rec.get("kind")
        if kind and kind not in by_kind:
            by_kind[kind] = uid
    return {"device": by_device, "kind": by_kind,
            "records": ordered, "uids": uids}


def _resolve_token(kind: str, name: str, evidence_map: dict) -> str:
    uid: str | None = None
    if kind == "device":
        uid = evidence_map.get("device", {}).get(name)
    elif kind == "kind":
        uid = evidence_map.get("kind", {}).get(name)
    else:
        needle = name.lower()
        for rec in evidence_map.get("records", []):
            if needle in (rec.get("content") or "").lower():
                uid = rec.get("uid")
                break
    # An unresolved token is deliberately left visible so the evaluation
    # fails that case instead of silently inventing a citation.
    return uid or f"@{kind}:{name}"


def resolve_tokens(value: Any, evidence_map: dict) -> Any:
    """Recursively replace ``@selector:value`` tokens in a fixture."""
    if isinstance(value, str):
        return _TOKEN.sub(
            lambda m: _resolve_token(m.group(1), m.group(2), evidence_map),
            value)
    if isinstance(value, list):
        return [resolve_tokens(v, evidence_map) for v in value]
    if isinstance(value, dict):
        return {k: resolve_tokens(v, evidence_map) for k, v in value.items()}
    return value


class ScriptedAssistant:
    """A tiny deterministic stand-in for ``GroqAssistant``.

    It exposes exactly the surface the workflow uses (``settings``, ``model``
    and ``complete_json``), validates every response with the same Pydantic
    schema the real adapter uses, and records how many calls were made.
    """

    def __init__(self, responses: dict, evidence_map: dict | None = None,
                 *, model: str = "offline-scripted-v1") -> None:
        self.responses = responses or {}
        self.evidence_map = evidence_map or {}
        self.settings = get_settings()
        self.model = model
        self.calls = 0

    def complete_json(self, system: str, user: str, schema: type):
        self.calls += 1
        task = task_for_prompt(system)
        if task not in self.responses:
            raise GroqBadResponse(
                f"offline fixture has no scripted response for {task!r}")
        raw = self.responses[task]
        if isinstance(raw, str):
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise GroqBadResponse(
                    "offline fixture is not valid JSON") from exc
        elif isinstance(raw, dict):
            payload = raw
        else:
            raise GroqBadResponse("offline fixture must be an object")
        payload = resolve_tokens(payload, self.evidence_map)
        try:
            return schema.model_validate(payload)
        except ValidationError as exc:
            raise GroqSchemaError(
                f"offline fixture failed schema validation: "
                f"{type(exc).__name__}") from exc


class MissingKeyAssistant:
    """Simulates the real adapter with no ``GROQ_API_KEY`` configured."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.model = "offline-missing-key"
        self.calls = 0

    def complete_json(self, system: str, user: str, schema: type):
        self.calls += 1
        raise MissingGroqKey("GROQ_API_KEY is not configured (offline fixture)")


class InvalidSchemaAssistant:
    """Simulates a provider returning output that cannot satisfy the schema."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.model = "offline-invalid-schema"
        self.calls = 0

    def complete_json(self, system: str, user: str, schema: type):
        self.calls += 1
        raise GroqSchemaError(
            "Model output failed schema validation (offline fixture)")
