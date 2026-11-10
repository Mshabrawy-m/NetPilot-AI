"""Strong-model forecasting: regular grid, SARIMA, HGB and mean ensemble.

All fixtures are synthetic and deterministic. These tests exercise the real
functions (no mocks of the forecast code) and assert the honesty contract:

* a short gap is interpolated, a long gap stays NaN and splits a run;
* every reported MAE/RMSE/MASE is finite where a scale exists;
* intervals always contain their point forecast;
* the 30-point floor is stated with the actual count;
* the same input gives the same output.
"""
from __future__ import annotations

import datetime as dt
import math

import numpy as np
import pandas as pd
import pytest

from analytics import derived_metrics, series_prep
from analytics.backtest import MEAN_ENSEMBLE_NAME, strong_rolling_origin_backtest
from analytics.forecasting import (
    FORECAST_MODELS,
    MODEL_KWARGS,
    hgb_lag_forecast,
    sarima_forecast,
)
from services.forecast_service import (
    compute_strong_forecast,
    strong_comparison_rows,
)

BASE = dt.datetime(2026, 1, 1, 0, 0, 0)


def ts_series(values, step_s=30):
    index = pd.date_range(BASE, periods=len(values), freq=f"{step_s}s")
    return pd.Series(np.asarray(values, dtype=float), index=index)


def synthetic(n=160, *, step_s=30, trend=0.03, amplitude=4.0,
              season=12, noise=0.25, seed=7):
    rng = np.random.default_rng(seed)
    t = np.arange(n, dtype=float)
    values = (50.0 + trend * t
              + amplitude * np.sin(2.0 * np.pi * t / season)
              + rng.normal(0.0, noise, n))
    return ts_series(values, step_s=step_s)


def _frame(rows):
    return pd.DataFrame({
        "collected_at": [ts for ts, _ in rows],
        "value": [value for _, value in rows],
    })


# ------------------------------------------------------------- series prep
def test_regular_grid_collapses_duplicate_timestamps():
    start = pd.Timestamp("2026-01-01 00:00:00")
    frame = _frame([
        (start, 10.0),
        (start + pd.Timedelta(seconds=30), 11.0),
        (start + pd.Timedelta(seconds=30), 12.0),   # duplicate, last wins
        (start + pd.Timedelta(seconds=60), 13.0),
    ])
    prepared = series_prep.prepare_regular_series(frame, interval_seconds=30)
    assert prepared["n_duplicates"] == 1
    assert prepared["n_raw_rows"] == 4
    assert prepared["n_points"] == 3
    assert prepared["frame"]["value"].tolist() == [10.0, 12.0, 13.0]
    assert not prepared["frame"]["imputed"].any()


def test_short_gap_is_interpolated_and_long_gap_stays_nan():
    start = pd.Timestamp("2026-01-01 00:00:00")
    # 30 s grid: observations at 0, 30, 60, then a 2-step gap to 150.
    short_gap = _frame([
        (start, 10.0),
        (start + pd.Timedelta(seconds=30), 12.0),
        (start + pd.Timedelta(seconds=60), 14.0),
        (start + pd.Timedelta(seconds=150), 20.0),
    ])
    prepared = series_prep.prepare_regular_series(
        short_gap, interval_seconds=30, max_gap_steps=3)
    assert prepared["n_imputed"] == 2
    assert prepared["n_missing"] == 0
    assert prepared["frame"]["value"].tolist() == [10.0, 12.0, 14.0, 16.0,
                                                   18.0, 20.0]
    assert prepared["frame"]["imputed"].tolist() == [False, False, False, True,
                                                     True, False]

    long_gap = _frame([
        (start, 10.0),
        (start + pd.Timedelta(seconds=30), 12.0),
        (start + pd.Timedelta(seconds=300), 20.0),
    ])
    prepared = series_prep.prepare_regular_series(
        long_gap, interval_seconds=30, max_gap_steps=3)
    assert prepared["n_long_gaps"] == 1
    assert prepared["n_imputed"] == 0
    values = prepared["series"].to_numpy()
    assert np.isnan(values[2:-1]).all()
    assert series_prep.finite_segments(values) == [(0, 2), (10, 11)]


def test_series_hash_is_stable_and_value_sensitive():
    frame = _frame([
        (pd.Timestamp("2026-01-01 00:00:00"), 1.0),
        (pd.Timestamp("2026-01-01 00:00:30"), 2.0),
    ])
    first = series_prep.prepare_regular_series(frame, interval_seconds=30)
    second = series_prep.prepare_regular_series(frame, interval_seconds=30)
    assert first["series_hash"] == second["series_hash"]
    changed = frame.copy()
    changed.loc[0, "value"] = 99.0
    third = series_prep.prepare_regular_series(changed, interval_seconds=30)
    assert third["series_hash"] != first["series_hash"]


def test_insufficient_message_states_the_actual_count():
    message = series_prep.insufficient_data_message(12)
    assert "12" in message
    assert "30" in message


# ------------------------------------------------------------------- SARIMA
def test_sarima_fits_trend_and_seasonality_deterministically():
    series = synthetic(150, season=12, trend=0.04)
    future = [series.index[-1] + pd.Timedelta(seconds=30 * (h + 1))
              for h in range(5)]
    first = sarima_forecast(series.iloc[:130], 5,
                            interval_seconds=30,
                            future_timestamps=future,
                            test=series.iloc[130:],
                            time_cap_seconds=3.0)
    second = sarima_forecast(series.iloc[:130], 5,
                             interval_seconds=30,
                             future_timestamps=future,
                             test=series.iloc[130:],
                             time_cap_seconds=3.0)
    assert first.ok, first.reason
    assert len(first.test_pred) == 20
    assert len(first.future) == 5
    assert all(math.isfinite(value) for value in first.test_pred)
    assert all(math.isfinite(row["predicted_value"]) for row in first.future)
    assert first.test_pred == second.test_pred
    assert ([row["predicted_value"] for row in first.future]
            == [row["predicted_value"] for row in second.future])
    for row in first.future:
        assert row["lower_bound"] <= row["predicted_value"] <= row["upper_bound"]


# ------------------------------------------------------- gradient boosting
def test_hgb_recursive_forecast_is_deterministic_with_valid_intervals():
    series = synthetic(150, season=10)
    future = [series.index[-1] + pd.Timedelta(seconds=30 * (h + 1))
              for h in range(6)]
    first = hgb_lag_forecast(series.iloc[:130], 6, max_lags=12,
                             future_timestamps=future,
                             test=series.iloc[130:])
    second = hgb_lag_forecast(series.iloc[:130], 6, max_lags=12,
                              future_timestamps=future,
                              test=series.iloc[130:])
    assert first.ok, first.reason
    assert len(first.test_pred) == 20
    assert len(first.future) == 6
    assert all(math.isfinite(value) for value in first.test_pred)
    assert first.test_pred == second.test_pred
    for row in first.future:
        assert row["lower_bound"] <= row["predicted_value"] <= row["upper_bound"]


# --------------------------------------------------------- strong backtest
def test_strong_backtest_reports_finite_metrics_and_mean_ensemble():
    series = synthetic(170, season=12)
    result = strong_rolling_origin_backtest(
        series, models=("naive", "holt_damped", "hgb_lag"),
        horizon=4, n_folds=3, min_train=50, interval_seconds=30)
    assert result["status"] == "ok", result.get("reasons")
    assert result["folds"] >= 3
    assert result["best_model"] in result["models"]
    for name in ("naive", "holt_damped", "hgb_lag"):
        stats = result["models"][name]
        assert math.isfinite(stats["mae"])
        assert math.isfinite(stats["rmse"])
        assert stats["mase"] is not None and math.isfinite(stats["mase"])
        assert all(width >= 0.0
                   for width in stats["interval_half_widths"].values())
    ensemble = result["ensemble"]
    assert ensemble is not None
    assert 2 <= len(ensemble["members"]) <= 3
    assert MEAN_ENSEMBLE_NAME in result["models"]
    assert math.isfinite(result["models"][MEAN_ENSEMBLE_NAME]["mae"])


def test_strong_backtest_refuses_below_the_thirty_point_floor():
    series = synthetic(20)
    result = compute_strong_forecast(series, horizon=3,
                                     models=("naive",), n_folds=3)
    assert result["status"] == "insufficient_data"
    assert "20" in result["message"]
    assert "30" in result["message"]
    assert result["forecast"] == []


def test_constant_series_keeps_finite_errors_and_zero_width_intervals():
    series = ts_series([5.0] * 80)
    result = compute_strong_forecast(
        series, horizon=4, models=("naive",), interval_seconds=30,
        n_folds=3, min_train=30)
    assert result["status"] == "ok", result.get("message")
    assert result["models"]["naive"]["mae"] == pytest.approx(0.0)
    # A flat history has no defensible MASE scale: None, never a fake 0.
    assert result["models"]["naive"]["mase"] is None
    assert len(result["forecast"]) == 4
    for row in result["forecast"]:
        assert row["lower_bound"] <= row["predicted_value"] <= row["upper_bound"]


def test_long_nan_gap_splits_runs_and_is_never_bridged():
    series = synthetic(130, season=12)
    series.iloc[55:75] = float("nan")
    prepared = series_prep.prepare_regular_series(
        pd.DataFrame({"collected_at": series.index, "value": series.values}),
        interval_seconds=30, max_gap_steps=3)
    assert prepared["n_long_gaps"] >= 1
    result = compute_strong_forecast(
        prepared["series"], horizon=3, models=("naive",),
        interval_seconds=30, n_folds=3, min_train=30)
    assert result["status"] == "ok", result.get("message")
    assert result["n_segments"] >= 2
    assert all(math.isfinite(row["predicted_value"])
               for row in result["forecast"])


def test_comparison_rows_separate_series_len_from_eval_points():
    series = synthetic(130, season=12)
    series.iloc[55:75] = float("nan")
    prepared = series_prep.prepare_regular_series(
        pd.DataFrame({"collected_at": series.index, "value": series.values}),
        interval_seconds=30, max_gap_steps=3)
    result = compute_strong_forecast(
        prepared["series"], horizon=3, models=("naive",),
        interval_seconds=30, n_folds=3, min_train=30)
    assert result["status"] == "ok", result.get("message")
    # 110 finite points exist, but the final fit uses one 55-point run.
    assert result["n_usable_points"] == 110
    assert result["series_len"] == 55
    assert result["eval_points"] == 9  # 3 folds x 3 scored steps
    rows = strong_comparison_rows({
        **result, "data_source": "demo", "device_name": "dev",
        "metric": "m"})
    assert len(rows) == 1
    assert list(rows[0]) == ["source", "device", "metric", "series_len",
                             "eval_points", "model", "MAE", "RMSE", "MAPE",
                             "MASE", "skill_vs_naive", "wins_vs_naive",
                             "naive_perfect", "skill_eval_points"]
    assert "n_points" not in rows[0]
    assert rows[0]["series_len"] == 55
    assert rows[0]["eval_points"] == 9
    assert rows[0]["skill_vs_naive"] == pytest.approx(1.0)
    assert rows[0]["wins_vs_naive"] == "tie"
    assert rows[0]["naive_perfect"] is False
    assert rows[0]["skill_eval_points"] == 9


@pytest.mark.parametrize(
    ("metric", "rate_metric", "rate_unit", "multiplier"),
    [
        ("if.rx_bytes", "if.rx_bps", "bps", 8.0),
        ("if.tx_bytes", "if.tx_bps", "bps", 8.0),
        ("if.rx_errors", "if.rx_errors_per_sec", "count/s", 1.0),
        ("if.tx_errors", "if.tx_errors_per_sec", "count/s", 1.0),
        ("if.rx_dropped", "if.rx_dropped_per_sec", "count/s", 1.0),
        ("if.tx_dropped", "if.tx_dropped_per_sec", "count/s", 1.0),
    ],
)
def test_counter_metrics_are_forecast_as_per_interval_rates(
        metric, rate_metric, rate_unit, multiplier):
    start = pd.Timestamp("2026-01-01 00:00:00")
    frame = pd.DataFrame({
        "collected_at": [start + pd.Timedelta(seconds=30 * i)
                         for i in range(6)],
        "value": [100.0, 200.0, 300.0, 50.0, 150.0, 250.0],  # reset at 50
        "sample_id": list(range(6)),
        "unit": ["bytes" if multiplier == 8.0 else "count"] * 6,
    })
    out, meta = derived_metrics.forecast_input_frame(frame, metric)
    assert meta["counter"] is True
    assert meta["rate_metric"] == rate_metric
    assert meta["n_reset_intervals"] == 1
    # Five intervals, one reset -> four usable; each delta is 100 bytes / 30 s.
    assert len(out) == 4
    assert out["value"].tolist() == pytest.approx(
        [100 * multiplier / 30] * 4)
    assert out["unit"].eq(rate_unit).all()

    same, meta2 = derived_metrics.forecast_input_frame(frame, "ping.avg_rtt_ms")
    assert meta2["counter"] is False
    assert same is frame


def test_strong_backtest_skills_are_measured_on_common_points():
    series = synthetic(170, season=12)
    result = strong_rolling_origin_backtest(
        series, models=("naive", "holt_damped", "hgb_lag"),
        horizon=4, n_folds=3, min_train=50, interval_seconds=30)
    assert result["status"] == "ok", result.get("reasons")
    naive = result["models"]["naive"]
    assert naive["skill_vs_naive"] == pytest.approx(1.0)
    assert naive["wins_vs_naive"] == "tie"
    for stats in result["models"].values():
        assert stats["wins_vs_naive"] in {
            "clearly better", "tie", "worse", None}
        assert 0 <= stats["skill_eval_points"] <= stats["eval_points"]


def test_outliers_do_not_break_the_metrics():
    series = synthetic(150, season=10)
    series.iloc[[20, 70, 120]] = 500.0
    result = strong_rolling_origin_backtest(
        series, models=("naive", "hgb_lag"), horizon=4, n_folds=3,
        min_train=50, interval_seconds=30)
    assert result["status"] == "ok", result.get("reasons")
    for stats in result["models"].values():
        assert math.isfinite(stats["mae"])
        assert math.isfinite(stats["rmse"])


def test_new_models_are_registered_and_declare_their_kwargs():
    assert "sarima" in FORECAST_MODELS
    assert "hgb_lag" in FORECAST_MODELS
    for name, keys in MODEL_KWARGS.items():
        for key in keys:
            assert key in FORECAST_MODELS[name].__code__.co_varnames, (
                f"{name} declares {key} but cannot accept it")
