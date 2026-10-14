# NetPilot AI — Phase 3 Delivery Report
### Multi-Agent AI Assistant & Safe Network Operations

Delivered on top of the completed Phase 1 (network foundation) and Phase 2
(data analysis, anomaly detection, forecasting). Phase 3 adds an
evidence-grounded, Arabic/English AI Network Assistant built on the **Groq**
SDK and **LangGraph**, gated by an independent deterministic verifier, and
stopped deliberately at *proposal + human review*.

---

## 1. Executive summary

| Item | Status |
|---|---|
| Groq provider integration (Python SDK) | ✅ Implemented & **verified live** |
| LangGraph multi-agent orchestration | ✅ Implemented (no deploy node) |
| Arabic + English natural-language requests | ✅ Implemented & verified live |
| Intent / Investigation / Diagnosis / Planner / Config agents | ✅ Implemented |
| Read-only evidence tools over existing Phase 1/2 data | ✅ Implemented |
| Independent deterministic verifier | ✅ Implemented (pure code, no LLM) |
| Digital Twin interface reporting `not_run` honestly | ✅ Implemented |
| AI Network Assistant Streamlit page (page 10) | ✅ Implemented |
| Persistent run history, evidence refs, audit records | ✅ Implemented |
| Secure Groq config (env / git-ignored `.env`, configurable model) | ✅ Implemented |
| Full test suite (mocked Groq, offline) | ✅ **201 passed** |
| UI smoke test (all pages render) | ✅ **all 11 scripts OK** |
| Phase 4 (deploy / post-change verify / rollback) | ⛔ Out of scope, not built |

---

## 2. Baseline (before Phase 3)

Recorded on this machine (Windows, Python 3.12.10, no Docker/Containerlab):

* `pytest` → **144 passed** (60 Phase 1 + 84 Phase 2).
* `tests/ui_smoke_check.py` → all 10 scripts OK, both Phase 2 action buttons
  executed without exceptions, demo banner verified.
* `groq`, `langgraph`, `pydantic` were **not** installed at baseline.

No Phase 1 or Phase 2 functionality was rewritten; Phase 3 is **additive**.

---

## 3. Implemented files

### New — `ai/` package (1,724 lines across 23 files, incl. 4 `__init__.py`)

| File | Lines | Responsibility |
|---|---|---|
| `ai/groq_client.py` | 198 | Single Groq SDK choke point: lazy client, per-call timeout, **bounded** retries, typed error classes, JSON-mode + Pydantic validation. |
| `ai/schemas.py` | 217 | Typed, bounded Pydantic v2 schemas for every model output (Intent, Investigation, Diagnosis, Plan, ConfigProposal, VerifierReport). |
| `ai/prompts.py` | 96 | System prompts + `<untrusted_data>` wrapping + `PROMPT_VERSION`. |
| `ai/state.py` | 50 | `WorkflowState` TypedDict with an additive status-log reducer. |
| `ai/graph.py` | 293 | LangGraph `StateGraph` orchestration + conditional routing + final-answer composition. **No apply/deploy node.** |
| `ai/agents/intent_agent.py` | 47 | AR/EN classification, code-side language detection, clarification gating. |
| `ai/agents/investigation_agent.py` | 44 | Evidence summary; forces stale/missing disclosure; filters bogus evidence refs. |
| `ai/agents/diagnosis_agent.py` | 40 | Causes vs evidence; unbacked claims marked inference, categorical confidence only. |
| `ai/agents/planner_agent.py` | 27 | Plan for explicit change requests; Phase-4 rollback noted, not executed. |
| `ai/agents/config_agent.py` | 75 | **Hard-gated in code before/after the LLM**; router/known/change-only. |
| `ai/tools/_common.py` | 48 | Byte truncation, provenance record builder, staleness helper. |
| `ai/tools/topology_tools.py` | 41 | Device/link status from the persisted inventory. |
| `ai/tools/telemetry_tools.py` | 67 | Latest values + series summaries; source-filtered; missing ≠ invented. |
| `ai/tools/connectivity_tools.py` | 38 | **Reads stored ping/traceroute history only** (never probes). |
| `ai/tools/evidence_tools.py` | 125 | Anomalies, forecasts, incidents, collection errors + bounded `gather_evidence` with stable `ev-N` uids. |
| `ai/verification/config_validation.py` | 74 | Rejects shell/CLI-injection metachars & prohibited apply commands. |
| `ai/verification/policy_checks.py` | 90 | Target/in-scope/policy/lab-invariant checks (inconclusive when unprovable). |
| `ai/verification/verifier.py` | 73 | Deterministic aggregation → `passed` / `failed` / `inconclusive` only. |
| `ai/simulation/__init__.py` | 52 | `DigitalTwin` ABC + `UnavailableDigitalTwin` that returns `not_run`. |

### New — service, UI & tooling

* `services/ai_workflow_service.py` — creates the `AIRun`, drives the graph,
  persists all structured outputs, evidence rows and status transitions, and
  exposes `list_runs` / `get_run_detail` / `submit_human_review`.
* `pages/10_AI_Network_Assistant.py` — chat page (demo/live selector,
  button-gated run), evidence/investigation/diagnosis/plan/config diff,
  verifier verdict, Digital-Twin status, run history, human-review record.
* `tools/phase3_live_check.py` — optional, manual live Groq integration check
  (prints classifications only, never the key). **Not** part of `pytest`.

### Edited (additive)

* `config.py` — added Phase 3 `Settings` fields + env wiring; default model
  set to `openai/gpt-oss-20b` (see §6).
* `database/models.py` — added `AIRun`, `AIStatusTransition`, `AIEvidence` +
  status/request-type constants.
* `database/repository.py` — added AI run/evidence/transition/human-review
  helpers (imports `func`).
* `app.py` — added the page-10 sidebar link (🤖 AI Network Assistant).
* `requirements.txt` — added `groq`, `langgraph`, `pydantic`.
* `.env.example` — added Phase 3 placeholders (no secrets).
* `README.md` — Phases 1–3 rewrite, Phase 3 sections, config table, counts.

---

## 4. Database changes (purely additive)

Three new tables, created automatically by `init_db()` (`CREATE TABLE IF NOT
EXISTS`). No Phase 1/2 table is altered and raw `metric_samples` are untouched.

* **`ai_runs`** — one row per run: `run_uuid`, request text, `source_language`,
  `data_source`, `request_type`, `risk_level`, the JSON payloads (intent /
  investigation / diagnosis / plan / config proposal / diff / verifier report /
  digital-twin report), `final_answer`, `status`, `human_review_outcome`,
  `provider`, `model`, `prompt_version`, `failure_reason`, timestamps.
* **`ai_status_transitions`** — ordered audit trail (FK → `ai_runs`, ON DELETE
  CASCADE) with `step_order`, `status`, `detail`, `at`.
* **`ai_evidence`** — bounded, provenance-tagged evidence references (FK →
  `ai_runs`) with `evidence_uid`, `kind`, `tool_name`, `device_name`,
  `data_source`, `observed_at`, `unit`, `content`, `source_ref_json`, `stale`,
  `missing`.

**No secret is ever persisted** — verified by
`test_provider_secret_is_never_stored`.

---

## 5. Safety enforcement (spec §4) — how each rule is met

| Requirement | Where enforced (in code, not just prompts) |
|---|---|
| No production-network access | Tools read only the local SQLite/PostgreSQL data; no new sockets. `connectivity_tools` reads stored history only. |
| Never execute generated config / modify routes/ACLs/firewalls/interfaces | There is **no** deploy/apply node or method (`test_runner_has_no_deployment_method`, `test_runner_exposes_no_execution_primitive`); proposal JSON is the terminal artifact. |
| No arbitrary shell/SQL/Python exposed to the LLM | Evidence tools are fixed, allowlisted functions; `config_validation` rejects shell metacharacters & prohibited commands. |
| Treat logs / retrieved docs as untrusted | `prompts.untrusted_block(...)` wraps every retrieved payload; injected text stays data (`test_untrusted_block_marks_injection_as_data`, `test_injected_evidence_cannot_make_a_question_generate_config`). |
| Deterministic verification before "statically verified" | `verifier.verify()` runs before any pass label; blocked/unknown/prohibited → `failed`/`inconclusive`, never a false pass. |
| Never fabricate missing data / diagnosis / simulation | Missing metrics → explicit "no stored metrics"; unbacked causes flagged as inference; Digital Twin returns `not_run`. |
| Stop at proposal + human review | Change requests end at `awaiting_human_review`; `submit_human_review` records an outcome only. |

Routing guarantees (tested): an informational **question** stops after
diagnosis; an **ambiguous** change request short-circuits to clarification;
only an **explicit** change request reaches Planner → Config → Verifier.

---

## 6. Groq configuration & security

* `GROQ_API_KEY` is read **only** from the environment / git-ignored `.env`
  (via `python-dotenv`). It is never hard-coded, logged, placed in a prompt,
  written to the database, or committed.
* Verified the file is excluded from Git:
  `git check-ignore -v .env` → matched `.gitignore` line 16 (`.env`). Only a
  placeholder (`GROQ_API_KEY=`) is in `.env.example`.
* Model is configurable (`GROQ_MODEL`); per-call `GROQ_TIMEOUT` and **bounded**
  `GROQ_MAX_RETRIES` (capped at 5). Rate limits / timeouts are retried then
  surfaced as typed errors; malformed JSON and schema violations fail safe.
* **Model note discovered during live testing:** the previous default
  `llama-3.1-8b-instant` returned **404 NotFound** on this Groq account
  (decommissioned / not entitled). A live catalog listing showed the working
  text models, and the default was switched to **`openai/gpt-oss-20b`**
  (also `qwen/qwen3.8-27b`, `allam-2-7b`, `openai/gpt-oss-120b` work).
  `.env.example` now explains how to list available models.

> ⚠️ **Secret handling:** the Groq key was pasted into the chat during kickoff.
> It was written **only** to the local git-ignored `.env` and is **not**
> reproduced in any tracked file, log, prompt, test or this report. Because it
> was shared in plaintext, **rotate/revoke it in the Groq console.**

---

## 7. Test results (actual)

All Groq calls are **mocked** (injected fake client), so the suite runs fully
offline with `GROQ_API_KEY=""` and needs no key or network.

```
$ .\.venv\Scripts\python.exe -m pytest
201 passed, 13 warnings in 28.16s
```

Phase 3 adds **57 tests** across 7 files:

| File | Tests | Focus |
|---|---|---|
| `test_groq_client.py` | 10 | Missing key, happy path, bounded retries on rate-limit/timeout, malformed JSON, schema violation, error classification. |
| `test_intent_agent.py` | 4 | EN/AR classification, code-side language detection, ambiguous-change clarification. |
| `test_workflow_routing.py` | 10 | No deploy/apply methods, question never reaches config, change reaches verifier + Digital Twin (`not_run`), ambiguous→clarify, missing-key→`failed_safe`. |
| `test_evidence_tools.py` | 8 | Source separation, stale/missing not invented, read-only connectivity, bounded output + stable uids. |
| `test_verifier.py` | 15 | Shell/prohibited rejection, unknown target fail, blocked→inconclusive, stated policy inconclusive, clean pass, determinism. |
| `test_prompt_injection.py` | 4 | Injection is data; poisoned malicious proposal fails verifier; `lab/configs/R2/frr.conf` bytes unchanged; no execution primitive. |
| `test_ai_persistence.py` | 6 | Full audit trail, ordered transitions, evidence refs, demo/live separation, review records outcome only, secret never stored. |

Preserved: all **144** Phase 1 + Phase 2 tests pass unchanged.

### UI smoke test

```
$ .\.venv\Scripts\python.exe tests/ui_smoke_check.py     # demo mode, throwaway DB
[ OK ] app.py
[ OK ] pages\10_AI_Network_Assistant.py (warnings=1, infos=1)
[ OK ] pages\1_..9  (all render)
[ OK ] pages/8_Anomalies.py: 'Run detection' executed without exceptions
[ OK ] pages/9_Forecasting.py: 'Run forecast comparison' executed without exceptions
demo banner shown: OK
REAL_EXIT=0
```

(The bare-mode "missing ScriptRunContext" lines are Streamlit's harmless stderr
notices, not failures — the process exit code is 0.)

### Live Groq integration (manual, network + real key)

```
$ .\.venv\Scripts\python.exe tools/phase3_live_check.py
model=openai/gpt-oss-20b timeout=30s retries=2 available=True
  input="Why can't PC1 reach PC2?"      -> type=question       lang=en  targets=['PC1','PC2'] risk=low
  input='اعزل شبكة HR عن الخوادم'        -> type=change_request lang=ar  targets=['HR network','servers'] risk=high
LIVE Groq integration OK.
```

---

## 8. Limitations & honest status

1. **Digital Twin does not simulate.** `UnavailableDigitalTwin` returns
   `not_run` with a reason — no network-os/Batfish/containerlab simulator is
   wired in Phase 3, and none is faked.
2. **No deployment / post-change verification / rollback** — intentionally
   Phase 4. Phase 3 stops at a human-reviewable proposal.
3. **Verifier is static only.** `passed` means the proposal is well-formed,
   targets a known configurable device, and contains no prohibited content;
   it does **not** prove live behaviour. Semantic policy constraints are
   reported `inconclusive` rather than passed.
4. **Live-lab paths were not exercised on this host.** As in Phases 1/2,
   Docker/Containerlab is unavailable on the Windows dev machine, so the
   assistant's reasoning was validated against **demo-mode** data and mocked
   providers. Real lab telemetry feeding the assistant still needs an
   end-to-end run on a Docker host.
5. **Model availability varies.** Groq's free catalog changes; a model that
   404s must be swapped via `GROQ_MODEL`. The suite is immune (mocked).
6. **Reasoning latency.** `openai/gpt-oss-20b` is a reasoning model; a full
   multi-node run issues several sequential calls and can take tens of
   seconds per request.
7. **Language detection** for Arabic is script-based in code; mixed-script or
   romanized-Arabic requests fall back to the model's `source_language` hint.

### Unverified live-lab checklist (to run on a Docker/Containerlab host)

1. Deploy the lab and collect ≥2 cycles so real (not demo) evidence exists.
2. Page 10 in **live** mode: ask "Why can't PC1 reach PC2?" → confirm it
   investigates live topology/metrics and stops at diagnosis (no config).
3. Ask an explicit change (e.g. "Isolate R2 from HR") → confirm a proposal +
   deterministic verifier verdict appear and status is `awaiting_human_review`.
4. Feed an injected directive inside a device log → confirm it is treated as
   data and no config file under `lab/configs/` changes.
5. Confirm `GROQ_API_KEY` is never shown in the UI, logs, or `ai_runs` rows.

---

## 9. Exact commands to run

```powershell
# 1) Activate the environment
.\.venv\Scripts\Activate.ps1

# 2) Install dependencies (incl. Phase 3: groq, langgraph, pydantic)
pip install -r requirements.txt

# 3) Configure Groq securely (key stays local & git-ignored)
copy .env.example .env      # edit GROQ_API_KEY (and GROQ_MODEL if needed)

# 4a) Demo mode (no Docker) — recommended for first look
$env:NETPILOT_DEMO_MODE="true"; streamlit run app.py

# 4b) Live lab mode (Docker + Containerlab host)
streamlit run app.py

# 5) Full offline test suite (mocked Groq; no key required)
$env:GROQ_API_KEY=""; pytest

# 6) Headless UI smoke test
$env:GROQ_API_KEY=""; $env:NETPILOT_DEMO_MODE="true"; python tests\ui_smoke_check.py

# 7) Optional LIVE Groq integration check (needs real key + network)
python tools\phase3_live_check.py
```

Then open **🤖 AI Network Assistant** in the sidebar.

---

## 10. Phase 4 hand-off (not implemented here)

Wiring a real Digital Twin / simulator for pre-change verification, plus an
approval-gated deployment path with automated rollback and post-change
validation. Everything needed to build safely on top of Phase 3 is already in
place: persisted intents, evidence references, verifier reports and a complete
human-review audit trail.
