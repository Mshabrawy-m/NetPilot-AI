"""Scale-free forecast metrics, holdout-calibrated bands and Holt-Winters.

Everything here is offline and deterministic on synthetic fixtures. The point of
these tests is the honesty contract: a metric that is not defensible returns
None (never a plausible 0), a band that was not measured says so in its own
metadata, and a seasonal model refuses rather than inventing a rhythm.
"""
from __future__ import annotations

import datetime as dt
import math

import pandas as pd
import pytest

from analytics import evaluation
from analytics.forecasting import (FORECAST_MODELS, MODEL_KWARGS,
                                   apply_holdout_bands,
                                   holdout_band_width,
                                   measure_band_coverage,
                                   realized_band_coverage,
                                   seasonal_naive_forecast, holt_winters_forecast)

BASE = dt.datetime(2026, 1, 1, 0, 0, 0)


def ts_series(values, step_s=60):
    return pd.Series([float(v) for v in values],
                     index=pd.DatetimeIndex(
                         [BASE + dt.timedelta(seconds=i * step_s)
                          for i in range(len(values))]))


# ----------------------------------------------------------------- mape/smape
def test_mape_is_a_percentage_on_a_known_case():
    assert evaluation.mape([100.0, 100.0], [90.0, 110.0]) == pytest.approx(10.0)


def test_mape_skips_near_zero_actuals_instead_of_blowing_up():
    # 0.0000000001 actual with a 1.0 error would claim 1e11 % - it is dropped
    assert evaluation.mape([0.0, 1e-12, 100.0], [1.0, 1.0, 90.0]) == \
        pytest.approx(10.0)
    assert evaluation.mape([0.0, 0.0], [1.0, -1.0]) is None


def test_mape_returns_none_on_length_mismatch_not_a_partial_number():
    assert evaluation.mape([1.0, 2.0, 3.0], [1.0, 2.0]) is None


def test_smape_is_bounded_and_symmetric():
    forward = evaluation.smape([100.0, 100.0], [90.0, 110.0])
    backward = evaluation.smape([90.0, 110.0], [100.0, 100.0])
    assert forward == pytest.approx(backward)
    assert 0.0 <= forward <= 200.0
    # a near-zero prediction cannot inflate sMAPE the way it inflates MAPE
    assert evaluation.smape([100.0], [0.0001]) == pytest.approx(200.0, rel=1e-3)


def test_smape_skips_the_both_zero_case():
    # (0,0) has no defensible denominator and is dropped; (100,80) scores
    # 2*20/180 = 22.2 %
    assert evaluation.smape([0.0, 100.0], [0.0, 80.0]) == pytest.approx(
        200.0 / 9.0)
    assert evaluation.smape([0.0], [0.0]) is None


# --------------------------------------------------------------------- mase
def test_mase_below_one_means_it_beats_the_in_sample_naive_scale():
    train = ts_series([10.0, 12.0, 14.0, 16.0, 18.0, 20.0])   # +2 per step
    good = evaluation.mase([22.0, 24.0], [22.0, 24.0], train)
    bad = evaluation.mase([22.0, 24.0], [10.0, 10.0], train)
    assert good == pytest.approx(0.0)          # perfect on a 2.0 step scale
    assert bad == pytest.approx(13.0 / 2.0)    # mean |error| 13 over scale 2
    assert good < 1.0 <= bad


def test_mase_refuses_a_flat_training_history():
    train = ts_series([10.0] * 8)
    assert evaluation.mase([11.0], [10.5], train) is None


def test_mase_needs_at_least_two_training_points():
    assert evaluation.mase([1.0], [1.0], [5.0]) is None


# ---------------------------------------------------------------- skill
def test_skill_is_zero_for_the_baseline_and_positive_when_it_is_beaten():
    actual = [10.0, 20.0, 30.0]
    naive = [11.0, 21.0, 31.0]
    assert evaluation.skill_vs_naive(actual, naive, naive) == pytest.approx(0.0)
    assert evaluation.skill_vs_naive(actual, actual, naive) == pytest.approx(1.0)
    assert evaluation.skill_vs_naive(actual, [0.0, 0.0, 0.0], naive) < 0.0


def test_skill_is_none_without_a_usable_naive_reference():
    assert evaluation.skill_vs_naive([1.0, 2.0], [1.0, 2.0], None) is None
    # a perfect naive baseline leaves no error to divide by
    assert evaluation.skill_vs_naive([1.0, 2.0], [1.5, 2.5],
                                     [1.0, 2.0]) is None


# ------------------------------------------------------- band half-width
def test_holdout_band_width_uses_the_nearest_rank_quantile():
    errors = list(range(1, 101))              # 1..100
    assert holdout_band_width(errors, 0.95) == 95.0
    assert holdout_band_width(errors, 0.5) == 50.0
    assert holdout_band_width(errors, 0.999) == 100.0


def test_holdout_band_width_never_returns_a_zero_width_for_no_errors():
    assert holdout_band_width([]) is None
    assert holdout_band_width([None, float("nan"), 2.0]) == 2.0


def test_holdout_band_width_clamps_a_silly_coverage_to_the_documented_default():
    """An out-of-range coverage must not silently become 0 % or 100 %."""
    errors = [1.0, 2.0, 3.0]
    for coverage in (1.5, 0.0, -5.0, 0.95):
        assert holdout_band_width(errors, coverage) == 3.0
    assert holdout_band_width(list(range(1, 101)), 1.0) == 95.0


def test_measure_band_coverage_counts_errors_inside_the_step_one_band():
    future = [{"step": 1, "predicted_value": 10.0, "lower_bound": 8.0,
               "upper_bound": 12.0}]
    assert measure_band_coverage(future, [0.5, 1.0, 2.0, 3.0]) == pytest.approx(
        0.75)
    assert measure_band_coverage([], [1.0]) is None
    assert measure_band_coverage(future, []) is None


def test_measure_band_coverage_is_none_when_the_band_is_not_numeric():
    future = [{"step": 1, "predicted_value": 10.0, "lower_bound": None,
               "upper_bound": None}]
    assert measure_band_coverage(future, [1.0]) is None


# --------------------------------------------------------- apply_holdout_bands
def _future(horizon=4, base=10.0, width=2.0):
    return [{"target_timestamp": BASE + dt.timedelta(minutes=5 * (h + 1)),
             "step": h + 1, "predicted_value": base + h,
             "lower_bound": base + h - width * math.sqrt(h + 1),
             "upper_bound": base + h + width * math.sqrt(h + 1)}
            for h in range(horizon)]


def test_apply_holdout_bands_replaces_width_with_the_measured_value():
    fut = _future()
    new, meta = apply_holdout_bands(fut, [1.0, 2.0, 3.0, 4.0], coverage=0.75,
                                    mode="empirical")
    assert meta["kind"] == "empirical"
    assert meta["half_width"] == 3.0
    assert meta["n_errors"] == 4
    for f in new:
        assert f["upper_bound"] - f["predicted_value"] == pytest.approx(3.0)
        assert f["predicted_value"] - f["lower_bound"] == pytest.approx(3.0)


def test_empirical_sqrt_h_grows_the_band_with_the_horizon():
    _, meta = apply_holdout_bands(_future(), [1.0, 2.0, 3.0, 4.0],
                                  coverage=0.75, mode="empirical_sqrt_h")
    assert meta["kind"] == "empirical_sqrt_h"
    fut, _ = apply_holdout_bands(_future(), [1.0, 2.0, 3.0, 4.0],
                                 coverage=0.75, mode="empirical_sqrt_h")
    widths = [f["upper_bound"] - f["predicted_value"] for f in fut]
    assert widths == sorted(widths) and len(set(widths)) == len(widths)
    assert widths[0] == pytest.approx(3.0)
    assert widths[3] == pytest.approx(3.0 * math.sqrt(4))


def test_apply_holdout_bands_never_mutates_or_re_aims_the_callers_values():
    fut = _future()
    before = [(f["predicted_value"], f["lower_bound"], f["upper_bound"])
              for f in fut]
    new, _ = apply_holdout_bands(fut, [1.0, 3.0, 5.0, 7.0], coverage=0.75,
                                mode="empirical_sqrt_h")
    assert [(f["predicted_value"], f["lower_bound"], f["upper_bound"])
            for f in fut] == before
    assert [f["upper_bound"] for f in new] != [f["upper_bound"] for f in fut]
    assert all(f["lower_bound"] <= f["predicted_value"] <= f["upper_bound"]
               for f in new)
    # a returned step is a copy, not the caller's dict
    assert all(new[i] is not fut[i] for i in range(len(fut)))


def test_apply_holdout_bands_keeps_the_approximation_when_nothing_was_measured():
    fut = _future()
    new, meta = apply_holdout_bands(fut, [], coverage=0.95,
                                    mode="empirical_sqrt_h")
    assert meta["kind"] == "normal_sqrt_h_unmeasured"
    assert meta["measured_coverage"] is None
    assert [f["upper_bound"] for f in new] == [f["upper_bound"] for f in fut]


def test_apply_holdout_bands_can_leave_the_models_own_band_alone():
    fut = _future()
    new, meta = apply_holdout_bands(fut, [1.0, 2.0, 3.0], coverage=0.95,
                                    mode="normal_sqrt_h")
    assert meta["kind"] == "normal_sqrt_h"
    assert meta["half_width"] is None
    assert [f["upper_bound"] for f in new] == [f["upper_bound"] for f in fut]
    # the measured column is still reported (step-1 half width is 2.0, so the
    # errors 1.0 and 2.0 fit and 3.0 does not), which is the point of keeping
    # the approximation visible instead of silently replacing it
    assert meta["measured_coverage"] == pytest.approx(2.0 / 3.0)


def test_apply_holdout_bands_reports_no_steps_honestly():
    new, meta = apply_holdout_bands([], [1.0, 2.0], mode="empirical")
    assert new == []
    assert meta["kind"] == "no-forecast-steps"


# --------------------------------------------------- realized band coverage
class _Row:
    def __init__(self, pred, lo, hi, actual=None):
        self.predicted_value = pred
        self.lower_bound = lo
        self.upper_bound = hi
        self.actual_value = actual


def test_realized_coverage_counts_only_verified_steps():
    rows = [_Row(10.0, 8.0, 12.0, 9.0), _Row(11.0, 9.0, 13.0, 20.0),
            _Row(12.0, 10.0, 14.0, None)]
    res = realized_band_coverage(rows)
    assert res == {"n_checked": 2, "n_inside": 1, "coverage": 0.5,
                   "n_unverified": 1}


def test_realized_coverage_is_none_not_one_hundred_when_unverified():
    res = realized_band_coverage([_Row(10.0, 8.0, 12.0), _Row(11.0, 9.0, 13.0)])
    assert res["coverage"] is None
    assert res["n_checked"] == 0 and res["n_unverified"] == 2


def test_realized_coverage_skips_a_prediction_without_a_band():
    res = realized_band_coverage([_Row(10.0, None, None, 9.0)])
    assert res["n_checked"] == 0 and res["n_unverified"] == 1


# ----------------------------------------------------------- holt_winters
SEASON_H = 0.05          # 3 minutes on a 60 s grid -> season length 3
INTERVAL = 60.0


def seasonal_history(n_cycles=12):
    """Two interleaved things HW must separate: a level trend and a rhythm."""
    vals = []
    for i in range(n_cycles * 3):
        phase = i % 3
        cycle = i // 3
        vals.append(50.0 + 2.0 * cycle + [0.0, 8.0, -4.0][phase])
    return ts_series(vals)


# cycles 12..13 of the same generator - the honest continuation of the history
NEXT_CYCLE = [74.0, 82.0, 70.0, 76.0, 84.0, 72.0]


def mae_on_scoreable(actual, predicted):
    """Aligned MAE over the pairs that exist (a seasonal baseline can only
    reach one season ahead; the gap is dropped, never filled with a guess)."""
    pairs = [(a, p) for a, p in zip(actual, predicted) if p == p]
    return evaluation.mae([a for a, _ in pairs], [p for _, p in pairs])


def test_holt_winters_is_registered_and_declares_its_kwargs():
    assert "holt_winters" in FORECAST_MODELS
    assert MODEL_KWARGS["holt_winters"] == ("interval_seconds", "season_hours")
    # one season definition for every seasonal-aware model, so a deployment that
    # sets NETPILOT_SEASON_HOURS cannot leave two models disagreeing
    assert MODEL_KWARGS["seasonal_naive"] == ("interval_seconds",
                                              "season_hours")
    for name, keys in MODEL_KWARGS.items():
        for key in keys:
            assert key in FORECAST_MODELS[name].__code__.co_varnames, (
                f"{name} declares {key} but cannot accept it")


def test_holt_winters_refuses_without_a_sampling_interval():
    res = holt_winters_forecast(seasonal_history(8), 3, interval_seconds=None)
    assert not res.ok
    assert "interval" in res.reason


def test_holt_winters_refuses_less_than_two_full_seasons():
    # m = 3 samples, so the documented budget is 2*m + 4 = 10 usable samples
    train = seasonal_history(3)                     # 9 samples
    res = holt_winters_forecast(train, 3, interval_seconds=INTERVAL,
                                season_hours=SEASON_H)
    assert not res.ok
    assert "10 samples" in res.reason
    ok = holt_winters_forecast(seasonal_history(4), 3, interval_seconds=INTERVAL,
                               season_hours=SEASON_H)
    assert ok.ok


def test_holt_winters_tracks_the_rhythm_a_level_only_model_cannot_see():
    train = seasonal_history(12)
    res = holt_winters_forecast(train, 3, interval_seconds=INTERVAL,
                                season_hours=SEASON_H,
                                test=ts_series(NEXT_CYCLE[:3]))
    assert res.ok
    assert len(res.test_pred) == 3
    pred = res.test_pred
    # phase 1 is the high point of the cycle and phase 2 the low point: a
    # model blind to seasonality flattens both into one level.
    assert pred[1] == pytest.approx(max(pred), abs=1e-6)
    assert pred[2] == pytest.approx(min(pred), abs=1e-6)
    assert pred[1] - pred[2] > 6.0
    # and it lands close to the true continuation, not on the last value
    assert evaluation.mae(NEXT_CYCLE[:3], pred) < 3.0


def test_holt_winters_beats_a_season_blind_trend_model():
    """The gap HW exists to fill: level+trend alone cannot see the rhythm."""
    from analytics.forecasting import holt_damped_forecast
    train = seasonal_history(12)
    test = ts_series(NEXT_CYCLE)
    hw = holt_winters_forecast(train, 6, interval_seconds=INTERVAL,
                               season_hours=SEASON_H, test=test)
    hd = holt_damped_forecast(train, 6, test=test)
    assert hw.ok and hd.ok
    hw_mae = evaluation.mae(list(test.to_numpy()), hw.test_pred)
    hd_mae = evaluation.mae(list(test.to_numpy()), hd.test_pred)
    assert hw_mae < hd_mae / 2.0, (
        f"HW {hw_mae:.2f} vs damped Holt {hd_mae:.2f}: the rhythm is worth "
        "more than half the error")


def test_holt_winters_beats_seasonal_naive_on_a_trending_seasonal_series():
    train = seasonal_history(12)
    test = ts_series(NEXT_CYCLE)
    hw = holt_winters_forecast(train, 6, interval_seconds=INTERVAL,
                               season_hours=SEASON_H, test=test)
    sn = seasonal_naive_forecast(train, 6, INTERVAL, test=test,
                                 season_hours=SEASON_H)
    assert hw.ok and sn.ok
    act = list(test.to_numpy())
    hw_mae = mae_on_scoreable(act, hw.test_pred)
    sn_mae = mae_on_scoreable(act, sn.test_pred)
    assert sn_mae is not None and hw_mae < sn_mae, (
        f"HW {hw_mae:.2f} vs seasonal naive {sn_mae:.2f}: the +2/cycle trend "
        "is what the plain seasonal copy misses")


def test_holt_winters_is_deterministic_and_reports_tuned_parameters():
    train = seasonal_history(12)
    a = holt_winters_forecast(train, 3, interval_seconds=INTERVAL,
                              season_hours=SEASON_H,
                              test=ts_series(NEXT_CYCLE[:3]))
    b = holt_winters_forecast(train, 3, interval_seconds=INTERVAL,
                              season_hours=SEASON_H,
                              test=ts_series(NEXT_CYCLE[:3]))
    assert a.test_pred == b.test_pred
    assert a.config["season_len"] == 3
    assert a.config["tuned_on"] == "train"
    assert {"alpha", "beta", "gamma", "phi"} <= set(a.config)


def test_holt_winters_future_bands_widen_with_the_horizon():
    train = seasonal_history(12)
    stamps = [train.index[-1] + pd.Timedelta(seconds=INTERVAL * (h + 1))
              for h in range(4)]
    res = holt_winters_forecast(train, 4, interval_seconds=INTERVAL,
                                season_hours=SEASON_H,
                                future_timestamps=stamps)
    widths = [f["upper_bound"] - f["predicted_value"] for f in res.future]
    assert widths == sorted(widths)
    assert widths[3] == pytest.approx(widths[0] * 2.0, rel=1e-6)


def test_holt_winters_test_path_never_consumes_its_own_observed_value():
    """Step h of the holdout may only use data before that timestamp."""
    train = seasonal_history(12)
    test = ts_series(NEXT_CYCLE[:3])
    res = holt_winters_forecast(train, 3, interval_seconds=INTERVAL,
                                season_hours=SEASON_H, test=test)
    changed = ts_series([900.0, 900.0, 900.0])
    res2 = holt_winters_forecast(train, 3, interval_seconds=INTERVAL,
                                 season_hours=SEASON_H, test=changed)
    assert res.test_pred == res2.test_pred, (
        "the holdout values must not feed back into their own predictions")


def test_holt_winters_survives_a_constant_seasonal_pattern():
    train = ts_series([10.0, 20.0, 30.0] * 10)
    res = holt_winters_forecast(train, 3, interval_seconds=INTERVAL,
                                season_hours=SEASON_H,
                                test=ts_series([10.0, 20.0, 30.0]))
    assert res.ok
    assert evaluation.mae([10.0, 20.0, 30.0], res.test_pred) < 5.0
