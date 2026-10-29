# NetPilot AI — Phases 1–5: Network Foundation, Analytics, AI Assistant & Gated Change Loop

A working virtual network lab (Containerlab + FRRouting), real periodic
network-data collection, a replaceable storage layer (SQLite by default,
PostgreSQL optional), a Streamlit dashboard for inspecting topology,
device/interface status, connectivity tests, and metric history — Phase 2
analytics: data-quality reporting, derived throughput from cumulative
counters, EDA (Data Analysis), explainable anomaly detection (rolling MAD /
z-score, EWMA, CUSUM, seasonal residual) and chronological forecast
evaluation with holdout-calibrated uncertainty bands, scale-free accuracy
metrics and a model comparison — and Phase 3: an evidence-grounded,
Arabic/English **AI Network Assistant** (Groq + LangGraph multi-agent
workflow) that investigates, diagnoses, plans and *proposes* configuration
for **human review**, gated by an independent deterministic verifier. Phase 4
adds a **gated, lab-only change loop** (apply → post-change verify → rollback,
both execution flags off by default), and Phase 5 an **AI Lab** that runs the
same anomaly / forecast / classification / statistics pipeline on simulated,
stored, uploaded or allowlisted telemetry. Its Agentic Operations workspace
plans and invokes registered read-only statistics, anomaly and forecasting
tools over the active validated dataset and persists the execution evidence.

> **Safety boundary.** The assistant **only proposes**. It never connects to a
> production network and never deploys a configuration by itself. The Phase 4
> apply/verify/rollback path is present but **disabled by default** (`change_loop`
> and `lab_execution` both off), lab-only, and requires an authenticated,
> non-self human approval. It has **no live-lab evidence** on this host
> (no Docker/Containerlab); see §14 and `NETPILOT_GATE3_ISOLATED_LAB_RUNBOOK.md`.

---

## 1. Project structure

```
netpilot-ai/
├── app.py                      # Streamlit entry point
├── pages/
│   ├── 1_Overview.py           # device counts, network status, last collection
│   ├── 2_Topology.py           # interactive graph generated from lab data
│   ├── 3_Devices.py            # device/interface tables + per-device detail
│   ├── 4_Connectivity_Tests.py # allowlisted ping / traceroute
│   ├── 5_Metrics_History.py    # time-series charts from stored measurements
│   ├── 6_System_Status.py      # DB, lab, scheduler, runs, incidents, reset
│   ├── 7_Data_Analysis.py      # retained legacy stored-data EDA page
│   ├── 8_Anomalies.py          # Phase 2 anomaly events + explanations + context
│   ├── 9_Forecasting.py        # retained legacy stored-data forecast page
│   ├── 10_AI_Network_Assistant.py  # Phase 3 workflow + Agentic Operations tools
│   ├── 11_Ops_Center.py        # v2: alerting, graph risk, insights, change loop, access
│   └── 12_AI_Lab.py            # unified cross-source analysis, forecasts, history
├── ai/                         # Phase 3 multi-agent core (LangGraph workflow)
│   ├── groq_client.py          # single Groq SDK choke point (timeouts, retries)
│   ├── schemas.py              # typed Pydantic schemas for every model output
│   ├── prompts.py              # system prompts + <untrusted_data> wrapping
│   ├── state.py                # typed workflow state (TypedDict)
│   ├── graph.py                # LangGraph orchestration + routing (NO deploy node)
│   ├── agents/                 # intent, investigation, diagnosis, planner, config
│   ├── tools/                  # read-only evidence tools (topology/telemetry/…)
│   ├── verification/           # independent deterministic verifier + policy checks
│   └── simulation/             # Digital Twin workflow boundary (reports not_run;
│                               #   read-only engine is services/digital_twin.py)
├── analytics/                  # Phase 2 computation core (no UI, no SQL writes)
│   ├── time_series.py          # frame loading, dedupe, resample, labelled gaps
│   ├── data_quality.py         # quality report + readiness gates per series
│   ├── lab_dataset.py          # Phase 5: one validated telemetry contract
│   │                           #   (provenance, lineage, splits, train-only prep)
│   ├── lab_simulator.py        # Phase 5: deterministic simulator + ground-truth labels
│   ├── lab_sources.py          # Phase 5: CSV upload / local file / stored adapters
│   ├── lab_classification.py   # Phase 5: label-gated fault classification
│   ├── derived_metrics.py      # counter → rate/delta with reset & gap handling
│   ├── eda.py                  # summaries, distributions, gated correlations
│   ├── anomaly_detection.py    # MAD / z-score / EWMA / CUSUM / seasonal residual (+ IF)
│   ├── forecasting.py          # naive, seasonal naive, damped Holt & Holt-Winters,
│   │                           # ridge-lag, holdout-calibrated bands
│   └── evaluation.py           # chronological splits, MAE/RMSE/R² + MAPE/sMAPE/
│                               #   MASE/skill, detection F1, leakage check
├── config.py                   # env-driven settings (UTC timestamps)
├── network/
│   ├── containerlab_manager.py # containerlab/docker CLI wrapper (allowlisted argv)
│   ├── device_inventory.py     # inventory parsed from the topology file + allowlist
│   ├── connectivity.py         # ping/traceroute execution + platform parsers
│   └── metrics_collector.py    # one collection cycle: probes → database
├── database/
│   ├── connection.py           # engine/session management (SQLite | PostgreSQL)
│   ├── models.py               # devices, interfaces, links, metric_samples,
│   │                           # connectivity_tests, collection_runs, incidents
│   │                           # + Phase 2: anomaly_events, forecast_runs,
│   │                           #   forecast_predictions (additive tables)
│   │                           # + Phase 3: ai_runs, ai_status_transitions,
│   │                           #   ai_evidence (additive audit tables)
│   └── repository.py           # all data access (kept out of the UI)
├── services/
│   ├── topology_service.py     # topology graph + documented status rules
│   ├── collection_service.py   # background scheduler (overlap-safe)
│   ├── demo_data.py            # clearly labelled synthetic fixtures (demo mode)
│   ├── anomaly_service.py      # detect → group/dedupe → persist + batch scan,
│   │                           #   event summary, threshold calibration
│   ├── forecast_service.py     # split → compare → persist → ensemble, crossing
│   │                           #   warnings, backfill actuals → realized coverage
│   ├── lab_tasks.py            # Phase 5: the four experiment tasks (bounded, gated)
│   ├── lab_experiment.py       # Phase 5: runner + history + CSV/MD/HTML exports
│   ├── agentic_analytics.py    # bounded planner + registered analysis tools
│   ├── prometheus_source.py    # Phase 5: optional read-only live source (off by default)
│   ├── ai_workflow_service.py  # Phase 3: drive a run + persist its audit trail
│   └── app_state.py            # shared Streamlit page bootstrap
├── lab/
│   ├── topology.clab.yml       # Containerlab topology (single source of truth)
│   └── configs/{R1,R2,R3}/     # FRR frr.conf + daemons per router
├── tests/                      # pytest suite + headless UI smoke check
├── tools/                      # one-off utilities (incl. optional live Groq check,
│                               #   secret_scan_check.py used by CI)
├── docs/                       # design specs, roadmap and delivery reports
│   ├── AGENTIC_ANALYTICS.md    # shared data path, tool limits and verification
│   ├── NetPilot_Phase_1_Network_Foundation_EN.md
│   ├── NetPilot_Phase_2_Data_Analysis_Forecasting_EN.md
│   ├── NetPilot_Phase_3_Multi_Agent_AI_Safe_Operations_EN.md
│   ├── NetPilot_AI_Advanced_Roadmap.md   # v2 advanced-capabilities roadmap
│   ├── NetPilot_Forecasting_Anomaly_Enhancement_v2_Report.md
│   │                           # forecasting/anomaly v2 pass (metrics, HW,
│   │                           #   detectors, bands, scan, calibration)
│   ├── Phase2_Delivery_Report.md
│   ├── Phase3_Delivery_Report.md
│   ├── NetPilot_Phase_5_AI_Lab_Guide_EN.md   # Phase 5 AI Lab user/ops guide
│   └── Phase5_AI_Lab_Delivery_Report.md      # Phase 5 evidence + acceptance mapping
├── .streamlit/config.toml      # theme + file watcher off (never host/port/secrets)
├── .env.example                # configuration template (no secrets)
├── requirements.txt            # also what Streamlit Community Cloud installs
├── packages.txt                # apt packages for Cloud (iputils-ping)
├── .gitattributes              # line-ending + GitHub language-stat filters
└── pytest.ini
```

Everything the dashboard reads is generated by the code above; `docs/` holds the
written requirements and the reports that describe what was built, and is also
the corpus the offline RAG retrieval indexes (`services/rag.py` globs `*.md`
and `docs/*.md`). Verification/acceptance paperwork stays local by policy and
is listed in `.gitignore` rather than published.

**v2 platform additions** (advanced capabilities roadmap, Slices 1–8) live
alongside the Phase 1–3 tree, additively — nothing above was rewritten:

```
├── app.py / pages/…            # + Ops Center (11) new page
├── analytics/                  # + graph.py, ml_anomaly.py, rca.py, backtest.py
├── services/                   # + alert_service, auth, change_service, rag,
│                               #   nlquery, telemetry (in-process metrics),
│                               #   impact_service, ops_center_actions,
│                               #   digital_twin, digital_twin_registry
├── api/                        # opt-in read-only FastAPI REST + /ws/topology
│   ├── app.py                  #   (standalone; no Streamlit import)
│   └── readmodels.py           #   thin JSON read-models over the repository
├── Dockerfile                  # one image → dashboard OR api (NETPILOT_SERVICE)
├── docker/entrypoint.sh        #   selects streamlit vs uvicorn; passthrough
├── docker-compose.yml          # db(Postgres) + dashboard + api (+ lab profile)
├── .dockerignore               # keeps .env / data / venv out of the image
├── .github/workflows/ci.yml    # secret gate + pytest matrix + UI smoke + docker
└── docs/                       # + D1-D4 Digital Twin reports, FEATURE_* audit
                                #   reports, FINAL_PROJECT_CLOSURE_REPORT.md
```

Justified deviations from the suggested layout: `services/app_state.py` and
`services/demo_data.py` were added (shared UI bootstrap and a strictly
separated demo-fixture generator); `pyvis` was replaced by a Plotly graph
already required for the charts — the topology is still generated from lab
data, not from a static image.

---

## 2. Prerequisites

| Requirement | Needed for | Notes |
|---|---|---|
| Python 3.10+ | everything | developed/tested on 3.11/3.12 |
| Docker + Containerlab | **live lab mode** | Linux, or Windows via WSL2 + Docker Desktop |
| Ubuntu 22.04/24.04 LTS VM | **Gate 3 / Gate D live validation** | disposable, isolated host — see the lab runbook |
| PostgreSQL (optional) | production-grade storage | otherwise the built-in SQLite is used |

If Docker/Containerlab are unavailable on your machine, the dashboard still
runs fully in **clearly labelled demo mode** (see §5) — the spec explicitly
allows this, and demo data is stored in a *separate* database file and tagged
`data_source='demo'` everywhere, so it can never masquerade as a real
measurement.

> **Running the live validation suites.** The real-environment harnesses
> (`tools/phase3_lab_validation.py` for the Gate 3 items L-1…L-6 and
> `tools/phase4_change_loop_validation.py` for the Gate D
> apply→verify→rollback loop) need Docker Engine + Containerlab and are
> executed on an isolated Ubuntu VM, never on a workstation and never against
> a production network. Both fail closed: a missing prerequisite is recorded
> `BLOCKED` (exit code 2) and is never substituted with demo data. The exact
> prerequisites, isolation checklist, commands, negative-path tests and
> teardown steps are in `NETPILOT_GATE3_ISOLATED_LAB_RUNBOOK.md` (local-only
> paperwork — kept on disk, not published).

### Installing Containerlab (on a Docker-supported host)

```bash
# Docker (Linux example)
curl -fsSL https://get.docker.com | sh

# Containerlab (official installer)
curl -sL https://get.containerlab.io | bash
```

On Windows, run Containerlab inside WSL2 with Docker Desktop's WSL
integration enabled, and keep the repository on a path accessible there.

---

## 3. Setup

```powershell
# From the repository root (Windows PowerShell shown; use the equivalent
# activate command on Linux/macOS)
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Optional: configuration
copy .env.example .env   # then edit values; .env is git-ignored
```

PostgreSQL (optional): set `DATABASE_URL` in `.env` and install the driver:

```bash
pip install psycopg2-binary
# DATABASE_URL=postgresql+psycopg2://user:pass@localhost:5432/netpilot
```

**Never commit real credentials.** Use a secret manager or environment for
production `DATABASE_URL` values; `.env`, `*.db`, and key files are covered
by `.gitignore`.

---

## 4. Running the virtual lab (requires Docker + Containerlab)

All commands below are run from the `lab/` directory (or pass `-t
lab/topology.clab.yml`). They only affect the isolated NetPilot lab —
nothing touches a real/production network. The PC nodes use
`network-mode: none`, so they have no route to any external network.

```bash
# Deploy
containerlab deploy -t lab/topology.clab.yml

# Verify / inspect state (deploy check)
containerlab inspect -g lab/topology.clab.yml
docker ps --filter "name=R1" --filter "name=PC1"

# Manual connectivity checks inside the lab (allowed destinations only)
docker exec R2 ping -c 3 10.1.1.1          # router loopback via OSPF
docker exec PC1 ping -c 3 10.0.32.32       # PC1 -> PC2 across the whole lab
docker exec R1 vtysh -c "show ip route"    # FRR routing table

# Stop / remove
containerlab destroy -t lab/topology.clab.yml

# Redeploy after config changes
containerlab deploy -t lab/topology.clab.yml --reconfigure
```

**Lab success checks** (all four must pass): `containerlab inspect` shows
5 nodes in state `running`; `R2 ping 10.1.1.1` succeeds; `PC1 ping
10.0.32.32` succeeds; `show ip route` on R1 lists OSPF routes for
`10.0.21.0/24` and `10.0.32.0/24`.

### Lab design

```
              R1                Loopbacks: R1 10.1.1.1, R2 10.1.1.2, R3 10.1.1.3
             /  \               R1–R2: 10.0.12.0/24   (R1 eth1 / R2 eth1)
           R2    R3             R1–R3: 10.0.13.0/24   (R1 eth2 / R3 eth1)
           |      |             R2–PC1: 10.0.21.0/24  (R2 eth2 / PC1 eth1)
          PC1    PC2            R3–PC2: 10.0.32.0/24  (R3 eth2 / PC2 eth1)
                                Routing: OSPF area 0 (FRR ospfd); PCs use
                                their router as default gateway.
```

The topology file is the **single source of truth**: the dashboard inventory,
allowlist, and demo fixtures are all parsed from it (`network/device_inventory.py`).

---

## 5. Running the dashboard

### Live lab mode (Docker + Containerlab host)

```bash
streamlit run app.py
```

Use the sidebar: **▶️ Start collector** (periodic, interval configured via
`NETPILOT_COLLECT_INTERVAL`) or **🔄 Collect now** (manual refresh).

### Demo mode (any machine, no lab)

```bash
# PowerShell
$env:NETPILOT_DEMO_MODE="true"; streamlit run app.py
# bash
NETPILOT_DEMO_MODE=true streamlit run app.py
```

Demo mode uses a **separate database** (`data/netpilot_demo.db`), seeds
deterministic synthetic history labelled `data_source='demo'`, and shows a
persistent warning banner on every page. It exists only to exercise the UI
without Docker — it is never presented as real network data. The Phase 5
**AI Lab** page works fully inside demo mode: no lab, no credentials, no
network.

### Deploying to Streamlit Community Cloud (public demo, no lab)

The repository is already in the shape Cloud expects — no build step, no Docker
image and no external service is required to start the UI:

| Cloud setting | Value |
|---|---|
| Repository | this repo (the free plan requires it to be public) |
| **Main script path** | `app.py` (landing page; `pages/` is picked up automatically) |
| Requirements file | `requirements.txt` at the root (Linux wheels only; no Windows-only package, `psycopg2` deliberately left commented out) |
| Python version | choose **3.11** or **3.12** in *Advanced settings* — both are the versions CI exercises. `runtime.txt` is not honoured by Community Cloud, so it is intentionally absent from this repo. |

**Configuration comes from the Secrets manager, never from a committed `.env`.**
Top-level scalar entries in app secrets are promoted to environment variables
before the first line of the app script runs, which is exactly how `config.py`
reads its settings — so no code change is needed. Under *Settings → Secrets*:

```toml
# required for a public demo
NETPILOT_DEMO_MODE = "true"

# optional: enables the AI assistant (page 10). Never commit this value.
GROQ_API_KEY = "<your-key>"
GROQ_MODEL = "openai/gpt-oss-20b"

# optional but recommended for a public URL: require a login (viewer<operator<admin)
NETPILOT_AUTH_ENABLED = "true"
NETPILOT_ADMIN_USER = "<bootstrap-admin-username>"
NETPILOT_ADMIN_PASSWORD = "<strong-password>"
```

The demo-mode application is designed to work on Cloud with an empty, freshly
created database: all 12 scripts, charts, topology/graph intelligence, anomaly
detection, forecasting and backtests, the offline natural-language query, RAG
search, the alert cycle and the change-proposal workflow. The offline UI smoke
check re-verifies all 12 scripts locally; **Streamlit Cloud itself was not
re-deployed during the closure audit**. `data/` is git-ignored, so on the first
launch the app creates its SQLite file (including the parent directory) and
seeds the labelled demo fixtures itself.

Two consequences of the Cloud runtime are worth stating plainly:

* **Storage is ephemeral.** A redeploy or restart discards the SQLite file; the
  demo seed simply regenerates it, but anything collected or recorded in that
  session is lost. Persistent data needs an external database
  (`DATABASE_URL=postgresql+psycopg2://…` plus `psycopg2-binary`), i.e. the
  containerized stack of §15 (`docker compose up --build`, which starts the
  dashboard, the API and PostgreSQL together) — not the free Cloud filesystem.
* **There is no lab and no change loop.** Docker/Containerlab are not available
  on Cloud, so live collection cannot run (the UI says so instead of inventing
  data), and `NETPILOT_CHANGE_LOOP_ENABLED` **and**
  `NETPILOT_LAB_EXECUTION_ENABLED` must both stay unset — `apply` therefore
  refuses by design, with no executor to inject.
* **The `ping` binary comes from `packages.txt`.** The Cloud base image, like
  `python:*-slim`, ships without `iputils-ping`, so the loopback self-test of
  page 4 cannot execute there. The committed root `packages.txt` installs it on
  Cloud (the `Dockerfile` does the same with `apt-get`); without it the page
  reports the probe as **not run** — an environment limitation, not a failed
  connectivity test — and records nothing.
* **The file watcher is off in the committed config**
  (`server.fileWatcherType = "none"`). Streamlit's watcher evicts watched
  application modules from `sys.modules` at the start of a run, and two libraries
  resolve a class's defining module through that registry *while the class body
  executes*: SQLAlchemy for `Mapped[...]` annotations, and CPython's `dataclasses`
  for the `__module__`/`__qualname__` strings. A rebuild that made the watcher
  fire therefore took the deployed app down twice — `MappedAnnotationError: Could
  not interpret annotation Mapped[int]` from `database/models.py`, then
  `AttributeError: 'NoneType' object has no attribute '__dict__'` from
  `network/containerlab_manager.py`. Nothing edits files in deployment, so the
  watcher stays off; `test_ship_hygiene.py::test_streamlit_config_is_theme_only_and_cloud_safe`
  keeps host/port/secrets out of that file. The option is not scriptable, so no
  environment variable or CLI flag overrides it — while developing locally, set
  `fileWatcherType` back to `"auto"` in your working copy to keep
  auto-rerun-on-save, and leave the committed value at `"none"`.
* **The ORM module avoids runtime string annotations.** `database/models.py`
  deliberately omits `from __future__ import annotations`, so mapping never has
  to consult the module registry at all — the belt to the watcher's braces above.
  `test_database.py::test_models_map_without_sys_modules_registration` keeps that
  guarantee in place.

### AI Network Assistant (page 10)

The assistant needs a Groq key in the local, git-ignored `.env`:

```powershell
copy .env.example .env      # then set GROQ_API_KEY (and, if needed, GROQ_MODEL)
$env:NETPILOT_DEMO_MODE="true"; streamlit run app.py
```

Open **🤖 AI Network Assistant**, choose demo/live, type a request in English
or Arabic and press **Run**. Without a key the page still loads and reports
that live AI mode is unavailable (it fails safe, never fabricates). To verify
the real provider integration once (network + key required, prints no secret):

```powershell
.\.venv\Scripts\python.exe tools\phase3_live_check.py
```

---

## 6. Configuration reference (`.env` / environment)

| Variable | Default | Purpose |
|---|---|---|
| `NETPILOT_DEMO_MODE` | `false` | labelled synthetic data, separate DB |
| `DATABASE_URL` | `sqlite:///data/netpilot.db` (`netpilot_demo.db` in demo) | storage backend |
| `NETPILOT_COLLECT_INTERVAL` | `60` | seconds between collection cycles |
| `NETPILOT_PING_TIMEOUT` / `NETPILOT_PING_COUNT` | `5` / `3` | ping behaviour |
| `NETPILOT_TRACEROUTE_TIMEOUT` | `30` | traceroute hard timeout |
| `NETPILOT_CMD_OUTPUT_LIMIT` | `65536` | max bytes kept from any command output |
| `NETPILOT_LAB_NAME` / `NETPILOT_LAB_TOPOLOGY` | `netpilot` / `lab/topology.clab.yml` | lab identification |
| `NETPILOT_ANALYTICS_MAX_POINTS` | `20000` | bounded query size for analytics loads |
| `NETPILOT_MIN_POINTS_ANALYSIS` / `_DETECTION` / `_FORECAST` | `5` / `12` / `30` | readiness gates (insufficient data → status, not a chart) |
| `NETPILOT_MAX_RATE_GAP_SECONDS` | `300` | beyond this a derived rate is flagged unreliable |
| `NETPILOT_DETECTOR_WINDOW` | `20` | trailing samples for rolling anomaly baselines |
| `NETPILOT_MAD_THRESHOLD` / `NETPILOT_ZSCORE_THRESHOLD` | `3.5` / `3.0` | rolling detector sensitivity |
| `NETPILOT_EWMA_THRESHOLD` | `3.0` | EWMA sensitivity (robust scales of the strictly-past weighted history; min `0.5`) |
| `NETPILOT_CUSUM_DRIFT` / `NETPILOT_CUSUM_THRESHOLD` | `0.5` / `4.0` | CUSUM drift allowance `k` and decision boundary `h` — smaller = more sensitive |
| `NETPILOT_ANOMALY_GROUP_WINDOW_SECONDS` | `300` | consecutive anomaly points group into one event |
| `NETPILOT_SCAN_MAX_SERIES` | `12` | hard cap on series evaluated per batch scan (the scan reports its own cap) |
| `NETPILOT_FORECAST_HORIZON` | `10` | default forecast horizon (steps) |
| `NETPILOT_SEASON_HOURS` | `24.0` | one seasonal cycle for the season-aware models/detector (clamped to ≥ `1.0` h) |
| `NETPILOT_BAND_COVERAGE` | `0.95` | nominal coverage the holdout-calibrated forecast bands are tuned to (clamped `0.5`–`0.999`) |
| `GROQ_API_KEY` | *(empty)* | Phase 3 — read **only** from env / git-ignored `.env`; never hard-coded, logged or stored |
| `GROQ_MODEL` | `openai/gpt-oss-20b` | Phase 3 — model id; **must exist on your Groq account** (catalog changes over time) |
| `GROQ_TIMEOUT` | `30` | Phase 3 — per-call timeout (seconds) |
| `GROQ_MAX_RETRIES` | `2` | Phase 3 — **bounded** retries for transient errors (capped at 5) |
| `NETPILOT_AI_REQUEST_MAX_CHARS` | `4000` | Phase 3 — hard cap on the incoming request length |
| `NETPILOT_AI_EVIDENCE_MAX_ROWS` | `50` | Phase 3 — bound on evidence rows gathered per run |
| `NETPILOT_AI_EVIDENCE_MAX_BYTES` | `8192` | Phase 3 — bound on each evidence record's content |
| `NETPILOT_AI_GRAPH_MAX_STEPS` | `12` | Phase 3 — orchestrator step ceiling (bounded loops) |
| `NETPILOT_AUTH_ENABLED` | `false` | v2 — opt-in login and RBAC (`viewer` < `operator` < `admin`) |
| `NETPILOT_PBKDF2_ITERATIONS` | `240000` | v2 — password-hash work factor |
| `NETPILOT_ADMIN_USER` / `NETPILOT_ADMIN_PASSWORD` | *(empty)* | v2 — optional bootstrap admin; environment only, never committed |
| `NETPILOT_API_TOKEN` | *(empty)* | v2 — when set, every `/api/*` route requires `Authorization: Bearer <token>` |
| `NETPILOT_API_CORS_ORIGINS` | *(empty)* | v2 — comma-separated allowed browser origins |
| `NETPILOT_CHANGE_LOOP_ENABLED` | `false` | v2 — enables the change state machine; apply still needs an injected lab executor |
| `NETPILOT_CHANGE_REQUEST_TTL_MINUTES` | `1440` | v2 — approval window before a request expires |
| `NETPILOT_LAB_EXECUTION_ENABLED` | `false` | Phase 4 — second gate for lab device I/O; only on the isolated lab host |
| `NETPILOT_LAB_EXECUTION_TIMEOUT` | `30` | Phase 4 — per-device config operation timeout (seconds) |
| `NETPILOT_DIGITAL_TWIN_MAX_AGE_SECONDS` | *(unset)* | D3/D4 — optional snapshot freshness TTL; unset keeps freshness explicitly unknown |
| `NETPILOT_SEASONAL_THRESHOLD` | `3.5` | robust scales a point may sit from its own same-phase history before the seasonal-residual detector flags it |
| `NETPILOT_AI_LAB_SIM_MAX_POINTS` / `_SIM_MAX_DEVICES` | `20000` / `12` | Phase 5 — simulator input bounds; an oversized request is **refused with the bound named**, never silently shrunk |
| `NETPILOT_AI_LAB_ML_MAX_POINTS` | `150` | Phase 5 — per-series evaluation cap for detectors that refit per judged point (disclosed in the result) |
| `NETPILOT_AI_LAB_UPLOAD_MAX_BYTES` | `20000000` | Phase 5 — max CSV text one browser upload may carry |
| `NETPILOT_AI_LAB_LOCAL_DIR` | *(empty)* | Phase 5 — allowlisted directory for historical imports; empty **disables local file import entirely** |
| `NETPILOT_AI_LAB_PROMETHEUS_URL` / `_TIMEOUT` | *(empty)* / `10` | Phase 5 — optional read-only live telemetry; empty keeps the source disabled and reported as not configured |
| `NETPILOT_AI_LAB_HISTORY_LIMIT` | `50` | Phase 5 — experiment-history rows listed in the UI |

**Timezone convention:** every timestamp is **UTC**. Values are stored as
naive-UTC datetimes (documented in `database/models.py`) and labelled
`(UTC)` in all UI tables.

---

## 7. Metrics actually collected

| Metric | Unit | Source | Availability |
|---|---|---|---|
| `ping.avg_rtt_ms`, `ping.loss_pct` | ms, % | real `ping` from collector host → node **management IP** (Docker bridge) | only when the lab runs on the same Docker host; otherwise stored as NULL |
| `inlab_ping.avg_rtt_ms`, `inlab_ping.loss_pct` | ms, % | real `ping` executed **inside** the node container (`docker exec`) toward a router loopback | FRR routers: yes (image provides ping); alpine PCs: yes |
| `if.rx_bytes`, `if.tx_bytes` | bytes | `/sys/class/net/<if>/statistics/*` inside the container | running containers |
| `if.rx_errors`, `if.tx_errors`, `if.rx_dropped`, `if.tx_dropped` | count | same | running containers |
| `if.operstate` | link (1=up, 0=down) | `/sys/class/net/<if>/operstate` | running containers |
| `frr.routes_total` | count | `docker exec <node> vtysh -c "show ip route json"` | FRR router nodes only |
| Connectivity tests (ping/traceroute) | various | user-initiated via the UI, allowlisted | lab deployed; traceroute requires `tracert`/`traceroute` on the collector host |

Unavailable metrics are stored as **NULL** and displayed as "not available";
they are never fabricated. The Metrics page only offers device/metric
combinations that actually have non-null stored values.

---

## 8. Automated tests

```bash
pytest                 # 971 unit/integration tests (full suite, offline)
python tests\ui_smoke_check.py   # headless run of app.py + all 12 pages,
                                 # clicking the detection/forecast/lab buttons
```

The Phase 3 tests use **mocked Groq responses** (an injected fake client), so
the whole suite runs **fully offline** and never needs an API key or network.

Coverage (mapping to spec §6):

- device/link creation and reading (`test_database.py`)
- time-series metric storage/retrieval, null handling, per-interface latest
  values (`test_database.py`)
- connection failures & timeouts: mocked subprocess timeouts, missing
  binaries, unreachable devices reported not crashed (`test_connectivity.py`)
- allowlist: only inventory members accepted; injection-style and external
  destinations rejected (`test_inventory.py`, `test_connectivity.py`)
- documented device/network status rules (`test_status_rules.py`)
- scheduler overlap prevention, error capture, lifecycle (`test_scheduler.py`)
- collector with injected probes: real values stored, unavailable = NULL,
  incidents open/resolve, run records (`test_collector.py`)
- demo fixtures separate from live data, always labelled (`test_demo_mode.py`)
- **live toolchain smoke**: a real `ping` subprocess run against the host's
  own loopback adapter only (127.0.0.1 — no network traffic), proving
  execution → parsing → storage (`test_connectivity.py::test_selftest_ping...`).
  It needs the system `ping` binary: the `Dockerfile` installs `iputils-ping`, a
  root `packages.txt` does the same on Streamlit Community Cloud, and a runtime
  without it is reported as *not run* rather than as a failure

Phase 2 additions (spec §11; all synthetic fixtures clearly labelled as
protocol tests, not real-world performance evidence):

- data quality: empty/short/irregular/duplicate/stale/mixed-source series
  and readiness gates (`test_data_quality.py`)
- derived metrics: bps units, counter resets, invalid elapsed time,
  oversized gaps, traceable sample ids (`test_derived_metrics.py`)
- EDA: gated summaries/histograms/profiles/comparison/correlations,
  chart gap breaks (`test_eda.py`)
- anomaly detection: spikes, sustained deviation, dedup/grouping, replay
  idempotency, resolution only via normal observation, no future leakage
  (`test_anomaly_detection.py`)
- temporal protocol: chronological splits, leakage rejection, exact
  MAE/RMSE/R², detection scoring (`test_temporal_splits.py`)
- forecasting: naive/seasonal/Holt/Holt-Winters/ridge mathematics, refusal on
  insufficient data, reproducibility (`test_forecasting.py`)
- persistence: runs+predictions stored with full metadata, identical
  comparison timestamps, raw samples immutable, backfill semantics,
  demo/live separation (`test_analytics_persistence.py`)
- UI smoke extended: pages 7–8–9 render and their action buttons run

Phase 3 additions (spec §11; all Groq calls mocked — no key/network required):

- provider adapter: missing key, happy path, bounded retries on rate-limit /
  timeout, malformed-JSON and schema-violation handling, error classification
  (`test_groq_client.py`)
- intent agent: Arabic/English classification, code-side language detection,
  ambiguous change requests force clarification (`test_intent_agent.py`)
- workflow routing/safety: questions never reach the Config Agent, change
  requests reach verifier + Digital Twin (`not_run`), ambiguous → clarify,
  missing key → `failed_safe`, and the runner exposes **no** deploy/apply
  method (`test_workflow_routing.py`)
- read-only evidence tools: source separation (demo/live never leak),
  stale/missing flagged not invented, connectivity reads stored history only,
  bounded output with stable `ev-N` uids (`test_evidence_tools.py`)
- deterministic verifier: prohibited/shell-injection lines rejected, unknown
  target failed, blocked proposal inconclusive (never a false pass), stated
  policy kept inconclusive, clean proposal passes required checks
  (`test_verifier.py`)
- prompt-injection defence: retrieved logs treated as data, a poisoned
  malicious proposal fails the verifier and no device config file changes
  (`test_prompt_injection.py`)
- persistence/audit: full run trail, ordered status transitions, evidence
  references, demo/live run separation, human-review records outcome only,
  and the provider secret is **never** stored (`test_ai_persistence.py`)

v2 additions (advanced capabilities roadmap, Slices 1–8; all offline):

- graph intelligence: reachability, shortest path, blast-radius, SPOF
  (`test_graph_intelligence.py`); alert rules engine with de-dup + open/ack/
  resolve lifecycle, live/demo never merge (`test_alerts.py`)
- auth + RBAC (opt-in): PBKDF2 verify, role ordering, self-approval/apply
  guards (`test_auth.py`)
- ML anomaly + root-cause + backtest: seeded Isolation Forest (`test_ml_anomaly.py`),
  topology+time RCA (`test_rca.py`), rolling-origin forecast backtest with a
  no-leakage assertion (`test_backtest.py`)
- offline RAG (`test_rag.py`) + deterministic NL→data query (`test_nlquery.py`)
- REST/WS API + Prometheus exposition: read-models, HTTP + WebSocket via
  `TestClient`, `/metrics` text format (`test_api.py`)
- gated change loop: validation, self-approval refusal, apply refused unless
  enabled+approved+executor, executor-called-with-proposed-config only,
  failure recorded, rollback, cancel (`test_change_service.py`)
- lab-only executor + gated apply/verify/rollback: config-block model and
  rollback-plan derivation (`test_lab_config_ops.py`), lab identity proof,
  static gate, argv rendering, timeout/partial/failure outcomes
  (`test_lab_executor.py`), the ten-step gated pipeline including
  precondition drift, verification failure, rollback and restoration
  (`test_change_execution.py`), and the shared L-4/L-5 static scanner —
  no execution primitives in `ai/`, and no agent able to hand retrieved
  evidence to the model without the `<untrusted_data>` envelope
  (`test_gate3_safety.py`)
- CI secret-hygiene scanner: clean on the real tree, catches planted secrets,
  ignores placeholders/fakes (`test_secret_scan.py`)
- multi-layout topology: pure layout engines (layered/force/circular/grid)
  + degree sizing + a demo-only large synthetic fabric
  (`test_topology_views.py`)

Forecasting & anomaly enhancement pass (115 tests, all offline synthetic
fixtures — **protocol tests, not real-network accuracy evidence**):

- new accuracy metrics: hand-checked MAPE / sMAPE / MASE / skill-vs-naive,
  NULL-instead-of-fabricated refusals, and points skipped by a metric's own
  guard never being counted as scored (`test_forecast_metrics_v2.py`)
- new detectors on strictly-past data: EWMA, two-sided CUSUM (reset after a
  flag and across gaps), seasonal residual (same-phase baseline, abstention
  when the season does not fit) (`test_anomaly_detectors_v2.py`)
- forecast service: metrics persisted per run, `skill` identity asserted only
  for full-holdout models (a partial-coverage model's skill must not be
  back-derived), Holt-Winters beating both season-blind baselines on the
  measured fixture, holdout band geometry + coverage clamping, ranking vs the
  in-memory blend, ensemble weights, crossing ETA refusal without a known
  interval, realized coverage after backfill, `skipped`/`insufficient_data`
  refusals (`test_forecast_service_v2.py`)
- anomaly service: stored-series inventory, the bounded device-level-only
  batch scan (interface rows must not contaminate it; the scan reports its own
  cap), event open/resolve semantics — an event is resolved only by a real
  normal observation, never by the run finishing — event aggregate honesty on
  an empty store, and label-driven detector calibration
  (`test_anomaly_service_v2.py`)
- rolling-origin backtest routing for the season-aware kwargs
  (`test_backtest.py`)

Phase 5 AI Lab additions (153 tests, all offline deterministic fixtures — no
internet, no paid service, no live device):

- unified telemetry contract: timestamp parsing (ISO / epoch-unit / already
  datetime), rejected-vs-retained row counts, non-numeric value → missing
  (never 0), duplicate/stale detection, mixed-unit quarantine, mixed-source
  refusal, source identity through the whole pipeline, stable content
  fingerprint (`test_ai_lab_dataset.py`, 35)
- simulator: same seed ⇒ same data, a different seed changes the traffic not
  the structure, declared units never mixed, injected events really move the
  metric inside their labelled interval, labels non-overlapping, cpu/memory
  flagged simulated-only, every input bound enforced, and neither config nor
  dataset ever claims to be a real measurement (`test_ai_lab_simulator.py`, 21)
- source adapters: stored project data keeps demo/live separation and states it
  has no ground truth, CSV preview/mapping/validation with malformed and
  duplicate lines counted, uploads treated as inert data (binary and oversized
  refused, guards advertised), local import disabled until the allowlisted
  directory is configured, live Prometheus disabled-by-default + bounded +
  failure-honest against a mocked transport (`test_ai_lab_sources.py`, 40)
- tasks: identical holdout timestamps for every model, recomputable metrics,
  zero/near-zero actuals handled by each metric's own guard, one broken model
  cannot poison the comparison, labelled vs unlabelled anomaly runs (accuracy
  only when truth exists), classification blocked without valid labels, thin
  and single-class refusals report the needed counts, unlabelled rows dropped
  and disclosed, statistics disclaim causation (`test_ai_lab_tasks.py`, 30)
- runner / history / exports / safety: additive `lab_experiments` persistence,
  `persist=False` writes nothing, unknown task or parameter rejected, seed
  reproducibility end to end, series-selection bound disclosed, raw samples
  never duplicated into the store, oversized point payloads dropped overtly,
  corrupt stored JSON reported not guessed, CSV/Markdown/HTML carry the actual
  computed numbers with provenance first, formula-injection neutralised, HTML
  escaped, the whole lab runs with `subprocess` poisoned, never imports the
  execution layer, and the change/lab gates stay off before and after every
  task (`test_ai_lab_experiment.py`, 27)

Latest verification (2026-10-28, Windows, no Docker): **971 passed**, 0 failed,
0 errors, 0 skipped; 13 pre-existing `datetime.utcnow()` DeprecationWarnings
from `services/ai_workflow_service.py`. UI smoke check: all 13 scripts
(app + 12 pages) render without exceptions, the detection/forecast actions run,
and the AI Lab round-trips a simulated experiment end to end while the
unconfigured live source stays honestly disabled — exit code **0**. GitHub
Actions run
[`40760222225`](https://github.com/Mshabrawy-m/NetPilot-AI/actions/runs/40760222225)
is green on every job, including `test (3.11)`, `test (3.12)`, `ui-smoke`,
`installability` (3.10–3.14) and `docker-build`.

Recent fixes on the tip:

- `pages/12_AI_Lab.py` used a PEP 701 multi-line f-string replacement field,
  which only parses on Python 3.12+; the CI 3.11 syntax gate failed and the app
  could not launch on 3.10/3.11. The string is now built in two statements.
- `pages/8_Anomalies.py` rendered its KPI row before handling the detection
  buttons, so a scan's success message could contradict the still-stale
  header. The result is now queued via `flash()` and followed by one rerun, the
  same pattern used by Topology and Ops Center.
- `pages/12_AI_Lab.py` was rebuilt as a five-tab workflow (data source /
  inspect / configure & run / results / history) with KPI headers instead of
  truncating `st.metric` tiles; nested JSON table cells now render as text
  instead of `[object Object]`, and every successful dataset load flashes a
  confirmation and reruns so the status strip and the tabs cannot disagree.
- `ui/theme.py:inject_css()` re-emits the stylesheet on every run. The old
  session-level "already injected" flag survived an in-app page switch while
  Streamlit cleared the page body that held the `<style>` element, so every
  page after the first rendered with Streamlit's default theme.

The optional **live** Groq integration check (`tools/phase3_live_check.py`) was
**not** re-run during this pass; live-provider behaviour is therefore unverified
here. The previously exposed credential is treated as compromised and must be
rotated (see `docs/FINAL_PROJECT_CLOSURE_REPORT.md`).

> The Phase 4 lab executor and the Gate 3 / Gate D harnesses **are** part of the
> published tip and covered by the count above, but the live-lab evidence for
> them does not exist yet (no Docker/Containerlab host). See §14 and
> `NETPILOT_GATE3_ISOLATED_LAB_RUNBOOK.md`.

### Offline AI-quality / groundedness evaluation (B4)

A separate, deterministic evaluation scores the AI workflows themselves
(groundedness, evidence relevance, abstention, safety, task correctness,
structured output, language handling, robustness). It is fully offline and
uses fixed synthetic fixtures — no API key, network, provider or paid service.

```bash
python tools/ai_quality_eval.py
```

The machine-readable report is written to
`evaluation/reports/ai_quality_report.json` (generated, git-ignored). The
versioned dataset is `evaluation/datasets/ai_quality_v1.json`; metric
definitions, scope boundaries and limitations are documented in
`evaluation/README.md`. Exit codes: `0` pass, `1` case failures, `2`
dataset/harness error, `3` safety-gate failure.

> This is **not** a live-provider, Streamlit, Gate 3 or Gate D test. Those are
> marked `not_run` in the report and excluded from every metric. pytest pass
> rates are engineering tests, not AI quality scores.

---

## 9. Manual verification checklist

With the dashboard running (demo or live mode):

1. **Overview** — device counters, network status badge, last collection time.
2. **Topology** — graph with R1/R2/R3/PC1/PC2 rendered from the database
   (status colors; hover shows IP and status; live mode after lab deploy).
3. **Devices** — select a device, see interfaces, addresses, latest metrics.
4. **Connectivity Tests** — pick source/destination (selector only offers
   lab inventory), run ping; failure/timeout messages are explicit; results
   are persisted (see Recent tests).
5. **Metrics & History** — choose device + metric + range → chart; metrics
   without data are not offered.
6. **System Status** — DB/lab connectivity, scheduler state, run history,
   open incidents, guarded data reset.
7. With the lab deployed: stop a node (`docker stop R3`) → next collection
   marks it `down`, opens an incident, links touching it go `down`; restart
   (`docker start R3`) → incident resolves on the next cycle.
8. **Anomalies (page 8)** — choose a series and press *Run detection*: each
   flagged point carries its baseline, threshold and explanation; a detector
   that cannot form an opinion (unknown interval, season that does not fit the
   sample) reports an explicit abstention note instead of a clean result, and
   *Scan ready series* states its own cap instead of quietly doing less.
9. **Forecasting (page 9)** — press *Run forecast comparison*: all models are
   scored on identical held-out timestamps, MAPE/sMAPE/MASE/skill are shown
   only where they are defensible (otherwise blank, never 0), the band is
   labelled with how it was calibrated, and coverage stays *unknown* until
   real actuals are backfilled — after that it becomes realized coverage.
10. **AI Lab (page 12)** — build a simulated dataset (or load project history /
    upload a CSV), inspect the provenance badge and per-task gates, pick a task,
    press *Run experiment*. A source that cannot support the task is refused
    with the threshold named; the run (success **or** refusal) appears in the
    history table and the four exports describe only that computed run.

---

## 10. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| "Containerlab is not available" on System Status | host has no `containerlab` binary → run the lab on Linux/WSL2, or use demo mode |
| Lab nodes all `unknown` | lab not deployed (`containerlab deploy -t lab/topology.clab.yml`) or Docker daemon stopped |
| `ping shows 100% loss` for a running node | management IP changed after redeploy — press Collect now; containerlab assigns IPs from the `netpilot-mgmt` bridge |
| PC1/PC2 have no IPs | containerlab `exec` startup race — redeploy, or set manually: `docker exec PC1 sh -c "ip addr add 10.0.21.21/24 dev eth1; ip link set eth1 up; ip route add default via 10.0.21.2"` |
| FRR image pull fails | adjust the image tag in `topology.clab.yml` to one available in your registry mirror |
| Windows: "sqlalchemy DLL load failed / App Control blocked" | install the pure-Python pin already in requirements: `pip install "sqlalchemy>=2.0,<2.1"` |
| Charts empty in live mode | wait for at least 2 collection cycles, or widen the time range |
| Database locked (SQLite) | WAL mode is enabled automatically; if multiple machines must share storage, switch to PostgreSQL |

---

## 11. Known limitations & environment blockers (honest status)

1. **The Containerlab lab could not be deployed/tested on the development
   machine used for this delivery** (Windows host without Docker/Containerlab
   per spec §10). All lab files, deploy/destroy commands, FRR configs, and
   collection code are complete and unit-tested, but end-to-end live-lab
   measurements (management ping, interface counters via `/sys`, `vtysh
   show ip route json`) have **only been verified with injected fake probes
   in the test suite**, plus the real loopback ping self-test of the command
   toolchain. They must be re-verified once on a Docker-supported host
   (checklist in §9).
2. `ping`/`traceroute` from the dashboard run **from the collector host**.
   In live mode the host can reach only Docker-bridge management IPs;
   lab-internal paths (10.x) are measured via `inlab_ping` (inside the
   containers) — the "source" device for host-initiated tests is therefore
   the collector host. Per-router on-demand ping is a Phase-2 candidate.
3. The FRR container image may lack `ping` in some builds; then `inlab_ping`
   samples are stored NULL (reported, not invented). Alpine PCs include it.
4. Interface counters are raw cumulative values; Phase 2 derives rates
   (bps) **on demand** from consecutive samples — the derived values are
   traceable to their source sample ids and are never written back into
   `metric_samples`.
5. Demo-mode "Run ping" exercises the real ping binary against the host
   loopback only, explicitly labelled as a toolchain self-test — it is not
   and never claims to be a lab measurement.
6. Single-process scheduler: the background collector is one daemon thread
   per Streamlit process (overlap-locked). Multi-instance deployments should
   run one dedicated collector (documented; `collect_once` is importable as
   a standalone entry point).
7. **Digital Twin and PostgreSQL remain partially validated.** The D1-D4
   engine/registry are covered by offline tests and the read-only Ops Center
   preview, but they are not wired into `ai/graph.py` and have no live-lab
   evidence. PostgreSQL is supported by the storage layer and Docker image but
   was not exercised in the closure environment (no driver/server); SQLite is
   the verified default. Real-browser E2E, live-provider evaluation,
   Docker/Containerlab and real-device tests were not run.
8. **Forecasting/anomaly quality is proven on synthetic fixtures only.** The
   new metrics, Holt-Winters, holdout-calibrated bands, EWMA/CUSUM/seasonal
   detectors and the batch scan are verified by offline tests whose series are
   generated in the test file, so they demonstrate the *mathematics and the
   refusals* — not accuracy on this lab's traffic. Seasonal models and the
   seasonal-residual detector additionally need a real cycle in the data: with
   the default 60-second collection interval `NETPILOT_SEASON_HOURS=24` a
   season is 1440 samples, so short histories legitimately report
   `skipped`/abstention rather than a forecast. Detector calibration scores
   against caller-supplied labels and is explicitly **not** real-world
   accuracy. Long-horizon accuracy on a live lab has not been measured.

## 12. Phase 2 — Data Analysis, Anomaly Detection & Forecasting

**Analytics core (`analytics/`, pure computation, no UI/SQL writes):**

* `time_series.py` — bounded chronological loads into frames;
  deterministic dedupe (last write wins), optional resample, gap
  interpolation only when short and always labelled `imputed=True`.
* `data_quality.py` — per-series quality report (nulls, duplicates,
  gaps, staleness, mixed sources) and readiness gates
  (`analysis ≥5`, `detection ≥12`, `forecast ≥30` non-null samples;
  highly irregular sampling blocks forecasting). Insufficient data
  produces a status with reasons, never a confident-looking result.
* `derived_metrics.py` — cumulative counters → rates
  (`Δcounter·8/Δt` → bps); negative deltas are omitted as
  `counter_reset_or_wrap` (reset vs wrap is not distinguishable from
  counters alone), non-positive elapsed times rejected, oversized
  intervals flagged unreliable; utilization only with a known capacity.
* `eda.py` — summaries, gated distributions/histograms, time-of-day
  profile, unit-compatibility-checked group comparison, correlations
  only on ≥12 time-aligned points; `insert_gap_breaks` keeps chart
  discontinuities honest.
* `anomaly_detection.py` — five explainable detectors, all on
  strictly-past shifted data (no self-masking, no future leakage):
  **rolling MAD** and **rolling z-score** baselines, **EWMA** (robust
  scale of the exponentially weighted past), **two-sided CUSUM** (catches
  a sustained small shift a 3σ window misses; resets after a flag and
  across sampling gaps) and **seasonal residual** (compares each point
  only with the same phase of earlier cycles, refusing when the season
  does not fit the sample); optional seeded Isolation Forest. The three
  new detectors are **opt-in** — the shipped default set is unchanged.
  Every point carries baseline, thresholds and a plain-language
  explanation; a detector with no opinion reports an abstention note,
  never a clean result.
* `forecasting.py` — **naive** (mandatory baseline), **seasonal naive**
  (refuses without ≥2 full seasons), **damped Holt** and **additive
  damped Holt-Winters** (both pure-NumPy, deterministic grid —
  statsmodels is blocked by this machine's Windows App Control; HW is
  initialised causally from the first two seasons only), **ridge-lag**
  (scikit-learn, scaler fitted on train rows only). Prediction bands are
  **holdout-calibrated** (empirical, or `√h`-scaled empirical, error
  quantiles from the validation split — not a Gaussian assumption), and
  the nominal coverage is clamped when the holdout is too small to
  support it.
* `evaluation.py` — chronological train|val|test split (never shuffled),
  `validate_split_order` leakage guard, MAE/RMSE/R² plus the scale-free
  comparators MAPE / sMAPE / MASE / skill-vs-naive — each scoring only the
  timestamps where an actual *and* a prediction both exist, and each returning
  NULL (never a plausible 0) when the denominators are not defensible;
  `score_detection` (precision/recall/F1) for labelled detectors.

**Services & persistence:** `services/anomaly_service.py` groups
consecutive anomaly points into one deduplicated event (occurrence/peak
escalation), resolves events **only** via a real normal observation
(missing data never resolves anything), walks a stored-series inventory
in `scan_ready_series` (device-level series only, bounded by
`NETPILOT_SCAN_MAX_SERIES` and reporting its own cap), summarises events
(`event_summary`: open/resolved, severity, detector, episode and
resolution durations — honest NULLs on an empty store) and calibrates
detector sensitivity against caller-supplied labels
(`calibrate_detectors` / `calibrate_series`, scored only where labels
exist and explicitly *not* real-world accuracy).
`services/forecast_service.py` compares all models on identical held-out
timestamps, persists runs (+ MAPE/sMAPE/MASE/skill/band metadata) and
future predictions in dedicated tables, reports a ranking with the best
reproducible run, builds an MAE-weighted **ensemble** in memory (never
persisted as a run, so nothing un-reproducible is presented as a model),
converts the forecast into a threshold-crossing warning (direction,
certain/possible, deterministic ETA — refused when the sampling interval
is unknown), backfills actuals once an observation exists and turns them
into **realized** coverage/error (a forecast whose bands were never
verified reports coverage NULL rather than 100 %). Raw `metric_samples`
are never modified, and demo/live provenance is preserved end-to-end
(`data_source` on every row, filter, run and event).

**New UI:** page 7 Data Analysis, page 8 Anomalies, page 9 Forecasting.
Page 8 adds detector selection (including the opt-in change-point
detectors), sensitivity controls, the bounded batch scan with its cap
reported, and the event aggregate. Page 9 adds the accuracy KPIs, the
model ranking + best-model callout, the ensemble blend, band-mode choice
with realized coverage and the threshold-crossing warnings. Heavy
analyses (detection, forecast comparison) run **only** on explicit button
press, never on every rerun.

**Database migration:** purely additive — `anomaly_events`,
`forecast_runs`, `forecast_predictions` are created automatically on
next start (`init_db()` → CREATE TABLE IF NOT EXISTS) and the new
hold-out metrics (`mape`, `smape`, `mase`, `skill`, `band_meta_json` on
`forecast_runs`) are added by `ALTER TABLE … ADD COLUMN` for only
missing **nullable** columns; no destructive or altering migration
touches Phase 1 tables. Applying this Phase to an existing Phase 1
database adds the tables/columns and leaves all stored samples untouched
(old runs keep NULL for the new metrics rather than a back-filled guess).

## 13. Phase 3 — Multi-Agent AI Assistant & Safe Network Operations

**Provider adapter (`ai/groq_client.py`)** — the only place the Groq SDK is
touched. It reads `GROQ_API_KEY` from the environment / git-ignored `.env`
(never hard-coded, never logged), uses a configurable model, a per-call
`timeout`, and **bounded** retries. Every call is JSON-mode and validated
against a typed Pydantic schema before it reaches a downstream node; a
missing key, rate limit, timeout, malformed response or schema violation
raises a typed error the workflow handles safely (it never fabricates).

**Orchestration (`ai/graph.py`, LangGraph)** — a typed-state `StateGraph`
running the Intake → Intent → Gather → Investigate → Diagnose → (Plan →
Config) → Verify → Digital-Twin → Finalize nodes with conditional routing:
an informational **question** stops after diagnosis, an **ambiguous** change
request short-circuits to clarification, and only an **explicit change
request** reaches the Planner/Config agents. There is deliberately **no
apply/deploy node**. Every state transition is written to the audit trail.

**Agents (`ai/agents/`)** — Intent (Arabic/English classification with
code-side language detection and clarification gating), Investigation,
Diagnosis (facts vs hypotheses, categorical confidence only), Planner
(change requests only; rollback is a documented Phase-4 note) and
Config-proposal. The Config Agent is **hard-gated in code before the LLM is
called**: it produces text only for a known, FRR-configurable router in an
explicit change request, and re-validates whatever the model returns.

**Read-only evidence tools (`ai/tools/`)** — topology, telemetry,
connectivity history, anomalies, forecasts, incidents and collection errors,
all read from the existing Phase 1/2 data, source-filtered (`demo`/`live`),
row/byte-bounded, provenance-tagged with stable `ev-N` references. They never
trigger a new probe and never run arbitrary SQL/shell.

**Independent deterministic verifier (`ai/verification/`)** — pure code,
no LLM. Rejects shell/CLI-injection and prohibited apply commands, unknown
targets and out-of-scope devices; returns `passed` only when every required
check conclusively passes, otherwise `failed` or `inconclusive` (it never
reports a false pass, and a static pass never implies live behaviour).

**Digital Twin (`ai/simulation/`)** — a clean `DigitalTwin` interface with an
`UnavailableDigitalTwin` that honestly reports `not_run`, so results are never
misrepresented. A separate read-only structural engine
(`services/digital_twin.py`, D1-D3) and persistent snapshot registry
(`services/digital_twin_registry.py`, D4) now exist and are used by the Ops
Center preview; they are deliberately **not wired into `ai/graph.py`**, so the
AI workflow still reports `not_run`.

**Prompt-injection defence** — retrieved logs/configs/user snippets are
wrapped in `<untrusted_data>` blocks and treated as data everywhere; the
safety guarantees live in code (allowlists, hard gates, the verifier, the
absent deploy path), with prompts as defence-in-depth only.

**New UI: page 10 AI Network Assistant** — a chat panel (demo/live selector,
runs only on explicit submit), evidence/investigation/diagnosis/plan/config
diff, the verifier verdict and Digital-Twin status, run history, and a
**human-review** record that captures an approve/reject outcome without ever
deploying anything.

**Database migration:** purely additive — `ai_runs`, `ai_status_transitions`
and `ai_evidence` are created on next start (`init_db()` → CREATE TABLE IF NOT
EXISTS). No Phase 1/2 table is altered, and no provider secret is ever stored.

## 14. Current scope boundary (what is not wired or not validated)

**Not implemented:** live routing/behaviour simulation (the Digital Twin engine
is structural reachability only), real-network execution, and MCP servers.
**Implemented but deliberately not wired into `ai/graph.py`:** the D1-D4
Digital Twin engine and persistent snapshot registry. **Implemented:** the
offline RAG store (`services/rag.py`) used by the Knowledge tab. The assistant
never connects to a production network and never executes generated config or
modifies routes/ACLs/firewalls/interfaces. Anomaly events, forecast warnings
and AI diagnoses remain clearly-labelled probabilistic/statistical statements
about data — never classified as confirmed failures or attacks.

**Where the Phase 4 change loop now stands.** The *runtime* half of the
change loop is implemented (`network/lab_executor.py`,
`network/lab_verifier.py`, `network/lab_config_ops.py`,
`services/change_execution.py`) — see the advanced-capabilities roadmap
(`docs/NetPilot_AI_Advanced_Roadmap.md`, slice 7) for the full design:
propose → approve → apply → post-change verify → rollback → verify
restoration. It is **lab-only and opt-in**, and every gate fails closed:

- it needs **both** `NETPILOT_CHANGE_LOOP_ENABLED=true` **and**
  `NETPILOT_LAB_EXECUTION_ENABLED=true` (both off by default), in live mode —
  demo mode always refuses;
- the target must be a **router node of the configured Containerlab
  topology**, running, and its container must carry Containerlab's own node
  label — a container name alone is never trusted;
- the proposal must pass the existing deterministic validator, the
  lab-invariant policy checks and a config-command allowlist;
- a **named, non-self human approval** and a **verified rollback plan** are
  mandatory; the device must still match the recorded base configuration;
- the LLM never reaches a shell: lines are sent as a fixed
  `docker exec … vtysh -c …` argv list (`shell=False`), one argv element per
  config line, and the executor lives **outside** `ai/` so the L-5 static
  safety scan of the AI package stays clean;
- changes are applied to the **running** configuration only — `write memory`,
  `copy running-config` and `configure replace` are prohibited — so a
  container restart discards them;
- the **rollback is a real undo**, not a merge: it removes the lines the
  change added (with FRR's own removal form — `no description`, not
  `no description <text>`) and re-applies the base value for the lines it
  changed. A change FRR gives no removal command for is **refused** before
  anything is applied;
- the **dashboard is unchanged**: from the UI, applying is still always a
  no-op. The loop is driven from the CLI
  (`tools/phase4_change_loop_validation.py`), which refuses to write to a
  device unless the operator passes the explicit `--approve-change` flag for
  that exact change.

> **Honest status:** this executor is covered by an offline test suite, but
> it has **not yet been executed against a running lab** — that requires the
> isolated Ubuntu VM described in
> `NETPILOT_GATE3_ISOLATED_LAB_RUNBOOK.md`. Until that run exists with
> captured evidence, apply / verification / rollback / restoration are
> **claimed as implemented and unit-tested, not as validated in a live
> environment**.


## 15. v2 platform: containerized stack, API & CI

Everything below is **opt-in and additive**; the plain `streamlit run app.py`
flow, the demo mode and the offline test suite are unchanged.

**One-command stack (Docker Compose).** `docker compose up --build` starts
PostgreSQL + the dashboard (http://localhost:8501) + the read-only API
(http://localhost:8000). The container image runs either role from the same
artifact via `NETPILOT_SERVICE` (`dashboard` → Streamlit, `api` → uvicorn);
see `Dockerfile`, `docker/entrypoint.sh` and `docker-compose.yml`. The stack
boots in clearly-labelled **demo mode** so `compose up` always shows a working
dashboard without a lab. The Containerlab lab itself is an optional
`--profile lab` service and only runs on a Docker-capable host (it needs the
host Docker socket) — never against production.

**Read-only REST/WS API.** `python -m uvicorn api.app:app --port 8000` (no
Streamlit required). Endpoints: `/health`, `/metrics`, `/api/devices[/name]`,
`/api/alerts`, `/api/topology`, `/api/risk`, `/api/ask` (GET + POST) and a
`/ws/topology` request/response socket. It reads the same `DATABASE_URL` as
the dashboard (SQLite **or** PostgreSQL by changing only the URL) and mutates
nothing. Set `NETPILOT_API_TOKEN` and every `/api/*` route then requires
`Authorization: Bearer <token>`; leave it unset for open read-only access.

**Observability.** `/metrics` returns a JSON snapshot of the in-process
counters by default, and Prometheus **text exposition** when scraped with
`Accept: text/plain` (e.g. `curl -H 'Accept: text/plain' localhost:8000/metrics`).
No compiled Prometheus client is used — the format is emitted by hand so the
whole stack stays importable under restrictive App-Control policies.

**CI (`.github/workflows/ci.yml`).** On every PR/push: a secret-hygiene gate
(`tools/secret_scan_check.py` + a `git ls-files` check that no `.env` is
committed), the offline **pytest matrix** on Python 3.11 and 3.12 with a
syntax `compileall` gate, the **headless UI smoke** over every page, and a
build-only Docker smoke. The suite needs no keys, network or Docker, so CI is
green wherever the code is.

```bash
# run the whole stack
docker compose up --build
# or just start the API locally against the default SQLite store
python -m uvicorn api.app:app --host 127.0.0.1 --port 8000
# Prometheus-format metrics
curl -H "Accept: text/plain" http://127.0.0.1:8000/metrics
```

---

## 16. Phase 5 — Real & Simulated Data Analytics Lab (AI Lab)

Page **🧪 AI Lab** (`pages/12_AI_Lab.py`) turns the existing computation core
into an experiment platform: pick a source → inspect it → pick a task →
configure → run → evaluate → export. Full guide:
`docs/NetPilot_Phase_5_AI_Lab_Guide_EN.md`; delivery evidence:
`docs/Phase5_AI_Lab_Delivery_Report.md`.

**Sources.** Built-in deterministic **simulator** (multi-device telemetry with
injected congestion / latency-spike / packet-loss / CPU-overload /
interface-outage events and ground-truth labels), **project history** from this
app's own database, browser **CSV/TSV upload** with preview + editable column
mapping and explicit rejected-vs-retained row counts, an **allowlisted local
file** directory (disabled unless `NETPILOT_AI_LAB_LOCAL_DIR` is set), and an
optional **read-only Prometheus** endpoint (disabled unless
`NETPILOT_AI_LAB_PROMETHEUS_URL` is set). Every source is converted into one
validated `LabDataset`, and its identity — Simulated / Historical / Uploaded /
Live — is carried into the result, the stored history row and the exports.
Synthetic data is labelled *not real-network traffic* wherever it appears; an
upload's provenance is recorded as an unverified operator claim.

**Tasks.** Anomaly detector comparison (6 detectors, labelled scoring when
truth exists, agreement diagnostics, threshold-sensitivity curve, opt-in
multivariate pass); forecast comparison (5 models + a marked ensemble blend,
all scored on identical chronological holdout timestamps, MAE/RMSE/R²/MAPE/
sMAPE/MASE/skill-vs-naive, holdout-calibrated bands); fault classification
(**hard-gated on valid labels** — never faked — stratified or chronological
split, confusion matrix, balanced accuracy / macro-F1 / per-class support plus
a majority baseline); statistical analysis (distribution, trend, autocorrelation,
correlation + cross-correlation peak, Mann-Whitney labelled-group test) with the
standing note that correlation does not establish causation.

**Safety posture.** Anything the data cannot support is **refused** with a
status and a concrete reason rather than a fabricated number. Runs persist
additively to a new `lab_experiments` table (created on next start; no existing
table altered) and never duplicate raw samples. Exports neutralise spreadsheet
formula injection and HTML-escape names. The lab imports nothing from the
execution layer, cannot reach a device, and does **not** enable
`NETPILOT_CHANGE_LOOP_ENABLED` or `NETPILOT_LAB_EXECUTION_ENABLED` — those
gates are asserted off before and after every task in the tests.

**No new dependencies**: pandas, scikit-learn, scipy, plotly and httpx were
already required by `requirements.txt`.

**Unverified integration**: the live Prometheus path is covered only against a
mocked HTTP transport — it has never been exercised against a real Prometheus
server, so it is documented as a boundary, not as working live operation.

## 17. Interface navigation and workflows

The Streamlit sidebar groups existing pages into **Overview**, **Network
operations**, **AI lab**, **Agentic Operations**, and **System status**. AI Lab
unifies source selection, data quality, statistics, anomaly detection,
forecasting, experiment history and exports. Network assistant also has a
bounded read-only tool workflow over the active AI Lab dataset and stored
topology. The Ops Center retains the structural Digital Twin report and human
change-review workflow. No user-editable settings page is currently provided.

See [`docs/UI_REDESIGN.md`](docs/UI_REDESIGN.md) for the page-to-feature map,
workflow guide, shared UI modules and current interface limitations.

