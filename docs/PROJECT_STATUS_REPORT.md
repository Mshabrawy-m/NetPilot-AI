# NetPilot AI — Project Status Report (v1.0 delivery)

**Date:** 2026-11-11
**Repository:** <https://github.com/Mshabrawy-m/NetPilot-AI>
**Branch:** `main`
**Base commit measured:** `df89716a9ad0e1b7f30d6674fd8dd95a3bafe9a3`
**Final delivery commit:** the commit tagged `v1.0`; resolve it with
`git rev-parse v1.0^{commit}`.

This report records only what was measured on the delivery tree. A value that
could not be verified is labelled **NOT VERIFIED**. The delivery finalisation
added the read-only Evaluation page, rewrote the README top, and added this
report; no write SQL was executed against Neon, and no protected user work was
committed.

## 1. Repository state

| Item | Value |
| --- | --- |
| Base commit before finalisation | `df89716` (`feat(eval): A/B/C change-request evaluation with verifier and twin`) |
| Branch / remote | `main` / `origin` = `https://github.com/Mshabrawy-m/NetPilot-AI.git` |
| Local Python | 3.12.10 (`.venv`) |
| Streamlit | 1.65.0 |
| Docker | not installed (`docker --version` not found) |
| Containerlab | not installed (`containerlab version` not found) |
| OS | Windows (`win32`) |

Files added or changed for this delivery:

- `app_pages/13_Evaluation.py` — new read-only Evaluation page.
- `tests/test_evaluation_page.py` — new AppTest coverage for the page.
- `app.py` — registers the Evaluation page in navigation.
- `tests/test_live_mode_pages.py` — adds the Evaluation page to the existing
  Live/Simulated/empty page matrix.
- `README.md` — reviewer-focused top section; detailed phase history retained.
- `evaluation/agent_eval.py` — scope note now points at the new page.
- `docs/agent_eval_summary.md` — the same scope note, updated in the committed
  summary.
- `docs/PROJECT_STATUS_REPORT.md` — this report.

Deliberately **not** committed (pre-existing user work):

- modified `ai/groq_client.py`
- modified `tests/test_forecast_metrics_v2.py`
- untracked `OMNIROUTE_SETUP.md`, `check_or.py`, `test_or.py`,
  `test_or_headers.py`

## 2. Full test suite

Command run on the delivery tree:

```powershell
.\.venv\Scripts\python -m pytest -q --junitxml=<temp>\junit_final.xml
```

| Metric | Result |
| --- | ---: |
| Exit code | 0 |
| Tests collected | 1638 |
| Failures | 0 |
| Errors | 0 |
| Skipped | 0 |
| Duration | 230.687 s (final run) |

The suite includes:

- the full analytics, database, network, agent, verifier, twin, API and UI
  unit/integration tests;
- the headless AppTest matrix in `tests/test_live_mode_pages.py` for every page
  in the Live, Simulated and empty database states;
- the new `tests/test_evaluation_page.py` for the Evaluation page in Demo,
  Simulated and Live modes.

## 3. Page status: Demo / Simulated / Live

Method: each page was executed with Streamlit's official
`streamlit.testing.v1.AppTest` harness. A page passes only when it renders with
no uncaught exception and does not show the old false gate (“Live telemetry is
unavailable because Containerlab is not installed…”).

| # | Navigation page | Script | Demo | Simulated | Live |
| ---: | --- | --- | :---: | :---: | :---: |
| 1 | Network overview | `app.py` | PASS | PASS | PASS |
| 2 | Topology | `app_pages/2_Topology.py` | PASS | PASS | PASS |
| 3 | Devices & interfaces | `app_pages/3_Devices.py` | PASS | PASS | PASS |
| 4 | Connectivity tests | `app_pages/4_Connectivity_Tests.py` | PASS | PASS | PASS |
| 5 | Metrics & history | `app_pages/5_Metrics_History.py` | PASS | PASS | PASS |
| 6 | Anomalies | `app_pages/8_Anomalies.py` | PASS | PASS | PASS |
| 7 | Operations center | `app_pages/11_Ops_Center.py` | PASS | PASS | PASS |
| 8 | AI Lab · data & forecasting | `app_pages/12_AI_Lab.py` | PASS | PASS | PASS |
| 9 | Data Analysis | `app_pages/7_Data_Analysis.py` | PASS | PASS | PASS |
| 10 | Forecasting | `app_pages/9_Forecasting.py` | PASS | PASS | PASS |
| 11 | Network assistant & tools | `app_pages/10_AI_Network_Assistant.py` | PASS | PASS | PASS |
| 12 | System status | `app_pages/6_System_Status.py` | PASS | PASS | PASS |
| 13 | Evaluation · safety & backtest | `app_pages/13_Evaluation.py` | PASS | PASS | PASS |

`app_pages/1_Overview.py` is a retained standalone duplicate of the inline
overview; it was measured separately and also passed in all three modes.

Mode definitions used for this matrix:

- **Demo:** `NETPILOT_DEMO_MODE=true`, fresh isolated SQLite database, synthetic
  fixtures seeded by `services/demo_data.py`.
- **Simulated:** `NETPILOT_DEMO_MODE=false`, schema initialised, rows seeded by
  `tools/seed_simulated.py`; where a page offers a source selector, the
  `Simulated` source was selected explicitly.
- **Live:** `NETPILOT_DEMO_MODE=false`, schema initialised, rows written through
  `tools/live_collector.run_cycle` with a **mocked ping function**. This
  exercises the live data path, but it is **not** a real external ICMP
  measurement.

The Evaluation page is mode-independent by design: it reads only
`docs/agent_eval_results.csv`, `docs/agent_eval_summary.md` and
`docs/forecast_backtest_results.csv`, and makes no database, network or LLM
call.

## 4. What is real and what is simulated

| Area | Status | Evidence |
| --- | --- | --- |
| Live telemetry path | Real ICMP probes + local host NIC counters when the collector runs | `tools/live_collector.py` writes `data_source='live'`; it refuses any other source |
| Live evidence in this finalisation | Locally seeded rows through the collector with mocked pings | AppTest Live mode; **no real external measurement was performed** |
| Simulated data | Synthetic and deterministic | `analytics/lab_simulator.py`, `tools/seed_simulated.py` |
| Demo data | Synthetic, isolated SQLite fixtures | `services/demo_data.py` |
| Containerlab lab | Topology and configs are present; not run here | Docker/Containerlab are not installed on the delivery host |
| Agent A/B/C evaluation | Offline replay of one cached recording | `tools/agent_eval.py --mode offline --no-write` printed 80 intents / 240 rows; no provider call |
| Forecast backtest | Offline export over stored series | `docs/forecast_backtest_results.csv`: 84 rows, 14 series (9 live, 5 demo), 6 models; exact export reproduction is **NOT VERIFIED** because the source database snapshot is not committed |
| Streamlit Community Cloud | Documented only | No deployment was performed in this finalisation; Cloud behaviour is **NOT VERIFIED** here |

## 5. Evaluation page contents

The new page renders, from the committed files only:

- the A/B/C comparison with Wilson 95% intervals recomputed from
  `docs/agent_eval_results.csv`;
- the per-category false-accept chart and table;
- the failure cases copied verbatim from `docs/agent_eval_summary.md`
  (twin extra failures, verifier/twin failures, safe-proposal rejections, and
  the honest interpretation);
- the forecast backtest summary: 84 model-series rows, 14 series (9 live,
  5 demo), six models (`naive`, `holt_damped`, `ridge_lag`, `hgb_lag`, `sarima`,
  `mean_ensemble`), median MASE and skill, and naive-perfect counts.

Verified rendering: four dataframes and one Plotly chart, no exception, in
Demo, Simulated and Live modes.

## 6. Streamlit Community Cloud notes

Declared repository facts (read from the files, not re-verified on Cloud):

| Item | Value |
| --- | --- |
| `runtime.txt` | `python-3.14` |
| `packages.txt` | `iputils-ping` |
| `requirements.txt` | mostly unpinned; `statsmodels==0.15.0`; `psycopg2-binary>=2.9,<3` |
| `.streamlit/config.toml` | dark theme; `server.fileWatcherType = "none"`; `browser.gatherUsageStats = false` |
| Local verification | Python 3.12.10, Streamlit 1.65.0 |

Secrets keys to set in the Cloud app (only the first is required for a public
demo; the rest are optional):

- `NETPILOT_DEMO_MODE=true` — public demo, isolated SQLite, synthetic fixtures.
- `DATABASE_URL` — optional PostgreSQL/SQLite URL; empty uses local SQLite.
- `GROQ_API_KEY` — optional; enables the AI assistant. Never commit it.
- `GROQ_MODEL` — optional model override.
- `NETPILOT_AUTH_ENABLED=true` plus `NETPILOT_ADMIN_USER` and
  `NETPILOT_ADMIN_PASSWORD` — optional login/RBAC bootstrap.
- `NETPILOT_DB_URL_APP`, `NETPILOT_DB_URL_COLLECTOR`,
  `NETPILOT_DB_URL_RETENTION`, `NETPILOT_DB_URL_ROLLUP`,
  `NETPILOT_DB_URL_ADMIN` — optional role-separated PostgreSQL URLs.

After adding or changing Secrets, use **Manage app → Reboot app** in the
Streamlit Cloud dashboard so the new environment is applied. This is the
documented Cloud procedure; it was **NOT VERIFIED** in this session because no
Cloud deployment was performed. Python 3.14 dependency resolution and the exact
Cloud runtime are also **NOT VERIFIED**.

## 7. Repository hygiene

Measured with `git ls-files` plus file sizes:

- **284 tracked files.**
- **0 tracked files over 1 MB.**
- Largest tracked file: `data/eval/change_request_intents_v1.json` at 325.8 KB.
- No tracked database, WAL/SHM, log, `.pyc`, `__pycache__`, coverage or test
  report artefacts.
- The only tracked files matching a “secret/artefact” name pattern are the
  intentional templates `.env.example` and
  `deploy/env/netpilot-collector.env.example`; both contain placeholders only.
- Local databases `data/netpilot.db` and `data/netpilot_demo.db` are untracked
  and ignored by `.gitignore` (`*.db`); `.env` is ignored as well.

No files were deleted during this hygiene check.

## 8. Known issues and limitations

1. **Live data is ICMP-only.** The collector measures ping reachability and
   local host interface counters. It does not collect SNMP, syslog, NetFlow or
   router configuration state.
2. **The digital twin models structural reachability only.** It does not model
   routing protocols, traffic, QoS or loops; a twin pass is not a safety proof.
3. **No physical or virtual router lab was run on the delivery host.**
   Docker/Containerlab are absent, so the gated apply/verify/rollback path has
   no live-lab evidence here.
4. **The forecast backtest CSV cannot be reproduced exactly from the repo.**
   The algorithm and the summary are in-tree, but the original live database
   snapshot and the export script are not committed.
5. **The agent evaluation is synthetic.** It is 80 curated intents replayed from
   one cached recording, not a production benchmark; a different model or prompt
   can move every number.
6. **Cloud deployment was not re-verified.** `runtime.txt` declares Python 3.14
   while local verification used Python 3.12.10, and most requirements are
   unpinned; exact Cloud resolution is NOT VERIFIED.
7. **The detailed README sections below the new top preserve the historical
   `pages/` layout.** The current navigation uses `app_pages/`; the new top
   describes the current layout.
8. **Protected user work remains uncommitted** (`ai/groq_client.py`,
   `tests/test_forecast_metrics_v2.py`, `OMNIROUTE_SETUP.md`, `check_or.py`,
   `test_or.py`, `test_or_headers.py`). It is not part of the v1.0 delivery.

## 9. Delivery checklist

| Check | Result |
| --- | --- |
| Read-only Evaluation page added and registered | PASS |
| AppTest for the Evaluation page | PASS (3 modes) |
| Demo / Simulated / Live page matrix | 14/14 page entries PASS in each mode (13 navigation pages + the standalone `app_pages/1_Overview.py`) |
| Full pytest suite | 1638 passed, 0 failed/errors/skipped, exit 0 |
| `compileall` | PASS (exit 0) |
| Ruff on changed files | PASS (`All checks passed!`, ruff 0.17.3) |
| Secret scan | PASS (`Secret scan clean.`, exit 0) |
| Diff reviewed | PASS (read before commit) |
| Protected files excluded | PASS |
| Annotated tag `v1.0` | created on the delivery commit and pushed |
