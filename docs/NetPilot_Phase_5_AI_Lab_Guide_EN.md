# NetPilot AI — Phase 5: Real & Simulated Data Analytics Lab (AI Lab)

Page: **🧪 AI Lab** (`pages/12_AI_Lab.py`) — a wizard-driven experimentation
platform that runs anomaly, forecasting, classification and statistical
analysis on top of *any* supported telemetry source, with validated
preconditions, persisted history and honest exports.

Everything in this lab is **analysis-only**. It never sends a command to a
device, never imports the lab executor, and never turns on
`NETPILOT_CHANGE_LOOP_ENABLED` or `NETPILOT_LAB_EXECUTION_ENABLED`.

---

## 1. Wizard flow

The page follows one fixed order, laid out as five tabs under a status strip
(loaded source, rows, series, last run). No step is pre-filled with sample
numbers:

1. **Data source** (tab ①) — simulator / project history / CSV upload / local
   historical file / live read-only Prometheus. A successful load queues one
   confirmation and reruns, so the status strip, the badges and every tab agree
   on what is loaded.
2. **Inspect** (tab ②) — provenance badge (Live / Historical / Uploaded /
   Simulated), KPI tiles (rows, series, labels, median interval), task gates,
   per-series inventory with readiness per task, data-quality summary, lineage
   ("what happened to the rows"), and raw values as read.
3. **Configure & run** (tab ③) — task selection (anomaly comparison, forecast
   comparison, fault classification, statistical analysis), the model/protocol
   form, and the single **Run experiment** button.
4. **Results** (tab ④) — run KPI tiles (runtime, series/rows in scope,
   outcome), supervised metrics, comparison table, charts built from the
   computed run only, "how to read these numbers", and the exports: Table CSV,
   Metrics CSV, Markdown, HTML.
5. **History** (tab ⑤) — the persisted **experiment history**, which includes
   refusals and their reasons, with stored table/metrics downloads.

A run that cannot be supported is **refused** with a status
(`refused` / `insufficient_data` / `no_labels`) and a concrete reason. A
refusal is stored and exported like any other result — it is never rendered as
a success.

---

## 2. Supported data-source types

| Source | `source_type` | What it is | Verified how |
|---|---|---|---|
| Built-in simulator | `simulated` | Deterministic synthetic telemetry **with ground-truth fault labels**. Labelled "NOT real-network traffic" everywhere it appears. | ✅ offline tests + headless UI smoke |
| Project history | `historical` | `metric_samples` this app already collected (live or demo DB). | ✅ offline tests |
| Browser CSV/TSV upload | `uploaded` | Operator-supplied file; provenance is *asserted*, not verified by the app. | ✅ offline tests + UI smoke |
| Local historical file | `historical` | File read from an **allowlisted directory** only. Disabled when `NETPILOT_AI_LAB_LOCAL_DIR` is empty (the default). | ✅ offline tests; directory guard enforced |
| Live Prometheus | `live` | Read-only instant + range queries. Disabled until `NETPILOT_AI_LAB_PROMETHEUS_URL` is set. | ⚠️ verified only through a mocked HTTP transport — **never against a real endpoint** |

Source identity is carried end to end: it is written into the dataset
provenance, into every `TaskResult`, into the persisted `lab_experiments` row
and into the header of each export. Synthetic data can never be relabelled as
real by a later step — the UI badge, the stored row and the report all derive
from one shared rule (`services.lab_experiment.is_synthetic`).

### CSV upload and column mapping

1. Choose **Upload a CSV/TSV file from the browser** and pick a `.csv`/`.tsv`
   file (delimiters `,` `;` `\t` `|` are sniffed).
2. **Preview and type guess** shows the first rows and what the parser thinks
   each column is.
3. The **mapping** panel proposes column roles (timestamp, device, interface,
   metric value columns, label column); you can override every one of them.
   The proposal is a heuristic, not a decision — nothing is loaded until you
   press **Validate and load upload**.
4. The load report states **explicitly** how many rows were rejected and why
   (unparseable timestamp, non-numeric value, duplicate timestamp+series,
   missing unit, mixed units in one series) and how many were retained.
   Rejected rows are counted, never silently dropped; a value that cannot be
   parsed stays NULL instead of becoming `0`.
5. Upload bounds (enforced, reported, never truncated silently): size
   `NETPILOT_AI_LAB_UPLOAD_MAX_BYTES`, `60` columns, `40` metric columns.

---

## 3. Generating simulated telemetry

Sidebar of step 1: **devices**, **duration (1–168 h)**, **sampling interval**
(15–900 s), **seed**, **metrics**, and the count of each injected fault event.

- **Metrics available** (unit in the dataset): `if.rx_bps` (bps),
  `if.tx_bps` (bps), `ping.avg_rtt_ms` (ms), `ping.loss_pct` (%),
  `cpu.usage_pct` (%), `memory.usage_pct` (%), `if.operstate` (link).
  `cpu.*`/`memory.*` are **simulator-only** — this project's collector does not
  measure them live, and the UI says so.
- **Fault events**: `congestion`, `latency_spike`, `packet_loss`,
  `cpu_overload`, `interface_outage`. Each injection writes a ground-truth
  label interval, so anomaly and classification runs can be scored against
  known truth.
- **Reproducibility**: the same seed + the same configuration produce byte-equal
  values (verified in tests: identical metrics, table and points; the dataset
  fingerprint matches; only the experiment UUID differs).
- **Volume bounds**: the generator *refuses* a request that would exceed
  `NETPILOT_AI_LAB_SIM_MAX_POINTS` per series, `NETPILOT_AI_LAB_SIM_MAX_DEVICES`
  devices, or `NETPILOT_ANALYTICS_MAX_POINTS` total rows. It reports the expected
  row count before you press the button rather than quietly generating a
  smaller dataset.

CLI equivalent (same code path the page uses):

```powershell
.\.venv\Scripts\python.exe -c "from analytics.lab_simulator import simulate; d=simulate(devices=('R1','R2'), duration_hours=6.0, interval_seconds=120, seed=3, events={'congestion':2,'packet_loss':2,'latency_spike':2}); print(d.profile)"
```

---

## 4. Optional live telemetry (read-only Prometheus)

Disabled by default. To turn it on you must explicitly configure it:

```powershell
$env:NETPILOT_AI_LAB_PROMETHEUS_URL = "http://your-prometheus:9090"
$env:NETPILOT_AI_LAB_PROMETHEUS_TIMEOUT = "10"
```

- The page only ever issues **read** queries (`/api/v1/query`,
  `/api/v1/query_range`) and a health check. No write/reload/admin endpoint is
  reachable from this code path.
- Bounds: `200` series, `5000` points per series, `4 MB` response, `400`
  characters per expression. Oversized responses are refused, not truncated.
- When no URL is configured the tab reports itself as **not configured /
  disabled** and states that the rest of the lab is unaffected. It never shows a
  success message in that state (checked by the headless UI smoke).
- ⚠️ **Unverified integration**: the fetch layer is covered by offline tests
  using a mocked HTTP transport only. It has never been run against a live
  Prometheus server, so real-world response shapes, auth, TLS and error
  behaviour are untested.

---

## 5. Supported tasks, models and protocols

All four tasks run on **one validated representation** (`analytics/lab_dataset.py`)
and reuse the existing Phase 2 computation core rather than re-implementing it.

### 5.1 Anomaly detection comparison
Detectors (`analytics/anomaly_detection.DETECTORS`): `rolling_mad`,
`rolling_zscore`, `ewma`, `cusum`, `seasonal_residual`, `isolation_forest`
(default selection: `rolling_mad` + `rolling_zscore`).
Per detector: flagged counts, timing, explanation, and — **when ground-truth
labels exist** — precision/recall/F1, plus a threshold-sensitivity curve and
average-precision, pairwise Jaccard agreement, and an optional multivariate
(Isolation-Forest) pass. Without labels the same detectors still run and the
result says explicitly that no quality score can be computed.
ML detectors that must refit per judged point are capped at
`NETPILOT_AI_LAB_ML_MAX_POINTS` recent samples and **say so** in the result.

### 5.2 Forecasting comparison
Models (`analytics/forecasting.FORECAST_MODELS`): `naive`, `seasonal_naive`,
`holt_damped`, `holt_winters`, `ridge_lag`, plus an ensemble blend that is
clearly marked as not-persisted and excluded from the scored table.
Defaults: `test_frac = 0.2`, `horizon` from `NETPILOT_FORECAST_HORIZON`,
`ensemble = true`, `band_coverage` from `NETPILOT_BAND_COVERAGE`.
Every model is scored on the **same chronological holdout timestamps**.
The split is strictly chronological, preprocessing is fitted on **train rows
only**, and `lab_dataset.assert_chronological` raises
`analytics.evaluation.LeakageError` if a test window is not entirely after the
train window.

### 5.3 Fault classification (label-gated)
Classifiers (`analytics/lab_classification.CLASSIFIERS`):
`logistic_regression`, `random_forest`, `gradient_boosting` (defaults: the
first two). Defaults: `test_frac = 0.25`, `split_mode = "chronological"`,
`add_lags = 0`, `random_seed = 42`.
The task is scoped to **one device** and one labelled metric.
**Hard precondition: valid labels.** With no labels the task is refused with
status `no_labels`; a single class or a class with too few rows is
`insufficient_data` with the needed counts reported. Labels are never
invented, and unlabelled feature rows are **dropped and disclosed** rather than
given a class. `split_mode` is `stratified` (random, seeded) or `chronological`
(future-only test window) and the choice is documented in the result.

### 5.4 Statistical analysis
Descriptive stats (n, mean, std, min/median/max, p95, skew, kurtosis), trend
slope/R²/direction, autocorrelation up to `lag_max`, Pearson/Spearman
correlation on time-aligned observations, cross-correlation peak, and a
Mann-Whitney U labelled-group difference test. Every run carries the standing
note that **correlation does not establish causation**, and the lagged peak is
reported as association strength only.

---

## 6. Metric definitions

Forecast accuracy (`analytics/evaluation.py`, all computed on the holdout):

| Metric | Definition / caveat |
|---|---|
| `mae` | Mean absolute error, in the series' own unit. |
| `rmse` | Root mean squared error — same unit, penalises large misses. |
| `r2` | Coefficient of determination vs the holdout mean; `None` when the holdout has no variance. |
| `mape` | Mean absolute **percentage** error. Points whose actual value is (near) zero are **skipped**, not clipped; `None` only when nothing survives the guard. Always read next to `n_eval`. |
| `smape` | Symmetric MAPE (0–200 %); bounded so a near-zero prediction cannot inflate it. |
| `mase` | Mean absolute scaled error, scaled by the training series' mean one-step difference. `<1` beats naive in-sample, `>1` is worse. |
| `skill_vs_naive` | `1 − MAE(model)/MAE(naive)` on the same timestamps; `None` if naive error is zero. |
| `n_test` / `n_train` / `n_eval` | How many points the number is actually based on. |
| band coverage | Nominal coverage target (`NETPILOT_BAND_COVERAGE`) reported next to the **measured** coverage, never as a guarantee. |

Detection / classification quality: `precision`, `recall`, `f1`, support per
class, confusion matrix (`rows = true class, columns = predicted class`),
`accuracy`, `balanced_accuracy`, `macro_f1`, `majority_baseline` (the score a
constant predictor reaches — a model that does not beat it is not a finding),
`imbalance_ratio`, and `best_criterion = balanced accuracy (macro-F1
tie-break)`.

Data picture: `n_rows`, `n_values`, `n_null_values`, `n_duplicates`,
`gap_count`, `n_stale_series`, `n_constant_series`, `observed_interval_seconds`.

---

## 7. Reproducing an experiment

Every run is persisted additively to the `lab_experiments` table (created with
`CREATE TABLE IF NOT EXISTS` on next start; no existing table is altered). The
stored record holds: `experiment_uuid`, task, status, message, source type +
label + id, `data_source` (`demo`/`live`), `is_synthetic`, dataset fingerprint,
label kind, time range, sample counts, selected series + selection bound,
features, target, split protocol, preprocessing protocol, seed, parameters,
metrics, warnings, dataset warnings, runtime, `points_stored`, created_by,
schema_version.

To reproduce a stored run exactly:

1. Open the row in **Experiment history** → its dataset fingerprint, source,
   seed, selection bound, split and preprocessing are all displayed.
2. Recreate the same source (same simulator seed/config, the same uploaded
   file, or the same DB window).
3. Re-run the same task with the same parameters. Equal fingerprint + equal
   seed ⇒ equal metrics, table and chart points.

Raw per-sample values are **not** duplicated into the lab table (the point
payload is size-budgeted and, when it exceeds the budget, dropped with
`points_stored = false` while metrics and the table survive). Reads
delimit at `NETPILOT_AI_LAB_HISTORY_LIMIT` rows. A corrupt stored JSON column
is reported as `parse_errors` instead of crashing the page.

Exports are generated from the computed run (or from a restored history row)
— never from a template:

- **Table CSV** — the comparison table, exact numbers as strings.
- **Metrics CSV** — sectioned: task, metrics, config, warnings (including the
  causation note).
- **Markdown / HTML report** — provenance and safety statement **before** the
  numbers, then fingerprint, UUID, every metric key and the full table.
  CSV/HTML cells that begin with a formula-active character are prefixed with
  an apostrophe, and HTML-escaped output cannot carry live script.

---

## 8. Known limitations and unverified items

- **Live Prometheus**: implemented and mocked-tested, **not** verified against
  a real endpoint. Until that happens it is a documented boundary, not a
  working integration.
- **Local file import**: disabled by default; enabled only by pointing
  `NETPILOT_AI_LAB_LOCAL_DIR` at an allowlisted directory. Any other path is
  refused.
- Uploads and historical imports are trusted only as far as their own
  provenance statement — the app cannot verify that an uploaded CSV really
  came from a network.
- `cpu.usage_pct` / `memory.usage_pct` exist only in the simulator; the
  collector does not measure them, so classification features built on them
  cannot be validated against a live lab here.
- Simulation reproduces *data shape*, not network behaviour: it is a
  statistical fixture, not a routing/behavioural digital twin, and no
  experiment result is evidence about a real device.
- ML detectors on long series are evaluated on a bounded recent window
  (`NETPILOT_AI_LAB_ML_MAX_POINTS`) — disclosed in the result, not silent.
- The lab does not wire the Digital Twin into the AI workflow, and it does not
  exercise the Phase 4 change loop. Both remain governed by their own
  (disabled) gates.

---

## 9. Running the application and the tests

```powershell
# from the repository root
.\.venv\Scripts\python.exe -m streamlit run app.py    # then open the "AI Lab" page
```

Demo mode (`NETPILOT_DEMO_MODE=true`) needs no lab, no credentials and no
network; the AI Lab works fully inside it.

Tests (all offline, deterministic fixtures, no internet / paid services /
live devices):

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pytest tests\test_ai_lab_dataset.py tests\test_ai_lab_simulator.py tests\test_ai_lab_sources.py tests\test_ai_lab_tasks.py tests\test_ai_lab_experiment.py -q
.\.venv\Scripts\python.exe tests\ui_smoke_check.py
```

Verified run (Windows, PowerShell, project venv):

- full suite: **966 passed, 0 failed, 0 errors, 0 skipped** in 165.2 s (13
  pre-existing `datetime.utcnow()` DeprecationWarnings from
  `services/ai_workflow_service.py`)
- AI Lab files: dataset 35 + sources 40 + simulator 21 + tasks 30 + experiment
  27 = **153 tests**
- headless UI smoke over `app.py` + all 12 pages: exit code **0**, including the
  two AI Lab checks (full simulated round-trip, and the live source staying
  honestly disabled)
