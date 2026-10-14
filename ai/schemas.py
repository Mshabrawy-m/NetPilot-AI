"""Typed, bounded Pydantic schemas for every model output (Phase 3 spec §5/§6).

Rule enforced here: **validate model outputs with typed schemas before passing
them to later workflow nodes.** A node never receives raw LLM text - only an
instance of one of these models, or the workflow fails safe. Fields are
length-bounded and free-form numbers are avoided (uncertainty is categorical:
low/medium/high - never an invented probability).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

RequestType = Literal["question", "investigation", "change_request"]
RiskLevel = Literal["low", "medium", "high"]
Confidence = Literal["low", "medium", "high"]
VerifyStatus = Literal["passed", "failed", "inconclusive"]
DTStatus = Literal["not_run", "passed", "failed", "inconclusive"]

#: Hard caps so no single LLM string can bloat a run or a stored row.
_MAX_ITEMS = 40
_MAX_ITEM_LEN = 800


def _bounded_str(v: str) -> str:
    return (v or "").strip()[:_MAX_ITEM_LEN]


class _Safe(BaseModel):
    """Base: reject unknown keys, clamp list sizes and string lengths."""
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


def _clean_list(items: object, *, limit: int = _MAX_ITEMS) -> list[str]:
    if not isinstance(items, (list, tuple)):
        return []
    out: list[str] = []
    for it in items:
        if isinstance(it, (str, int, float)):
            s = _bounded_str(str(it))
            if s and s not in out:
                out.append(s)
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------
# Intent
# --------------------------------------------------------------------------
class Intent(_Safe):
    request_type: RequestType = "question"
    goal: str = ""
    source_language: Literal["en", "ar", "other"] = "other"
    targets: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    services_or_reachability: list[str] = Field(default_factory=list)
    risk_level: RiskLevel = "low"
    missing_fields: list[str] = Field(default_factory=list)
    clarifying_questions: list[str] = Field(default_factory=list)
    #: Set by the graph when required fields are missing / request ambiguous.
    needs_clarification: bool = False

    @field_validator("goal", mode="before")
    @classmethod
    def _g(cls, v: str) -> str:
        return _bounded_str(v)

    @field_validator("targets", "constraints", "services_or_reachability",
                     "missing_fields", "clarifying_questions", mode="before")
    @classmethod
    def _l(cls, v: object) -> list[str]:
        return _clean_list(v)


# --------------------------------------------------------------------------
# Investigation (observed facts vs stale/missing data)
# --------------------------------------------------------------------------
class Investigation(_Safe):
    facts: list[str] = Field(default_factory=list)          # grounded in ev ids
    observations: list[str] = Field(default_factory=list)   # interpretation
    stale_or_missing: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)

    @field_validator("facts", "observations", "stale_or_missing",
                     "evidence_refs", mode="before")
    @classmethod
    def _l(cls, v: object) -> list[str]:
        return _clean_list(v)


# --------------------------------------------------------------------------
# Diagnosis (facts vs hypotheses, with evidence + uncertainty)
# --------------------------------------------------------------------------
class Cause(_Safe):
    cause: str = ""
    supporting_evidence: list[str] = Field(default_factory=list)
    contradicting_evidence: list[str] = Field(default_factory=list)
    confidence: Confidence = "low"
    #: True when not backed by a concrete evidence id (an inference).
    is_inference: bool = True

    @field_validator("cause", mode="before")
    @classmethod
    def _c(cls, v: str) -> str:
        return _bounded_str(v)

    @field_validator("supporting_evidence", "contradicting_evidence",
                     mode="before")
    @classmethod
    def _l(cls, v: object) -> list[str]:
        return _clean_list(v)


class Diagnosis(_Safe):
    causes: list[Cause] = Field(default_factory=list)
    next_read_only_checks: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)

    @field_validator("next_read_only_checks", "missing_evidence", mode="before")
    @classmethod
    def _l(cls, v: object) -> list[str]:
        return _clean_list(v)


# --------------------------------------------------------------------------
# Plan (change requests only)
# --------------------------------------------------------------------------
class Plan(_Safe):
    objective: str = ""
    scope: list[str] = Field(default_factory=list)
    preconditions: list[str] = Field(default_factory=list)
    proposed_steps: list[str] = Field(default_factory=list)
    expected_outcomes: list[str] = Field(default_factory=list)
    required_checks: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    side_effects: list[str] = Field(default_factory=list)
    #: Phase 4 will execute rollback; here it is only a documented note.
    rollback_considerations: list[str] = Field(default_factory=list)

    @field_validator("objective", mode="before")
    @classmethod
    def _o(cls, v: str) -> str:
        return _bounded_str(v)

    @field_validator("scope", "preconditions", "proposed_steps",
                     "expected_outcomes", "required_checks", "risks",
                     "side_effects", "rollback_considerations", mode="before")
    @classmethod
    def _l(cls, v: object) -> list[str]:
        return _clean_list(v)


# --------------------------------------------------------------------------
# Config proposal (never applied)
# --------------------------------------------------------------------------
class ConfigProposal(_Safe):
    #: 'blocked' when essential details are unknown; 'clarify' to ask user.
    status: Literal["ok", "blocked", "clarify"] = "blocked"
    device: str | None = None
    intent: str = ""
    assumptions: list[str] = Field(default_factory=list)
    #: Structured, template-derived configuration lines (a *proposal*).
    config_lines: list[str] = Field(default_factory=list)
    diff: str | None = None
    blocked_reason: str | None = None

    @field_validator("intent", mode="before")
    @classmethod
    def _i(cls, v: str) -> str:
        return _bounded_str(v)

    @field_validator("assumptions", "config_lines", mode="before")
    @classmethod
    def _l(cls, v: object) -> list[str]:
        return _clean_list(v, limit=200)


# --------------------------------------------------------------------------
# Independent verifier report (produced by code, not the LLM)
# --------------------------------------------------------------------------
class Check(_Safe):
    name: str = ""
    #: None means the check could not be executed -> contributes 'inconclusive'.
    passed: bool | None = None
    severity: Literal["info", "warning", "critical"] = "info"
    finding: str = ""
    evidence_refs: list[str] = Field(default_factory=list)

    @field_validator("name", "finding", mode="before")
    @classmethod
    def _s(cls, v: str) -> str:
        return _bounded_str(v)

    @field_validator("evidence_refs", mode="before")
    @classmethod
    def _l(cls, v: object) -> list[str]:
        return _clean_list(v)


class VerifierReport(_Safe):
    status: VerifyStatus = "inconclusive"
    checks: list[Check] = Field(default_factory=list)
    skipped_checks: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    version: str = "1"

    @field_validator("skipped_checks", "evidence_refs", mode="before")
    @classmethod
    def _l(cls, v: object) -> list[str]:
        return _clean_list(v)


def intent_from_llm(payload: dict) -> Intent:
    """Coerce a validated dict into Intent (raises pydantic.ValidationError)."""
    return Intent.model_validate(payload)
