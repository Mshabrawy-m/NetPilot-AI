"""Typed, bounded LangGraph workflow state (Phase 3 spec §6).

The graph state stays small: it carries identifiers, compact validated
structures and *references* to evidence persisted in the database - never
large payloads and never the API key. ``status_log`` uses an additive reducer
so each node can append an audit step without clobbering history.
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class WorkflowState(TypedDict, total=False):
    # ---- identity / intake ----------------------------------------------
    run_uuid: str
    request_text: str
    data_source: str            # 'live' | 'demo'
    status: str                 # one of models.AI_RUN_STATUSES
    #: Appended by every node (additive reducer) -> ordered audit trail.
    status_log: Annotated[list[dict], operator.add]
    #: Monotonic step counter used by the orchestrator to bound the walk.
    step_count: int

    # ---- agent outputs (validated schema dicts) -------------------------
    intent: dict[str, Any]
    evidence: list[dict[str, Any]]      # compact references, uid-tagged
    investigation: dict[str, Any]
    diagnosis: dict[str, Any]
    plan: dict[str, Any]
    config_proposal: dict[str, Any]
    verifier_report: dict[str, Any]
    digital_twin: dict[str, Any]
    final_answer: str

    # ---- control / safety -----------------------------------------------
    needs_clarification: bool
    clarifying_questions: list[str]
    #: True once any node signals a safe stop (missing key, verifier block...).
    failed_safe: bool
    failure_reason: str
    #: Provenance recorded per run.
    provider: str
    model: str
    prompt_version: str


def record(status_log: list[dict], status: str, detail: str = "") -> list[dict]:
    """Helper for nodes to emit one audit entry (returned for reducer add)."""
    return list(status_log) + [{"status": status, "detail": detail}]
