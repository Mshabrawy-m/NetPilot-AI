"""Offline tests for the rolling-origin forecast backtest (Slice 4c)."""
import numpy as np
import pandas as pd

from analytics.backtest import _model_kwargs, rolling_origin_backtest


def _series(values):
    idx = pd.date_range("2026-01-01", periods=len(values), freq="5min")
    return pd.Series(np.asarray(values, dtype=float), index=idx)


def test_insufficient_history_returns_status():
    result = rolling_origin_backtest(_series([1.0, 2.0, 3.0]), horizon=6)
    assert result["status"] == "insufficient_data"
    assert result["models"] == {}


def test_constant_series_naive_perfect():
    result = rolling_origin_backtest(_series([5.0] * 60), horizon=6,
                                     models=("naive",))
    assert result["status"] == "ok"
    assert result["models"]["naive"]["mae"] == 0.0
    assert result["best_model"] == "naive"


def test_per_step_profile_length_matches_horizon():
    result = rolling_origin_backtest(_series(np.arange(60, dtype=float)),
                                     horizon=4, models=("naive",))
    assert len(result["models"]["naive"]["per_step_mae"]) == 4


def test_best_model_is_the_lowest_mae():
    values = [10 + 0.5 * i + (3 if i % 7 == 0 else 0) for i in range(80)]
    result = rolling_origin_backtest(_series(values), horizon=6,
                                     models=("naive", "holt_damped"))
    ranking = result["models"]
    expected_best = min(ranking, key=lambda m: ranking[m]["mae"])
    assert result["best_model"] == expected_best


def test_deterministic_across_runs():
    values = [50 + np.sin(i / 3) * 10 for i in range(70)]
    series = _series(values)
    first = rolling_origin_backtest(series, horizon=6)
    second = rolling_origin_backtest(series, horizon=6)
    assert first["models"] == second["models"]
    assert first["best_model"] == second["best_model"]


def test_appended_future_does_not_change_earlier_origins():
    # Origins are anchored at the start, so adding later data must not alter
    # the already-reported errors -> proves no future leakage into an origin.
    values = [20 + (i % 5) for i in range(60)]
    base = _series(values)
    extended = _series(values + [values[-1] + 100] * 20)  # wild future tail
    common = dict(horizon=6, min_train=24, max_origins=5, models=("naive",))
    a = rolling_origin_backtest(base, **common)
    b = rolling_origin_backtest(extended, **common)
    assert a["models"]["naive"]["mae"] == b["models"]["naive"]["mae"]
    assert a["origins_used"] == b["origins_used"] == 5


def test_refused_seasonal_baseline_is_skipped_not_fabricated():
    # Unknown interval -> seasonal_naive cannot justify a season and is skipped.
    result = rolling_origin_backtest(_series(np.arange(60, dtype=float)),
                                     horizon=6, models=("seasonal_naive",),
                                     interval_seconds=None)
    assert "seasonal_naive" in result["skipped"]
    assert "seasonal_naive" not in result["models"]


# ------------------------------------------------------- kwargs routing (A-4 v2)
def _seasonal_series(n=90, interval_min=5):
    """Level trend + a 3-sample rhythm on a regular grid (season = 15 min)."""
    idx = pd.date_range("2026-01-01", periods=n, freq=f"{interval_min}min")
    vals = [50.0 + 2.0 * (i // 3) + [0.0, 8.0, -4.0][i % 3] for i in range(n)]
    return pd.Series(np.asarray(vals, dtype=float), index=idx)


SEASON_H = 0.25          # 3 samples at 5 min
INTERVAL_S = 300.0


def test_model_kwargs_only_forwards_what_a_model_declares():
    assert _model_kwargs("naive", INTERVAL_S) == {}
    assert _model_kwargs("holt_damped", INTERVAL_S) == {}
    # ridge_lag declares max_lags, which a rolling-origin replay never supplies
    assert _model_kwargs("ridge_lag", INTERVAL_S) == {}
    for seasonal in ("seasonal_naive", "holt_winters"):
        assert _model_kwargs(seasonal, INTERVAL_S, SEASON_H) == {
            "interval_seconds": INTERVAL_S, "season_hours": SEASON_H}
        # None is forwarded, not dropped: the model must refuse with its own
        # reason instead of crashing on a missing argument
        assert _model_kwargs(seasonal, None)["interval_seconds"] is None


def test_holt_winters_runs_through_the_backtest_kwargs_route():
    result = rolling_origin_backtest(
        _seasonal_series(), horizon=6, models=("holt_winters", "naive"),
        interval_seconds=INTERVAL_S, season_hours=SEASON_H)
    assert result["status"] == "ok"
    assert "holt_winters" not in result["skipped"]
    hw = result["models"]["holt_winters"]
    assert hw["n_points"] > 0 and hw["mae"] < 4.0
    assert len(hw["per_step_mae"]) == 6
    # the season-blind naive baseline copies the previous sample and is wrecked
    # by the rhythm, which is what the skill score is meant to expose
    assert hw["mae"] < result["models"]["naive"]["mae"]
    assert hw["skill_vs_naive"] > 0.0


def test_holt_winters_without_an_interval_is_skipped_not_fabricated():
    result = rolling_origin_backtest(
        _seasonal_series(), horizon=6, models=("holt_winters",),
        interval_seconds=None, season_hours=SEASON_H)
    assert "holt_winters" in result["skipped"]
    assert result["models"] == {}
    assert result["best_model"] is None
    assert "interval" in result["skipped"]["holt_winters"].lower()
