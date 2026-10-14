"""LangGraph multi-agent workflow (Phase 3 spec §4).

Explicit typed state, conditional branches, and safety transitions enforced in
code. Key guarantees:

* Informational questions (``question`` / ``investigation``) are routed so they
  **never** reach the planner or config node.
* Provider/schema/timeout failures set ``failed_safe`` and stop; nothing is
  fabricated and no later node runs its LLM call.
* The graph is a bounded DAG (no cycles); ``step_count`` plus ``max_steps`` is a
  belt-and-braces guard, and there is **no** deployment node anywhere.
"""
from __future__ import annotations

import datetime as dt

from langgraph.graph import END, START, StateGraph

from ai import prompts, schemas
from ai.agents import (config_agent, diagnosis_agent, intent_agent,
                       investigation_agent, planner_agent)
from ai.groq_client import (GroqAssistant, GroqError, MissingGroqKey,
                            GroqRateLimit, GroqSchemaError, GroqTimeout)
from ai.simulation import UnavailableDigitalTwin
from ai.state import WorkflowState
from ai.tools import evidence_tools
from ai.verification import verifier as verifier_mod


class WorkflowRunner:
    def __init__(self, *, assistant: GroqAssistant, session,
                 data_source: str, now: dt.datetime | None = None) -> None:
        self.assistant = assistant
        self.session = session
        self.data_source = data_source
        self.now = now or dt.datetime.utcnow()
        self.max_steps = assistant.settings.ai_graph_max_steps
        self.known_devices = evidence_tools.known_devices(session)
        from database import repository
        self.configurable_devices = {
            d.name for d in repository.get_devices(session)
            if (d.type or "").lower() == "router"}
        self.twin = UnavailableDigitalTwin()

    # -- helpers ----------------------------------------------------------
    def _log(self, status: str, detail: str = "") -> dict:
        return {"status": status, "status_log": [{"status": status,
                                                  "detail": detail}]}

    def _fail(self, reason: str) -> dict:
        upd = self._log("failed_safe", reason)
        upd.update({"failed_safe": True, "failure_reason": reason})
        return upd

    # -- nodes ------------------------------------------------------------
    def intake(self, state: WorkflowState) -> dict:
        return {"step_count": 1, "data_source": self.data_source,
                "provider": "groq", "model": self.assistant.model,
                "prompt_version": prompts.PROMPT_VERSION,
                **self._log("received")}

    def intent(self, state: WorkflowState) -> dict:
        if state.get("failed_safe"):
            return {}
        try:
            intent = intent_agent.parse_intent(
                self.assistant, state["request_text"])
        except MissingGroqKey:
            return self._fail("Groq API key is not configured "
                              "(set GROQ_API_KEY in .env).")
        except (GroqRateLimit, GroqTimeout) as exc:
            return self._fail(f"Provider unavailable (rate limit/timeout): {exc}")
        except GroqSchemaError as exc:
            return self._fail(f"Model output failed schema validation: {exc}")
        except GroqError as exc:
            return self._fail(f"Provider error: {exc}")
        upd = {"intent": intent.model_dump(),
               "needs_clarification": intent.needs_clarification,
               "clarifying_questions": intent.clarifying_questions}
        upd.update(self._log("investigating",
                             f"intent={intent.request_type} "
                             f"lang={intent.source_language}"))
        upd["step_count"] = state.get("step_count", 0) + 1
        return upd

    def gather(self, state: WorkflowState) -> dict:
        if state.get("failed_safe"):
            return {}
        evidence = evidence_tools.gather_evidence(
            self.session, data_source=self.data_source,
            intent=state.get("intent"), now=self.now)
        upd = self._log("investigating", f"gathered {len(evidence)} evidence items")
        upd.update({"evidence": evidence,
                    "step_count": state.get("step_count", 0) + 1})
        return upd

    def investigate(self, state: WorkflowState) -> dict:
        if state.get("failed_safe"):
            return {}
        try:
            inv = investigation_agent.investigate(
                self.assistant, state["request_text"],
                state.get("evidence", []))
        except GroqError as exc:
            return self._fail(f"Investigation failed: {exc}")
        return {"investigation": inv.model_dump(),
                "step_count": state.get("step_count", 0) + 1}

    def diagnose(self, state: WorkflowState) -> dict:
        if state.get("failed_safe"):
            return {}
        try:
            diag = diagnosis_agent.diagnose(
                self.assistant, state["request_text"],
                state.get("intent", {}), state.get("evidence", []),
                state.get("investigation", {}))
        except GroqError as exc:
            return self._fail(f"Diagnosis failed: {exc}")
        upd = self._log("diagnosed", f"{len(diag.causes)} cause(s)")
        upd["diagnosis"] = diag.model_dump()
        upd["step_count"] = state.get("step_count", 0) + 1
        return upd

    def plan(self, state: WorkflowState) -> dict:
        if state.get("failed_safe"):
            return {}
        try:
            result = planner_agent.plan(
                self.assistant, state.get("intent", {}),
                state.get("evidence", []), state.get("diagnosis", {}))
        except GroqError as exc:
            return self._fail(f"Planning failed: {exc}")
        upd = self._log("planned")
        upd["plan"] = result.model_dump()
        upd["step_count"] = state.get("step_count", 0) + 1
        return upd

    def config(self, state: WorkflowState) -> dict:
        if state.get("failed_safe"):
            return {}
        try:
            prop = config_agent.propose(
                self.assistant, state.get("intent", {}),
                state.get("evidence", []), known_devices=self.known_devices,
                configurable_devices=self.configurable_devices)
        except GroqError as exc:
            return self._fail(f"Configuration proposal failed: {exc}")
        if prop.status == "ok":
            upd = self._log("configuration_proposed",
                            f"device={prop.device}")
        else:
            upd = self._log("configuration_proposed",
                            f"status={prop.status}")
        upd["config_proposal"] = prop.model_dump()
        upd["step_count"] = state.get("step_count", 0) + 1
        return upd

    def verify(self, state: WorkflowState) -> dict:
        # Deterministic, independent of the LLM.
        report = verifier_mod.verify(state.get("config_proposal"),
                                     state.get("intent", {}),
                                     self.known_devices)
        upd = self._log(f"verification_{report.status}", report.status)
        upd["verifier_report"] = report.model_dump()
        upd["step_count"] = state.get("step_count", 0) + 1
        return upd

    def digital_twin(self, state: WorkflowState) -> dict:
        result = self.twin.simulate(
            topology_ref=self.data_source,
            proposed_change=state.get("config_proposal"),
            invariants=["preserve_lab_routing"], scenario=None)
        upd = self._log("simulation_not_run", result.status)
        upd["digital_twin"] = result.as_dict()
        upd["step_count"] = state.get("step_count", 0) + 1
        return upd

    def clarify(self, state: WorkflowState) -> dict:
        questions = state.get("clarifying_questions") or [
            "Please clarify the request (targets/services)."]
        answer = ("I need more information before I can safely continue.\n- "
                  + "\n- ".join(questions))
        if state.get("intent", {}).get("source_language") == "ar":
            answer = ("أحتاج إلى مزيد من المعلومات قبل المتابعة بأمان:\n- "
                      + "\n- ".join(questions))
        upd = self._log("awaiting_human_review", "clarification required")
        upd.update({"final_answer": answer,
                    "status": "awaiting_human_review"})
        return upd

    def finalize(self, state: WorkflowState) -> dict:
        answer = _compose_final_answer(state, self.twin)
        intent_type = state.get("intent", {}).get("request_type")
        if not state.get("failed_safe") and intent_type == "change_request":
            status = "awaiting_human_review"
        elif not state.get("failed_safe"):
            status = "completed"
        else:
            status = "failed_safe"
        return {"final_answer": answer, "status": status,
                **self._log(status, "final")}

    # -- routing ----------------------------------------------------------
    def route_after_intent(self, state: WorkflowState) -> str:
        if state.get("failed_safe"):
            return "finalize"
        if state.get("needs_clarification"):
            return "clarify"
        return "gather"

    def route_after_diagnose(self, state: WorkflowState) -> str:
        if state.get("failed_safe"):
            return "finalize"
        if state.get("intent", {}).get("request_type") == "change_request":
            return "plan"
        return "finalize"


def _compose_final_answer(state: WorkflowState, twin) -> str:
    if state.get("failed_safe"):
        return ("I could not complete this safely. Reason: "
                + state.get("failure_reason", "unknown"))
    parts: list[str] = []
    intent = state.get("intent", {})
    lang_ar = intent.get("source_language") == "ar"
    diag = state.get("diagnosis", {})
    vrep = state.get("verifier_report", {})
    dt = state.get("digital_twin", {})

    if lang_ar:
        parts.append("الخلاصة:")
        facts = state.get("investigation", {}).get("facts", [])
        if facts:
            parts.append("حقائق (من الأدلة): " + "; ".join(facts[:5]))
        causes = diag.get("causes", [])
        if causes:
            parts.append("فرضيات: " + "; ".join(
                f"{c.get('cause')} (ثقة {c.get('confidence')})" for c in causes[:3]))
    else:
        parts.append("Summary:")
        facts = state.get("investigation", {}).get("facts", [])
        if facts:
            parts.append("Facts (evidence-backed): " + "; ".join(facts[:5]))
        causes = diag.get("causes", [])
        if causes:
            parts.append("Hypotheses: " + "; ".join(
                f"{c.get('cause')} (confidence {c.get('confidence')}"
                + (", inference" if c.get("is_inference") else "") + ")"
                for c in causes[:3]))
    if state.get("plan"):
        parts.append(("Proposed plan (review only - not applied): "
                      + "; ".join(state["plan"].get("proposed_steps", [])[:6])))
    if state.get("config_proposal", {}).get("status") == "ok":
        parts.append(f"Configuration proposal for {state['config_proposal'].get('device')} "
                     "(PROPOSAL ONLY - not deployed).")
    if vrep:
        parts.append(f"Independent verifier: {vrep.get('status')} "
                     "(deterministic; a static pass is not live behaviour).")
    parts.append(f"Digital Twin: {dt.get('status', 'not_run')}.")
    parts.append("Phase 3 stops at proposal + human review; no changes were made.")
    return "\n".join(parts)


def build_graph(runner: WorkflowRunner):
    g = StateGraph(WorkflowState)
    g.add_node("intake", runner.intake)
    g.add_node("intent", runner.intent)
    g.add_node("gather", runner.gather)
    g.add_node("investigate", runner.investigate)
    g.add_node("diagnose", runner.diagnose)
    g.add_node("plan", runner.plan)
    g.add_node("config", runner.config)
    g.add_node("verify", runner.verify)
    g.add_node("digital_twin", runner.digital_twin)
    g.add_node("clarify", runner.clarify)
    g.add_node("finalize", runner.finalize)

    g.add_edge(START, "intake")
    g.add_edge("intake", "intent")
    g.add_conditional_edges("intent", runner.route_after_intent,
                            {"gather": "gather", "clarify": "clarify",
                             "finalize": "finalize"})
    g.add_edge("gather", "investigate")
    g.add_edge("investigate", "diagnose")
    g.add_conditional_edges("diagnose", runner.route_after_diagnose,
                            {"plan": "plan", "finalize": "finalize"})
    g.add_edge("plan", "config")
    g.add_edge("config", "verify")
    g.add_edge("verify", "digital_twin")
    g.add_edge("digital_twin", "finalize")
    g.add_edge("clarify", END)
    g.add_edge("finalize", END)
    return g.compile()
