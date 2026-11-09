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

import numpy as np
import pandas as pd

from analytics.forecasting import FORECAST_MODELS, MODEL_KWARGS
from analytics.series_prep import finite_segments

DEFAULT_MODELS = ("naive", "holt_damped", "seasonal_naive")

#: Models compared by the strong backtest. SARIMA and HistGradientBoosting are
#: last because they are the slowest; the cheap baselines still provide the
#: mandatory naive reference and a fallback if a heavy model refuses.
STRONG_DEFAULT_MODELS = (
    "naive", "holt_damped", "holt_winters", "seasonal_naive",
    "ridge_lag", "hgb_lag", "sarima",
)

#: Name of the in-memory mean ensemble (never persisted as its own run).
MEAN_ENSEMBLE_NAME = "mean_ensemble"


def _mape(actuals, preds, eps: float = 1e-9):
    """Mean absolute percentage error, skipping near-zero actuals so the
    ratio stays defensible (returns None when nothing survives the guard)."""
    terms = [abs(a - p) / abs(a) for a, p in zip(actuals, preds)
             if abs(a) > eps]
    return (sum(terms) / len(terms) * 100.0) if terms else None


def _model_kwargs(name: str, interval_seconds,
                  season_hours: float = 24.0) -> dict:
    """Only the extra arguments the registry entry declares are passed on.

    ``interval_seconds`` is forwarded even when it is None: a seasonal model
    must hear "unknown interval" and refuse with its own reason, instead of
    failing on a missing argument. ``val``/``max_lags`` are not part of a
    rolling-origin replay, so models declaring them simply do not receive them.
    """
    supplied = {"interval_seconds": interval_seconds,
                "season_hours": season_hours}
    return {key: supplied[key] for key in MODEL_KWARGS.get(name, ())
            if key in supplied}


def rolling_origin_backtest(
    series: pd.Series, *,
    models=DEFAULT_MODELS,
    horizon: int = 6,
    min_train: int = 24,
    step: int = 1,
    max_origins: int = 12,
    interval_seconds: float | None = None,
    season_hours: float = 24.0,
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
                         test=test, **_model_kwargs(name, interval_seconds,
                                                    season_hours))
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


# --------------------------------------------------------------- strong models
def _strong_model_kwargs(name: str, interval_seconds, season_hours: float,
                         time_cap_seconds: float, max_lags: int) -> dict:
    """Only the arguments the registry entry actually declares."""
    supplied = {
        "interval_seconds": interval_seconds,
        "season_hours": season_hours,
        "time_cap_seconds": time_cap_seconds,
        "max_lags": max_lags,
    }
    return {key: supplied[key] for key in MODEL_KWARGS.get(name, ())
            if key in supplied}


def _new_bucket(horizon: int) -> dict:
    return {"abs": [], "sq": [], "pct": [], "mase": [],
            "step_err": [[] for _ in range(horizon)]}


def _add_point(bucket: dict, actual: float, predicted: float,
               step: int, scale: float | None) -> None:
    if not (math.isfinite(actual) and math.isfinite(predicted)):
        return
    error = actual - predicted
    bucket["abs"].append(abs(error))
    bucket["sq"].append(error * error)
    if abs(actual) > 1e-9:
        bucket["pct"].append(abs(error) / abs(actual))
    if scale is not None and scale > 1e-12:
        bucket["mase"].append(abs(error) / scale)
    if 0 <= step < len(bucket["step_err"]):
        bucket["step_err"][step].append(abs(error))


def _finalize_bucket(bucket: dict, coverage: float) -> dict | None:
    n = len(bucket["abs"])
    if not n:
        return None
    per_step = [
        (sum(values) / len(values)) if values else None
        for values in bucket["step_err"]
    ]
    pooled = sorted(bucket["abs"])
    k = max(1, min(len(pooled), math.ceil(coverage * len(pooled))))
    pooled_width = pooled[k - 1]
    widths = {}
    for step, values in enumerate(bucket["step_err"], start=1):
        if values:
            ordered = sorted(values)
            kk = max(1, min(len(ordered), math.ceil(coverage * len(ordered))))
            widths[step] = float(ordered[kk - 1])
    return {
        "mae": sum(bucket["abs"]) / n,
        "rmse": math.sqrt(sum(bucket["sq"]) / n),
        "mape": (sum(bucket["pct"]) / len(bucket["pct"]) * 100.0
                 if bucket["pct"] else None),
        "mase": (sum(bucket["mase"]) / len(bucket["mase"])
                 if bucket["mase"] else None),
        "eval_points": n,
        "per_step_mae": per_step,
        "interval_half_widths": widths,
        "pooled_half_width": float(pooled_width),
    }


def _fold_scale(train: pd.Series) -> float | None:
    values = pd.to_numeric(train, errors="coerce")
    differences = values.diff().abs().dropna()
    if differences.empty:
        return None
    scale = float(differences.mean())
    return scale if scale > 1e-12 else None


def strong_rolling_origin_backtest(
    series: pd.Series, *,
    models=STRONG_DEFAULT_MODELS,
    horizon: int = 6,
    n_folds: int = 5,
    min_train: int = 30,
    interval_seconds: float | None = None,
    season_hours: float = 24.0,
    coverage: float = 0.95,
    ensemble_size: int = 3,
    time_cap_seconds: float = 4.0,
    max_lags: int = 24,
) -> dict:
    """Rolling-origin backtest with SARIMA, gradient boosting and a mean blend.

    The prepared series may contain NaN gaps. Folds are drawn only inside
    contiguous finite runs (``finite_segments``), so a model never trains or
    scores across a gap. Origins are evenly spaced across every eligible run
    and the training window always expands from the start of that run, which is
    the standard expanding-window rolling-origin protocol.

    Per model the result carries MAE/RMSE/MAPE (zero-guarded)/MASE (computed
    against the naive one-step scale of each fold's training window), a
    per-step error profile and empirical residual-quantile interval widths.
    The mean ensemble of the best ``ensemble_size`` (2-3) models is evaluated on
    the same origins and included in the ranking, so "chosen" is always a
    measured backtest MAE winner.
    """
    if horizon < 1:
        return {"status": "invalid_horizon",
                "reasons": ["horizon must be >= 1"],
                "models": {}, "skipped": {}, "folds": 0}
    series = pd.to_numeric(pd.Series(series), errors="coerce").sort_index()
    values = series.to_numpy(dtype=float)
    index = series.index
    segments = finite_segments(values)

    origins: list[tuple[int, int, int]] = []
    for start, end in segments:
        if end - start < min_train + horizon:
            continue
        for pos in range(start + min_train, end - horizon + 1):
            origins.append((start, pos, end))
    if len(origins) < 3:
        return {
            "status": "insufficient_data",
            "reasons": [
                (f"need at least 3 rolling origins inside contiguous finite "
                 f"runs; found {len(origins)} (series has "
                 f"{int(np.isfinite(values).sum())} finite point(s) in "
                 f"{len(segments)} run(s), horizon {horizon}, min_train "
                 f"{min_train}).")
            ],
            "models": {}, "skipped": {}, "folds": 0,
            "n_finite_points": int(np.isfinite(values).sum()),
            "n_segments": len(segments),
        }

    wanted = max(3, int(n_folds))
    picks = np.unique(np.linspace(0, len(origins) - 1,
                                  num=min(wanted, len(origins))).round()
                      .astype(int))
    folds = [origins[int(i)] for i in picks]
    if len(folds) < 3:
        return {
            "status": "insufficient_data",
            "reasons": [(f"need at least 3 distinct rolling origins, found "
                         f"{len(folds)}")],
            "models": {}, "skipped": {}, "folds": len(folds),
            "n_finite_points": int(np.isfinite(values).sum()),
            "n_segments": len(segments),
        }

    buckets = {name: _new_bucket(horizon) for name in models}
    skipped: dict[str, str] = {}
    fold_predictions: list[dict[str, list[float]]] = []
    fold_actuals: list[list[float]] = []
    fold_scales: list[float | None] = []
    fold_origins: list[str] = []

    for start, pos, _end in folds:
        train = pd.Series(values[start:pos], index=index[start:pos])
        test = pd.Series(values[pos:pos + horizon],
                         index=index[pos:pos + horizon])
        actuals = test.to_numpy(dtype=float)
        scale = _fold_scale(train)
        fold_actuals.append(actuals)
        fold_scales.append(scale)
        fold_origins.append(str(index[pos - 1]))
        predictions: dict[str, list[float]] = {}
        for name in models:
            fn = FORECAST_MODELS.get(name)
            if fn is None:
                skipped[name] = "unknown model"
                continue
            try:
                res = fn(train, horizon, future_timestamps=None, test=test,
                         **_strong_model_kwargs(
                             name, interval_seconds, season_hours,
                             time_cap_seconds, max_lags))
            except Exception as exc:  # noqa: BLE001 - model error is reported
                skipped[name] = f"{type(exc).__name__}: {exc}"
                continue
            if not res.ok:
                skipped.setdefault(name, res.reason or "model refused")
                continue
            preds = list(res.test_pred) + [float("nan")] * max(
                0, horizon - len(res.test_pred))
            predictions[name] = preds[:horizon]
            for step, (actual, predicted) in enumerate(zip(actuals,
                                                           preds)):
                _add_point(buckets[name], actual, predicted, step, scale)
        fold_predictions.append(predictions)

    ranking: dict[str, dict] = {}
    for name, bucket in buckets.items():
        stats = _finalize_bucket(bucket, coverage)
        if stats is None:
            skipped.setdefault(name, "no evaluable predictions")
            continue
        ranking[name] = stats

    if not ranking:
        return {
            "status": "insufficient_data",
            "reasons": [("no model produced an evaluable prediction on any "
                         "rolling origin")],
            "models": {}, "skipped": skipped, "folds": len(folds),
            "n_finite_points": int(np.isfinite(values).sum()),
            "n_segments": len(segments),
        }

    # ---- mean ensemble of the best 2-3 single models by backtest MAE ----
    eligible = sorted(
        ((name, stats["mae"]) for name, stats in ranking.items()
         if name != MEAN_ENSEMBLE_NAME),
        key=lambda item: (item[1], item[0]))
    members = [name for name, _ in eligible[:max(2, min(3, ensemble_size))]]
    ensemble_info = None
    if len(members) >= 2:
        ensemble_bucket = _new_bucket(horizon)
        for predictions, actuals, scale in zip(fold_predictions, fold_actuals,
                                               fold_scales):
            for step, actual in enumerate(actuals):
                available = [predictions[m][step] for m in members
                             if m in predictions
                             and step < len(predictions[m])
                             and math.isfinite(predictions[m][step])]
                if not available:
                    continue
                _add_point(ensemble_bucket, actual,
                           sum(available) / len(available), step, scale)
        stats = _finalize_bucket(ensemble_bucket, coverage)
        if stats is not None:
            ranking[MEAN_ENSEMBLE_NAME] = stats
            ensemble_info = {
                "model": MEAN_ENSEMBLE_NAME,
                "members": members,
                "size": len(members),
                "method": "unweighted mean of the best models by backtest MAE",
                "mae": stats["mae"],
            }

    best = min(ranking, key=lambda name: (ranking[name]["mae"], name))
    return {
        "status": "ok",
        "horizon": horizon,
        "folds": len(folds),
        "fold_origins": fold_origins,
        "models": ranking,
        "skipped": skipped,
        "best_model": best,
        "best_criterion": "backtest MAE",
        "ensemble": ensemble_info,
        "coverage": coverage,
        "n_finite_points": int(np.isfinite(values).sum()),
        "n_segments": len(segments),
        "min_train": min_train,
        "models_requested": list(models),
    }
