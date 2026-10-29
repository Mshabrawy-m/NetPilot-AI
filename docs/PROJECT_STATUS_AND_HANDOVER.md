# NetPilot AI — Current Project Status and Owner Handover

**Audit date:** 2026-10-29  
**Base revision inspected:** `main` at `c4461ff` (`origin/main` matched before edits)  
**Scope:** code, source-controlled requirements/reports, ignored acceptance artifacts (names/status only), offline tests, headless UI checks, and safe provider preflight. Historical reports remain unchanged.

This document is the current status supplement. Earlier delivery, gate, and
closure reports are evidence from the dates and revisions printed inside them;
they are not silently treated as current verification. The roadmap's §11
delivery log is used to distinguish the selected v2 track from optional menu
items. The detailed `docs/FEATURE_ACCEPTANCE_MATRIX.md` remains a historical
snapshot; rows it labels E4 missing and D6 partial are superseded by the
additive 2026-10-29 findings below.

## Executive verdict

The codebase implements the planned Streamlit network-operations product across
its core modules: telemetry storage and source provenance; analysis and model
evaluation; the Phase 3 LangGraph assistant; the unified AI Lab; graph
intelligence; Digital Twin structural analysis and persistent snapshot
bindings; gated change services; Ops Center; API, Docker, and CI assets.

**Phase 3 verdict: implemented and offline-verified, but not fully externally
verified.** Its mocked-provider orchestration, schemas, routing, read-only tools,
deterministic verifier, audit persistence, and safety boundaries are covered by
the test suite. On this audit pass a provider request was attempted using the
existing local configuration; the configured Groq model reported available,
but the request ended with `GroqTimeout: Groq connection error`. No key or raw
provider response was printed. Thus current live-provider EN/AR behavior is not
verified, and live-lab acceptance L-1…L-6 is blocked because Docker,
Containerlab, and a configured Prometheus source are unavailable.

No production-network operation occurred. `change_loop_enabled` and
`lab_execution_enabled` both read `False`. The Digital Twin remains a
structural, read-only model; the Phase 3 AI graph correctly reports `not_run`
for a real isolated routing simulation.

## Phase and capability status

Statuses use the requested vocabulary. “Verified complete” means the local
implementation and its available deterministic acceptance tests pass; it does
not imply live infrastructure acceptance.

| Phase / capability | Status | Implementation and verification | Remaining evidence / work |
|---|---|---|---|
| Phase 1 — topology, inventory, collection, history, status | IMPLEMENTED BUT NOT FULLY VERIFIED | `network/`, `database/`, `services/`, pages 1–6; current full offline suite includes collector, scheduler, graph, provenance and repository tests | Deploy isolated Containerlab and collect real live telemetry; not available here |
| Phase 2 — quality, derived metrics, EDA, anomalies, forecasting | VERIFIED COMPLETE (offline implementation) | `analytics/`, `services/anomaly_service.py`, `services/forecast_service.py`; chronological splits and real computed metrics; AI Lab and legacy stored-data pages | Real-network data and observed forecast accuracy require a live source; not claimed here |
| Phase 3 — multi-agent assistant and safe operations | IMPLEMENTED BUT NOT FULLY VERIFIED | `ai/graph.py`, agents/tools, provider adapter, verifier, `ai_workflow_service`; phase-specific routing, schema, safety, and persistence tests pass in the full suite | Live Groq evaluation is unverified after connection timeout; live lab L-1…L-6 blocked |
| Phase 4 — approval, structural twin, gated apply/verify/rollback | IMPLEMENTED BUT NOT FULLY VERIFIED | `services/digital_twin.py`, `digital_twin_registry.py`, change services and executor; D1–D4 reports and deterministic tests; both execution flags are false | Live apply/verify/rollback and routing behavior require an isolated Docker/Containerlab host; AI Phase 3 twin adapter stays `not_run` intentionally |
| Phase 5 — AI Lab and unified analytics | VERIFIED COMPLETE (offline implementation) | `analytics/lab_dataset.py`, `lab_sources.py`, `lab_simulator.py`, `services/lab_tasks.py` and `lab_experiment.py`; deterministic tests for imported, stored, simulated and validated source contracts | Prometheus is not configured in this environment. A live dataset must be verified from its actual fetch provenance |
| v2 Ops Center, graph, alerts, auth and API | IMPLEMENTED BUT NOT FULLY VERIFIED | `pages/11_Ops_Center.py`, `analytics/graph.py`, `services/impact_service.py`, alert/auth services and `api/`; corresponding tests pass | Live authenticated browser, PostgreSQL integration and HTTP/WebSocket TestClient checks were not verified on this pass |
| v2 containerization, CI, telemetry observability | IMPLEMENTED BUT NOT FULLY VERIFIED | Docker/Compose, GitHub Actions workflow, `/metrics` JSON/Prometheus exposition exist | Docker/Containerlab unavailable; current remote CI status was not queried; structured tracing is not implemented |

## Phase 3 acceptance matrix

| Requirement | Expected behavior | Implementation evidence | Verification performed | Status | Remaining work |
|---|---|---|---|---|---|
| Request interpretation | Arabic/English question, investigation, or change request; clarify ambiguity | `ai/agents/intent_agent.py`, `ai/schemas.py` | Mocked intent and routing tests in full offline run | VERIFIED COMPLETE (offline) | Live provider EN/AR response not verified on this pass |
| Orchestration | Explicit LangGraph state, conditional routing, bounded workflow, no deploy node | `ai/graph.py`, `ai/state.py` | Workflow routing and safety tests in full offline run | VERIFIED COMPLETE | None in tested scope |
| Evidence tools | Read-only topology, telemetry, anomaly/connectivity evidence with provenance and bounds | `ai/tools/**`, repository, `services/ai_workflow_service.py` | Evidence-tool tests, source separation and persistence tests | VERIFIED COMPLETE (offline) | Real telemetry evidence blocked by absent lab |
| Investigation and diagnosis | Separate facts, hypotheses, confidence, missing/stale evidence | AI investigation/diagnosis agents and schemas | Grounding, abstention and prompt-injection tests | VERIFIED COMPLETE (offline) | Live groundedness evaluation not run |
| Change plan/config proposal | Only explicit change requests reach proposal; no active config writes | planner/config agents, validators | Workflow routing, verifier and safety tests | VERIFIED COMPLETE (offline) | Live lab proposal journey blocked |
| Independent verifier | Deterministic passed/failed/inconclusive checks | `ai/verification/` | Verifier suite and safety scan included in full run | VERIFIED COMPLETE (offline) | Static verification is not live network proof |
| Digital Twin boundary | Report `not_run` if no isolated simulation evidence | `ai/simulation/UnavailableDigitalTwin`, graph finalization | Workflow test asserts `not_run`; separate structural engine tests exist | VERIFIED COMPLETE (boundary) | No real isolated route simulation available |
| Persistence and human review | Persist requests, evidence, transitions, verifier and review outcome; never store provider key | `AIRun`, `AIEvidence`, transitions and service/repository | Persistence and secret non-storage tests | VERIFIED COMPLETE (offline) | Real authenticated-browser journey remains unverified |
| Provider error behavior | Missing key, timeout, rate limit, malformed response fail safely | `ai/groq_client.py` | Deterministic fake-client tests pass; safe live attempt returned connection timeout | IMPLEMENTED BUT NOT FULLY VERIFIED | Repeat from a network-enabled host; no credential change was made |
| Live isolated-lab criteria L-1…L-6 | Real collection, live evidence, source separation, injection safety, no execution, no secret exposure | `tools/phase3_lab_validation.py`, runbook, safety scans | Code/tests only; no live harness executed here | BLOCKED BY EXTERNAL INFRASTRUCTURE | Docker, Containerlab, Linux lab host and real telemetry required |
| Live provider quality evaluation | Evaluate actual provider output separately from mocked/synthetic evals | Offline evaluation explicitly marks live status `not_run` | Live request attempted, but connection failed before response | BLOCKED BY EXTERNAL NETWORK | Run provider-eval harness from connected host; never treat fake-client scores as live evidence |

## Planned feature inventory (selected roadmap track)

| Area | Current status | Important remaining work / honest limits |
|---|---|---|
| Topology and graph | VERIFIED COMPLETE for stored graph algorithms; the audit adds a per-hop view | Reachability uses documented links, not a probe or router FIB; link operational state is displayed as evidence |
| Alerts and Ops Center | VERIFIED COMPLETE by service and offline UI-action tests | Notification transports and scheduled escalation are roadmap menu items, not selected scope |
| Phase 4 approval/change services | IMPLEMENTED BUT NOT FULLY VERIFIED | Apply/verify/rollback logic is isolated and flag-gated; actual device execution remains disabled and unverified |
| Digital Twin | PARTIALLY IMPLEMENTED at the product-wide routing-simulation scope | Structural topology what-if engine, integrity checks and append-only snapshot registry exist; no routing daemon simulation and no AI graph integration. The AI workflow's `not_run` is intentional |
| Forecasting and analysis | VERIFIED COMPLETE for supported local data and shared task contract | Live measurements are unavailable; forecasts do not establish future accuracy; model errors and insufficient series remain explicit |
| Agentic Operations | IMPLEMENTED BUT NOT FULLY VERIFIED | Actual registered tools now include dataset inspection, statistics, anomaly, forecast, stored topology context, structural impact, and dataset-scoped experiment history. LLM plan/runtime requires reachable Groq. Operational anomaly-event tables are not directly queried by this agent |
| AI quality evaluation | VERIFIED COMPLETE for deterministic offline fixtures | Live provider evaluation not run; no score is inferred from mocked providers |
| RAG / NL query | VERIFIED COMPLETE for implemented local deterministic behavior | TF-IDF over local Markdown and deterministic read-model queries; no embeddings/vector service or unrestricted SQL |
| Authentication/RBAC | IMPLEMENTED, offline tested, opt-in | `auth_enabled=False` locally; no real browser auth journey verified this pass |
| REST/WebSocket API | IMPLEMENTED, read-model tested | Pure read-model tests pass; HTTP/WebSocket integration tests are not counted as passed in this handover |
| PostgreSQL | IMPLEMENTED as a SQLAlchemy URL path | No PostgreSQL server/test instance available; SQLite-only evidence |
| Container/Compose stack | Files and CI build path exist | No Docker Engine here, so compose runtime is unverified |
| CI | Workflow source exists | Remote CI was not queried at current revision; historical workflow reports are not current evidence |
| Full lineage UI, telemetry traces, notifications, scheduled reports, retention, drift, resolution memory, streaming | NOT IMPLEMENTED or PARTIAL (optional roadmap items unless separately selected) | See roadmap §§2–3 and the historical acceptance matrix; do not present these as delivered |

## Unified data and model behavior

`analytics.lab_dataset.LabDataset` is the common validated representation for
simulator data, stored telemetry, browser CSV uploads, allowlisted historical
files, and fetched Prometheus ranges. Each loader sets source identity,
provenance, timestamps, units and quality; a configured endpoint alone is not
live evidence. `services/lab_tasks.py` calls the existing statistics,
anomaly, classification and forecasting implementations.

Forecasting includes naive persistence, seasonal-naive, damped Holt,
Holt-Winters and ridge-lag models where compatible. It uses chronological
training/holdout splits and actual held-out values; a future path is separate
from holdout predictions. Statistical analysis reports computed summary,
quality, trend, autocorrelation and compatible association results. Supervised
classification and anomaly scores require source labels; otherwise anomaly
metrics explicitly remain unsupervised. Simulator labels are synthetic
ground-truth labels, never real event truth.

## Architecture and current UI organization

The primary data path is:

```text
stored / upload / allowlisted history / Prometheus fetch / simulator
                           ↓
             validated analytics.lab_dataset.LabDataset
                           ↓
           analytics + services/lab_tasks + experiment persistence
              ↙                                  ↘
     AI Lab UI and exports             Agentic Operations tools
```

Navigation is **Overview**, **Network operations** (topology, devices,
connectivity, Metrics & history, anomalies, Ops Center), **AI lab** (unified
source selection and experiments), **Agentic Operations** (Network Assistant),
and **System status**. The older stored-only Data Analysis and Forecasting page
modules remain available in the repository but no longer duplicate AI Lab in
navigation. The Ops Center owns structural Digital Twin reports and human
change review. There is no user-editable settings page.

## Reproduction / owner handover

Windows PowerShell, from the project root:

```powershell
.\.venv\Scripts\Activate.ps1
$env:NETPILOT_DEMO_MODE = 'true'
streamlit run app.py
```

Use **AI Lab · data & forecasting** to build a deterministic simulated dataset,
inspect its source/fingerprint/quality, and run a statistics, anomaly, or
forecast experiment. The simulator can label injected events for supervised
detection evaluation. For imported experiments, choose CSV and inspect the
mapping before loading. Exported history contains the actual run metadata and
results.

To demonstrate Agentic Operations, load a dataset in AI Lab, then open
**Agentic Operations → Network assistant & tools**. Enter a request such as
“Run statistics and anomaly detection on this dataset, then forecast the
latency series.” With a reachable Groq provider configured through the existing
local/host secret mechanism, the UI shows the validated plan, registered tool
outputs, actual AI Lab experiment IDs, warnings, report and saved run history.
The automated end-to-end tool test mocks only the planner response; statistics,
anomaly, forecasting, topology impact and experiment-history reads invoke the
real application services over deterministic SQLite/dataset fixtures.

## Verification evidence for this audit

Commands run (all from repository root):

```powershell
python -m pytest -q --ignore=tests/test_api.py --basetemp=.pytest-workspace-temp
python -m pytest -q tests/test_api.py -k readmodel --basetemp=.pytest-workspace-temp
python -m pytest -q tests/test_topology_path.py tests/test_agentic_analytics.py --basetemp=.pytest-workspace-temp
python -m py_compile services/topology_path.py services/agentic_analytics.py pages/2_Topology.py pages/10_AI_Network_Assistant.py
python tools/phase3_live_check.py
```

Baseline before audit edits: **969 passed**, 13 existing `datetime.utcnow()`
deprecation warnings, full offline suite in 167.25 seconds. This run excluded
`tests/test_api.py`. The post-change collection contains **977 tests**. The
post-change offline suite reached 100% with no failures and the same 13
deprecation warnings; the tool output did not return its final pytest summary
line, so this is recorded as a successful completed run rather than asserting
a reported pass count. The four pure API read-model tests passed separately,
as did the focused path/agent suite (**12 passed**). Python compilation,
`git diff --check`, the repository safety scan, and the secret scan passed.
The browserless Streamlit/AppTest smoke attempt stalled without a page result;
UI rendering is **not verified**. Full API HTTP/WebSocket integration tests
were excluded and remain unverified.

Provider preflight result: the configured SDK client was constructed, followed
by `GroqTimeout: Groq connection error` on the first request. The environment
has no configured Prometheus URL and no Docker or Containerlab executable.
These are external verification blockers, not mocked passes.

The agent tool planner is bounded to three distinct calls and validates a
strict per-tool argument schema. Analysis uses the existing dataset caps and
forecast horizon limits; however, there is no separate hard wall-clock
cancellation boundary around synchronous local analytics tools. Keep inputs
bounded and do not describe the system as a general-purpose autonomous
network operator.

### Security and configuration notes

The local `.env` is ignored by Git; its raw contents were not read or printed.
The existing settings loader read configuration in-process, and only
non-secret booleans were inspected: Groq key configured, auth disabled,
change loop disabled, lab execution disabled, Prometheus not configured. An
ignored historical Phase 3 report records prior Groq credential exposure and
could not establish rotation. Current revocation status is therefore
**UNVERIFIED**: the connection timeout is not proof of revocation. This audit
did not rotate or modify credentials. The owner should verify/revoke any
previously exposed provider credential in the provider console and update the
host secret out of band; never paste it into chat or repository files.

The test run generated/updated ignored artifacts such as the temporary pytest
directory, caches, and the UI smoke database. Existing ignored evidence
directories and reports were preserved. No destructive Git command or
production-network operation was run.
