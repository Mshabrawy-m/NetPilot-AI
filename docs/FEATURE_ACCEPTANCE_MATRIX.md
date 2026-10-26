# NetPilot AI — Feature Acceptance Matrix (Phase C)

> **Status update (Phase D1-D4, 2026-10-26):** the E4 row below is the historical
> Phase C finding and is **superseded**. A deterministic Digital Twin engine
> (`services/digital_twin.py`), its integrity/freshness layer, and a persistent
> append-only snapshot registry (`services/digital_twin_registry.py`) now exist
> and are covered by tests. They are **not wired into `ai/graph.py`**, so the AI
> workflow still reports `digital_twin_status == "not_run"`. See
> `docs/D1_Digital_Twin_Implementation_Report.md` through
> `docs/D4_Persistent_Snapshot_Registry_Report.md` and
> `docs/FINAL_PROJECT_CLOSURE_REPORT.md` for the current status. The rest of this
> document remains the historical Phase C audit.

**Audit date:** 2026-10-26
**Repository snapshot:** branch `main` @ `f5dcb5f5518a7d69af1bb36242536c60d2170526`
**Working tree:** 17 modified tracked files + 13 B1–B4 untracked files (8
`git status` entries because `evaluation/` is one directory), plus the two Phase C
reports created by this audit. **Nothing was staged, committed, pushed, deployed,
or reset.**
**Audit type:** read-only, repository-grounded feature audit. No application logic,
runtime flag, credential, Streamlit Cloud setting, Docker/WSL/Containerlab resource,
Gate 3 run, or Gate D run was started.

---

## 1. Source of truth and scope interpretation

### 1.1 Canonical roadmap

The canonical roadmap is:

- **`docs/NetPilot_AI_Advanced_Roadmap.md` — Advanced Capabilities Roadmap (v2.0),
  including §11 "Delivery log (v2 build) — evidence only".**

Supporting requirement sources, in descending authority:

1. `docs/NetPilot_Phase_1_Network_Foundation_EN.md` — Phase 1 requirements and
   §7 Definition of Done.
2. `docs/NetPilot_Phase_2_Data_Analysis_Forecasting_EN.md` — Phase 2 requirements
   and §14 Definition of Done.
3. `docs/NetPilot_Phase_3_Multi_Agent_AI_Safe_Operations_EN.md` — Phase 3
   requirements, mandatory constraints and §7 Digital Twin boundary.
4. `README.md` — documented behaviour, limitations and explicit scope boundaries
   (§11, §14, §15).
5. Prior acceptance artifacts (evidence, not requirements):
   `V2_Acceptance_Gate_Report.md`, `Phase3_Final_Acceptance_Report.md`,
   `Phase3_Gate3_Acceptance_Matrix.md`, `Phase3_Gate3_Gap_Report.md`,
   `docs/Phase2_Delivery_Report.md`, `docs/Phase3_Delivery_Report.md`.

### 1.2 Roadmap is a menu; the delivery log names the confirmed track

The roadmap preamble calls the document "a plan and menu, not a changelog" and
says "nothing here is implemented yet". That preamble is stale: §11 is the live
delivery log. This audit treats §11 as the current record and the roadmap §2
pillars as the feature catalogue.

§11 states the confirmed all-in track:

> **Sprint 1 + Phase 4 + ML + RAG + REST/WS + Postgres + best auth.**

Therefore:

- **CORE** rows are the Phase 1–3 baseline plus the confirmed v2 track.
- **MENU** rows are roadmap catalogue features that were **not** selected in the
  confirmed track. Their absence is not a freeze blocker; they are recorded for
  completeness and future planning.
- **SUPPORT** rows are cross-cutting evidence/readiness items that are not
  product features but are required to accept the product.

This avoids the false choice between "the roadmap is a menu, so nothing is
mandatory" and "every menu item is mandatory".

### 1.3 Conflict and ambiguity register

| # | Conflict / ambiguity | Resolution used in this audit |
|---|---|---|
| X-1 | Roadmap preamble says "nothing here is implemented yet"; §11 lists slices 1–8 as shipped. | §11 is the current delivery log; the preamble is historical boilerplate. |
| X-2 | Roadmap design principle 1 makes the safety chain **propose → deterministic verify → real Digital-Twin simulate → human approval → lab-only apply → post-change verify → auto-rollback** non-negotiable. §11 says slice 7 (change loop) is shipped but §11's "Next (pending)" mentions only live-lab e2e, not the Digital Twin engine. | The Digital Twin engine is **in the confirmed Phase 4 definition** (roadmap §3: "Digital Twin engine → approval workflow → lab-only apply → post-change verify → auto-rollback") and in design principle 1. It is therefore a **mandatory confirmed-scope gap**, not a menu item. |
| X-3 | README §14 says "the dashboard is unchanged: from the UI, applying is still always a no-op" and the loop is driven from the CLI. Roadmap pillar A does not explicitly require a UI apply control. | The **capabilities** (apply/verify/rollback) are mandatory; the **UI integration** is a documented safety boundary, not an explicit roadmap requirement. It is recorded as a workflow gap (E10), not as a roadmap violation. |
| X-4 | Phase 3 spec §7 requires a Digital Twin **interface** that honestly reports `not_run`; roadmap Phase 4 requires a **real** Digital Twin engine. | Both are true at different scopes. The Phase 3 interface is COMPLETE (C9); the real v2 engine is MISSING (E4). |
| X-5 | Gate 3 and Gate D are sometimes conflated. | Gate 3 authority is **Reading B** (`tools/phase3_lab_validation.py`); Gate D is the separate apply→verify→rollback validation (`tools/phase4_change_loop_validation.py`). Both are BLOCKED on this host. |
| X-6 | Roadmap says "CI reports eval score; regression gates merge" for the eval harness. B4 ships an offline eval but does not wire it into CI. | The eval harness is COMPLETE (C14); the **CI eval gate** is PARTIAL (F8). |
| X-7 | "Predictive alerts" in Phase 2 §8 is phrased "A simple rule **may** flag a forecast…". | Optional; recorded as MENU (B11), not a mandatory gap. |

---

## 2. Status and evidence definitions

| Status | Meaning |
|---|---|
| **COMPLETE** | The requirement's implementation and available evidence satisfy its acceptance criteria at the stated scope. A separate live/environment row may still be BLOCKED. |
| **PARTIAL** | Implemented, but at least one acceptance criterion is unmet (missing UI integration, alternative technology, unvalidated live half, incomplete guard). |
| **MISSING** | No implementation of the requirement was found. |
| **BLOCKED** | Acceptance requires an external environment that is unavailable here (live lab, Docker/Containerlab, PostgreSQL, real browser/provider), so it cannot be executed. |
| **UNVERIFIED** | Implementation exists but no executed evidence in this phase or in repository artifacts proves the acceptance criterion. |

Evidence classes used in the matrix:

| Class | Meaning |
|---|---|
| **EXECUTED-NOW** | A command was actually run during this Phase C audit and its result is recorded in `FEATURE_FREEZE_REPORT.md` §8. |
| **ARTIFACT** | Supported by a versioned repository artifact (test file, report, delivery log, README) that was read in this phase. |
| **REPORTED** | Claimed by a previous phase; not independently re-executed in this phase. |
| **BLOCKED/UNTESTED** | Cannot be executed in this environment; no claim of success is made. |

A passing test proves the logic it exercises, not that a user-facing workflow is
complete, and never that a live network/provider/browser was validated.

---

## 3. Acceptance matrix

### 3.1 Phase 1 — network foundation and data collection

| ID | Requirement (source) | Implementation | Evidence / tests | Status | Scope | Remaining acceptance criteria / dependencies |
|---|---|---|---|---|---|---|
| A1 | Virtual lab definition, FRR configs, deploy/destroy automation (Phase 1 §3.1, §7) | `lab/topology.clab.yml`, `lab/configs/**`, `network/containerlab_manager.py` | ARTIFACT: lab files present; deploy/destroy documented in README §2/§9. No live deploy executed. | **COMPLETE** | CORE | Live deploy/stop is row A2. |
| A2 | Lab deployed and healthy on a Docker/Containerlab host (Phase 1 §7; Gate 3 prerequisite) | `lab/**`, `network/containerlab_manager.py` | BLOCKED/UNTESTED: `docker` and `containerlab` absent; WSL has no distro. | **BLOCKED** | CORE | Install Docker + Containerlab on an isolated Linux host; run `containerlab deploy`/`inspect`. |
| A3 | Periodic live telemetry collection: management ping, `/sys` counters, `vtysh show ip route json` (Phase 1 §3.2, §3.5) | `network/collector.py` / `services/collection_service.py`, `services/telemetry.py`, `network/connectivity.py` | ARTIFACT: `tests/test_collector.py` (4), `tests/test_scheduler.py` (5), `tests/test_connectivity.py` (14) pass offline; fake probes only. | **BLOCKED** | CORE | ≥2 real collection cycles with `data_source="live"` on the lab host (Gate 3 L-3). |
| A4 | Data storage with timestamps, source provenance, additive schema (Phase 1 §3.3; Phase 2 §9) | `database/models.py`, `database/connection.py`, `database/repository.py` | ARTIFACT: `tests/test_database.py` (10), `tests/test_analytics_persistence.py` (7); additive `init_db()` + `_ensure_additive_columns`. EXECUTED-NOW: full suite exit 0. | **COMPLETE** | CORE | PostgreSQL is row F4. |
| A5 | Topology representation, visualization, provenance; four layouts (Phase 1 §3.4; roadmap §2 G; delivery log "Enh") | `services/topology_service.py`, `pages/2_Topology.py`, `analytics/graph.py` | ARTIFACT: `tests/test_topology_views.py` (18), `tests/test_graph_intelligence.py` (11). EXECUTED-NOW: UI smoke page 2 `[OK]`. | **COMPLETE** | CORE | Live topology animation is a MENU row (G3). |
| A6 | Device and interface inventory (Phase 1 §3.4) | `pages/3_Devices.py`, `network/device_inventory.py`, `database/repository.py` | ARTIFACT: `tests/test_inventory.py` (16); UI smoke page 3 `[OK]`. | **COMPLETE** | CORE | — |
| A7 | Connectivity tests (ping/traceroute) with allowlist, timeouts, clear failures (Phase 1 §3.4, §5) | `pages/4_Connectivity_Tests.py`, `network/connectivity.py`, `network/device_inventory.py` | ARTIFACT: `tests/test_connectivity.py` (14). Logic complete; no live lab run. | **PARTIAL** | CORE | Execute a real in-lab ping/traceroute on the Gate 3 host. |
| A8 | Historical metrics and system-status UI (Phase 1 §3.4) | `pages/5_Metrics_History.py`, `pages/6_System_Status.py`, `services/topology_service.py` | ARTIFACT: `tests/test_status_rules.py` (10); UI smoke pages 5/6 `[OK]`. | **COMPLETE** | CORE | Live charts require A3. |
| A9 | Demo/live separation; demo never presented as real (Phase 2 §14; README §11) | `config.py` (`demo_mode`, separate DB), `services/demo_data.py`, `data_source` columns | ARTIFACT: `tests/test_demo_mode.py` (4), `tests/test_analytics_persistence.py`; delivery log slice 1b provenance test. | **COMPLETE** | CORE | — |
| A10 | UI navigation, branding, all pages reachable (roadmap §2 G; delivery log slice 3) | `app.py`, `pages/1..11`, `ui/theme.py`, `services/app_state.py` | EXECUTED-NOW: UI smoke 12 targets `[OK]`, demo banner OK, exit 0. | **COMPLETE** | CORE | — |

### 3.2 Phase 2 — analytics, anomaly detection, forecasting

| ID | Requirement (source) | Implementation | Evidence / tests | Status | Scope | Remaining acceptance criteria / dependencies |
|---|---|---|---|---|---|---|
| B1 | Time-series preparation, data quality, readiness gates (Phase 2 §4) | `analytics/time_series.py`, `analytics/data_quality.py` | ARTIFACT: `tests/test_data_quality.py` (16); `tests/test_temporal_splits.py` (7). | **COMPLETE** | CORE | — |
| B2 | Derived metrics: counter→rate/utilization, reset/gap handling (Phase 2 §5) | `analytics/derived_metrics.py` | ARTIFACT: `tests/test_derived_metrics.py` (10). | **COMPLETE** | CORE | — |
| B3 | EDA: summaries, distributions, correlations, units/source (Phase 2 §6) | `analytics/eda.py`, `pages/7_Data_Analysis.py` | ARTIFACT: `tests/test_eda.py` (15); UI smoke page 7 `[OK]`. | **COMPLETE** | CORE | — |
| B4 | Anomaly detection: ≥2 explainable baselines, no future leakage (Phase 2 §7) | `analytics/anomaly_detection.py`, `services/anomaly_service.py` | ARTIFACT: `tests/test_anomaly_detection.py` (17), `tests/test_analytics_persistence.py` (7). | **COMPLETE** | CORE | — |
| B5 | ML/multivariate anomaly detection (roadmap §2 C; delivery log 4a) | `analytics/ml_anomaly.py`, `pages/11_Ops_Center.py` Insights | ARTIFACT: `tests/test_ml_anomaly.py` (5); UI smoke "Run multivariate check" `[OK]`. | **COMPLETE** | CORE | — |
| B6 | Forecasting: naive baseline + seasonal naive + damped Holt + ridge-lag (Phase 2 §8; roadmap §2 C) | `analytics/forecasting.py`, `services/forecast_service.py`, `pages/9_Forecasting.py` | ARTIFACT: `tests/test_forecasting.py` (12), `tests/test_backtest.py` (7); UI smoke forecast comparison `[OK]`. | **COMPLETE** | CORE | sktime/Prophet were not used; statsmodels is blocked by Windows App Control, so pure-NumPy Holt + ridge-lag are the documented substitutes. |
| B7 | Forecast evaluation: chronological splits, MAE/RMSE/R², identical timestamps (Phase 2 §8, §14) | `analytics/evaluation.py`, `analytics/backtest.py`, `pages/9_Forecasting.py` | ARTIFACT: `tests/test_backtest.py` (7), `tests/test_temporal_splits.py` (7). | **COMPLETE** | CORE | — |
| B8 | Anomaly event persistence, grouping/dedup, lifecycle (Phase 2 §7) | `services/anomaly_service.py`, `database/models.py` | ARTIFACT: `tests/test_anomaly_detection.py`, `tests/test_analytics_persistence.py`; page 8 UI smoke `[OK]`. | **COMPLETE** | CORE | — |
| B9 | Forecast persistence separate from observations (Phase 2 §8) | `services/forecast_service.py`, `database/models.py` | ARTIFACT: `tests/test_analytics_persistence.py` (7). | **COMPLETE** | CORE | — |
| B10 | Analytics UI pages 7–9, explicit-run heavy actions (Phase 2 §10) | `pages/7_Data_Analysis.py`, `pages/8_Anomalies.py`, `pages/9_Forecasting.py` | EXECUTED-NOW: UI smoke pages 7/8/9 `[OK]`; detection and forecast buttons executed. | **COMPLETE** | CORE | — |
| B11 | Predictive/forecast-based alerts (Phase 2 §8, "may") | None found | No implementation or test. | **MISSING** | MENU | Optional in Phase 2 wording; not selected in the confirmed track. |

### 3.3 Phase 3 — multi-agent AI and safe operations

| ID | Requirement (source) | Implementation | Evidence / tests | Status | Scope | Remaining acceptance criteria / dependencies |
|---|---|---|---|---|---|---|
| C1 | Arabic/English intent handling, language detection, clarification gating (Phase 3 §3, §6) | `ai/agents/intent_agent.py`, `ai/prompts.py`, `ai/schemas.py` | ARTIFACT: `tests/test_intent_agent.py` (4), `tests/test_workflow_routing.py` (10). REPORTED: live Groq EN/AR classification passed in prior phases. | **COMPLETE** | CORE | Live provider re-check is C15. |
| C2 | LangGraph multi-agent workflow, bounded routing, no deploy node (Phase 3 §3–§6) | `ai/graph.py`, `ai/state.py`, `services/ai_workflow_service.py` | ARTIFACT: `tests/test_workflow_routing.py` (10), `tests/test_ai_persistence.py` (6). | **COMPLETE** | CORE | — |
| C3 | Read-only evidence tools, provenance IDs, source filtering, bounds (Phase 3 §4) | `ai/tools/**`, `services/rag.py`, `services/nlquery.py` | ARTIFACT: `tests/test_evidence_tools.py` (10), `tests/test_rag.py` (5), `tests/test_nlquery.py` (7). | **COMPLETE** | CORE | — |
| C4 | Investigation/diagnosis: facts vs hypotheses, missing/stale evidence disclosure (Phase 3 §5) | `ai/agents/investigation_agent.py`, `ai/agents/diagnosis_agent.py`, `ai/graph.py` | ARTIFACT: `tests/test_ai_evaluation.py` (37); B4 abstention/robustness metrics. | **COMPLETE** | CORE | — |
| C5 | Planner and config proposal with diff; review only (Phase 3 §5) | `ai/agents/planner_agent.py`, `ai/agents/config_agent.py`, `ai/verification/config_validation.py` | ARTIFACT: `tests/test_workflow_routing.py`, `tests/test_verifier.py` (17). | **COMPLETE** | CORE | — |
| C6 | Independent deterministic verifier; never a false pass (Phase 3 §6) | `ai/verification/verifier.py`, `policy_checks.py`, `config_validation.py` | ARTIFACT: `tests/test_verifier.py` (17); safety scan exit 0 (EXECUTED-NOW). | **COMPLETE** | CORE | — |
| C7 | Prompt-injection resistance; retrieved text is data (Phase 3 §9) | `ai/tools/_common.py`, `ai/prompts.py`; safety scan | ARTIFACT: `tests/test_prompt_injection.py` (4); `tools/safety_scan.py` OK (EXECUTED-NOW). | **COMPLETE** | CORE | — |
| C8 | Refusal/abstention/safe failure when provider/data/verifier unavailable (Phase 3 §2, §10) | `ai/groq_client.py`, `ai/graph.py`, `services/ai_workflow_service.py` | ARTIFACT: `tests/test_groq_client.py` (10), `tests/test_ai_evaluation.py`; B4 abstention 15/15, safety 16/16 (EXECUTED-NOW). | **COMPLETE** | CORE | — |
| C9 | Digital Twin **interface** reports `not_run` honestly (Phase 3 §7) | `ai/simulation/__init__.py` | ARTIFACT: `UnavailableDigitalTwin` returns `not_run`; page 10 renders status. | **COMPLETE** | CORE | This is the Phase 3 boundary, not the real v2 engine (E4). |
| C10 | Human review outcome recorded without deploying (Phase 3 §11) | `pages/10_AI_Network_Assistant.py`, `services/ai_workflow_service.py` | ARTIFACT: `tests/test_ai_persistence.py` (6); UI smoke page 10 `[OK]`. | **COMPLETE** | CORE | — |
| C11 | AI run persistence and audit trail; no provider secret stored (Phase 3 §11) | `database/models.py`, `database/repository.py`, `services/ai_workflow_service.py` | ARTIFACT: `tests/test_ai_persistence.py` (6); secret scan clean (EXECUTED-NOW). | **COMPLETE** | CORE | — |
| C12 | RAG over docs/runbooks with verbatim source provenance (roadmap §2 D; delivery log 5a) | `services/rag.py`, page 10 "Knowledge & Ask" | ARTIFACT: `tests/test_rag.py` (5); UI smoke page 10 `[OK]`. | **COMPLETE** | CORE | TF-IDF retrieval, not Chroma/embeddings; the roadmap AC (citation + provenance) is met. |
| C13 | NL → telemetry query, AR/EN, read-only (roadmap §2 D; delivery log 5b) | `services/nlquery.py`, page 10 "Knowledge & Ask" | ARTIFACT: `tests/test_nlquery.py` (7). | **COMPLETE** | CORE | — |
| C14 | Offline AI-quality/groundedness evaluation, reproducible report (B4; roadmap §2 D) | `evaluation/**`, `tools/ai_quality_eval.py`, `tools/ai_eval_safety_scan.py` | EXECUTED-NOW: 27/27 cases pass, exit 0; all 6 safety gates pass; report written. ARTIFACT: `tests/test_ai_evaluation.py` (37). | **COMPLETE** | CORE | Not a live-provider/Streamlit/Gate 3/Gate D test. |
| C15 | Live-provider AI quality evaluation (B4 scope boundary; roadmap eval AC) | No live evaluation run | B4 report marks `live_provider_evaluation.status = not_run`, `counted_in_metrics = false`. | **UNVERIFIED** | SUPPORT | Run a separately authorized live-provider evaluation; never cite B4 synthetic scores as provider evidence. |

### 3.4 Ops Center — alerting, graph intelligence, insights

| ID | Requirement (source) | Implementation | Evidence / tests | Status | Scope | Remaining acceptance criteria / dependencies |
|---|---|---|---|---|---|---|
| D1 | Alert rules engine: thresholds, de-dup, open/ack/resolve lifecycle, source-scoped (roadmap §2 E; delivery log 1b) | `analytics/alerts.py`, `services/alert_service.py`, `database/models.py` | ARTIFACT: `tests/test_alerts.py` (11). | **COMPLETE** | CORE | — |
| D2 | Ops Center alert UI: rule CRUD, run cycle, fired queue (roadmap §2 E; delivery log 1c) | `pages/11_Ops_Center.py` tabs 1–2, `services/ops_center_actions.py` | EXECUTED-NOW: UI smoke page 11 `[OK]`; B3 action tests (31). | **COMPLETE** | CORE | — |
| D3 | Network graph engine: reachability, shortest path, blast radius, SPOF (roadmap §2 B; delivery log 1a) | `analytics/graph.py`, `pages/11_Ops_Center.py` graph tab | ARTIFACT: `tests/test_graph_intelligence.py` (11); UI smoke page 11 `[OK]`. | **COMPLETE** | CORE | — |
| D4 | Blast-radius/impact analysis of a proposed change from stored topology (roadmap §2 B; B2) | `services/impact_service.py`, page 11 impact panel | ARTIFACT: `tests/test_impact_analysis.py` (24). EXECUTED-NOW: UI smoke impact guards (5) `[OK]`. | **COMPLETE** | CORE | Stored topology only; cannot prove live connectivity or service impact. |
| D5 | Root-cause correlation across anomalies/devices (roadmap §2 C; delivery log 4b) | `analytics/rca.py`, page 11 Insights | ARTIFACT: `tests/test_rca.py` (8). | **COMPLETE** | CORE | — |
| D6 | Path troubleshooting: hop-by-hop diagnosis with telemetry overlay (roadmap §2 B; Sprint 1 item 3) | `analytics/graph.py::shortest_path`/`reachable_from`; no dedicated UI overlay | ARTIFACT: graph tests. No page renders a path with per-hop metrics/health. | **PARTIAL** | CORE | Add a path view with per-hop telemetry/health and honest gaps. |
| D7 | Ops Center action layer: short transaction, refusal→feedback, no executor injection, durable messages (B3) | `services/ops_center_actions.py`, `services/app_state.py::flash/render_flash`, pages 11/2 | ARTIFACT: `tests/test_ops_center_actions.py` (31). EXECUTED-NOW: UI smoke confirmation/rerun guards `[OK]`. | **COMPLETE** | CORE | — |
| D8 | Insights UI: RCA + multivariate check (delivery log 4a/4b) | `pages/11_Ops_Center.py` Insights tab | EXECUTED-NOW: UI smoke "Run multivariate check" `[OK]`. | **COMPLETE** | CORE | — |

### 3.5 Change loop and Phase 4 safe automation

| ID | Requirement (source) | Implementation | Evidence / tests | Status | Scope | Remaining acceptance criteria / dependencies |
|---|---|---|---|---|---|---|
| E1 | Change proposal validation with the deterministic FRR validator; diff recorded (roadmap §2 A; delivery log slice 7) | `services/change_service.py::propose/validate_proposal`, `ai/verification/config_validation.py` | ARTIFACT: `tests/test_change_service.py` (27). | **COMPLETE** | CORE | — |
| E2 | Approval workflow: authenticated requester/approver, no self-approval, reason, expiry, full audit (roadmap §2 A; B1) | `services/change_service.py`, `database/models.py` (`change_audit_events`), page 11 approval panel | ARTIFACT: `tests/test_change_service.py` B1 section (identity separation, reason, expiry, audit); EXECUTED-NOW: UI smoke open-mode fail-closed, expiry, cancellation/audit guards `[OK]`. | **COMPLETE** | CORE | Open mode intentionally fails closed; live authenticated browser is F2. |
| E3 | Simulation/dry-run before applying (roadmap design principle 1; Phase 4) | CLI dry-run in `tools/phase4_change_loop_validation.py` (prints change/impact/rollback plan, refuses without `--approve-change`); no UI dry-run; no Digital Twin | ARTIFACT: `tests/test_change_loop_harness.py` (8); README §14. | **PARTIAL** | CORE | A real Digital Twin engine (E4) and a UI dry-run are absent. |
| E4 | Real Digital Twin simulation engine: routing/reachability/config diff behind the ABC (roadmap §2 A; Phase 4 definition) | `ai/simulation/__init__.py` only; `UnavailableDigitalTwin` returns `not_run` | ARTIFACT: no simulator implementation; page 10 displays `not_run`. | **MISSING** | CORE | Implement a real isolated simulation and record evidence; do not claim `simulated` without it. |
| E5 | Execution gating: both flags, live mode, lab identity, allowlist, injected executor (roadmap §2 A; Phase 4) | `services/change_execution.py` (11 gates), `network/lab_executor.py` (identity/label/static gate), `services/change_service.py::apply` | ARTIFACT: `tests/test_change_execution.py` (29), `tests/test_lab_executor.py` (35), `tests/test_lab_config_ops.py` (25), `tests/test_gate3_safety.py` (12). | **PARTIAL** | CORE | Live execution is BLOCKED (E11). |
| E6 | Post-change verification: read back the device, compare expected lines, persist verdict (roadmap §2 A; Phase 4) | `network/lab_verifier.py`, `services/change_execution.py` step 10 | ARTIFACT: `tests/test_lab_verifier.py` (9), `tests/test_change_execution.py` (29). | **PARTIAL** | CORE | Service logic COMPLETE; live run BLOCKED; no UI verdict. |
| E7 | Rollback + restoration verification: real undo, added lines removed, base restored (roadmap §2 A; Phase 4) | `services/change_service.py::rollback`, `services/change_execution.py::_rollback`, `network/lab_config_ops.py`, `network/lab_verifier.py::verify_restored_config` | ARTIFACT: `tests/test_change_service.py`, `tests/test_change_execution.py`, `tests/test_lab_config_ops.py`, `tests/test_lab_verifier.py`. | **PARTIAL** | CORE | Service logic COMPLETE; live run BLOCKED; no UI rollback control. |
| E8 | Auto-rollback on regression/verification failure (roadmap §2 A; design principle 1) | `services/change_execution.py` step 11 (`verification failed -> rollback`) | ARTIFACT: `tests/test_change_execution.py::test_verification_failure_triggers_a_rollback_and_verifies_restoration`, partial-apply/timeout cases. | **PARTIAL** | CORE | Live failure-injection run is BLOCKED (E11). |
| E9 | Change-loop UI: propose, approve, reject, cancel, apply refusal, audit trail (delivery log slice 7; B1/B3) | `pages/11_Ops_Center.py` Change loop tab, `services/ops_center_actions.py` | EXECUTED-NOW: UI smoke proposal, open-mode approval fail-closed, authenticated feedback, apply gating, expiry, cancellation/audit, invalid JSON `[OK]`. | **COMPLETE** | CORE | Apply from the UI is intentionally a no-op (E10). |
| E10 | Fully integrated UI apply → verify → rollback workflow (user-workflow interpretation of Phase 4) | None: `ops_center_actions.apply_request` passes no executor; page 11 has no rollback control | ARTIFACT: README §14 says the dashboard apply is always a no-op and the loop is CLI-driven; UI smoke confirms refusal. | **MISSING** | CORE | Documented safety boundary; implement only if the product requires a UI-driven loop. Not an explicit roadmap AC. |
| E11 | Gate D live apply → verify → rollback → restoration on a real device | `tools/phase4_change_loop_validation.py` (real `LabExecutor`) | BLOCKED/UNTESTED: no Docker/Containerlab; harness self-reports `[BLOCKED] PRE-FLIGHT`, exit 2; needs D-3 (lab) + D-4 (per-change approval). | **BLOCKED** | CORE | Run on the isolated lab host with both flags, auth enabled, named non-self principals, `--approve-change`, and captured evidence. |
| E12 | Gate 3 live L-1…L-6 validation (Reading B) | `tools/phase3_lab_validation.py` | BLOCKED/UNTESTED: no Docker/Containerlab/WSL distro; prior runs report `[BLOCKED] PRE-FLIGHT`, exit 2. | **BLOCKED** | CORE | Run the Reading B harness on the isolated lab host and archive evidence. |

### 3.6 Platform, enterprise readiness, deployment

| ID | Requirement (source) | Implementation | Evidence / tests | Status | Scope | Remaining acceptance criteria / dependencies |
|---|---|---|---|---|---|---|
| F1 | Authentication + RBAC: viewer/operator/admin, per-action guards, login gate (roadmap §2 F; delivery log slice 2) | `services/auth.py`, `services/app_state.py`, `pages/11_Ops_Center.py` access panel, `config.py` | ARTIFACT: `tests/test_auth.py` (13); delivery log auth-on AppTest check. | **COMPLETE** | CORE | Live authenticated browser is F2. |
| F2 | Live authenticated browser runtime (acceptance evidence, not a separate feature) | Same as F1 | REPORTED: AppTest auth-on check only; no real browser session artifact found. | **UNVERIFIED** | SUPPORT | Perform a real browser login/logout/RBAC journey on a running server and capture evidence. |
| F3 | Read-only REST + WebSocket API, token guard, CORS, no Streamlit import (roadmap §2 F; delivery log slice 6) | `api/app.py`, `api/readmodels.py`, `services/telemetry.py` | ARTIFACT: `tests/test_api.py` (10); REPORTED: prior real uvicorn probes all green. | **COMPLETE** | CORE | — |
| F4 | PostgreSQL storage path and integration test (roadmap §2 F; delivery log slice 6) | `database/connection.py` (SQLAlchemy URL switch), `requirements.txt` | BLOCKED/UNTESTED: no PostgreSQL instance or `psql`; tests use SQLite only. | **UNVERIFIED** | CORE | Run an integration test against a real PostgreSQL instance (schema, additive migration, repository, API). |
| F5 | Audit and data lineage: change audit, AI audit, metric provenance (roadmap §2 F) | `change_audit_events`, AI run/status/evidence tables, `data_source` columns | ARTIFACT: `tests/test_change_service.py`, `tests/test_ai_persistence.py`; no end-to-end lineage view. | **PARTIAL** | CORE | Add a lineage view from source sample → derived metric → chart/alert. |
| F6 | Secrets/KMS hygiene: no secret in logs/UI/DB, rotation flow, scan (roadmap §2 F, P0) | `tools/secret_scan_check.py`, `config.py` env-only, no secret persistence | EXECUTED-NOW: secret scan clean, exit 0; safety scan clean. REPORTED: exposed Groq key still not revoked (`2dc8fa5762c2`). | **PARTIAL** | CORE | Operator must revoke/replace the exposed key; add a documented rotation flow (KMS optional). |
| F7 | Containerization: one image, two roles, compose stack, non-root (roadmap §2 H; delivery log slice 8) | `Dockerfile`, `docker/entrypoint.sh`, `docker-compose.yml`, `.dockerignore` | ARTIFACT: files present; CI `docker-build` job green (build-only). BLOCKED/UNTESTED: compose stack never run on a Docker host. | **PARTIAL** | CORE | Run `docker compose up --build` and capture dashboard/API/DB health. |
| F8 | CI + coverage + eval gate (roadmap §2 H, P0; delivery log slice 8) | `.github/workflows/ci.yml` | EXECUTED-NOW (remote): GitHub Actions run #14 on HEAD `f5dcb5f` is `completed successfully` (secret scan, installability, pytest 3.11/3.12, UI smoke, docker build). | **PARTIAL** | CORE | Coverage is reported, not gated; B4 eval is not wired into CI; the uncommitted B1–B4 tree is not covered by CI. |
| F9 | Self-observability: metrics, structured logs, traces (roadmap §2 H) | `services/telemetry.py`, `/metrics` JSON + Prometheus text | ARTIFACT: `tests/test_api.py` Prometheus test; REPORTED: prior real-server probes. | **PARTIAL** | CORE | Add structured logs/traces; no OTel instrumentation found. |
| F10 | Async/parallel collection + queue/backpressure (roadmap §2 H) | Single-process daemon thread (`services/collection_service.py`, `tests/test_scheduler.py`) | ARTIFACT: single-process scheduler documented; no queue/backpressure. | **MISSING** | MENU | Not selected in the confirmed track. |
| F11 | Threat model and hardening review (roadmap §2 H) | Safety scans, prompt-injection tests, secret scan; no formal threat-model doc | ARTIFACT: `tests/test_gate3_safety.py`, `tests/test_prompt_injection.py`, `tests/test_secret_scan.py`. | **PARTIAL** | MENU | Add a documented threat model (injection, authz, supply chain). |
| F12 | Deployment readiness (hosted/local) | Streamlit app, Docker files, runbooks, CI | ARTIFACT: Gate 2 runtime acceptance report; CI run #14 green. BLOCKED/UNTESTED: compose stack, live auth, live lab. | **PARTIAL** | SUPPORT | Execute the documented deploy/runbook checks on the target host. |
| F13 | Documentation, configuration, testing readiness | `README.md`, `docs/**`, `.env.example`, 43 test files | EXECUTED-NOW: full suite exit 0; UI smoke exit 0; B4 eval exit 0; `git diff --check` exit 0. | **COMPLETE** | SUPPORT | — |

### 3.7 UX and visualization

| ID | Requirement (source) | Implementation | Evidence / tests | Status | Scope | Remaining acceptance criteria / dependencies |
|---|---|---|---|---|---|---|
| G1 | Brand all pages 2–10 (roadmap §2 G; delivery log slice 3) | `ui/theme.py`, `services/app_state.py`, pages 2–11 | EXECUTED-NOW: UI smoke 12 targets `[OK]`. | **COMPLETE** | CORE | — |
| G2 | Plotly interactive charts + dark theme (roadmap §2 G; delivery log slice 3) | `ui/theme.py`, pages 5/7/8/9 | ARTIFACT: global template; UI smoke renders all chart pages. | **COMPLETE** | CORE | — |
| G3 | Live topology view: animated, health-coloured, click-through (roadmap §2 G) | `pages/2_Topology.py` renders stored state in four interactive layouts | EXECUTED-NOW: page 2 `[OK]`. No live animation/click-through. | **PARTIAL** | MENU | Not selected in the confirmed track. |
| G4 | Command-center home: alerts, health, AI inbox in one view (roadmap §2 G) | `pages/1_Overview.py` shows device health and last collection | EXECUTED-NOW: page 1 `[OK]`. No alert/AI inbox. | **PARTIAL** | MENU | Not selected in the confirmed track. |
| G5 | Cross-page drill-downs preserving filters (roadmap §2 G) | Page links only | No linked-filter navigation found. | **MISSING** | MENU | Not selected in the confirmed track. |
| G6 | CSV/PDF export and shareable links (roadmap §2 G) | None found | No export/download implementation. | **MISSING** | MENU | Not selected in the confirmed track. |

### 3.8 Notifications, reporting and remaining analytics menu

| ID | Requirement (source) | Implementation | Evidence / tests | Status | Scope | Remaining acceptance criteria / dependencies |
|---|---|---|---|---|---|---|
| H1 | Notifications: email/Slack/Telegram/webhook (roadmap §2 E) | None found | No implementation or test. | **MISSING** | MENU | Not selected in the confirmed track. |
| H2 | Escalation/on-call policy (roadmap §2 E) | None found | No implementation or test. | **MISSING** | MENU | Not selected in the confirmed track. |
| H3 | Scheduled reports: daily/weekly PDF/HTML (roadmap §2 E) | None found | No implementation or test. | **MISSING** | MENU | Not selected in the confirmed track. |
| H4 | Retention and downsampling/rollups (roadmap §2 C) | None found; raw samples retained | No implementation or test. | **MISSING** | MENU | Not selected in the confirmed track. |
| H5 | Concept-drift detection (roadmap §2 C) | None found | No implementation or test. | **MISSING** | MENU | Not selected in the confirmed track. |
| H6 | Capacity and trend insights: p95, utilization, growth, SLA/jitter (roadmap §2 C) | EDA summaries only; no capacity report | No dedicated implementation or test. | **MISSING** | MENU | Not selected in the confirmed track. |
| H7 | Resolution memory: similar past incidents/outcomes (roadmap §2 D) | None found | No implementation or test. | **MISSING** | MENU | Not selected in the confirmed track. |
| H8 | Self-critique/guardrail loop (roadmap §2 D) | Deterministic verifier + grounding guard; no second-pass critique | ARTIFACT: verifier and B4 grounding tests; no self-critique loop. | **MISSING** | MENU | Not selected in the confirmed track. |
| H9 | Provider routing, caching, fallback, per-run cost/latency (roadmap §2 D) | `ai/groq_client.py` has timeout + bounded retries + configurable model | ARTIFACT: `tests/test_groq_client.py` (10). No fallback/caching/cost ledger. | **PARTIAL** | MENU | Not selected in the confirmed track. |
| H10 | Streaming token-by-token responses (roadmap §2 D) | None found | No implementation or test. | **MISSING** | MENU | Not selected in the confirmed track. |

---

## 4. Workflow-gap resolution: simulation/dry-run, post-change verification, rollback

The roadmap's design principle 1 makes the full chain **propose → deterministic
verify → real Digital-Twin simulate → human approval → lab-only apply →
post-change verify → auto-rollback on regression** non-negotiable. The table below
separates a **callable service** from a **fully integrated user workflow** and from
**live validation**.

| Capability | Callable service / CLI | UI accessible | Guarded | Tested | Live-validated | Documented | Overall |
|---|---|---|---|---|---|---|---|
| **Simulation / dry-run** | **Yes, partial**: `tools/phase4_change_loop_validation.py` dry-run prints the exact change, impact and rollback plan and refuses to execute without `--approve-change`. No Digital Twin engine. | **No** | Yes (refuses without explicit approval; no device I/O in dry-run) | `tests/test_change_loop_harness.py` (8) | **No** | README §14; runbook §7.4 | **PARTIAL** |
| **Real Digital Twin simulation** | **No**: `ai/simulation/__init__.py` only returns `not_run`. | **No** | N/A | Interface only | **No** | Phase 3 spec §7; roadmap §2 A | **MISSING** |
| **Post-change verification** | **Yes**: `network/lab_verifier.verify_applied_config`, `services/change_execution.execute_change` step 10. | **No** (no verdict/control in page 11) | Yes: unreadable device = failure, missing line = failure, never a false pass | `tests/test_lab_verifier.py` (9), `tests/test_change_execution.py` (29) | **No** | README §14; runbook | **PARTIAL** |
| **Rollback + restoration** | **Yes**: `services/change_service.rollback`, `services/change_execution._rollback`, `network/lab_verifier.verify_restored_config`; auto-rollback on verification failure. | **No** (no rollback button; UI apply is a no-op) | Yes: real undo, removed-line check, restoration must be proven; refuses without executor/flags/approval | `tests/test_change_service.py` (27), `tests/test_change_execution.py` (29), `tests/test_lab_config_ops.py` (25), `tests/test_lab_verifier.py` (9) | **No** | README §14; runbook | **PARTIAL** |
| **Full apply → verify → rollback workflow** | **Yes, CLI-only**: Gate D harness with real `LabExecutor`. | **No** | Yes: both flags, live mode, auth-bound non-self approval, verified rollback plan, `--approve-change` | Offline/simulated tests; harness fail-closed | **No** | README §14; runbook §7.4 | **PARTIAL (service) / MISSING (UI) / BLOCKED (live)** |

**Conclusion.** Simulation/dry-run, post-change verification, and rollback are
**mandatory product capabilities** under the roadmap's non-negotiable safety
chain and the Phase 4 definition. They exist as **callable, guarded, unit-tested
services** and as a **CLI dry-run/apply harness**, but they are **not integrated
into the Streamlit user workflow** (page 11 proposes/approves/rejects/cancels and
refuses apply; it has no apply/verify/rollback controls). They are **not live-
validated**. The real Digital Twin engine is **not implemented**.

---

## 5. Prior limitations re-checked

| Limitation | Status in this audit | Evidence |
|---|---|---|
| Live authenticated browser runtime has not been validated. | **CONFIRMED — still unvalidated.** | `tests/ui_smoke_check.py` uses Streamlit `AppTest` (headless), not a browser. The auth-on check in the delivery log is also AppTest. No browser session artifact found. |
| PostgreSQL migration behaviour has not been tested against a PostgreSQL instance. | **CONFIRMED — still untested.** | No `psql`, no PostgreSQL instance; `tests/test_database.py` uses SQLite; `database/connection.py` supports the URL but no PG integration test exists. |
| B2 uses stored topology and cannot establish live connectivity. | **CONFIRMED — by design.** | `services/impact_service.py` docstring: read-only, stored devices/links, undirected graph, last stored statuses, no live probes; demo topology labelled. |
| B4's dataset is synthetic and the live provider evaluation is not run. | **CONFIRMED — by design.** | `evaluation/datasets/ai_quality_v1.json` labels every case synthetic; `evaluation/README.md` scope boundaries; B4 run prints `provider_tested=False`. |
| Open-mode approval intentionally fails closed. | **CONFIRMED — by design and tested.** | `services/change_service.py` docstring; `tests/test_change_service.py`; UI smoke "open-mode approval fails closed with durable feedback" `[OK]`. |
| Gate 3 remains blocked and Gate D remains NOT RUN. | **CONFIRMED.** | No Docker/Containerlab/WSL distro; prior harnesses report `[BLOCKED] PRE-FLIGHT`, exit 2. No live evidence exists. |

No prior limitation was upgraded to COMPLETE without new evidence.

---

## 6. Status counts

Counts are row counts from §3. `CORE` = Phase 1–3 baseline + confirmed v2 track;
`MENU` = roadmap catalogue not selected in the confirmed track; `SUPPORT` =
cross-cutting evidence/readiness.

| Scope | COMPLETE | PARTIAL | MISSING | BLOCKED | UNVERIFIED | Total |
|---|---:|---:|---:|---:|---:|---:|
| **CORE** | **45** | **12** | **2** | **4** | **1** | **64** |
| MENU | 0 | 4 | 13 | 0 | 0 | 17 |
| SUPPORT | 1 | 1 | 0 | 0 | 2 | 4 |
| **ALL** | **46** | **17** | **15** | **4** | **3** | **85** |

**CORE unresolved rows:** E4 (Digital Twin engine, MISSING), E10 (full UI
apply→verify→rollback, MISSING), D6 (path troubleshooting overlay, PARTIAL),
E3/E5/E6/E7/E8 (simulation and live loop, PARTIAL), F5/F6/F7/F8/F9 (lineage,
secrets, containerization, CI gate, observability, PARTIAL), F4 (PostgreSQL,
UNVERIFIED), A2/A3/E11/E12 (live lab/Gate 3/Gate D, BLOCKED), A7 (live
connectivity, PARTIAL).

---

## 7. Evidence register

### 7.1 Executed during this Phase C audit

| Check | Command | Result |
|---|---|---|
| Test inventory | `pytest --collect-only -q` | **550 tests collected** across 43 files |
| Full offline suite | `pytest -q` | **exit 0** (all collected tests pass; 13 pre-existing `datetime.utcnow()` warnings) |
| Headless UI smoke | `python tests/ui_smoke_check.py` | **exit 0**; 12/12 page targets `[OK]`; 16 action/flash guards `[OK]`; demo banner OK |
| B4 AI-quality evaluation | `python tools/ai_quality_eval.py` | **exit 0**; 27/27 cases pass; groundedness 9/10, all other metrics 100%; 6/6 safety gates pass; live/provider/Streamlit marked not run |
| AI safety scan | `python tools/safety_scan.py` | **exit 0**; no execution/deploy primitives in `ai/`; evidence wrapped in `<untrusted_data>` |
| Secret scan | `python tools/secret_scan_check.py` | **exit 0**; "Secret scan clean." |
| Eval-harness safety scan | `python tools/ai_eval_safety_scan.py` | **exit 0**; no shell/provider/approval/dynamic-code primitives in `evaluation/` |
| Whitespace/conflict check | `git diff --check` | **exit 0** (only LF→CRLF warnings) |
| Environment probe | `docker`, `containerlab`, `wsl -l -v`, `psql` | all absent / no WSL distro / no PostgreSQL client |
| Remote/CI check | `git ls-remote --heads origin`; GitHub Actions page | `origin/main` = `f5dcb5f…`; Actions run #14 on `f5dcb5f` = **completed successfully** |

### 7.2 Supported by repository artifacts (read, not re-executed)

- `docs/NetPilot_AI_Advanced_Roadmap.md` §2/§3/§11 — feature catalogue, Phase 4
  definition, delivery log.
- Phase 1/2/3 specifications — requirements and definitions of done.
- `README.md` §11–§15 — limitations, Phase 3 scope, Phase 4 change-loop status,
  v2 stack/API/CI.
- Prior reports: `V2_Acceptance_Gate_Report.md`,
  `Phase3_Final_Acceptance_Report.md`, `Phase3_Gate3_Acceptance_Matrix.md`,
  `Phase3_Gate3_Gap_Report.md`, `docs/Phase2_Delivery_Report.md`,
  `docs/Phase3_Delivery_Report.md`.
- Test files and their docstrings (B1/B2/B3/B4 labels).

### 7.3 Reported by a previous phase, not independently reverified

- Live Groq EN/AR classification (`LIVE_EXIT=0`, `openai/gpt-oss-20b`).
- Real uvicorn API probes (all endpoints green).
- Hosted Streamlit runtime acceptance (Gate 2 runtime report).
- CI runs before #14 (run #14 was verified this phase).

### 7.4 Not tested / blocked

- Live lab deployment, FRR/OSPF, live telemetry, Gate 3 L-1…L-6.
- Gate D live apply → verify → rollback → restoration.
- PostgreSQL integration.
- Live authenticated browser journey.
- Live-provider AI-quality evaluation.
- `docker compose up` stack.
- Streamlit Cloud deployment changes (not touched).

---

## 8. Matrix maintenance notes

- Every row cites a requirement source; no requirement was invented from a
  similarly named file.
- A `COMPLETE` row does not imply the live environment was validated; live rows
  are separate and BLOCKED.
- A `MISSING` MENU row is not a freeze blocker; the confirmed track did not
  select it.
- The matrix should be updated when Gate 3, Gate D, PostgreSQL, or the live
  browser/provider evaluations produce evidence.
