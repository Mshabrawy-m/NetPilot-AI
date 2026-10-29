# Unified Analytics and Agentic Operations

## Shared data path

AI Lab canonicalizes simulated, previously stored, uploaded, allowlisted local,
and configured read-only Prometheus observations into one validated
`analytics.lab_dataset.LabDataset`. Forecasting and descriptive analysis use
that same representation and existing task gates. A configured Prometheus
endpoint is not proof that live telemetry was retrieved; provenance records the
source used for the specific loaded dataset. Historical and uploaded origins
remain operator/source claims unless separately verified.

The Forecast task compares `naive`, `seasonal_naive`, `holt_damped`,
`holt_winters`, and `ridge_lag` where selected and supported. Forecast
comparisons use one chronological holdout per series, and keep retrospective
holdout points distinct from the separate future path. MAE/RMSE and other
available metrics are computed from actual held-out observations. Future paths
have no observed accuracy yet. Statistics include descriptive distributions,
quality counts, trend/autocorrelation summaries and gated association tests;
association is not causal evidence. See AI Lab history and exports for the
effective protocol, warnings, provenance and computed outputs.

## Agentic Operations

The Agentic Operations tab on Network assistant requires an active AI Lab
dataset in the current Streamlit session. The existing Groq provider returns a
strictly validated ordered plan with at most three calls. A deterministic
`inspect_dataset` operation runs first. The registered tools are:

* `inspect_dataset` — deterministic read-only profile, source, quality and task
  readiness inspection.
* `run_statistics` — the existing AI Lab statistics service.
* `detect_anomalies` — the existing AI Lab anomaly comparison, limited to
  rolling MAD and EWMA without the threshold sweep.
* `run_forecast` — the existing AI Lab forecast comparison, limited to
  persistence, seasonal-naive and damped Holt, with a horizon capped at 48.
* `get_topology_context` — bounded read of stored device/link status, graph
  connectivity and articulation points.
* `run_structural_impact_analysis` — stored-graph reachability impact for one
  existing device, without device I/O or claims about runtime routing.
* `inspect_experiment_history` — recent AI Lab runs for the current dataset
  fingerprint, with a 1–20 row limit.

The last three are selected by the validated plan. Series IDs are resolved
against the active dataset inventory in Python. Topology targets are matched
against stored nodes. Unknown IDs and unknown tool or parameter fields fail
closed; tools have one-call limits, and the complete plan has at most three
steps. No model-provided code, shell, SQL, or arbitrary network access is
accepted. Each analytics task is persisted by the AI Lab experiment runner; the
ordered plan, bounded outputs, status, redacted request, dataset fingerprint,
and evidence-based report are also persisted in `agentic_analysis_runs`. The UI
shows executed operations separately from the final report and exposes recent
saved runs.

The agent is analysis-only. It does not make topology or production changes.
`change_loop_enabled` and `lab_execution_enabled` remain disabled. A missing
provider key, invalid response, or missing active dataset stops the agent
safely. Topology tools use the application’s stored topology; operational
anomaly events are not queried by this agent. Analytics tools use only the
validated dataset explicitly loaded in AI Lab.

## Verification

Run deterministic tests with:

```powershell
pytest -q tests/test_agentic_analytics.py
```

No live provider, Prometheus server, network device, or Containerlab is needed
for these tests. Live-provider and live-telemetry operation must be verified in
an environment where those services are configured and available.
