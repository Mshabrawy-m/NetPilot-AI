"""Phase 2 persistence tests: forecast runs/predictions + immutability.

Synthetic DB fixtures verify storage semantics required by the spec:
predictions never mix with observations, runs record the full evaluation
protocol, and all compared models share identical test timestamps.
"""
from __future__ import annotations

import datetime as dt

import pytest

from database import repository
from services import forecast_service

BASE = dt.datetime(2026, 1, 1, 0, 0, 0)


def seed(session, values, device="R1", metric="m", source="live",
         interface=None, step_s=60, unit="bps"):
    for i, v in enumerate(values):
        repository.add_metric_sample(
            session, device, metric, v, unit=unit,
            interface_name=interface, data_source=source,
            collected_at=BASE + dt.timedelta(seconds=i * step_s))
    session.commit()


WAVE = [50.0 + 10.0 * ((i % 20) - 10) / 10.0 for i in range(80)]


def test_forecast_gate_blocks_thin_data_without_persisting(session):
    seed(session, [1.0, None, 3.0])
    result = forecast_service.run_forecast_comparison(
        session, device_name="R1", metric="m",
        interface_name=None, data_source="live")
    assert result["status"] == "insufficient_data"
    assert result["runs"] == []
    assert repository.get_forecast_runs(session) == []


def test_run_persists_runs_and_future_predictions(session):
    seed(session, WAVE)
    result = forecast_service.run_forecast_comparison(
        session, device_name="R1", metric="m",
        interface_name=None, data_source="live", horizon=6,
        models=("naive", "holt_damped"))
    assert result["status"] == "ok"
    runs = repository.get_forecast_runs(session)
    assert len(runs) == 2                     # one per compared model
    group = runs[0].comparison_group
    assert all(r.comparison_group == group for r in runs)
    for r in runs:
        # full protocol metadata persisted
        assert r.n_train > 0 and r.n_test >= 5 and r.n_eval == r.n_test
        assert r.train_end < r.test_end       # chronological boundaries
        assert r.mae is not None and r.rmse is not None
        assert r.data_source == "live"
        preds = repository.get_forecast_predictions(session, r.id)
        assert len(preds) == 6
        assert all(p.actual_value is None for p in preds)  # future: no actuals
        assert all(p.lower_bound <= p.predicted_value <= p.upper_bound
                   for p in preds)


def test_models_compared_on_identical_timestamps(session):
    seed(session, WAVE)
    result = forecast_service.run_forecast_comparison(
        session, device_name="R1", metric="m",
        interface_name=None, data_source="live", horizon=5,
        models=("naive", "seasonal_naive", "holt_damped"))
    ok_runs = [r for r in result["runs"] if r.get("status") == "ok"]
    assert ok_runs, "at least one model should run on 80 synthetic samples"
    lens = {len(r["test_pred"]) for r in ok_runs}
    assert lens == {len(result["split"].test)}   # identical eval timestamps
    ts_sets = [list(r["test_timestamps"]) for r in ok_runs]
    assert all(t == list(result["split"].test.index) for t in ts_sets)


def test_raw_samples_never_modified(session):
    seed(session, WAVE)
    before = session.query(repository.MetricSample).count()
    forecast_service.run_forecast_comparison(
        session, device_name="R1", metric="m",
        interface_name=None, data_source="live", horizon=4,
        models=("naive",))
    after = session.query(repository.MetricSample).count()
    assert before == after                      # no new/edited observations
    # and stored values are byte-identical to what we wrote
    samples = repository.get_metric_series(session, "R1", "m")
    assert [s.value for s in samples] == WAVE


def test_backfill_actuals_only_after_observation_exists(session):
    # 80 samples; a further observation is stored later, matching one
    # prediction target timestamp, so backfill finds exactly one actual.
    seed(session, WAVE)
    result = forecast_service.run_forecast_comparison(
        session, device_name="R1", metric="m",
        interface_name=None, data_source="live", horizon=3,
        models=("naive",))
    run = repository.get_forecast_runs(session)[0]
    preds = repository.get_forecast_predictions(session, run.id)
    # Simulate time passing: store a real observation at step 1's timestamp.
    target = preds[0].target_timestamp
    repository.add_metric_sample(
        session, "R1", "m", 42.0, data_source="live", collected_at=target)
    session.commit()
    filled = forecast_service.backfill_actuals(session, run.id)
    assert filled == 1
    preds = repository.get_forecast_predictions(session, run.id)
    assert preds[0].actual_value == 42.0
    assert preds[0].abs_error == pytest.approx(
        abs(42.0 - preds[0].predicted_value))
    assert preds[1].actual_value is None       # still unobserved
    assert result["status"] == "ok"


def test_data_source_separation_in_persistence(session):
    seed(session, WAVE, source="demo")
    forecast_service.run_forecast_comparison(
        session, device_name="R1", metric="m",
        interface_name=None, data_source="demo", horizon=3,
        models=("naive",))
    demo_runs = repository.get_forecast_runs(session, data_source="demo")
    live_runs = repository.get_forecast_runs(session, data_source="live")
    assert demo_runs and not live_runs


def test_forecast_warnings_are_labelled_forecast_based(session):
    seed(session, WAVE)
    result = forecast_service.run_forecast_comparison(
        session, device_name="R1", metric="m",
        interface_name=None, data_source="live", horizon=5,
        models=("naive",), persist=False)
    warnings = forecast_service.forecast_warnings(result["runs"],
                                                  threshold=45.0,
                                                  direction="above")
    for w in warnings:
        assert w["kind"] == "forecast-based warning"
        assert w["model_mae"] is not None      # limitation shown with warning
    # an unreachable threshold produces no warnings (not a forced alert)
    none = forecast_service.forecast_warnings(result["runs"],
                                              threshold=10 ** 9)
    assert none == []
