"""Rolling-origin forecast backtest (roadmap A-4).

Answers "which baseline would actually have been right?" by replaying many
forecast origins over one history. For every origin the model is fit on data
strictly *before* the window and scored on the *next* ``horizon`` points, so a
prediction can never see its own future (the same chronological discipline the
live forecast service uses). Aggregated per model we report MAE / RMSE / MAPE,
a per-horizon-step error profile and a skill score against the naive baseline.

Deterministic and self-contained: no DB, no wall-clock, no new dependencies.
"""
from __future__ import annotations

import math

import pandas as pd

from analytics.forecasting import FORECAST_MODELS

DEFAULT_MODELS = ("naive", "holt_damped", "seasonal_naive")


def _mape(actuals, preds, eps: float = 1e-9):
    """Mean absolute percentage error, skipping near-zero actuals so the
    ratio stays defensible (returns None when nothing survives the guard)."""
    terms = [abs(a - p) / abs(a) for a, p in zip(actuals, preds)
             if abs(a) > eps]
    return (sum(terms) / len(terms) * 100.0) if terms else None


def _model_kwargs(name: str, interval_seconds):
    if name == "seasonal_naive":
        return {"interval_seconds": interval_seconds}
    return {}


def rolling_origin_backtest(
    series: pd.Series, *,
    models=DEFAULT_MODELS,
    horizon: int = 6,
    min_train: int = 24,
    step: int = 1,
    max_origins: int = 12,
    interval_seconds: float | None = None,
) -> dict:
    """Replay ``models`` across rolling origins of a chronological ``series``.

    Origins are anchored from the START (the first ``max_origins`` valid ones)
    so appending newer data never changes already-reported results - a
    leakage/consistency guarantee the tests rely on. Each model that cannot run
    (refused baseline, all-NaN window) is recorded under ``skipped`` and simply
    does not appear in the ranking. Returns ``status`` insufficient_data when
    the series is too short for even one origin.
    """
    values = pd.to_numeric(pd.Series(series), errors="coerce").dropna()
    values = values.sort_index()
    n = len(values)
    if horizon < 1:
        return {"status": "invalid_horizon", "reasons": ["horizon must be >= 1"],
                "models": {}, "skipped": {}, "origins_used": 0}
    last_start = n - horizon
    if last_start < min_train + 1:
        return {"status": "insufficient_data",
                "reasons": [f"need at least {min_train + horizon + 1} usable "
                            f"samples for one origin, has {n}"],
                "models": {}, "skipped": {}, "origins_used": 0}

    # First max_origins origins: origin index i means train=[:i], test=[i:i+h].
    starts = list(range(min_train, last_start + 1, max(1, step)))[:max_origins]
    arrays: dict[str, dict] = {
        name: {"abs": [], "sq": [], "pct": [], "step_err": [[] for _ in range(horizon)],
               "points": 0}
        for name in models
    }
    skipped: dict[str, str] = {}

    for i in starts:
        train = values.iloc[:i]
        test = values.iloc[i:i + horizon]
        test_actuals = test.to_numpy(dtype=float)
        for name in models:
            fn = FORECAST_MODELS.get(name)
            if fn is None:
                skipped[name] = "unknown model"
                continue
            try:
                res = fn(train, horizon, future_timestamps=None,
                         test=test, **_model_kwargs(name, interval_seconds))
            except Exception as exc:  # a model error is reported, never hidden
                skipped[name] = f"{type(exc).__name__}: {exc}"
                continue
            if not res.ok:
                skipped.setdefault(name, res.reason or "model refused")
                continue
            preds = res.test_pred
            bucket = arrays[name]
            for s, (a, p) in enumerate(zip(test_actuals, preds)):
                if p is None or (isinstance(p, float) and math.isnan(p)):
                    continue
                err = a - p
                bucket["abs"].append(abs(err))
                bucket["sq"].append(err * err)
                if abs(a) > 1e-9:
                    bucket["pct"].append(abs(err) / abs(a))
                if s < horizon:
                    bucket["step_err"][s].append(abs(err))
                bucket["points"] += 1

    ranking = {}
    for name, bucket in arrays.items():
        if not bucket["abs"]:
            skipped.setdefault(name, "no evaluable predictions")
            continue
        model_mae = sum(bucket["abs"]) / len(bucket["abs"])
        model_rmse = math.sqrt(sum(bucket["sq"]) / len(bucket["sq"]))
        model_mape = (sum(bucket["pct"]) / len(bucket["pct"]) * 100.0
                      if bucket["pct"] else None)
        per_step = [
            (sum(se) / len(se)) if se else None
            for se in bucket["step_err"]
        ]
        ranking[name] = {
            "mae": model_mae,
            "rmse": model_rmse,
            "mape": model_mape,
            "n_points": len(bucket["abs"]),
            "per_step_mae": per_step,
        }

    naive_mae = ranking.get("naive", {}).get("mae")
    for stats in ranking.values():
        stats["skill_vs_naive"] = (
            (1.0 - stats["mae"] / naive_mae) if naive_mae else None
        )

    best = None
    if ranking:
        best = min(ranking, key=lambda m: ranking[m]["mae"])

    return {
        "status": "ok",
        "horizon": horizon,
        "origins_used": len(starts),
        "models": ranking,
        "skipped": skipped,
        "best_model": best,
    }
