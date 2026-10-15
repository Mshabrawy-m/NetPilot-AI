# NetPilot AI — Phases 1–3: Network Foundation, Data Analysis & Multi-Agent AI Assistant

A working virtual network lab (Containerlab + FRRouting), real periodic
network-data collection, a replaceable storage layer (SQLite by default,
PostgreSQL optional), a Streamlit dashboard for inspecting topology,
device/interface status, connectivity tests, and metric history — Phase 2
analytics: data-quality reporting, derived throughput from cumulative
counters, EDA (Data Analysis), explainable anomaly detection and
chronological forecast evaluation — and Phase 3: an evidence-grounded,
Arabic/English **AI Network Assistant** (Groq + LangGraph multi-agent
workflow) that investigates, diagnoses, plans and *proposes* configuration
for **human review**, gated by an independent deterministic verifier.

> **Phase 3 scope boundary.** The assistant **only proposes**. It never
> connects to a production network, never executes or deploys a
> configuration, never modifies routes/ACLs/firewalls/interfaces, and
> requires deterministic verification before anything is labelled
> *statically verified*. Deployment, post-change verification and rollback
> are **Phase 4** and are intentionally absent (there is no apply code path
> to remove).

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
│   ├── 7_Data_Analysis.py      # Phase 2 EDA: quality, distributions, derived rates
│   ├── 8_Anomalies.py          # Phase 2 anomaly events + explanations + context
│   ├── 9_Forecasting.py        # Phase 2 model comparison + forecasts + warnings
│   ├── 10_AI_Network_Assistant.py  # Phase 3 chat, evidence, verdict & review
│   └── 11_Ops_Center.py        # v2: alerting, graph risk, insights, change loop, access
├── ai/                         # Phase 3 multi-agent core (LangGraph workflow)
│   ├── groq_client.py          # single Groq SDK choke point (timeouts, retries)
│   ├── schemas.py              # typed Pydantic schemas for every model output
│   ├── prompts.py              # system prompts + <untrusted_data> wrapping
│   ├── state.py                # typed workflow state (TypedDict)
│   ├── graph.py                # LangGraph orchestration + routing (NO deploy node)
│   ├── agents/                 # intent, investigation, diagnosis, planner, config
│   ├── tools/                  # read-only evidence tools (topology/telemetry/…)
│   ├── verification/           # independent deterministic verifier + policy checks
│   └── simulation/             # Digital Twin interface (honestly reports not_run)
├── analytics/                  # Phase 2 computation core (no UI, no SQL writes)
│   ├── time_series.py          # frame loading, dedupe, resample, labelled gaps
│   ├── data_quality.py         # quality report + readiness gates per series
│   ├── derived_metrics.py      # counter → rate/delta with reset & gap handling
│   ├── eda.py                  # summaries, distributions, gated correlations
│   ├── anomaly_detection.py    # rolling MAD + z-score baselines (+ optional IF)
│   ├── forecasting.py          # naive, seasonal naive, damped Holt, ridge-lag
│   └── evaluation.py           # chronological splits, MAE/RMSE/R², leakage check
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
│   ├── anomaly_service.py      # detect → group/dedupe → persist lifecycle
│   ├── forecast_service.py     # split → compare → persist → backfill actuals
│   ├── ai_workflow_service.py  # Phase 3: drive a run + persist its audit trail
│   └── app_state.py            # shared Streamlit page bootstrap
├── lab/
│   ├── topology.clab.yml       # Containerlab topology (single source of truth)
│   └── configs/{R1,R2,R3}/     # FRR frr.conf + daemons per router
├── tests/                      # pytest suite + headless UI smoke check
├── tools/                      # one-off utilities (incl. optional live Groq check,
│                               #   secret_scan_check.py used by CI)
├── .env.example                # configuration template (no secrets)
├── requirements.txt
└── pytest.ini
```

**v2 platform additions** (advanced capabilities roadmap, Slices 1–8) live
alongside the Phase 1–3 tree, additively — nothing above was rewritten:

```
├── app.py / pages/…            # + Ops Center (11) new page
├── analytics/                  # + graph.py, ml_anomaly.py, rca.py, backtest.py
├── services/                   # + alert_service, auth, change_service, rag,
│                               #   nlquery, telemetry (in-process metrics)
├── api/                        # opt-in read-only FastAPI REST + /ws/topology
│   ├── app.py                  #   (standalone; no Streamlit import)
│   └── readmodels.py           #   thin JSON read-models over the repository
├── Dockerfile                  # one image → dashboard OR api (NETPILOT_SERVICE)
├── docker/entrypoint.sh        #   selects streamlit vs uvicorn; passthrough
├── docker-compose.yml          # db(Postgres) + dashboard + api (+ lab profile)
├── .dockerignore               # keeps .env / data / venv out of the image
└── .github/workflows/ci.yml    # secret gate + pytest matrix + UI smoke + docker
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
| PostgreSQL (optional) | production-grade storage | otherwise the built-in SQLite is used |

If Docker/Containerlab are unavailable on your machine, the dashboard still
runs fully in **clearly labelled demo mode** (see §5) — the spec explicitly
allows this, and demo data is stored in a *separate* database file and tagged
`data_source='demo'` everywhere, so it can never masquerade as a real
measurement.

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
without Docker — it is never presented as real network data.

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

What works on Cloud, verified in Demo Mode with an empty, freshly created
database: all 12 pages, charts, topology/graph intelligence, anomaly detection,
forecasting and backtests, the offline natural-language query, RAG search, the
alert cycle and the change-proposal workflow. `data/` is git-ignored, so on the
first launch the app creates its SQLite file (including the parent directory) and
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
  data), and `NETPILOT_CHANGE_LOOP_ENABLED` must stay unset — `apply` therefore
  refuses by design, with no executor to inject.

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
| `NETPILOT_MAD_THRESHOLD` / `NETPILOT_ZSCORE_THRESHOLD` | `3.5` / `3.0` | detector sensitivity |
| `NETPILOT_ANOMALY_GROUP_WINDOW_SECONDS` | `300` | consecutive anomaly points group into one event |
| `NETPILOT_FORECAST_HORIZON` | `10` | default forecast horizon (steps) |
| `GROQ_API_KEY` | *(empty)* | Phase 3 — read **only** from env / git-ignored `.env`; never hard-coded, logged or stored |
| `GROQ_MODEL` | `openai/gpt-oss-20b` | Phase 3 — model id; **must exist on your Groq account** (catalog changes over time) |
| `GROQ_TIMEOUT` | `30` | Phase 3 — per-call timeout (seconds) |
| `GROQ_MAX_RETRIES` | `2` | Phase 3 — **bounded** retries for transient errors (capped at 5) |
| `NETPILOT_AI_REQUEST_MAX_CHARS` | `4000` | Phase 3 — hard cap on the incoming request length |
| `NETPILOT_AI_EVIDENCE_MAX_ROWS` | `50` | Phase 3 — bound on evidence rows gathered per run |
| `NETPILOT_AI_EVIDENCE_MAX_BYTES` | `8192` | Phase 3 — bound on each evidence record's content |
| `NETPILOT_AI_GRAPH_MAX_STEPS` | `12` | Phase 3 — orchestrator step ceiling (bounded loops) |

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
pytest                 # 311 unit/integration tests (201 Phase 1-3 + 110 v2)
python tests\ui_smoke_check.py   # headless run of app.py + all 11 pages,
                                 # clicking the detection/forecast buttons
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
  execution → parsing → storage (`test_connectivity.py::test_selftest_ping...`)

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
- forecasting: naive/seasonal/Holt/ridge mathematics, refusal on
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
- CI secret-hygiene scanner: clean on the real tree, catches planted secrets,
  ignores placeholders/fakes (`test_secret_scan.py`)
- multi-layout topology: pure layout engines (layered/force/circular/grid)
  + degree sizing + a demo-only large synthetic fabric
  (`test_topology_views.py`)

Last recorded result on this machine (Windows, no Docker): **311 passed**,
UI smoke check: all 12 scripts (app + 11 pages incl. the AI Assistant and Ops
Center) render without exceptions + both Phase 2 action buttons verified +
demo banner verified. A **live** Groq integration check
(`tools/phase3_live_check.py`) was run separately with the real key and
classified both an English question and an Arabic change request correctly.

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
* `anomaly_detection.py` — two explainable baselines: **rolling MAD**
  and **rolling z-score**, both on strictly-past shifted windows (no
  self-masking, no future leakage); optional Isolation Forest; every
  point carries baseline, thresholds and a plain-language explanation.
* `forecasting.py` — **naive** (mandatory baseline), **seasonal naive**
  (refuses without ≥2 full seasons), **damped Holt** (pure-NumPy,
  deterministic grid — statsmodels is blocked by this machine's Windows
  App Control), **ridge-lag** (scikit-learn, scaler fitted on train
  rows only).
* `evaluation.py` — chronological train|val|test split (never shuffled),
  `validate_split_order` leakage guard, MAE/RMSE/R².

**Services & persistence:** `services/anomaly_service.py` groups
consecutive anomaly points into one deduplicated event (occurrence/peak
escalation), resolves events **only** via a real normal observation
(missing data never resolves anything); `services/forecast_service.py`
compares all models on identical held-out timestamps, persists runs +
future predictions in dedicated tables, and backfills actuals once an
observation exists. Raw `metric_samples` are never modified, and
demo/live provenance is preserved end-to-end (`data_source` on every
row, filter, run and event).

**New UI:** page 7 Data Analysis, page 8 Anomalies, page 9 Forecasting.
Heavy analyses (detection, forecast comparison) run **only** on explicit
button press, never on every rerun.

**Database migration:** purely additive — `anomaly_events`,
`forecast_runs`, `forecast_predictions` are created automatically on
next start (`init_db()` → CREATE TABLE IF NOT EXISTS); no destructive or
altering migration touches Phase 1 tables. Applying this Phase to an
existing Phase 1 database adds the three new tables and leaves all
stored samples untouched.

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
`UnavailableDigitalTwin` that honestly reports `not_run` (no simulator is
wired in Phase 3), so results are never misrepresented.

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

## 14. Explicitly out of scope (Phase 4+)

Automated **configuration deployment**, post-change verification, live
routing/behaviour simulation and **rollback** are Phase 4 and are not
implemented — Phase 3 stops at a reviewed proposal. The assistant never
connects to a production network and never executes generated config or
modifies routes/ACLs/firewalls/interfaces. No RAG store and no MCP servers.
Anomaly events, forecast warnings and AI diagnoses remain clearly-labelled
probabilistic/statistical statements about data — never classified as
confirmed failures or attacks.

> **v2 update:** the advanced-capabilities roadmap (see
> `NetPilot_AI_Advanced_Roadmap.md`) adds a **gated, lab-only change loop**
> (Slice 7) behind the same deterministic FRR verifier: propose → approve →
> (opt-in) apply → rollback. Applying is refused by default and, from the
> dashboard, always a no-op — it needs `NETPILOT_CHANGE_LOOP_ENABLED` **and**
> an injected virtual-lab executor on a host that runs the lab. A production
> device is never reachable from this path, so the safety stance above holds.

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
