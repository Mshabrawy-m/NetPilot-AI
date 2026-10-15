# NetPilot AI — Phase 2 Implementation Specification
## Network Data Analysis, Anomaly Detection, and Forecasting

**Audience:** Vibe Coder / Implementation Engineer  
**Project:** NetPilot AI  
**Phase:** 2 of 4  
**Prerequisite:** Phase 1 Network Foundation & Data Collection  
**Primary goal:** Turn Phase 1's stored network measurements into a reliable, testable analytics layer for historical analysis, anomaly detection, forecasting, and predictive alerts.

---

## 1. Phase 2 Objective

Build a dependable network analytics subsystem on top of the existing Phase 1 codebase. It must analyze actual time-series measurements, calculate valid network utilization rates from cumulative counters, detect unusual behavior, forecast selected metrics, and present the results in the existing Streamlit dashboard.

Phase 2 is an analytics milestone, **not** the agentic automation milestone. Do not implement the Groq-powered multi-agent workflow, automatic configuration changes, or production network control in this phase.

### Expected workflow

1. Collect measurements using the existing Phase 1 collector.
2. Validate, normalize, and aggregate time-series data.
3. Calculate derived metrics where the source measurements support them.
4. Explore historical behavior and identify data-quality problems.
5. Detect anomalies and produce explainable alerts.
6. Forecast supported metrics using chronological evaluation.
7. Display results, confidence/uncertainty where available, and model evaluation metrics.
8. Persist results so they can be inspected and later consumed by Phase 3.

---

## 2. Mandatory Rules and Constraints

1. **Preserve Phase 1.** Review the current repository before editing. Reuse its database, models, configuration, collection service, and Streamlit conventions. Do not rewrite working modules unnecessarily.
2. **Do not fabricate live measurements.** Missing values must remain missing. Never substitute zero for unavailable metrics unless zero is the documented and measured value.
3. **Label data provenance.** Preserve `data_source` or the project's existing equivalent. Clearly distinguish live/lab data from demo data in queries, charts, model training, evaluations, and reports.
4. **Demo data is not real-world evidence.** Demo mode may be used for UI and pipeline smoke tests. Do not present metrics evaluated on demo fixtures as proof of real network forecasting or detection performance.
5. **Prevent temporal leakage.** Split by time, not random row shuffling. Fit scalers, imputers, feature selection, and model parameters on training data only.
6. **Keep the system safe.** Phase 2 must not apply configurations, alter routing, run arbitrary shell commands, or connect to production networks.
7. **Keep the stack free/open-source where practical.** Reuse Python, pandas, NumPy, scikit-learn, statsmodels, and the current Streamlit/database stack. Do not add paid APIs or hosted services.
8. **Make results reproducible.** Set random seeds where relevant, record model/configuration versions, evaluation windows, and the data source.
9. **Handle insufficient data honestly.** If a series is too short, too sparse, or too irregular for a requested analysis, display a clear “insufficient data” status instead of returning misleading results.
10. **Do not claim tests or end-to-end checks passed unless they were actually executed.**

---

## 3. Inspect Phase 1 Before Implementation

Start by reading the repository and README. Confirm the actual names and schemas of:

- `metric_samples`
- `interfaces`
- `devices`
- `collection_runs`
- `incidents`
- The metrics collector and collection service
- Database connection/repository helpers
- Streamlit pages and shared UI utilities
- `data_source` handling and demo/live database separation
- `.env.example`, configuration handling, and test setup

Use the existing schema as the source of truth. If a schema change is required, add a migration or safe, documented initialization change compatible with existing Phase 1 databases. Do not silently drop or recreate user data.

### Phase 1 acceptance caveat

The delivery report says Docker and Containerlab were unavailable on the original Windows host, so live-lab deployment was not verified end-to-end there. Do not assume live lab data is available. Phase 2 should work with stored data and should report insufficient data gracefully. If a Linux/WSL2 Containerlab environment becomes available, verify the live pipeline using the Phase 1 README checklist.

---

## 4. Scope A — Data Quality and Time-Series Preparation

Implement a reusable analytics data-access and preparation layer, separate from Streamlit UI code.

### Required capabilities

- Query measurements by metric, device, interface, source, and time window.
- Parse timestamps consistently. Phase 1 documents naive UTC timestamps; preserve that convention or migrate consistently and document any change.
- Sort observations chronologically and handle duplicate timestamps deterministically.
- Detect missing values, gaps, irregular intervals, duplicates, and stale measurements.
- Distinguish a missing sample from a measured zero.
- Avoid silently interpolating long gaps. If short-gap interpolation is supported, make it configurable and label imputed values.
- Support configurable resampling/aggregation intervals where appropriate.
- Produce a data-quality summary for each series:
  - Total observations
  - Start/end timestamps
  - Expected versus observed intervals, where the sampling schedule is known
  - Missing-value count
  - Duplicate count
  - Gap count and longest gap
  - Data source
  - Readiness status for analysis, anomaly detection, and forecasting

### Acceptance criteria

- No future observation is used to fill or transform an earlier observation in a way that leaks future information into evaluation.
- NULL samples remain NULL unless a documented, explicitly enabled transformation is applied.
- Tests cover duplicates, missing values, irregular intervals, short series, and empty queries.
- The UI clearly explains why a series is not ready when data is insufficient.

---

## 5. Scope B — Derived Network Metrics

Phase 1 records cumulative interface counters such as `rx_bytes`, `tx_bytes`, errors, and drops. Implement rate calculations using consecutive valid samples.

### Required derived metrics

Where the source data is available:

- Receive throughput / rate (bps)
- Transmit throughput / rate (bps)
- Receive/transmit error-rate or delta counts
- Receive/transmit drop deltas
- Optional utilization percentage only when a valid interface capacity/speed is known
- Rolling summaries such as mean, median, minimum, maximum, and standard deviation

For two valid samples:

- `delta_bytes = current_counter - previous_counter`
- `elapsed_seconds = current_timestamp - previous_timestamp`
- `rate_bps = (delta_bytes * 8) / elapsed_seconds`

### Counter-reset and data-gap handling

- If a counter decreases, do not treat the negative delta as real traffic. Mark it as a counter reset/wrap candidate and omit or specially handle that interval.
- If elapsed time is zero or negative, reject the rate calculation.
- If the interval exceeds a configurable maximum gap, mark the rate as unreliable or unavailable.
- Do not assume interface capacity. Utilization percentage must be unavailable unless capacity is known and valid.
- Document whether the implementation can distinguish a counter reset from a counter wrap; if it cannot, label the result accordingly.
- Keep original raw measurements unchanged.

### Acceptance criteria

- Unit tests verify the rate formula and unit conversion.
- Tests cover zero elapsed time, negative elapsed time, counter decrease/reset, large gaps, NULL counters, and normal traffic.
- Derived metrics are traceable to their source samples and timestamps.

---

## 6. Scope C — Exploratory Data Analysis (EDA)

Create a reusable EDA service and a Streamlit page or a clearly integrated section called **Data Analysis**.

### Dashboard features

- Select data source, device, interface, metric, and date range.
- Plot raw time-series data and derived rates.
- Display summary statistics and data-quality indicators.
- Show metric distributions and correlations only where statistically meaningful.
- Show missing-data and sampling-gap summaries.
- Compare devices/interfaces only when their metric definitions and units are compatible.
- Make units and aggregation intervals visible on charts.
- Preserve gaps in plots rather than drawing misleading continuous lines through missing periods.
- Avoid charting demo and live measurements together without an explicit filter and clear labeling.

### Acceptance criteria

- Charts are based on database queries, not hard-coded demonstration values.
- Empty or filtered-out results produce helpful messages rather than exceptions.
- The UI identifies the selected data source and time range.
- EDA calculations have unit tests for core summary outputs.

---

## 7. Scope D — Anomaly Detection

Implement a small, interpretable baseline first. Avoid a complex deep-learning detector unless the data volume and evaluation evidence justify it.

### Initial methods

Provide at least two configurable approaches, selected based on data availability:

1. **Robust rolling baseline:** rolling median and MAD or another robust, documented threshold method.
2. **Statistical baseline:** rolling z-score or a suitable simple statistical detector.

An optional Isolation Forest implementation may be added only if it is tested and clearly separated from the required baseline. It must not replace interpretable baselines by default.

### Anomaly record

Persist each detected event using the existing incidents table if it supports the fields; otherwise add a compatible table or migration. Record at minimum:

- Unique event identifier
- Device/interface/metric, where applicable
- Detection timestamp and observed interval
- Observed value
- Expected baseline or threshold
- Severity
- Detector name and version/configuration
- Short explanation of why the point was flagged
- Data source
- Status (for example, open/resolved) if supported
- Link to relevant source measurements, where practical

Avoid creating a new incident for every collection cycle when the same continuous anomaly remains active. Implement deduplication/grouping or a documented event lifecycle. Do not automatically mark a real incident resolved solely because a sample is missing.

### Alerting and interpretation

- Display anomaly events in the dashboard with filters and event details.
- Make clear that an anomaly is unusual behavior, not proof of a failure or attack.
- Show the threshold/baseline and relevant time-series context.
- Do not generate unsupported root-cause claims.
- No external email, SMS, or paid notification integration is required.

### Evaluation

When labeled anomaly ground truth exists, report Precision, Recall, F1, and False Alarm Rate with a documented event/point matching rule. When labels do not exist, report descriptive counts and perform clearly labeled qualitative/synthetic unit tests; do not claim supervised detection accuracy from unlabeled live data.

### Acceptance criteria

- Tests cover normal values, spikes, sustained deviations, missing samples, insufficient history, and repeated-anomaly deduplication.
- Thresholds are configurable and documented.
- Detection output is reproducible for fixed input/configuration.

---

## 8. Scope E — Forecasting

Implement a modest, defensible forecasting baseline for supported numeric time series. Forecasting must be optional when data is insufficient.

### Forecasting targets

Prioritize metrics that are actually stored and have adequate temporal coverage, such as:

- Receive/transmit throughput derived from counters
- RTT/latency
- Packet loss
- CPU/RAM utilization, only if collected and stored
- Other valid metrics supported by the existing schema

Do not forecast metrics that are not actually measured. Do not present interface byte counters themselves as traffic rates without deriving rates first.

### Required baseline models

Implement at least:

1. **Naive forecast:** last observed value.
2. **Seasonal naive**, only when the sampling frequency and seasonality are defensible.
3. **One statistical or machine-learning model:** for example, Exponential Smoothing/ETS or a suitable scikit-learn regression baseline.

Keep the model set small and explainable. Avoid adding deep learning in this phase unless there is a strong, documented reason and sufficient data.

### Forecasting protocol

- Use chronological train/validation/test partitions, or rolling-origin evaluation where practical.
- Never shuffle time-series rows randomly.
- Tune model choices on training/validation data only; reserve the test period for final evaluation.
- Fit all preprocessing steps using training data only.
- Compare models on the same target timestamps and same evaluation horizon.
- Report sample counts, split boundaries, forecast horizon, sampling interval, metric, and data source.
- State how missing intervals are handled.
- Ensure each forecast is based only on information available before its forecast origin.
- Do not report test metrics when the test segment is too short or empty.

### Metrics

Report MAE and RMSE at minimum. Report R² only when appropriate for the sample size and target variance. Include a baseline comparison and sample count. If possible, provide prediction intervals or uncertainty bands for models that support them; explain their limitations.

### Forecast persistence

Store forecasts and evaluations in a separate table or tables, or use the project's established pattern if one exists. Include:

- Model name/version and configuration
- Target metric/device/interface
- Forecast origin timestamp
- Target timestamp
- Forecast horizon
- Predicted value
- Lower/upper interval, if available
- Data source/training dataset reference
- Evaluation split/version
- Actual value and error only after the target timestamp is observed

Do not overwrite raw metric samples with forecasts. Clearly distinguish predictions from observations in the database and UI.

### Predictive alerts

A simple rule may flag a forecast that is expected to cross a configured threshold. Label it **forecast-based warning**, not a confirmed incident. Show forecast horizon, predicted value, threshold, and model performance/limitations. Do not automatically execute network changes.

### Acceptance criteria

- Tests verify time ordering, leakage prevention, baseline predictions, forecast persistence, and evaluation metrics.
- A naive baseline is always available for comparison.
- Models are compared on identical target timestamps.
- Insufficient or poor-quality data produces a clear status rather than a misleading forecast.
- The UI visually distinguishes historical observations, predictions, and optional uncertainty intervals.

---

## 9. Database and Architecture Requirements

Keep analytics logic separate from Streamlit pages. Suggested modules below are a guide; adapt them to the repository rather than duplicating existing modules.

```text
analytics/
  __init__.py
  data_quality.py
  time_series.py
  derived_metrics.py
  eda.py
  anomaly_detection.py
  forecasting.py
  evaluation.py
  schemas.py
services/
  analytics_service.py
  anomaly_service.py
  forecast_service.py
pages/
  7_Data_Analysis.py
  8_Anomalies.py
  9_Forecasting.py
tests/
  test_data_quality.py
  test_derived_metrics.py
  test_eda.py
  test_anomaly_detection.py
  test_forecasting.py
  test_temporal_splits.py
  test_analytics_persistence.py
```

This structure is illustrative. Prefer the existing project's conventions and avoid duplicate abstractions.

### Storage principles

- Reuse Phase 1 SQLAlchemy/database repository patterns.
- Add schema changes safely and document them.
- Add appropriate indexes for time-based and device/metric queries.
- Use UTC consistently and document timestamp semantics.
- Store model/evaluation metadata so results can be reproduced.
- Keep demo data separated from live data as Phase 1 does.
- Do not store secrets, API keys, or unnecessary raw sensitive payloads.

---

## 10. Streamlit UI Deliverables

Extend the existing NetPilot AI dashboard with the following pages or equivalent sections:

### A. Data Analysis
- Source, device, interface, metric, and time-range filters
- Raw and derived time-series charts
- Summary statistics and data-quality report
- Visible units and sampling interval
- Honest empty/insufficient-data states

### B. Anomalies
- Anomaly event list and filters
- Severity, timestamp, device/interface, metric, observed value, threshold, and explanation
- Time-series context for a selected event
- Clear distinction between demo and live events
- No unsupported claim that an anomaly is an attack or confirmed failure

### C. Forecasting
- Select target metric/device/interface and forecast horizon
- Select among implemented baseline models
- Historical observations and future predictions on a clear chart
- Forecast evaluation table with MAE/RMSE and sample counts
- Baseline comparison and uncertainty interval where supported
- Data readiness and model limitations
- Clear labels for forecast-based warnings

The app must remain responsive and stable. Do not run long training jobs on every Streamlit rerun. Cache only where cache invalidation is safe, and expose explicit controls to run or refresh analyses. Do not create uncontrolled background threads.

---

## 11. Testing Requirements

Add automated tests without weakening or deleting the existing Phase 1 tests.

### Required test groups

1. **Regression:** all Phase 1 tests continue to pass.
2. **Data quality:** empty, short, irregular, duplicate, stale, and missing series.
3. **Derived metrics:** throughput units, counter resets, invalid intervals, and gaps.
4. **Leakage:** preprocessing is fitted only on training data; evaluation timestamps occur after training timestamps.
5. **Forecasting:** naive model, selected model, evaluation metrics, identical comparison timestamps, and insufficient data.
6. **Anomaly detection:** spikes, sustained deviations, repeated events, missing data, and configuration changes.
7. **Persistence:** results can be stored and retrieved; raw samples are not modified.
8. **Data provenance:** demo/live separation is preserved end-to-end.
9. **UI smoke:** all new and existing Streamlit pages execute without exceptions.
10. **Reproducibility:** fixed input and configuration produce repeatable outputs where the chosen method supports it.

Use synthetic fixtures for deterministic unit tests, but label them as synthetic and do not use them as evidence of real-world performance.

### Commands

Document the actual commands used to run the suite. At minimum, run:

```bash
pytest
```

Run the project's UI smoke test if present and extend it to cover new pages. Report actual pass/fail counts and any skipped tests. Do not hide failures or modify tests simply to make them pass.

---

## 12. Security and Operational Guardrails

- No production-network access or production changes.
- No configuration deployment, route modification, firewall modification, or automatic remediation.
- No arbitrary shell execution added to analytics.
- No external service or API required for Phase 2.
- Keep demo/live databases separated.
- Validate user-selected filters and date ranges.
- Avoid exposing credentials or sensitive configuration in logs and exception messages.
- Bound query sizes and chart data volumes to keep the dashboard usable.
- Do not allow a missing metric or stale data to silently trigger a confident alert or forecast.

---

## 13. Explicitly Out of Scope for Phase 2

Do **not** implement these yet:

- Groq LLM integration
- LangGraph or multi-agent orchestration
- Intent parsing or natural-language network commands
- AI-generated network configurations
- Batfish verification workflows
- Digital Twin change simulation
- Human approval workflow for network changes
- Automated configuration deployment or rollback
- MCP tools
- Production network access
- Paid APIs or hosted AI services

These belong to later phases. Phase 2 should expose clean, documented analytics interfaces that Phase 3 can call.

---

## 14. Definition of Done

Phase 2 is complete only when all applicable conditions below are satisfied:

- [ ] Existing Phase 1 behavior and tests are preserved.
- [ ] Data-quality reporting works on stored live and demo datasets.
- [ ] Derived throughput rates use valid counter deltas and elapsed time.
- [ ] Counter resets, invalid intervals, and data gaps are handled safely.
- [ ] The Data Analysis UI uses database-backed measurements and displays units/source.
- [ ] Anomaly detection has at least two documented baseline approaches or a documented reason why only one is supported for a given series.
- [ ] Anomaly events are explainable, persisted, and deduplicated/grouped appropriately.
- [ ] Forecasting includes a naive baseline and at least one additional suitable model.
- [ ] Forecast evaluation uses chronological splits and identical target timestamps for comparisons.
- [ ] MAE/RMSE and evaluation sample counts are reported honestly.
- [ ] Forecasts are persisted separately from observed samples.
- [ ] Demo data is never presented as real-world evaluation evidence.
- [ ] New automated tests pass; all existing Phase 1 tests still pass.
- [ ] Streamlit UI smoke checks pass for all pages.
- [ ] README documents setup, data requirements, methods, limitations, and commands.
- [ ] A delivery report lists implemented files, actual test results, examples, limitations, and any unverified end-to-end items.
- [ ] No unsupported claims are made about live network validation or model accuracy.

---

## 15. Required Handover Report

At completion, provide a Markdown delivery report containing:

1. Environment and repository inspection summary.
2. Features implemented.
3. Exact files created or changed.
4. Database/schema changes and migration instructions.
5. Data sources and available metrics.
6. Derived-metric formulas and counter-reset handling.
7. Anomaly methods, thresholds, and event lifecycle.
8. Forecast models, horizons, split dates, sample counts, and baseline comparisons.
9. Actual MAE/RMSE results only for valid held-out evaluation data.
10. Test commands and exact pass/fail/skip counts.
11. Screenshots or a documented UI smoke-test alternative if screenshots cannot be captured.
12. Limitations, insufficient-data cases, and unverified live-lab steps.
13. Confirmation that Phase 3 agentic AI and network changes were not started.

---

## 16. Recommended Execution Order for the Vibe Coder

1. Inspect the repository, run all Phase 1 tests, and record the baseline.
2. Map the existing database schema and metric semantics before coding.
3. Implement data-quality summaries and tests.
4. Implement derived metrics with counter-reset and gap handling.
5. Add the Data Analysis UI.
6. Implement anomaly baselines, event persistence, deduplication, and tests.
7. Implement chronological forecasting evaluation and naive baseline first.
8. Add one additional suitable forecasting model and compare on identical target timestamps.
9. Add Forecasting UI and forecast-based warnings.
10. Run regression tests, analytics tests, and UI smoke tests.
11. Update README and produce the handover report.
12. Review the final diff to confirm no Phase 1 safety guardrails were removed and no Phase 3 scope was added.

**Final instruction:** Prioritize correctness, traceability, and honest reporting over model complexity. A simple baseline evaluated correctly is preferable to a sophisticated model evaluated with leakage or insufficient data.
