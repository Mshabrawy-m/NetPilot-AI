# Phase 5 — Real & Simulated Data Analytics Lab (AI Lab): Delivery Report

Task: build an integrated AI experimentation platform (real + simulated
telemetry, validated data model, four analysis tasks, runner + history +
exports, wizard UI) as **implementation, not a proposal**.
Status: **complete and verified offline**. No commit, push, tag or deploy was
performed; the change-loop and lab-execution gates stay disabled.

---

## 1. Module map

### Added

| File | Responsibility |
|---|---|
| `analytics/lab_dataset.py` | One validated telemetry contract: `LabDataset`, `SeriesRef`, `SeriesInventory`, long-frame canonicalisation (`collected_at, device, interface, metric, value, unit, label`), timestamp parsing (ISO / epoch / already-datetime), duplicate/missing/invalid/stale accounting, mixed-unit quarantine, mixed-source refusal, lineage (rejected vs retained), `SOURCE_*` + `SOURCE_LABELS` provenance wording, label normalisation (`none`/`binary`/`multiclass`), `TrainPreprocessor` (median fill + MAD scale, **train rows only**), `assert_chronological` → `LeakageError`, `task_gate()`, content fingerprint |
| `analytics/lab_simulator.py` | Deterministic multi-device telemetry generator: `SimulationConfig` (devices, duration, interval, seed, metrics, events, interface, start), `METRIC_PROFILES` (7 metrics with fixed units), `EVENT_PROFILES` (congestion, latency_spike, packet_loss, cpu_overload, interface_outage) writing ground-truth label intervals, volume bounds that **refuse** rather than shrink, provenance that never claims to be real |
| `analytics/lab_sources.py` | Source adapters: `load_stored_data` (project DB, demo/live kept separate), `preview_upload`, `suggest_upload_mapping`, `load_uploaded_csv` (delimiter sniffing, column mapping, per-row rejection reasons, upload guards), `local_dir_status` / `list_local_files` / `load_local_file` (allowlisted directory only), `upload_guard_status` |
| `analytics/lab_classification.py` | Label-gated fault classification: `CLASSIFIERS` (logistic_regression, random_forest, gradient_boosting), stratified/chronological splits, confusion matrix, accuracy / balanced accuracy / macro-F1 / per-class precision-recall-support / majority baseline / imbalance ratio, feature importances, refusal payloads with the missing counts |
| `services/lab_tasks.py` | The four bounded tasks on the shared contract: `run_anomaly_comparison`, `run_forecast_comparison`, `run_fault_classification`, `run_statistics`; `TaskResult` with `status` (`ok`/`refused`/`insufficient_data`/`no_labels`/`error`), a single `_refusal_status()` table, precondition warnings, per-detector/model failure isolation, `CAUSATION_NOTE`, ML evaluation cap |
| `services/lab_experiment.py` | Runner + persistence + exports: `run_experiment(session, dataset, task, refs, params, persist, created_by, seed)`, `accepted_params()` / `task_defaults()`, `build_meta()` (§5 record), `is_synthetic()` (shared by service **and** UI), `history()` / `get_experiment()` / `restore_result()`, `export_table_csv` / `export_metrics_csv` / `export_markdown` / `export_html`, formula-injection guard, point-payload budget, message truncation |
| `services/prometheus_source.py` | Optional **read-only** live telemetry: `status()` (disabled when unconfigured), `health()`, `fetch_range()`, query/series/point/response-size/expression-length bounds |
| `pages/12_AI_Lab.py` | Wizard UI: 1 Source → 2 Inspect → 3 Task → 4 Configure + **Run experiment** → 5 Evaluate → 6 Export, plus the persisted history table |
| `docs/NetPilot_Phase_5_AI_Lab_Guide_EN.md` | User/ops guide (sources, simulator, CSV mapping, live config, tasks/models, metric definitions, reproduction, limitations, commands) |
| `tests/ai_lab_fixtures.py` | Shared small deterministic fixtures for the lab tests |
| `tests/test_ai_lab_dataset.py` (35) | Contract, validation, splits, preprocessing, gates, fingerprint |
| `tests/test_ai_lab_simulator.py` (21) | Determinism, labels/units, bounds enforcement, honesty |
| `tests/test_ai_lab_sources.py` (40) | Stored/upload/local/live adapters and guards |
| `tests/test_ai_lab_tasks.py` (30) | The four tasks: holdout identity, metrics, refusals, label gating |
| `tests/test_ai_lab_experiment.py` (27) | Runner, history, exports, no-external-service, safety |

### Changed

| File | Change |
|---|---|
| `database/models.py` | Additive `LabExperiment` (`lab_experiments`) + `LAB_STATUSES`; no existing table altered |
| `database/repository.py` | `add_lab_experiment` / `get_lab_experiments` / `get_lab_experiment` |
| `config.py` | Eight `NETPILOT_AI_LAB_*` settings with the documented defaults and clamps |
| `app.py` | AI Lab navigation entry |
| `pages/12_AI_Lab.py` | Step 2 badge, the stored history row and the Markdown/HTML report all call the one shared `lab_experiment.is_synthetic()` rule instead of each restating it |
| `tests/ui_smoke_check.py` | Two page-12 checks: a full simulated round-trip, and the unconfigured live source staying honest |
| `README.md` | §1 tree, §5 demo note, §6 config table, §8 counts + Phase 5 coverage, §9 checklist item 10, new §16 |
| `.env.example` | Phase 5 AI Lab block (8 variables, disabled-by-default explained) + the previously missing `NETPILOT_SEASONAL_THRESHOLD` |

**Dependencies:** none added. pandas, scikit-learn, scipy, plotly and httpx were
already in `requirements.txt`. The dependency stack was not re-pinned.

---

## 2. Acceptance criteria (§8) → tests

| # | Criterion | Verified by |
|---|---|---|
| 1 | Simulator deterministic for same seed + config | `simulator::test_same_seed_and_configuration_reproduce_the_same_dataset`, `…_a_different_seed_changes_the_traffic_not_the_structure`, `experiment::test_same_seed_and_same_source_reproduce_the_same_numbers` |
| 2 | Valid timestamps, metrics, units, expected event labels | `simulator::test_generated_frame_carries_valid_timestamps_devices_and_intervals`, `…_every_metric_keeps_its_declared_unit_and_units_are_never_mixed`, `…_injected_events_are_recorded_and_label_their_intervals`, `…_values_really_move_inside_the_labelled_window`, `…_labels_make_normal_and_abnormal_periods_distinguishable` |
| 3 | CSV handles valid / malformed / missing / duplicate / non-numeric | `sources::test_upload_validation_reports_retained_versus_rejected_counts`, `…_preview_counts_malformed_lines_and_duplicate_rows`, `dataset::test_non_numeric_value_becomes_missing_not_zero`, `…_rows_are_chronological_and_duplicate_timestamps_are_counted`, `…_unparseable_timestamps_are_rejected_not_repaired` |
| 4 | Source identity retained through the pipeline | `dataset::test_source_identity_survives_the_whole_pipeline`, `sources::test_one_dataset_carries_exactly_one_provenance`, `experiment::test_uploaded_source_is_labelled_as_operator_asserted_in_exports` |
| 5 | Chronological splits, no future leakage | `dataset::test_chronological_split_is_contiguous_and_ordered`, `…_random_shuffle_split_is_rejected_by_the_leakage_gate`, `tasks::test_holdout_is_backtesting_and_the_future_path_is_separate` |
| 6 | Preprocessing fitted on train only | `dataset::test_preprocessor_learns_fill_and_scale_from_train_rows_only`, `…_records_a_constant_column_without_scaling_it`, `…_refuses_an_empty_learning_set` |
| 7 | All models scored on the same holdout timestamps | `tasks::test_every_model_is_scored_on_the_identical_holdout_timestamps`, `…_ensemble_is_reported_as_a_blend_not_an_independent_fit` |
| 8 | Forecast metrics correct, incl. edge cases | `tasks::test_forecast_metrics_are_recomputable_from_the_returned_points`, `…_constant_series_reports_zero_error_without_inventing_r2`, `…_a_series_of_zeros_reports_no_percentage_or_scaled_error`, `…_mape_skips_zero_actuals_instead_of_diluting_or_nulling_them` |
| 9 | Anomaly detection with **and** without ground truth | `tasks::test_labelled_source_yields_supervised_detector_scores`, `…_unlabelled_source_reports_evidence_but_never_accuracy`, `…_threshold_sweep_curve_accompanies_the_pr_auc`, `…_multivariate_pass_is_opt_in_and_abstains_without_two_metrics` |
| 10 | Classification blocked on missing/insufficient labels | `dataset::test_classification_gate_blocks_an_unlabelled_source_in_words`, `tasks::test_classification_refuses_a_source_without_labels`, `…_refuses_one_class_and_thin_classes`, `…_refuses_impossible_setups`, `…_unlabelled_rows_are_dropped_and_disclosed_not_given_a_class` |
| 11 | Model comparison survives individual failures | `tasks::test_one_broken_model_cannot_poison_the_comparison`, `…_unknown_model_is_refused_before_anything_is_fitted`, `…_classification_model_failure_becomes_an_error_row` |
| 12 | History persists valid metadata and retrieves it | `experiment::test_run_experiment_returns_protocol_result_and_a_persisted_row`, `…_history_lists_runs_and_filters_by_task_and_source`, `…_restore_result_round_trips_the_stored_payload`, `…_a_corrupt_json_cell_is_reported_rather_than_guessed_at`, `…_points_are_stored_within_budget_and_dropped_overtly_when_too_large` |
| 13 | Exports contain the real computed results + accurate source labels | `experiment::test_table_csv_carries_the_exact_computed_numbers`, `…_metrics_csv_flattens_status_metrics_protocol_and_warnings`, `…_markdown_report_puts_provenance_before_numbers`, `…_uploaded_source_is_labelled_as_operator_asserted_in_exports` |
| 14 | Missing external services don't block simulation/CSV | `experiment::test_simulation_and_csv_workflows_run_with_no_external_service`, `…_lab_workflows_do_not_call_out_when_subprocess_is_poisoned`, `sources::test_live_source_is_disabled_and_says_so` |
| 15 | Safety guards stay disabled; existing tests pass | `experiment::test_execution_gates_are_disabled_before_and_after_every_task`, `…_lab_modules_never_import_the_execution_layer`, `…_importing_the_lab_does_not_load_the_execution_layer`, `…_text_that_looks_like_a_device_command_stays_inert_data` + the full-suite run below |

---

## 3. Verified commands and results

Windows / PowerShell, project venv, run from the repository root:

```powershell
.\.venv\Scripts\python.exe -m pytest -q --tb=no -p no:cacheprovider --junitxml=<report>.xml
.\.venv\Scripts\python.exe -m pytest tests\test_ai_lab_dataset.py tests\test_ai_lab_simulator.py tests\test_ai_lab_sources.py tests\test_ai_lab_tasks.py tests\test_ai_lab_experiment.py -rN --tb=no -p no:cacheprovider
.\.venv\Scripts\python.exe tests\ui_smoke_check.py
```

Results (this machine, no Docker, fully offline):

- **full suite: 966 passed, 0 failed, 0 errors, 0 skipped** in 165.2 s
  (junit XML: `tests=966 failures=0 errors=0 skipped=0`), with 13 pre-existing
  `DeprecationWarning: datetime.utcnow()` warnings from
  `services/ai_workflow_service.py` (Phase 3 code, untouched by this task)
- AI Lab files: **153 passed** in 36.5 s — dataset 35, simulator 21,
  sources 40, tasks 30, experiment 27 (each also verified per file)
- **headless UI smoke: exit code 0**, all `[ OK ]` — app + 12 pages render, the
  page-8/page-9 actions run, the AI Lab completes a simulated round-trip, and
  the unconfigured live source reports "disabled / not configured" without ever
  claiming success. Re-run after the documentation edits, still exit code 0.
- pre-existing suite before this work: 813 tests → still green, none deleted or
  weakened (813 + 153 = 966)

Not run: `tools/phase3_live_check.py` (live Groq, needs a key + network) and any
real Prometheus/lab host. Both are documented as unverified rather than claimed.

---

## 4. Defects found and fixed while testing this feature

All four were real defects surfaced by the new tests, fixed at the source (not
by relaxing a test):

1. **pandas 3 label normalisation crashed.** `Series.map()` re-infers a `str`
   dtype and turns `None` back into `NaN`, so the "is this cell missing" filter
   leaked floats and `_normalize_labels` raised
   `'float' object has no attribute 'lower'`. Now built as plain Python lists
   with an explicit `dtype="object"` and a `pd.isna` guard
   (`analytics/lab_dataset.py`).
2. **An empty unit cell invented a mixed-unit conflict.** A blank cell arrives
   as a missing value, which is *truthy*, so `sorted()` on the unit set raised
   `'<' not supported between 'float' and 'str'` and a series could have been
   flagged as mixing units. Both the conflict check and the unit pick now
   require `isinstance(u, str) and u.strip()`.
3. **Operator uploads were mislabelled as synthetic.** Uploads carry
   `is_lab_real: false`, which the first rule read as "fabricated" — so an
   uploaded CSV was stored as `is_synthetic=true` / `data_source="demo"`.
   The rule now keys off `provenance["synthetic"]`, was made public as
   `lab_experiment.is_synthetic()`, and the UI calls that single function so
   the badge, the stored row and the report cannot disagree.
4. **Refusals were dishonest in status *and* wording.** Refusal statuses were
   mapped inconsistently across tasks, a `no_labels` refusal could look like an
   `insufficient_data` one, the forecast MAPE warning described behaviour the
   metric does not have, and an *empty selection* quoted the dataset-wide gate
   ("10/10 series meet the anomaly gate"). Unified in `_refusal_status()`, with
   precondition counts surfaced as warnings and a dedicated empty-selection
   message.

---

## 5. Safety posture

- `change_loop_enabled` and `lab_execution_enabled` were **not** enabled and are
  asserted `False` before and after every task in the tests.
- The lab imports nothing from `network/` (no executor, no containerlab, no
  netmiko/napalm/paramiko, no `subprocess`) — enforced by a static scan of the
  lab modules **and** by a clean-interpreter check that importing the lab does
  not load the execution layer.
- Uploaded files, model output and metric values are inert data. A device name
  containing `<script>` is escaped in HTML; a cell starting with `=`, `+`, `-`,
  `@`, tab or CR is apostrophe-prefixed in CSV; text that *looks* like a device
  command stays in the report and is never executed.
- Live Prometheus is read-only and disabled until explicitly configured; local
  file import is disabled until an allowlisted directory is set; every size
  bound refuses rather than truncating.
- The database change is additive (`CREATE TABLE IF NOT EXISTS lab_experiments`
  on next start), raw measurements are not duplicated into the lab table, and
  no credential is stored or logged.

---

## 6. Scope boundaries honoured

- The Digital Twin is **not** wired into the AI workflow; no real-network
  execution was added or authorised.
- No placeholder buttons, hardcoded results, fake charts or simulated success:
  every panel is populated from a computed run, and every "cannot run" path
  returns a status + reason instead of a number.
- Existing uncommitted work (the forecasting/anomaly enhancement pass) was
  preserved; nothing untracked or generated was deleted; scratch files created
  during this task were removed.
- Documentation of what is implemented-and-tested vs optional vs unverified is
  in `docs/NetPilot_Phase_5_AI_Lab_Guide_EN.md` §2, §4, §8 and README §16.
