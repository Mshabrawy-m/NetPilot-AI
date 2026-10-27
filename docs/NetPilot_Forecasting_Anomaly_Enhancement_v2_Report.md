# Forecasting & Anomaly Detection Enhancement (v2 analytics pass) — Implementation Report

**Date:** 2026-10-27
**Branch / HEAD:** `main` / `538915f` (upstream `origin/main` = `538915f`)
**Working tree:** 17 modified tracked files, 4 new test modules and this report,
all uncommitted by instruction. Nothing was staged, committed, pushed, deployed
or reset.
**Scope:** `analytics/` (evaluation, forecasting, anomaly detection, backtest),
`services/forecast_service.py`, `services/anomaly_service.py`,
`database/models.py` + `database/repository.py` (additive only), `config.py`,
`.env.example`, `pages/8_Anomalies.py`, `pages/9_Forecasting.py`, tests and docs.
**Out of scope:** no execution primitives, no lab/device I/O, no AI workflow
(`ai/`) change, no new dependency, no destructive migration.

---

## 1. Objective and outcome

Before this pass the analytics stack shipped two explainable rolling detectors
(MAD, z-score), four forecast models without a seasonal-trend model, MAE/RMSE/R²
only, Gaussian-ish hardcoded bands, and no cross-series operations.

The pass upgrades both domains end to end — engine → service → UI → tests →
docs — while keeping the three project invariants intact:

1. **Strictly-past mathematics.** Every new detector and model uses shifted
   history only; the existing leakage guard (`validate_split_order`) still
   covers the new code paths, and the new seasonal model is initialised
   causally from the first two seasons.
2. **Refuse instead of fabricate.** An undefensible number is `NULL`, never a
   plausible 0: MAPE with a zero actual, MASE on flat training history, skill
   without a naive reference, band coverage with nothing verified, a crossing
   ETA with an unknown sampling interval, a seasonal model without two full
   seasons, a detector without a usable interval.
3. **Provenance and immutability.** `data_source` still separates demo from
   live on every row, run and event; raw `metric_samples` are never modified;
   schema changes are additive nullable columns only.

Result: 5 rule-based detectors (3 new, opt-in) beside the existing optional
seeded Isolation Forest, 5 forecast models (1 new), 8 holdout
metrics (4 new), holdout-calibrated bands with *realized* coverage reporting,
an in-memory ensemble, model ranking, threshold-crossing warnings, a bounded
batch scan, an event aggregate and label-driven detector calibration — all
covered by **115 new offline tests** (suite: **813 passed**).

---

## 2. Analytics core (`analytics/`, pure computation, no UI, no SQL writes)

### 2.1 `evaluation.py` — scale-free comparators

| Function | Definition | Refusal / guard semantics |
|---|---|---|
| `mape` | mean absolute percentage error, in percent | points whose *actual* is (near) zero are **skipped**, not clipped; `NULL` when nothing survives the guard — an unavailable metric is never shown as 0 % |
| `smape` | symmetric MAPE, `100·mean(2·|f−a| / (|f|+|a|))`, bounded 0–200 % | pairs with an exactly-zero denominator skipped; `NULL` when nothing is scoreable |
| `mase` | MAE ÷ mean absolute **one-step difference of the training series** (in-sample naive); `<1` beats naive | `NULL` on a flat (or <2-point) training history — no honest scale exists |
| `skill_vs_naive` | `1 − MAE / MAE_naive` on the *same* holdout timestamps | `NULL` without a naive reference, and when the naive baseline is perfect (dividing by zero error is meaningless) |

All four keep the existing `_pairs` rule: only timestamps with both an actual
and a prediction are scored, and the number of scoreable points is reported so
a 3-of-7 evaluation can never be read as a 7-point result. `score_detection`
(precision/recall/F1 on timestamp sets) already existed and is now the engine
behind the new calibration service.

### 2.2 `forecasting.py` — Holt-Winters and calibrated bands

* **`holt_winters_forecast`** — additive, damped Holt-Winters implemented in
  pure NumPy (statsmodels remains blocked by this machine's Windows App
  Control, the same documented reason as damped Holt). Deterministic grid over
  (α, β, γ, φ); the first grid cell wins ties, so a run is reproducible.
  Level/trend/seasonal state is initialised **causally from the first two
  seasons only** — no centred moving average that would read future points.
  Refusals: unknown sampling interval, fewer than `2·m + 4` samples, no season
  length that fits the data.
* **`MODEL_KWARGS` registry** — a model is only passed the extra kwargs it
  declares (`interval_seconds`, `season_hours`, `val`, `max_lags`). This is
  what keeps `seasonal_naive`/`holt_winters` seasonal and `naive`/`holt_damped`/
  `ridge_lag` untouched, and it is asserted directly in
  `tests/test_backtest.py::test_model_kwargs_only_forwards_what_a_model_declares`.
* **Holdout-calibrated bands**
  * `holdout_band_width(abs_errors, coverage)` — nearest-rank empirical
    half-width from the model's *own validation errors*; a coverage target the
    holdout cannot support is clamped (never silently optimistic).
  * `measure_band_coverage(future, abs_errors)` — coverage actually achieved.
  * `apply_holdout_bands(..., mode=)` — `empirical`, `empirical_sqrt_h`
    (default: grows with `√h`, last step exactly `width₀·√horizon`) or
    `normal_sqrt_h` (documented Gaussian approximation, still reported next to
    the measured coverage).
  * `realized_band_coverage(rows)` — over backfilled predictions; **0 checked
    points → coverage `NULL`**, not 100 %.

### 2.3 `anomaly_detection.py` — three new detectors

| Detector | Principle | Why it earns its place |
|---|---|---|
| `ewma` | exponentially weighted mean/MAD of the **strictly-past** window (`shift(1)`), flag at `k` robust scales | reacts to drift earlier than a flat rolling window, still explainable (baseline + threshold per point) |
| `cusum` | two-sided cumulative sum with drift allowance `k` and boundary `h`; resets after a flag and across sampling gaps | catches a sustained small shift that a 3σ rolling window never flags (the fixture's +0.5-level step) |
| `seasonal_residual` | compares a point only with the **same phase** of earlier cycles (median baseline, MAD band) | a daily-rhythm metric does not spike at 03:00 for the same reason it spikes at 15:00; abstains when the season does not fit the sample |

`DEFAULT_DETECTORS` is unchanged (`rolling_mad`, `rolling_zscore`): the new
detectors are **opt-in** so no existing behaviour, event history or UI default
shifts silently. `INTERVAL_REQUIRED = ("seasonal_residual",)` — a detector that
has no interval reports an **abstention note**, and the service distinguishes
"no opinion" from "nothing wrong". Sensitivities resolve from
`SENSITIVITY_DEFAULTS` ← settings (env) unless the caller passes an explicit
override, and the value used is stored with every point
(`detector_config`), so an event remains auditable after the knob changes.

### 2.4 `backtest.py`

`_model_kwargs(name, interval_seconds, season_hours=24.0)` now routes through
`MODEL_KWARGS`, so rolling-origin backtests can exercise the seasonal models;
`holt_winters` skips (with a reason) rather than degrading when no sampling
interval is available.

---

## 3. Service layer

### 3.1 `services/forecast_service.py`

* `DEFAULT_MODELS = ("naive", "seasonal_naive", "holt_damped", "holt_winters")`;
  ridge-lag remains available explicitly.
* Every run persists `mae/rmse/r2` **plus** `mape/smape/mase/skill` and
  `band_meta_json` (band kind, nominal vs measured coverage, number of errors,
  per-step widths). `status`/`message` carry the refusal reason verbatim, and a
  skipped model stores no fabricated metrics.
* `model_ranking(runs, …)` ranks by holdout MAE; the ensemble blend is shown
  *among* the scored rows, while `best_model` remains the best **persisted,
  reproducible** run — a blend is never presented as a deployable model.
* `build_ensemble(ok_runs, …)` — inverse-MAE weights (`_inverse_error_weights`),
  members summing to 1, monotone in error, only over runs scored on identical
  timestamps; `not_applicable` when a single member qualifies; never persisted.
* `threshold_crossing(runs, threshold, …)` — direction, `certain` vs `possible`
  (band-aware), deterministic ETA in seconds, and a **refusal** when the
  sampling interval is unknown rather than a guessed clock time.
  `forecast_warnings` turns that into the UI-facing warning list.
* `backfill_actuals` + `realized_coverage(session, run_id)` — once real
  observations exist, the published bands are scored: realized coverage, the
  gap against the nominal target, MAE and signed bias. Before that, coverage is
  `NULL`.

### 3.2 `services/anomaly_service.py`

* `scan_ready_series(...)` — walks the stored-series inventory
  (`repository.get_series_inventory`: richest series first, NULLs not counted,
  filters by source/device/`min_samples`), is hard-capped by
  `settings.scan_max_series`, and **reports its own cap** (`capped`,
  `"scan capped at N of M"` note) instead of quietly doing less.
* `event_summary(...)` — open/resolved counts, critical-open, per-severity /
  per-detector / per-metric breakdowns, episode and resolution durations,
  recurring offenders, and an explicit `truncated` flag on the bounded read.
  An empty store returns `NULL` medians — "no events" is not a zero-second
  episode.
* `calibrate_detectors(frame, …)` / `calibrate_series(session, …)` — grid over
  sensitivities scored with `evaluation.score_detection` against
  **caller-supplied labels**; rows are marked `scoreable`/`labels_available`,
  un-tunable detectors are named rather than silently dropped, thin data is
  refused without a `best`, and every scored row carries the note that a
  labelled sample is *not* real-world accuracy.
* **Defect found and fixed during this pass:** `scan_ready_series` documented
  device-level-only scanning, but its per-series load passed
  `interface_name=None` alone, which `load_metric_frame` treats as "no
  interface filter" — interface rows could contaminate a device-level series.
  The scan now passes `device_level_only=True`;
  `tests/test_anomaly_service_v2.py::test_scan_walks_device_level_series_only`
  asserts the interface-only value never appears in the stored events.
* Event lifecycle is unchanged in contract and is now pinned by tests: an
  event is resolved **only** by a real normal observation, and because
  `run_detection` also resolves against the newest sample, a spike at the end
  of the data stays open (tested with two distinct fixtures, `SPIKED` and
  `SPIKE_LAST`).

---

## 4. Persistence

`forecast_runs` gained five **nullable** columns: `mape`, `smape`, `mase`,
`skill`, `band_meta_json`. They are created for fresh databases by
`init_db()` and added to existing ones by the pre-existing
`database/connection.py::_ensure_additive_columns()` helper, which only ever
issues `ALTER TABLE … ADD COLUMN` for **missing nullable** columns. Old runs
keep `NULL` for the new metrics rather than being back-filled with guesses; no
Phase 1/2 table is altered destructively and `metric_samples` is untouched.

---

## 5. Configuration

| Variable | Default | Clamp | Effect |
|---|---|---|---|
| `NETPILOT_SEASON_HOURS` | `24.0` | ≥ `1.0` | season length for `seasonal_naive`, `holt_winters`, `seasonal_residual` |
| `NETPILOT_BAND_COVERAGE` | `0.95` | `0.5`–`0.999` | nominal coverage the holdout bands aim for |
| `NETPILOT_EWMA_THRESHOLD` | `3.0` | ≥ `0.5` | EWMA sensitivity |
| `NETPILOT_CUSUM_DRIFT` | `0.5` | ≥ `0.05` | CUSUM `k` |
| `NETPILOT_CUSUM_THRESHOLD` | `4.0` | ≥ `1.0` | CUSUM `h` |
| `NETPILOT_SCAN_MAX_SERIES` | `12` | ≥ `1` | hard cap per batch scan |

Documented in `.env.example` (template only, no secrets) and in the README
configuration table. `config.get_settings()` is uncached, so tests exercise
the env path with `monkeypatch.setenv` rather than by patching objects.

---

## 6. UI

* **Page 8 — Anomalies:** store-wide KPI row (open / critical open / resolved /
  grouped occurrences / median episode / newest open) from `event_summary`,
  detector multi-select, a *Sensitivity* expander (strictly-past window, MAD,
  z-score, EWMA, CUSUM `k` and `h` sliders — defaults read from config; the
  seasonal detector uses its configured default and is retuned through the
  calibration grid), the bounded *Scan all ready series* button (its cap printed
  on the button and in the result notes), the event aggregate with
  status/severity/device/detector filters and a truncation caption, and a
  calibration panel that is honest about being unavailable without labels
  ("…calibrate, so it is not offered").
* **Page 9 — Forecasting:** model multi-select, band-method selector and history
  window; KPI row = *Best model (holdout MAE)*, *Best MAE* (with the
  skill-vs-naive hint), *Models evaluated* `ok/total`, *Holdout samples* (with
  the train·val split), *Band coverage (measured)* (with the target and the band
  kind) and *Ensemble MAE* (stating whether the blend beats its best member).
  The evaluation table ranks every run with MAE/RMSE/R²/MAPE/sMAPE/MASE/skill/
  `n_eval` and the refusal reason, "n/a" being rendered for a metric that is not
  defensible instead of 0; a success callout names the selected model and states
  it was measured on stored demo/lab samples and is **not** a general accuracy
  claim; models that did not run are listed with their honest reason. Below that:
  the band chart annotated with the calibration method, the threshold-crossing
  panel (direction + threshold + certain/possible + ETA), the backfill button and
  the realized-coverage table, and the rolling-origin backtest.

Heavy work still runs **only** on an explicit button press, never on rerun.
First-load safety: the page guards a partially absent result dict, so a refused
run renders its status instead of raising `KeyError`.

---

## 7. Measured evidence (and why one claimed improvement was dropped)

The service-level fixture is a 36-sample synthetic series on a 20-minute grid
with a 3-sample season (`NETPILOT_SEASON_HOURS=1`), split chronologically
24 train | 5 validation | 7 test.

| Model | holdout MAE | note |
|---|---|---|
| `naive` | 11.14 | mandatory baseline |
| `seasonal_naive` | 6.67 | scored on 3 of the 7 test points (its own lead-time coverage) |
| `holt_damped` | 7.07 | season-blind trend |
| `holt_winters` | 6.80 | beats both season-blind models |
| ensemble blend | 6.14 | weights ≈ naive 0.175 / sn 0.282 / hd 0.267 / hw 0.277 |

An earlier draft asserted "Holt-Winters beats naive" on a *different* fixture
size. Measured on a 14-sample holdout, `holt_winters` (10.66) actually **lost**
to `naive` (6.57) because the damped multi-step path saturates, so the claim was
rewritten to what the data supports, and the assertion that
`skill == 1 − MAE/MAE_naive` is now checked only for models that cover the whole
holdout — `seasonal_naive`'s skill is computed on the aligned subset and must
not be back-derived. This is the project's standing rule: performance claims are
measured first, then encoded, and synthetic numbers are protocol evidence, not
network evidence.

---

## 8. Tests

New modules (all offline, deterministic, no network, no key, no lab):

| Module | Tests | Covers |
|---|---|---|
| `tests/test_forecast_metrics_v2.py` | 34 | MAPE/sMAPE/MASE/skill hand-checked values, NULL refusals, scoreable coverage, band width/clamping, `MODEL_KWARGS` routing |
| `tests/test_anomaly_detectors_v2.py` | 32 | EWMA, CUSUM (incl. gap reset and a critical case), seasonal residual, strictly-past assertion per detector, abstention, registry/defaults unchanged |
| `tests/test_forecast_service_v2.py` | 23 | persistence of the new metrics + band metadata, band geometry and coverage clamping, ranking vs blend, ensemble maths and non-persistence, refusals (`skipped`/`insufficient_data`/`unknown_model`), crossing warnings, realized coverage after backfill |
| `tests/test_anomaly_service_v2.py` | 23 | inventory rules, sensitivity plumbing (kwargs + env), device-level-only scan with its cap, dedup-vs-inventory gate, event lifecycle and aggregate, calibration contract |
| `tests/test_backtest.py` (+3) | 10 | kwargs-routing unit test, `holt_winters` through the rolling-origin route, skip-without-interval |

Full suite on this machine (Windows, no Docker):

```
.\.venv\Scripts\python.exe -m pytest
→ 813 passed, 0 failed, 0 errors, 0 skipped in 117.9 s

.\.venv\Scripts\python.exe tests\ui_smoke_check.py     → exit 0
  all 12 scripts [ OK ], every action button [ OK ]
  (incl. pages/8 'Run detection', pages/9 'Run forecast comparison'),
  demo banner shown: OK
```

---

## 9. Honest boundaries — what this pass does **not** prove

1. **No real-network accuracy.** Every metric, band and detector claim above is
   measured on series generated inside the test files. They demonstrate
   mathematics, refusal behaviour and persistence contracts — not precision,
   recall or forecast skill on this lab's traffic.
2. **Seasonal models need a real cycle.** With the default 60-second collection
   interval and `NETPILOT_SEASON_HOURS=24`, a season is 1440 samples, so the
   demo/live histories available here legitimately produce `skipped` or an
   abstention note rather than a seasonal forecast. That is the intended
   behaviour, not a failure.
3. **Calibration is label-relative.** F1 here is against caller-supplied
   timestamps; the service states in every row that it is not real-world
   accuracy.
4. **Live lab, PostgreSQL, Docker, Streamlit Cloud and browser E2E were not
   re-run** in this environment (no Docker/Containerlab host). The headless
   `AppTest` smoke is the UI evidence.
5. **No action is taken on any finding.** Anomaly events and forecast crossing
   warnings remain informational; the change loop, lab executor and Digital
   Twin wiring are untouched by this pass and remain gated / `not_run` as
   documented in `README.md` §14.

---

## 10. Reproducing the verification

```powershell
# from the repository root
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt          # no new dependency was added
pytest                                    # expect: 813 passed
python tests\ui_smoke_check.py            # expect: every [ OK ], exit 0

# the pass in isolation
pytest tests\test_forecast_metrics_v2.py tests\test_anomaly_detectors_v2.py `
       tests\test_forecast_service_v2.py tests\test_anomaly_service_v2.py `
       tests\test_backtest.py

# optional: demo dashboard with the enhanced pages 8/9
$env:NETPILOT_DEMO_MODE="true"; streamlit run app.py
```

Nothing was committed or pushed by this report; the 17 modified files, the 4 new
test modules and this report remain in the working tree for review. (The
pre-existing untracked scratch files at the repository root —
`check_deployed_page.py`, `run_topology_apptest.py`, `tmp_interact.py`,
`tmp_read.py`, `tmp_read2.py` — are unrelated to this pass and were left in
place.)
