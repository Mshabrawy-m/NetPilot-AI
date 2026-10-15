# NetPilot AI — Phase 2 Delivery Report
## Data Analysis, Anomaly Detection & Forecasting

**Date:** 2026-10-12 · **Implemented by:** AI coding agent · **Spec:** `NetPilot_Phase_2_Data_Analysis_Forecasting_EN.md`

---

## 1. Environment & inspection summary

| Item | Value |
|---|---|
| OS / shell | Windows 26H2, PowerShell 5.1 |
| Python | 3.12 (project venv `.venv`) |
| pandas / numpy | 3.0.6 / 2.5.4 (pandas 3 µs datetime resolution — handled) |
| scikit-learn / scipy | 1.5.2 / 1.18.1 (import OK under App Control) |
| statsmodels | **blocked**: compiled Kalman/CFA extensions refused by Windows App Control (same class of blocker as the Phase 1 SQLAlchemy 2.1 pin) → damped Holt implemented in pure NumPy (documented decision) |
| Docker / Containerlab | not present on this machine → **no live lab data can be produced here** (Phase 1 blocker unchanged) |
| Databases | `data/netpilot.db` (live config), `data/netpilot_demo.db` (demo, WAL) — strictly separated |
| Baseline (start of Phase 2) | `pytest` → **60 passed**, 0 failed (all Phase 1 tests) |

## 2. Features implemented (spec scopes A–E + persistence + UI)

**Scope A — Data quality & preparation** (`analytics/time_series.py`, `analytics/data_quality.py`)
bounded chronological frame loader; deterministic duplicate resolution (last write wins);
resample; short-gap interpolation only when opt-in and always labelled `imputed=True`;
per-series quality report (nulls, duplicates, gaps, staleness, mixed sources) and
readiness gates: analysis ≥5, detection ≥12, forecast ≥30 non-null samples;
irregular sampling (interval CV > 1) blocks forecasting. Insufficient data returns
a status with concrete reasons — never a confident-looking number.

**Scope B — Derived throughput** (`analytics/derived_metrics.py`)
`rate = Δcounter·8/Δt` per interval; negative deltas omitted as
`counter_reset_or_wrap` (reset vs wrap indistinguishable from counters alone);
non-positive elapsed rejected; intervals > `max_rate_gap_seconds` computed but
flagged unreliable; utilization only with an explicitly supplied capacity.
Derived values are computed on demand, traceable via `start_sample_id` /
`end_sample_id`, and never written back into `metric_samples`.

**Scope C — EDA / Data Analysis** (`analytics/eda.py`, page 7)
source/device/interface/metric/range filters; raw series with gap-preserving
charts (`insert_gap_breaks`, `connectgaps=False`); derived-rate chart with
rejected-interval audit; summary statistics; gated histogram, time-of-day
profile, unit-compatibility-checked interface comparison, Pearson correlations
only on ≥12 time-aligned points; data-quality panel; explicit source + units +
observed interval on every view.

**Scope D — Anomaly detection** (`analytics/anomaly_detection.py`, `services/anomaly_service.py`, page 8)
two explainable baselines — **rolling MAD** (1.4826-scaled) and **rolling
z-score**, both on strictly-past shifted windows (no self-masking, no future
leakage); optional Isolation Forest (sklearn, seeded, causal trailing fits).
Events persist with observed value, baseline, thresholds, deviation, severity,
plain-language explanation, detector config snapshot and source sample ids.
Consecutive anomaly points **group into one event** (occurrence/peak/severity
escalation, replay idempotent); events resolve **only** via a real normal
observation after the anomaly — missing/stale data never resolves anything.

**Scope E — Forecasting** (`analytics/forecasting.py`, `analytics/evaluation.py`, `services/forecast_service.py`, page 9)
naive baseline (always available) + seasonal naive (refuses without ≥2 full
seasons) + damped Holt (pure NumPy, deterministic grid) + ridge-lag
(sklearn; scaler fitted on train rows only). Chronological train|val|test
split, never shuffled; `validate_split_order` leakage guard; all models
compared on **identical held-out timestamps**; MAE/RMSE (+R² when meaningful);
future forecasts from a refit on all in-time data; ~95% bands from each
model's own training residuals (documented √h approximation). Predictions
persist separately and actuals are **backfilled only after an observation
exists**. Forecast-based warnings are labelled and show the model's own MAE.

**Guardrails:** no Phase 3 features, no Groq/LangGraph/paid APIs, no shell
execution in analytics, bounded queries (`analytics_max_points`, 5000-point
detection lookback), heavy analyses run only on explicit button press,
demo/live provenance preserved end-to-end (`data_source` on every row,
event, run and filter).

## 3. Files created / changed

**Created (13):**

| File | Purpose |
|---|---|
| `analytics/__init__.py` | Phase 2 package |
| `analytics/time_series.py` | frame loading, dedupe, resample, gap interpolation |
| `analytics/data_quality.py` | quality report + readiness gates |
| `analytics/derived_metrics.py` | counter → rate/delta with reset & gap handling |
| `analytics/eda.py` | EDA summaries, gated distributions/correlations, gap breaks |
| `analytics/anomaly_detection.py` | rolling MAD/z-score (+ optional IsolationForest) |
| `analytics/forecasting.py` | naive, seasonal naive, damped Holt, ridge-lag |
| `analytics/evaluation.py` | chronological split, leakage guard, MAE/RMSE/R², detection scoring |
| `services/anomaly_service.py` | detection orchestration + event lifecycle |
| `services/forecast_service.py` | comparison protocol + persistence + backfill + warnings |
| `pages/7_Data_Analysis.py` | EDA dashboard |
| `pages/8_Anomalies.py` | anomaly event list/detail/context |
| `pages/9_Forecasting.py` | forecast comparison/evaluation/warnings |
| `tests/test_data_quality.py`, `tests/test_derived_metrics.py`, `tests/test_eda.py`, `tests/test_anomaly_detection.py`, `tests/test_temporal_splits.py`, `tests/test_forecasting.py`, `tests/test_analytics_persistence.py` | 84 new tests |
| `tools/phase2_demo_eval.py` | reproducible demo-dataset evaluation (throwaway copy) |

**Modified (8):** `config.py` (+10 env-overridable Phase 2 settings),
`database/models.py` (+3 additive tables), `database/repository.py`
(+10 analytics persistence functions), `requirements.txt` (scikit-learn,
scipy; statsmodels rationale comment), `.env.example` (Phase 2 defaults),
`app.py` + `services/app_state.py` (nav/title for pages 7–9),
`tests/ui_smoke_check.py` (extended: 9 pages + Phase 2 action buttons).

**Deleted:** nothing — Phase 1 files (including
`NetPilot_Phase_1_Network_Foundation_EN.md`) are untouched.

## 4. Database changes & migration instructions

Three **new additive tables** (no ALTERs, no destructive changes):

- `anomaly_events` — one grouped, explainable event per episode
  (detector config + source sample ids + lifecycle status)
- `forecast_runs` — one row per model per comparison, with full protocol
  metadata (split boundaries, n_train/val/test, MAE/RMSE/R², status
  `ok|insufficient_data|error`, config snapshot JSON)
- `forecast_predictions` — per-step future predictions
  (`run_id + target_timestamp` unique), with `actual_value/abs_error/
  backfilled_at` filled only once a real observation exists

**Migration:** none needed manually — on next start `init_db()`
(CREATE TABLE IF NOT EXISTS) adds the tables. Verified by applying this
Phase directly onto the existing Phase 1 `data/netpilot.db`: tables went
from 7 → 10, all Phase 1 rows untouched (repository tests cover
immutability; `test_raw_samples_never_modified`).

## 5. Data sources & available metrics

- `live` — collector measurements from the Containerlab/FRR lab. **Not
  producible on this machine (no Docker)**; live paths are code-complete
  and unit-tested with injected probes, same Phase 1 caveat.
- `demo` — synthetic fixtures in `data/netpilot_demo.db` (~146–148 samples
  per series at a 2-minute cadence for PC/R devices; metrics: `ping.avg_rtt_ms`,
  `ping.loss_pct`, `if.rx_bytes`, `if.tx_bytes`, `if.rx_errors`,
  `if.operstate`). Always labelled; **used for the example numbers below —
  they characterise the synthetic fixtures, not any real network.**

## 6. Derived-metric formulas & counter handling

`rate_bps = (counter_t − counter_{t−1}) × 8 / elapsed_s` for `*_bytes`
(× 1 for packet counters → count/s). Negative delta ⇒ interval omitted as
`counter_reset_or_wrap` (never guessed, never a fake zero). Elapsed ≤ 0 ⇒
rejected. Elapsed > `NETPILOT_MAX_RATE_GAP_SECONDS` (300 s) ⇒ computed but
flagged unreliable and excluded from the clean rate chart. NULL counter
readings break the chain; nothing is substituted.

## 7. Anomaly methods, thresholds, lifecycle

- Rolling MAD: value > shifted rolling median ± 3.5·1.4826·MAD(window=20,
  min_periods=window/2); abstain on degenerate (zero) scale with global-
  scale fallback.
- Rolling z-score: |value − shifted rolling mean| > 3.0·std, same window.
- Severity: warning; critical when deviation ≥ 2× threshold.
- Lifecycle: open → (grouped updates: occurrences, peak by |deviation|,
  severity escalation) → resolved **only by a subsequent normal
  observation**; a replay of the same data creates 0 new events (tested).

## 8. Forecast models, splits & comparison (demo dataset, SYNTHETIC)

Series: `PC1/ping.avg_rtt_ms` (148 stored samples, 60 s usable grid),
horizon 10. Split (chronological): **train 47 · val 11 · test 14**
(train_end 2026-10-12 08:04:11 UTC, test_end 10:09:11 UTC).

| Model | Status | MAE | RMSE | R² | n_eval |
|---|---|---|---|---|---|
| naive | ok | **0.3646** | **0.4459** | −0.025 | 14 |
| seasonal_naive | refused (insufficient data) | — | — | — | 0 (needs ≥ 2 877 samples for 2 full 24-h seasons; persisted as `insufficient_data`) |
| holt_damped | ok | 0.4118 | 0.4756 | −0.166 | 14 |

Naive wins the holdout on this noisy synthetic series — an expected,
honest result. All compared models were evaluated on the **same 14 test
timestamps**. Persistence after the run: 3 `forecast_runs` rows
(statuses `ok`, `ok`, `insufficient_data`) and 10 `forecast_predictions`
rows for the Holt run. Reproduction: `python tools\phase2_demo_eval.py`
(uses a throwaway WAL-safe copy; the demo DB is untouched).

Detection example (same synthetic dataset): `PC1/if.rx_errors@eth1` →
2 critical events (e.g. z +8.2: observed 2.00 vs rolling mean 0.056),
`PC2/if.rx_errors@eth1` → 1 warning, `PC2/ping.avg_rtt_ms` → 1 warning
(z −3.8); all subsequently **resolved by normal observations**; all other
demo series clean (0 events). These demonstrate the pipeline mechanics —
there is no labelled ground truth, so no precision/recall claim is made.

## 9. Test results (actual)

```powershell
.\.venv\Scripts\python.exe -m pytest          # 144 passed in ~21 s
.\.venv\Scripts\python.exe tests\ui_smoke_check.py   # exit code 0
```

- **Baseline:** 60 passed (Phase 1, recorded before any change).
- **Final:** **144 passed, 0 failed, 0 skipped/errors** — 60 Phase 1
  regression + 84 new (data quality 16, derived 10, EDA 15, anomaly
  detection 17, temporal protocol 7, forecasting 12, persistence 7).
- **UI smoke (Streamlit AppTest, headless alternative to screenshots):**
  app.py + all 9 pages render without exceptions; the Phase 8 "Run
  detection" and Phase 9 "Run forecast comparison" buttons were clicked
  and executed cleanly; demo banner verified → exit 0.
- Tests caught and fixed **real implementation defects** (no test was
  weakened): detector self-masking → strictly-past windows; IsolationForest
  score/offset tie → causal trailing fits; pandas 3.0 epoch math in gap
  interpolation; Ridge multi-horizon target alignment; invalid Plotly
  `fill="silhouette"` band → `tonexty`; pandas ns-discard UserWarning in
  persisted timestamps → explicit `_to_dt` conversion.

## 10. How to run

```powershell
.\.venv\Scripts\pip install -r requirements.txt
streamlit run app.py                       # live-lab config (needs Docker host)
# demo (this machine):
$env:NETPILOT_DEMO_MODE="true"; streamlit run app.py
.\.venv\Scripts\python.exe -m pytest       # 144 tests
.\.venv\Scripts\python.exe tests\ui_smoke_check.py
.\.venv\Scripts\python.exe tools\phase2_demo_eval.py   # §8 numbers, reproducible
```

## 11. Limitations, insufficient-data cases & unverified items

1. **No live-lab end-to-end verification** (no Docker/Containerlab on this
   host): all analytics were validated against the stored demo dataset and
   synthetic test fixtures. Live code paths are exercised only by injected
   fake probes in tests — identical caveat carried over from Phase 1.
2. **No labelled anomaly ground truth** → detection quality is verified
   structurally (spikes flagged / normal not / replay idempotent /
   resolution semantics), never claimed as precision/recall on real traffic.
3. **Forecast accuracy claims are not made for any real network.** §8
   numbers are holdout results on synthetic demo data; seasonal models
   require ≥2 full days of regular samples (demo window is ~2.5 h —
   honestly refused); ridge-lag requires a longer history than the demo
   provides.
4. statsmodels ETS unavailable (App Control) → NumPy damped Holt with a
   fixed deterministic grid (slightly less optimal than full ML-based ETS,
   fully reproducible; documented).
5. UI verification is headless (AppTest) — **screenshots could not be
   captured** in this environment; the documented smoke-check output in §9
   is the substitute.
6. Uncertainty bands are the documented √h approximation from training
   residual σ, not calibrated empirical intervals.
7. `uitest.db` and WAL sidecars under `data/` are test artifacts
   (throwaway), never production stores.

## 12. Phase 3 confirmation

**No Phase 3 work was started.** No LLM/Groq/LangGraph integration, no
intent parsing, no AI-generated configurations, no remediation, no MCP
tools, no paid APIs, no production network access. The analytics surface
(`analytics/*` pure functions + service orchestration returning dicts) is
deliberately Phase-3-consumable as required by spec §13.

