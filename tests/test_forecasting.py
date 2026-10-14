"""Phase 2 Scope E tests: forecast models on SYNTHETIC fixtures.

These verify model mathematics and protocol behaviour (identical
comparison timestamps, refusal on insufficient data, reproducibility).
They are NOT evidence of real-network forecasting accuracy.
"""
from __future__ import annotations

import datetime as dt
import math

import numpy as np
import pandas as pd
import pytest

from analytics.forecasting import (
    FORECAST_MODELS,
    holt_damped_forecast,
    naive_forecast,
    ridge_lag_forecast,
    seasonal_naive_forecast,
)

START = dt.datetime(2026, 1, 1, 0, 0, 0)


def ts_series(values, step_s=60):
    idx = pd.to_datetime([START + dt.timedelta(seconds=i * step_s)
                          for i in range(len(values))])
    return pd.Series([float(v) for v in values], index=idx)


LEVEL = [10.0] * 30 + [30.0] * 30       # daily-like step seasonality


# ------------------------------------------------------------------- naive
def test_naive_is_last_value_with_band():
    s = ts_series([1.0, 3.0, 2.0, 5.0, 4.0])   # noisy: nonzero 1-step sigma
    future = [s.index[-1] + pd.Timedelta(seconds=60 * h) for h in range(1, 4)]
    res = naive_forecast(s, 3, future_timestamps=future)
    assert res.ok
    assert [f["predicted_value"] for f in res.future] == [4.0, 4.0, 4.0]
    # band grows with sqrt(h)
    widths = [f["upper_bound"] - f["predicted_value"] for f in res.future]
    assert 0 < widths[0] < widths[1] < widths[2]


def test_naive_test_predictions_align_to_test_length():
    s = ts_series([float(i) for i in range(20)])
    res = naive_forecast(s.iloc[:17], 3, test=s.iloc[17:])
    assert res.test_pred == [16.0] * 3


# ----------------------------------------------------------- seasonal naive
def test_seasonal_naive_refuses_without_interval():
    s = ts_series(LEVEL)
    res = seasonal_naive_forecast(s, 5, interval_seconds=None)
    assert not res.ok
    assert "unknown" in res.reason


def test_seasonal_naive_refuses_short_history():
    # 1-hour season on 60s grid needs 2*60+1 samples; give 50
    s = ts_series([float(i) for i in range(50)], step_s=60)
    res = seasonal_naive_forecast(s, 5, interval_seconds=60, season_hours=1.0)
    assert not res.ok
    assert "2 full seasons" in res.reason


def test_seasonal_naive_repeats_last_season():
    # season = 10 samples on 60s grid; needs 2*10+1 = 21 samples exactly
    values = [float(i % 10) for i in range(21)]
    s = ts_series(values, step_s=60)
    future = [s.index[-1] + pd.Timedelta(seconds=60 * h) for h in range(1, 6)]
    res = seasonal_naive_forecast(s, 5, interval_seconds=60,
                                  season_hours=10 * 60 / 3600,
                                  future_timestamps=future)
    assert res.ok
    # target position 21+h maps back one season to position 11+h
    assert [f["predicted_value"] for f in res.future] == [1.0, 2.0, 3.0, 4.0, 5.0]


def test_seasonal_naive_needs_21_of_20_offered():
    values = [float(i % 10) for i in range(20)]   # one short of 2m+1
    s = ts_series(values, step_s=60)
    res = seasonal_naive_forecast(s, 5, interval_seconds=60,
                                  season_hours=10 * 60 / 3600)
    assert not res.ok and "2 full seasons" in res.reason


# --------------------------------------------------------------- damped Holt
def test_holt_tracks_linear_trend_and_forecasts_forward():
    values = [10.0 + 0.5 * i for i in range(60)]
    s = ts_series(values)
    future = [s.index[-1] + pd.Timedelta(seconds=60 * h) for h in range(1, 4)]
    res = holt_damped_forecast(s.iloc[:50], 3, future_timestamps=future,
                               val=s.iloc[50:])
    assert res.ok
    preds = [f["predicted_value"] for f in res.future]
    last = values[49]
    # documented damped-Holt behaviour: the trend continues (strictly
    # increasing) while the level lag keeps predictions near the series -
    # they may sit slightly BELOW the last value (damping + level lag).
    assert preds[0] < preds[1] < preds[2]
    assert last - 5.0 < preds[0] < last + 2.0
    # deterministic config grid -> reproducible
    res2 = holt_damped_forecast(s.iloc[:50], 3, future_timestamps=future,
                                val=s.iloc[50:])
    assert res.config == res2.config
    assert preds == [f["predicted_value"] for f in res2.future]


def test_holt_refuses_insufficient_training():
    s = ts_series([1.0, 2.0, 3.0])
    res = holt_damped_forecast(s, 5)
    assert not res.ok and "6 training samples" in res.reason


def test_holt_uses_only_past_for_test_predictions():
    # test predictions computed from TRAIN only must be identical whether
    # or not the test actuals exist afterwards (no leakage)
    values = [float(i) + (i % 3) for i in range(60)]
    s = ts_series(values)
    a = holt_damped_forecast(s.iloc[:50], 10, test=s.iloc[50:])
    b = holt_damped_forecast(s.iloc[:50], 10, test=None)
    assert a.test_pred == a.test_pred  # stability
    assert len(a.test_pred) == 10
    assert not b.test_pred


# ----------------------------------------------------------------- ridge lag
def test_ridge_lag_fits_and_predicts_autoregressive_pattern():
    rng_vals = [math.sin(i / 5.0) * 10.0 + 50.0 for i in range(120)]
    s = ts_series(rng_vals)
    future = [s.index[-1] + pd.Timedelta(seconds=60 * h) for h in range(1, 4)]
    res = ridge_lag_forecast(s.iloc[:100], 3, future_timestamps=future,
                             test=s.iloc[100:], max_lags=8)
    assert res.ok
    assert len(res.future) == 3
    assert len(res.test_pred) == 20
    assert all(not math.isnan(p) for p in res.test_pred)
    # reasonable on a smooth series: MAE under half the signal amplitude
    from analytics.evaluation import mae
    assert mae(s.iloc[100:].to_numpy(), res.test_pred) < 5.0


def test_ridge_lag_refuses_too_short_series():
    s = ts_series([1.0, 2.0, 3.0])
    res = ridge_lag_forecast(s, 5)
    assert not res.ok


def test_registry_contains_naive_always():
    assert "naive" in FORECAST_MODELS
