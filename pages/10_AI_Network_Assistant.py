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

tab_new, tab_hist, tab_know = st.tabs(["💬 New request", "🗂 Run history",
                                        "📚 Knowledge & Ask"])


# ------------------------------------------------------------------ helpers
def _jload(x):
    return json.loads(x) if isinstance(x, str) else x


def _render_detail(uuid: str) -> None:
    detail = read(lambda s: ai_svc.get_run_detail(s, uuid))
    if not detail:
        st.info("Run not found.")
        return
    run = detail["run"]
    st.markdown(
        f"**Run** `{run.run_uuid[:8]}` · status: **{run.status}** · "
        f"source: `{run.data_source}` · type: `{run.request_type}` · "
        f"model: `{run.model}` · prompt v{run.prompt_version}")

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
    with st.expander("✍️ Record human review outcome"):
        outcome = st.selectbox(
            "Outcome", ["approved_for_phase4_review", "rejected",
                       "needs_more_investigation"],
            key=f"review_{run.run_uuid}")
        if st.button("Save review (does not deploy anything)",
                     key=f"save_{run.run_uuid}"):
            ok = False
            sess = get_session()
            try:
                ok = ai_svc.submit_human_review(sess, run.run_uuid, outcome)
            finally:
                sess.close()
            st.success("Saved" if ok else "Run not found")
        st.caption(
            f"Currently: {run.human_review_outcome or 'not reviewed'}")


# ---------------------------------------------------------------- new request
with tab_new:
    source = st.radio("Evidence source", ["demo", "live"], horizontal=True,
                      help="demo = clearly-labelled synthetic fixtures; "
                           "live = lab measurements already stored.")
    request = st.text_area(
        "Your question or request (Arabic or English)",
        placeholder="Why can’t PC1 reach PC2?  /  اعزل شبكة HR عن الخوادم باستثناء HTTPS")
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
        _render_detail(st.session_state["last_ai_run"])

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
        _render_detail(sel)


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
