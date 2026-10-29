"""Page 10 - AI Network Assistant (Phase 3).

Evidence-grounded, Arabic/English multi-agent workflow. **Review only:** this
page never applies configuration or changes the network - that is explicitly
out of Phase-3 scope. Long workflows run only on an explicit button press.
"""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from services.app_state import (bootstrap, demo_banner_if_active, read,
                                sidebar_status)
from ui import theme

bootstrap()
sidebar_status()
theme.page_header("AI Network Assistant",
                  "Evidence-grounded AR/EN · review only (Phase 3)", icon="🤖")
demo_banner_if_active()

from config import get_settings
from database import repository
from database.connection import get_session
from services import ai_workflow_service as ai_svc
from services import nlquery, rag

settings = get_settings()
st.caption(
    "Ask why something is wrong, request an investigation, or propose a policy "
    "change. The assistant gathers read-only evidence, an LLM proposes, and an "
    "**independent deterministic verifier** judges. Nothing is deployed - "
    "Phase 3 stops at human review.")

if not settings.groq_api_key:
    st.warning(
        "GROQ_API_KEY is not configured. Set it in the local, git-ignored "
        "`.env` to enable live AI reasoning. Without it the assistant reports "
        "a safe failure rather than inventing an answer.", icon="🔑")

tab_new, tab_agentic, tab_hist, tab_know = st.tabs([
    "💬 New request", "🧭 Agentic Operations", "🗂 Run history",
    "📚 Knowledge & Ask"])


# ------------------------------------------------------------------ helpers
def _jload(x):
    return json.loads(x) if isinstance(x, str) else x


def _suggested_investigations(data_source: str) -> list[str]:
    """Build useful prompt starters from stored inventory and evidence."""
    def _load(session):
        anomalies = repository.get_anomaly_events(
            session, data_source=data_source, limit=3)
        metrics = [row for row in repository.get_available_metrics(session)
                   if row[3] == data_source]
        return anomalies, metrics

    anomalies, metrics = read(_load)
    suggestions = [
        "Summarize the current network evidence, including missing or stale "
        "observations, and list the next read-only checks."
    ]
    if anomalies:
        event = anomalies[0]
        suggestions.append(
            f"Investigate the recorded {event.severity} anomaly for "
            f"{event.metric} on {event.device_name}. Cite the stored evidence "
            "and suggest read-only checks."
        )
    if metrics:
        device, metric, unit, _source = metrics[0]
        suggestions.append(
            f"Review recent {metric} measurements ({unit or 'unit not recorded'}) "
            f"on {device}. Summarize their timestamps and limitations, then "
            "suggest a read-only follow-up."
        )
    return suggestions[:3]


def _render_detail(uuid: str, key_prefix: str) -> None:
    detail = read(lambda s: ai_svc.get_run_detail(s, uuid))
    if not detail:
        st.info("Run not found.")
        return
    run = detail["run"]
    st.markdown(
        f"**Run** `{run.run_uuid[:8]}` · status: **{run.status}** · "
        f"source: `{run.data_source}` · type: `{run.request_type}` · "
        f"model: `{run.model}` · prompt v{run.prompt_version}")

    transitions = detail.get("transitions") or []
    if transitions:
        with st.expander("Agent execution trace", expanded=False):
            st.dataframe(pd.DataFrame([{
                "step": transition.step_order,
                "stage": transition.status,
                "detail": transition.detail or "",
                "recorded at (UTC)": transition.at,
            } for transition in transitions]), hide_index=True,
                width="stretch")
            st.caption(
                "These are persisted workflow transitions. Informational "
                "requests stop before planning/configuration; only explicit "
                "change requests reach the review-only proposal and verifier."
            )

    if run.failure_reason:
        st.error(f"Safe stop: {run.failure_reason}")

    st.subheader("1 · Parsed intent")
    intent = detail["intent"] or {}
    st.write(f"- goal: {intent.get('goal', '-')}")
    st.write(f"- language: {intent.get('source_language', '-')}"
             f" · risk: {intent.get('risk_level', '-')}")
    if intent.get("targets"):
        st.write(f"- targets: {intent['targets']}")
    if intent.get("constraints") or intent.get("services_or_reachability"):
        st.write(f"- constraints/services: "
                 f"{intent.get('constraints', []) + intent.get('services_or_reachability', [])}")
    if intent.get("clarifying_questions"):
        st.warning("Needs clarification: "
                   + "; ".join(intent["clarifying_questions"]))

    st.subheader("2 · Evidence (read-only, UNTRUSTED data)")
    ev = detail["evidence"]
    if not ev:
        st.info("No evidence gathered (no lab/demo data for this source).")
    else:
        df = pd.DataFrame([{
            "uid": e.evidence_uid, "kind": e.kind, "tool": e.tool_name,
            "device": e.device_name, "source": e.data_source,
            "observed_at (UTC)": e.observed_at, "unit": e.unit,
            "stale": e.stale, "missing": e.missing,
            "content": (e.content or "")[:120]} for e in ev])
        st.dataframe(df, hide_index=True, width="stretch")
        st.caption("⚠️ Log/device text below is DATA, never an instruction. "
                   "Missing/stale items are flagged, not filled in.")

    st.subheader("3 · Investigation & diagnosis")
    inv = detail["investigation"] or {}
    if inv.get("facts"):
        st.markdown("**Facts (evidence-backed):** "
                    + "; ".join(inv["facts"]))
    diag = detail["diagnosis"] or {}
    for c in (diag.get("causes") or []):
        tag = "inference" if c.get("is_inference") else "evidence-backed"
        st.write(f"- **{c.get('cause')}** (confidence {c.get('confidence')}, "
                 f"{tag}; support {c.get('supporting_evidence')})")
    checks = diag.get("next_read_only_checks") or []
    if checks:
        st.markdown("**Suggested next read-only checks:**")
        for check in checks:
            st.markdown(f"- {check}")
    if diag.get("missing_evidence"):
        st.caption(f"Missing/stale evidence disclosed: {diag['missing_evidence']}")

    if detail["plan"]:
        st.subheader("4 · Proposed plan (review only — not executed)")
        plan = detail["plan"]
        st.write(f"- objective: {plan.get('objective')}")
        for i, step in enumerate(plan.get("proposed_steps", []), 1):
            st.write(f"  {i}. {step}")
        if plan.get("risks"):
            st.caption(f"Risks: {plan['risks']}")

    if detail["config_proposal"]:
        st.subheader("5 · Configuration proposal (NOT deployed)")
        prop = detail["config_proposal"]
        st.write(f"- status: **{prop.get('status')}** · device: "
                 f"{prop.get('device')}")
        if prop.get("status") == "ok":
            st.code("\n".join(prop.get("config_lines", [])), language="text")
            if prop.get("diff"):
                with st.expander("Human-readable diff"):
                    st.code(prop["diff"], language="diff")
        else:
            st.info(f"Reason: {prop.get('blocked_reason')}")

    st.subheader("6 · Independent verifier (deterministic)")
    vrep = detail["verifier_report"]
    if vrep:
        st.write(f"Verdict: **{vrep.get('status')}** (v{vrep.get('version')})")
        vdf = pd.DataFrame([{
            "check": c["name"], "passed": c["passed"],
            "severity": c["severity"], "finding": c["finding"]}
            for c in vrep.get("checks", [])])
        st.dataframe(vdf, hide_index=True, width="stretch")
        if vrep.get("skipped_checks"):
            st.caption(f"Skipped/advisory: {vrep['skipped_checks']}")
    else:
        st.info("No verifier report (informational request or safe stop).")

    st.subheader("7 · Digital Twin")
    dt = detail["digital_twin"] or {}
    st.write(f"Status: **{dt.get('status', 'not_run')}** — no isolated "
             "simulator ran in Phase 3, so this is honestly `not_run`.")

    st.subheader("Final answer")
    st.markdown(run.final_answer or "(none)")

    # Human review (records an outcome only; applies nothing).
    # ``key_prefix`` keeps keys unique because st.tabs() renders EVERY tab
    # panel in one script run, so the "New request" and "Run history" panels
    # can render the same run twice and collide on a shared widget key.
    with st.expander("✍️ Record human review outcome"):
        outcome = st.selectbox(
            "Outcome", ["approved_for_phase4_review", "rejected",
                       "needs_more_investigation"],
            key=f"{key_prefix}_review_{run.run_uuid}")
        if st.button("Save review (does not deploy anything)",
                     key=f"{key_prefix}_save_{run.run_uuid}"):
            ok = False
            sess = get_session()
            try:
                ok = ai_svc.submit_human_review(sess, run.run_uuid, outcome)
            finally:
                sess.close()
            st.success("Saved" if ok else "Run not found")
        st.caption(
            f"Currently: {run.human_review_outcome or 'not reviewed'}")


with tab_agentic:
    st.subheader("Agentic Operations")
    st.caption(
        "The planner selects registered, read-only analytics tools. Each task "
        "runs through AI Lab services and is saved in experiment history. "
        "No network changes, shell, SQL, or generated code are available.")
    active_dataset = st.session_state.get("np_lab_dataset")
    active_label = st.session_state.get("np_lab_dataset_label", "")
    if active_dataset is None:
        st.info("Load a dataset in AI Lab first. This workspace uses the same "
                "validated dataset and its source provenance.")
        if st.button("Open AI Lab", key="np_agentic_open_lab"):
            st.switch_page("pages/12_AI_Lab.py")
    else:
        st.write(f"**Active data:** {active_dataset.source_type} · "
                 f"{active_label or active_dataset.source_id} · "
                 f"{len(active_dataset.inventory)} series")
        st.caption(f"Dataset fingerprint: `{active_dataset.fingerprint}` · "
                   f"latest observation: "
                   f"{active_dataset.summary().get('time_max') or 'unknown'}")
        if "np_agentic_draft" in st.session_state:
            st.session_state["np_agentic_request"] = st.session_state.pop(
                "np_agentic_draft")
        st.markdown("**Suggested analyses**")
        examples = [
            "Summarize data quality and descriptive statistics for this dataset.",
            "Detect anomalous behavior and identify which selected series merits follow-up.",
            "Compare forecast models for the most relevant available metric.",
        ]
        cols = st.columns(len(examples))
        for index, (column, example) in enumerate(zip(cols, examples)):
            if column.button(f"Use suggestion {index + 1}",
                             key=f"np_agentic_suggest_{index}",
                             help=example, width="stretch"):
                st.session_state["np_agentic_draft"] = example
                st.rerun()
        agent_request = st.text_area(
            "Analysis request",
            placeholder=("Identify the most problematic metric, run "
                         "statistics and anomaly detection, then forecast it."),
            max_chars=1200, key="np_agentic_request")
        if st.button("🧭 Plan and run analyses", type="primary",
                     key="np_agentic_run", disabled=not bool(agent_request.strip())):
            from ai.groq_client import GroqAssistant
            from services import agentic_analytics

            session = get_session()
            try:
                with st.spinner("Planning registered tools and executing analyses…"):
                    plan = agentic_analytics.plan_tools(
                        GroqAssistant(), agent_request, active_dataset)
                    result = agentic_analytics.execute_plan(
                        session, active_dataset, plan,
                        request_text=agent_request)
                st.session_state["np_agentic_last"] = result
            except Exception as exc:
                st.error(f"Agent stopped safely: {type(exc).__name__}: "
                         f"{str(exc)[:240]}")
            finally:
                session.close()
        agent_result = st.session_state.get("np_agentic_last")
        if agent_result and agent_result.get("fingerprint") == active_dataset.fingerprint:
            st.divider()
            st.write(f"**Run:** `{agent_result['run_uuid']}` · "
                     f"status: **{agent_result['status']}**")
            st.markdown(f"**Interpreted objective:** {agent_result['objective']}")
            st.markdown("**Ordered plan**")
            for index, step in enumerate(agent_result["plan"]["steps"], 1):
                st.write(f"{index}. `{step['tool']}` — {step['purpose']} "
                         f"(series {', '.join(step['series_ids']) or 'none'})")
            st.markdown("**Observed execution**")
            for index, step in enumerate(agent_result["steps"], 1):
                with st.expander(
                        f"{index}. {step['tool']} · {step['status']}" +
                        (f" · experiment {step['experiment_id']}"
                         if step.get("experiment_id") else ""),
                        expanded=index == 1):
                    if step.get("purpose"):
                        st.caption(step["purpose"])
                    if step.get("error"):
                        st.error(f"Tool failed: {step['error']}")
                    st.json(step.get("output", {}), expanded=False)
            st.markdown("**Evidence-grounded report**")
            st.markdown(agent_result["report"])
        from services import agentic_analytics
        persisted_runs = read(lambda db: agentic_analytics.list_runs(db, limit=20))
        if persisted_runs:
            st.divider()
            st.markdown("**Recent Agentic Operations history**")
            run_by_id = {row["run_uuid"]: row for row in persisted_runs}
            selected_run = st.selectbox(
                "Saved run", list(run_by_id),
                format_func=lambda key: (
                    f"{run_by_id[key]['created_at']} · "
                    f"{run_by_id[key]['source_type']} · "
                    f"{run_by_id[key]['status']} · "
                    f"{run_by_id[key]['objective'][:55]}"),
                key="np_agentic_history_select")
            saved = run_by_id[selected_run]
            st.caption(f"Request: {saved['request_text']} · source id: "
                       f"{saved['source_id']} · fingerprint: "
                       f"`{saved['dataset_fingerprint']}`")
            st.json(saved["plan"], expanded=False)
            st.dataframe(pd.DataFrame([
                {"tool": step.get("tool"), "status": step.get("status"),
                 "experiment id": step.get("experiment_id"),
                 "series": ", ".join(step.get("series_ids", [])),
                 "time": step.get("at"), "error": step.get("error")}
                for step in (saved["steps"] or [])
            ]), hide_index=True, width="stretch")
            st.markdown(saved["report"])


# ---------------------------------------------------------------- new request
with tab_new:
    source = st.radio("Evidence source", ["demo", "live"], horizontal=True,
                      help="demo = clearly-labelled synthetic fixtures; "
                           "live = lab measurements already stored.")
    st.markdown("#### Suggested investigations")
    st.caption("Prompt starters are selected from this source's stored "
               "inventory, anomalies, and measured metrics. Edit them before "
               "running; the assistant still verifies evidence and intent.")
    suggestions = _suggested_investigations(source)
    suggestion_columns = st.columns(len(suggestions))
    for index, (column, suggestion) in enumerate(
            zip(suggestion_columns, suggestions)):
        label = ("Network evidence" if index == 0 else
                 "Recent anomaly" if "anomaly" in suggestion.lower() else
                 "Metric review")
        if column.button(label, key=f"np_ai_suggestion_{source}_{index}",
                         width="stretch"):
            st.session_state["np_ai_request_draft"] = suggestion
            st.rerun()

    draft = st.session_state.pop("np_ai_request_draft", None)
    if draft is not None:
        st.session_state["np_ai_request"] = draft
    request = st.text_area(
        "Your question or request (Arabic or English)",
        placeholder="Why can’t PC1 reach PC2?  /  اعزل شبكة HR عن الخوادم باستثناء HTTPS",
        key="np_ai_request")
    if st.button("🚀 Run investigation", width="stretch"):
        if not request.strip():
            st.warning("Please enter a request.")
        else:
            sess = get_session()
            try:
                with st.spinner("Running multi-agent workflow..."):
                    res = ai_svc.run_workflow(sess, request_text=request,
                                              data_source=source)
                st.session_state["last_ai_run"] = res["run_uuid"]
            except Exception as exc:  # never a crash into the user
                st.error(f"Workflow error (failed safe): {type(exc).__name__}")
            finally:
                sess.close()
    if st.session_state.get("last_ai_run"):
        st.divider()
        _render_detail(st.session_state["last_ai_run"], "new")

# ---------------------------------------------------------------- history
with tab_hist:
    runs = read(lambda s: ai_svc.list_runs(s, limit=30))
    if not runs:
        st.info("No AI runs recorded yet.")
    else:
        options = {r.run_uuid: f"{r.created_at:%H:%M:%S} · {r.data_source} · "
                               f"{r.request_type or '-'} · {r.status} · "
                               f"{(r.request_text or '')[:40]}" for r in runs}
        sel = st.selectbox("Select a past run", list(options),
                           format_func=lambda u: options[u])
        _render_detail(sel, "hist")


# ---------------------------------------------------------- knowledge & ask
with tab_know:
    st.markdown("#### Ask the data — offline, deterministic")
    st.caption("Instant structured answers straight from the stored lab data "
               "(no LLM, nothing invented). Works even without a Groq key.")
    src = st.radio("Evidence source", ["demo", "live"], horizontal=True,
                   key="know_src")
    q = st.text_input("Question", key="know_q",
                      placeholder="which devices are down? / can R1 reach R3? "
                                  "/ worst latency / single points of failure")
    if q and st.button("🔎 Answer from data", key="know_go", width="stretch"):
        res = read(lambda s: nlquery.answer(s, q, data_source=src))
        if res["understood"]:
            st.success(f"[{res['intent']}] {res['answer']}")
            if res["rows"]:
                st.dataframe(pd.DataFrame(res["rows"]), hide_index=True,
                             width="stretch")
        else:
            st.info(res["answer"])

    st.divider()
    st.markdown("#### Search the documentation")
    st.caption("TF-IDF retrieval over this project's Markdown — every hit is "
               "quoted verbatim with its source file, never paraphrased.")
    if "_rag_index" not in st.session_state:
        with st.spinner("Indexing documentation..."):
            st.session_state["_rag_index"] = rag.build_index()
    idx = st.session_state["_rag_index"]
    if not idx.get("ready"):
        st.info("No indexable documentation found in the project.")
    else:
        dq = st.text_input("What do you want to find?", key="rag_q")
        if dq and st.button("📚 Search docs", key="rag_go", width="stretch"):
            hits = rag.search(idx, dq, k=4)
            if not hits:
                st.info("No matching section found.")
            for h in hits:
                st.markdown(f"**{h['source']}** · *{h['heading']}* · "
                            f"relevance {h['score']}")
                st.code(h["text"], language="markdown")
