"""Forecast orchestration and persistence (Phase 2 Scope E service).

Protocol implemented here (spec §8):

1. Load one series, apply the data-quality FORECAST readiness gate -
   insufficient data returns a status, never a forecast.
2. Chronological train|val|test split (never shuffled).
3. Each model is fitted on TRAIN only (tuning may use train+val). Scores use
   the intersection of finite predictions across models, so every reported
   comparison is made on identical target timestamps.
4. Hold-out error is reported on several scales - MAE/RMSE (and R^2 when
   meaningful) plus the scale-free MAPE/sMAPE/MASE and the skill against the
   mandatory naive baseline - computed against the held-out test actuals. A
   metric that is not defensible for this data stays None and is shown as
   unavailable, never as a plausible-looking zero.
5. Uncertainty bands are recalibrated from the errors the model actually made
   on its own holdout (``analytics.forecasting.apply_holdout_bands``): the
   requested nominal coverage is always reported next to the coverage measured
   on that holdout, and ``realized_coverage`` closes the loop later against
   observations that arrived after the prediction.
6. The best model is selected on hold-out MAE (documented primary criterion) and
   an inverse-MAE weighted ensemble is evaluated on the *same* hold-out
   timestamps. The ensemble is an in-memory comparison result: it is never
   persisted as a forecast run, so every stored run remains a real model fit.
7. Future forecasts come from a refit on ALL in-time data up to the forecast
   origin; predictions persist in ``forecast_predictions``, strictly separate
   from observed ``metric_samples``.
8. Actuals/errors are backfilled once real observations exist - a prediction
   never overwrites an observation.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import uuid

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from analytics import (
    data_quality,
    derived_metrics,
    evaluation,
    series_prep,
    time_series,
)
from analytics.backtest import (
    MEAN_ENSEMBLE_NAME,
    STRONG_DEFAULT_MODELS,
    strong_rolling_origin_backtest,
)
from analytics.forecasting import (
    FORECAST_MODELS,
    MODEL_KWARGS,
    apply_holdout_bands,
    realized_band_coverage,
)
from config import get_settings
from database import repository
from database.models import db_utcnow
from services.anomaly_service import load_ready_series

#: Models compared when the caller does not choose. ``seasonal_naive`` and
#: ``holt_winters`` are listed because both refuse out loud (a recorded skip
#: with the reason) when the sampling interval or the seasonal history is not
#: defensible - they never produce a quiet, wrong answer.
DEFAULT_MODELS = ("naive", "seasonal_naive", "holt_damped", "holt_winters")

#: Name under which the in-memory ensemble is reported (never persisted).
ENSEMBLE_NAME = "ensemble_inverse_mae"

#: How the service re-cuts published bands. ``empirical_sqrt_h`` uses the
#: half-width measured on the model's shared holdout errors and grows it with
#: sqrt(h), because multi-step error does usually grow; a flat empirical band
#: would be over-confident late in the horizon.
BAND_MODE = "empirical_sqrt_h"


def _series_key(device_name, interface_name, metric, data_source):
    return (device_name, interface_name, metric, data_source)


def list_forecast_series(
    session: Session, *, data_source: str | None = None,
    device_name: str | None = None, min_samples: int | None = None,
) -> list[dict]:
    """Unified forecasting-candidate inventory: raw samples + daily rollups.

    Stage A6-4 discovery. Merges :func:`repository.get_series_inventory` (raw
    ``metric_samples``) with :func:`repository.get_rollup_series_inventory`
    (daily ``metric_daily_rollups``) on the (device, interface, metric,
    data_source) identity so a series that exists in BOTH is listed exactly
    once, and a series that survives only as rollups is still discovered once
    raw history is thinned by retention.

    Each row carries ``coverage`` (``'raw'`` / ``'rollup'`` / ``'both'``), the
    separate ``n_raw_samples`` and ``n_rollup_days`` counts, and forecast
    hints. ``usable_points`` is ``max(n_raw_samples, n_rollup_days)`` - the
    number of points the *exact* series can offer on one cadence. Raw samples
    and daily rollup buckets are alternative representations of the same
    history, so adding them together would double-count and could make a thin
    series look forecastable. ``forecastable_raw`` / ``forecastable_daily``
    require that cadence alone to reach the floor, and ``forecastable`` is
    true only when ``usable_points`` does. These are cheap count-based hints;
    the authoritative check is :func:`check_series_forecastability`, which runs
    the real data-quality gate. Demo/Live isolation is preserved because both
    source queries take the same ``data_source`` filter and rollups are
    live-only.
    """
    settings = get_settings()
    floor = (settings.min_points_forecast if min_samples is None
             else max(1, int(min_samples)))
    raw_rows = repository.get_series_inventory(
        session, data_source=data_source, device_name=device_name)
    rollup_rows = repository.get_rollup_series_inventory(
        session, data_source=data_source, device_name=device_name)

    merged: dict[tuple, dict] = {}
    for r in raw_rows:
        key = _series_key(r["device_name"], r["interface_name"],
                          r["metric"], r["data_source"])
        merged[key] = {
            "device_name": r["device_name"], "metric": r["metric"],
            "interface_name": r["interface_name"], "unit": r["unit"],
            "data_source": r["data_source"],
            "n_raw_samples": int(r["n_samples"]), "n_rollup_days": 0,
            "coverage": "raw",
        }
    for r in rollup_rows:
        key = _series_key(r["device_name"], r["interface_name"],
                          r["metric"], r["data_source"])
        existing = merged.get(key)
        if existing is not None:
            existing["n_rollup_days"] = int(r["n_days"])
            existing["coverage"] = "both"
            if existing["unit"] is None:
                existing["unit"] = r["unit"]
        else:
            merged[key] = {
                "device_name": r["device_name"], "metric": r["metric"],
                "interface_name": r["interface_name"], "unit": r["unit"],
                "data_source": r["data_source"],
                "n_raw_samples": 0, "n_rollup_days": int(r["n_days"]),
                "coverage": "rollup",
            }

    out: list[dict] = []
    for row in merged.values():
        # Raw samples and rollup days describe the same history at two
        # cadences; summing them would double-count overlapping days and let
        # rollups inflate a thin series past the forecast floor.
        usable = max(row["n_raw_samples"], row["n_rollup_days"])
        row["usable_points"] = usable
        # Kept for callers that display the raw+rollup totals; never used for
        # ranking or forecastability.
        row["total_points"] = row["n_raw_samples"] + row["n_rollup_days"]
        row["forecastable_raw"] = row["n_raw_samples"] >= floor
        row["forecastable_daily"] = row["n_rollup_days"] >= floor
        row["forecastable"] = usable >= floor
        out.append(row)
    out.sort(key=lambda row: (-row["usable_points"], row["device_name"],
                              row["metric"], row["interface_name"] or ""))
    return out


def rank_forecast_series(
    inventory: list[dict], *, data_source: str | None = None,
) -> list[dict]:
    """Order forecast candidates so the first load lands on real data.

    The page used to take the alphabetically first device/metric, which on a
    live deployment can be a placeholder (raw IP, ``target-*``,
    ``unresponsive-path:*``) or a two-sample series. This ranks the inventory
    that :func:`list_forecast_series` already produced:

    * placeholders are dropped entirely - they are not measured entities;
    * a series the inventory marks forecastable comes first, so a default is
      never a series that cannot reach the point floor;
    * a ``ping.*`` series comes ahead of other metrics, and
      ``traceroute.hop_*`` comes last, because a hop RTT is a derived path
      internal rather than a device health metric;
    * within the same forecastability/metric group the series with the most
      ``usable_points`` wins. ``usable_points`` is ``max(raw, rollup)``, not
      their sum: the two representations cover the same history, so adding
      them would let rollups inflate a thin series;
    * the remaining order is deterministic (device, metric, scope).

    Only stored counts are used - this is a read-only ranking, no query and no
    forecast is run.
    """
    rows = [row for row in inventory
            if (data_source is None or row.get("data_source") == data_source)
            and not repository.is_placeholder_device_name(
                row.get("device_name"))]

    def _metric_rank(metric: str) -> int:
        metric = str(metric or "")
        if metric.startswith("ping."):
            return 0
        if metric.startswith("traceroute.hop_"):
            return 2
        return 1

    floor = int(get_settings().min_points_forecast)

    def _usable(row: dict) -> int:
        # ``usable_points`` is the authoritative field. Fall back to the old
        # total only for hand-built inventories that predate it.
        if "usable_points" in row:
            return int(row.get("usable_points") or 0)
        return int(row.get("total_points") or 0)

    def _key(row: dict) -> tuple:
        usable = _usable(row)
        forecastable = row.get("forecastable")
        if forecastable is None:
            forecastable = usable >= floor
        return (
            0 if forecastable else 1,
            _metric_rank(row.get("metric")),
            -usable,
            str(row.get("device_name") or ""),
            str(row.get("metric") or ""),
            str(row.get("interface_name") or ""),
        )

    return sorted(rows, key=_key)


def latest_regular_run(
    frame: pd.DataFrame, interval_seconds: float | None, *,
    min_points: int = 1, gap_factor: float | None = None,
) -> dict | None:
    """Most recent contiguous, gap-free run of observations.

    A live collector can sample one burst densely and then stop for hours. A
    window that spans the burst *and* a later collection gap is genuinely
    irregular, and the data-quality gate rightly refuses it. The models,
    however, are only ever fit inside one contiguous run. This helper finds the
    run the pipeline can actually use: consecutive observations no further
    apart than ``gap_factor * interval_seconds`` (the same threshold the
    quality report uses for ``gap_count``), taking the newest run that reaches
    ``min_points``.

    Returns ``{"start", "end", "n_points"}`` or ``None`` when no run is long
    enough. Pure function: no DB, no Streamlit, and it never alters the frame.
    """
    if frame is None or frame.empty or "collected_at" not in frame:
        return None
    if not interval_seconds or float(interval_seconds) <= 0:
        return None
    stamps = (pd.to_datetime(frame["collected_at"]).dropna()
              .sort_values().drop_duplicates())
    floor = max(1, int(min_points))
    if len(stamps) < floor:
        return None
    factor = float(data_quality.GAP_FACTOR if gap_factor is None
                   else gap_factor)
    threshold = pd.Timedelta(seconds=factor * float(interval_seconds))
    runs: list[tuple] = []
    start = prev = stamps.iloc[0]
    count = 1
    for stamp in stamps.iloc[1:]:
        if (stamp - prev) > threshold:
            runs.append((start, prev, count))
            start = stamp
            count = 1
        else:
            count += 1
        prev = stamp
    runs.append((start, prev, count))
    for run_start, run_end, run_count in reversed(runs):
        if run_count >= floor:
            return {"start": _to_dt(run_start), "end": _to_dt(run_end),
                    "n_points": int(run_count)}
    return None


def latest_regular_run_window(
    session: Session, *, device_name: str, metric: str,
    interface_name: str | None, data_source: str, cadence: str = "raw",
    device_level_only: bool = False, min_points: int | None = None,
    gap_factor: float | None = None,
) -> dict | None:
    """Read-only: the newest gap-free run long enough to forecast.

    Loads the stored history through the same pipeline a forecast uses, then
    delegates to :func:`latest_regular_run`. The page turns the returned bounds
    into an explicit ``[start, end]`` window, so the readiness gate runs on a
    genuinely regular grid instead of being weakened.
    """
    frame, quality = load_ready_series(
        session, device_name, metric, interface_name, data_source,
        purpose=data_quality.FORECAST, since=None, until=None,
        device_level_only=device_level_only, include_rollups=True,
        cadence=cadence)
    run = latest_regular_run(
        frame, quality.observed_interval_seconds,
        min_points=(STRONG_MIN_POINTS if min_points is None else min_points),
        gap_factor=gap_factor)
    if run is None:
        return None
    run["cadence"] = cadence
    run["interval_seconds"] = quality.observed_interval_seconds
    return run


def check_series_forecastability(
    session: Session, *, device_name: str, metric: str,
    interface_name: str | None, data_source: str,
    since: dt.datetime | None = None, until: dt.datetime | None = None,
    cadence: str = "raw",
    device_level_only: bool = False,
) -> dict:
    """Authoritative 'can this exact selection be forecast right now?' check.

    Loads the series through the same pipeline the forecast uses
    (:func:`load_ready_series` with the requested ``cadence``) and reports the
    real data-quality gate result, so the UI enables forecasting actions only
    on a genuinely forecastable window rather than a count estimate. It returns
    the readiness flag, human-readable reasons, the usable-observation count,
    the observed interval, and the raw/rollup provenance split. No forecast is
    computed and nothing is persisted - this is a read-only probe.

    ``until`` closes the window when the caller is evaluating one gap-free run
    (see :func:`latest_regular_run_window`); the readiness rules are unchanged.
    """
    frame, quality = load_ready_series(
        session, device_name, metric, interface_name, data_source,
        purpose=data_quality.FORECAST, since=since, until=until,
        device_level_only=device_level_only, include_rollups=True,
        cadence=cadence)
    provenance = time_series.frame_provenance(frame)
    ready = bool(quality.ready(data_quality.FORECAST))
    reasons = list(quality.reasons_for(data_quality.FORECAST) or quality.reasons)
    if (not ready and cadence != "daily"
            and provenance.get("mixes_granularity")):
        reasons.append(
            "forecast: window spans two sampling granularities; switch to the "
            "daily cadence or pick a single-granularity window.")
    return {
        "ready": ready,
        "cadence": cadence,
        "reasons": reasons,
        "n_observations": int(frame["value"].notna().sum()),
        "observed_interval_seconds": quality.observed_interval_seconds,
        "provenance": provenance,
    }


# ------------------------------------------------------- strong models (A)
#: Minimum usable points before a strong-model backtest is attempted. The same
#: floor is stated in the UI message, so a refusal can never be vague.
STRONG_MIN_POINTS = series_prep.MIN_POINTS
STRONG_MODELS = STRONG_DEFAULT_MODELS


def _strong_future(name: str, finite: pd.Series, horizon: int,
                   future_timestamps: list[pd.Timestamp],
                   interval_seconds: float | None, season_hours: float,
                   time_cap_seconds: float, max_lags: int):
    """Fit one registry model on the full finite run and return its future."""
    fn = FORECAST_MODELS.get(name)
    if fn is None:
        return None, "unknown model"
    supplied = {
        "interval_seconds": interval_seconds,
        "season_hours": season_hours,
        "time_cap_seconds": time_cap_seconds,
        "max_lags": max_lags,
    }
    kwargs = {key: supplied[key] for key in MODEL_KWARGS.get(name, ())
              if key in supplied and supplied[key] is not None}
    try:
        res = fn(finite, horizon,
                 future_timestamps=future_timestamps or None, **kwargs)
    except Exception as exc:  # noqa: BLE001 - model error is reported
        return None, f"{type(exc).__name__}: {exc}"
    if not res.ok:
        return None, res.reason or "model refused"
    return res, None


def compute_strong_forecast(
    series: pd.Series, *,
    horizon: int = 10,
    models: tuple[str, ...] = STRONG_MODELS,
    interval_seconds: float | None = None,
    season_hours: float = 24.0,
    coverage: float = 0.95,
    n_folds: int = 5,
    min_train: int = STRONG_MIN_POINTS,
    ensemble_size: int = 3,
    time_cap_seconds: float = 4.0,
    max_lags: int = 24,
    series_hash: str | None = None,
) -> dict:
    """Backtest strong models on a prepared series and forecast its future.

    Pure function (no DB, no Streamlit) so the page can cache it with
    ``st.cache_data`` keyed by ``series_hash``. The series is expected to be the
    regular-grid output of :func:`analytics.series_prep.prepare_regular_series`;
    NaN values mark long gaps and are never bridged.

    The result separates two counts that used to be conflated as ``n_points``:
    ``series_len`` is the number of usable points in the single contiguous run
    used for the final fit, and ``eval_points`` is how many points the chosen
    model actually scored across the rolling-origin folds.
    """
    floor = max(int(STRONG_MIN_POINTS), int(min_train))
    values = pd.to_numeric(pd.Series(series), errors="coerce")
    finite = values.dropna()
    n_usable = len(finite)

    # The final refit must use ONE contiguous run as well: concatenating across
    # a NaN gap would invent a transition that never happened. Prefer the most
    # recent run (the natural forecast origin); fall back to the longest run
    # only when the latest one is too short to fit anything.
    segments = series_prep.finite_segments(values.to_numpy(dtype=float))
    if segments:
        start, end = segments[-1]
        if end - start < 20:
            start, end = max(segments, key=lambda bounds: bounds[1] - bounds[0])
        fit_run = values.iloc[start:end]
    else:
        fit_run = finite
    series_len = len(fit_run)

    base = {
        "status": "insufficient_data", "series_len": series_len,
        "eval_points": 0, "n_usable_points": n_usable,
        "n_grid_points": len(values), "series_hash": series_hash,
        "interval_seconds": interval_seconds, "coverage": coverage,
        "models": {}, "backtest": {}, "forecast": [],
        "best_model": None, "chosen_model": None,
        "best_criterion": "backtest MAE",
    }
    if n_usable < floor:
        base["message"] = series_prep.insufficient_data_message(n_usable, floor)
        return base

    bt = strong_rolling_origin_backtest(
        values, models=tuple(models), horizon=horizon, n_folds=n_folds,
        min_train=min_train, interval_seconds=interval_seconds,
        season_hours=season_hours, coverage=coverage,
        ensemble_size=ensemble_size, time_cap_seconds=time_cap_seconds,
        max_lags=max_lags)
    if bt.get("status") != "ok":
        base.update({"backtest": bt,
                     "message": "; ".join(bt.get("reasons") or [bt["status"]])})
        return base

    best = bt["best_model"]
    ranked = sorted(bt["models"].items(), key=lambda kv: (kv[1]["mae"],
                                                          kv[0]))
    origin = fit_run.index.max()
    if interval_seconds and isinstance(fit_run.index, pd.DatetimeIndex):
        future_timestamps = [
            origin + pd.Timedelta(seconds=float(interval_seconds) * (h + 1))
            for h in range(horizon)
        ]
    else:
        future_timestamps = []

    fit_names = ([m for m in (bt.get("ensemble") or {}).get("members", [])]
                 if best == MEAN_ENSEMBLE_NAME else [best])
    if best == MEAN_ENSEMBLE_NAME:
        # Only the blend's own members may enter its average; a fallback to a
        # non-member is handled separately below and changes ``chosen``.
        attempts = fit_names
    else:
        attempts = [best] + [name for name, _ in ranked
                             if name != best
                             and name != MEAN_ENSEMBLE_NAME]
    chosen = best
    chosen_stats = bt["models"].get(best, {})
    forecasts: list[list[float]] = []
    fit_notes: dict[str, str] = {}
    used_names: list[str] = []
    for name in attempts:
        if name == MEAN_ENSEMBLE_NAME:
            continue
        res, reason = _strong_future(
            name, fit_run, horizon, future_timestamps, interval_seconds,
            season_hours, time_cap_seconds, max_lags)
        if res is None:
            fit_notes[name] = reason or "no future forecast"
            continue
        preds = [float(f.get("predicted_value", float("nan")))
                 for f in res.future]
        if len(preds) < horizon:
            preds += [float("nan")] * (horizon - len(preds))
        if not any(math.isfinite(p) for p in preds):
            fit_notes[name] = "model produced no finite future steps"
            continue
        forecasts.append(preds[:horizon])
        used_names.append(name)
        if best == MEAN_ENSEMBLE_NAME:
            if len(forecasts) >= len(fit_names):
                break
        else:
            break

    if best == MEAN_ENSEMBLE_NAME and not forecasts:
        # The blend's members all failed to refit: fall back to the best single
        # model and say so rather than publishing an empty "ensemble".
        for name, _ in ranked:
            if name == MEAN_ENSEMBLE_NAME:
                continue
            res, reason = _strong_future(
                name, fit_run, horizon, future_timestamps, interval_seconds,
                season_hours, time_cap_seconds, max_lags)
            if res is None:
                fit_notes[name] = reason or "no future forecast"
                continue
            preds = [float(f.get("predicted_value", float("nan")))
                     for f in res.future]
            if not any(math.isfinite(p) for p in preds):
                fit_notes[name] = "model produced no finite future steps"
                continue
            forecasts = [preds + [float("nan")] * (horizon - len(preds))]
            used_names = [name]
            chosen = name
            chosen_stats = bt["models"].get(name, {})
            break
    elif best != MEAN_ENSEMBLE_NAME and not forecasts:
        for name, stats in ranked:
            if name in used_names or name == MEAN_ENSEMBLE_NAME:
                continue
            res, reason = _strong_future(
                name, fit_run, horizon, future_timestamps, interval_seconds,
                season_hours, time_cap_seconds, max_lags)
            if res is None:
                fit_notes[name] = reason or "no future forecast"
                continue
            preds = [float(f.get("predicted_value", float("nan")))
                     for f in res.future]
            if not any(math.isfinite(p) for p in preds):
                fit_notes[name] = "model produced no finite future steps"
                continue
            forecasts = [preds + [float("nan")] * (horizon - len(preds))]
            used_names = [name]
            chosen = name
            chosen_stats = stats
            break

    widths = (chosen_stats.get("interval_half_widths") or {})
    pooled = chosen_stats.get("pooled_half_width")
    rows: list[dict] = []
    if forecasts and future_timestamps:
        for step in range(1, horizon + 1):
            available = [pred[step - 1] for pred in forecasts
                         if step - 1 < len(pred) and math.isfinite(pred[step - 1])]
            if not available:
                continue
            predicted = sum(available) / len(available)
            half = widths.get(step, pooled)
            if half is None or not math.isfinite(float(half)):
                half = 0.0
            half = max(0.0, float(half))
            rows.append({
                "target_timestamp": future_timestamps[step - 1],
                "step": step, "predicted_value": predicted,
                "lower_bound": predicted - half,
                "upper_bound": predicted + half,
                "interval_half_width": half,
                "method": ("mean ensemble" if len(used_names) > 1
                           else used_names[0] if used_names else chosen),
            })

    result = dict(base)
    result.update({
        "status": "ok",
        "backtest": bt,
        "models": bt["models"],
        "best_model": best,
        "chosen_model": chosen,
        "chosen_members": used_names if len(used_names) > 1 else [],
        "best_criterion": "backtest MAE",
        "forecast": rows,
        "series_len": series_len,
        "eval_points": (chosen_stats or {}).get("eval_points"),
        "forecast_note": (
            None if rows else
            "no future steps could be published (the selected model's "
            "full-history refit produced none)"),
        "fit_notes": fit_notes,
        "ensemble": bt.get("ensemble"),
        "n_folds": bt.get("folds"),
        "n_segments": bt.get("n_segments"),
    })
    return result


def run_strong_forecast(
    session: Session, *,
    device_name: str, metric: str,
    interface_name: str | None, data_source: str,
    horizon: int | None = None,
    models: tuple[str, ...] = STRONG_MODELS,
    interval_seconds: float | None = None,
    max_gap_steps: int = series_prep.DEFAULT_MAX_GAP_STEPS,
    n_folds: int = 5,
    min_train: int = STRONG_MIN_POINTS,
    coverage: float = 0.95,
    ensemble_size: int = 3,
    since: dt.datetime | None = None,
    time_cap_seconds: float = 4.0,
    max_lags: int = 24,
    device_level_only: bool = False,
) -> dict:
    """Load one series read-only, regularise it, then run the strong backtest.

    This is the DB-facing entry point. It uses the app's existing read path
    (:func:`services.anomaly_service.load_ready_series`), never writes, and
    returns the regularisation provenance alongside the measured backtest.
    """
    settings = get_settings()
    horizon = horizon or settings.forecast_horizon_default
    frame, quality = load_ready_series(
        session, device_name, metric, interface_name, data_source,
        purpose=data_quality.FORECAST, since=since,
        device_level_only=device_level_only, include_rollups=False,
        cadence="raw")
    frame, counter_meta = derived_metrics.forecast_input_frame(
        frame, metric,
        max_gap_seconds=max(float(settings.max_rate_gap_seconds),
                            (quality.observed_interval_seconds or 0) * 1.5))
    interval = (float(interval_seconds) if interval_seconds
                else (quality.observed_interval_seconds
                      or series_prep.DEFAULT_INTERVAL_SECONDS))
    prepared = series_prep.prepare_regular_series(
        frame, interval_seconds=interval, max_gap_steps=max_gap_steps)
    result = compute_strong_forecast(
        prepared["series"], horizon=horizon, models=tuple(models),
        interval_seconds=interval, season_hours=settings.season_hours,
        coverage=coverage, n_folds=n_folds, min_train=min_train,
        ensemble_size=ensemble_size, time_cap_seconds=time_cap_seconds,
        max_lags=max_lags, series_hash=prepared["series_hash"])
    result.update({
        "device_name": device_name, "metric": metric,
        "interface_name": interface_name, "data_source": data_source,
        "unit": prepared.get("unit"),
        "counter": counter_meta,
        "interval_seconds": interval,
        "n_grid_points": prepared["n_points"],
        "prep": {key: prepared[key] for key in (
            "n_raw_rows", "n_duplicates", "n_points", "n_observed",
            "n_imputed", "n_missing", "n_long_gaps", "interval_seconds",
            "max_gap_steps", "series_hash")},
        "quality_ready": bool(quality.ready(data_quality.FORECAST)),
        "quality_reasons": list(quality.reasons_for(data_quality.FORECAST)
                                or quality.reasons),
    })
    return result


def strong_comparison_rows(result: dict) -> list[dict]:
    """The exact comparison table the UI and the proof export share.

    ``series_len`` is the number of usable points in the contiguous run used
    for the final fit; ``eval_points`` is how many points that model actually
    scored across the rolling-origin folds (per model, and it may exceed
    ``series_len`` because every fold is scored). ``skill_vs_naive`` is the
    MAE ratio on the points that model and naive both scored; a perfect naive
    baseline leaves it undefined (None) with ``naive_perfect=True``.
    """
    rows = []
    for name, stats in (result.get("backtest") or {}).get("models", {}).items():
        rows.append({
            "source": result.get("data_source"),
            "device": result.get("device_name"),
            "metric": result.get("metric"),
            "series_len": result.get("series_len"),
            "eval_points": stats.get("eval_points"),
            "model": name,
            "MAE": stats.get("mae"),
            "RMSE": stats.get("rmse"),
            "MAPE": stats.get("mape"),
            "MASE": stats.get("mase"),
            "skill_vs_naive": stats.get("skill_vs_naive"),
            "wins_vs_naive": stats.get("wins_vs_naive"),
            "naive_perfect": stats.get("naive_perfect"),
            "skill_eval_points": stats.get("skill_eval_points"),
        })
    return rows


def run_forecast_comparison(
    session: Session, *,
    device_name: str, metric: str,
    interface_name: str | None, data_source: str,
    horizon: int | None = None,
    models: tuple[str, ...] = DEFAULT_MODELS,
    persist: bool = True,
    device_level_only: bool = False,
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
    include_rollups: bool = True,
    cadence: str = "raw",
    band_mode: str = BAND_MODE,
    band_coverage: float | None = None,
    ensemble: bool = True,
) -> dict:
    settings = get_settings()
    horizon = horizon or settings.forecast_horizon_default
    frame, quality = load_ready_series(
        session, device_name, metric, interface_name, data_source,
        purpose=data_quality.FORECAST, device_level_only=device_level_only,
        since=since, until=until, include_rollups=include_rollups,
        cadence=cadence)
    # Stage A6-3/A6-4 provenance: how much of this history is raw vs daily
    # rollups. The daily cadence always reads rollups, so it reports provenance
    # even when include_rollups was left at its default.
    reads_rollups = include_rollups or cadence == "daily"
    provenance = (time_series.frame_provenance(frame) if reads_rollups
                  else {time_series.ORIGIN_RAW: int(frame["value"].notna().sum()),
                        time_series.ORIGIN_ROLLUP: 0,
                        "mixes_granularity": False})
    if not quality.ready(data_quality.FORECAST):
        reasons = (quality.reasons_for(data_quality.FORECAST)
                   or quality.reasons)
        # The mixed-granularity warning is specific to the native-sampling
        # cadence. The daily cadence has already reconciled raw and rollup onto
        # one UTC-day grid, so a blend of provenances there is expected and not
        # a refusal cause - the safety gate stays exactly as written for raw.
        if cadence != "daily" and provenance.get("mixes_granularity"):
            reasons = list(reasons) + [
                ("forecast: history spans two sampling granularities (recent "
                 "raw samples plus older daily rollups), which is not a "
                 "regular grid. Choose a window covered entirely by raw "
                 "samples or entirely by daily rollups, or switch to the "
                 "daily cadence - the pipeline never silently blends "
                 "aggregation levels.")]
        return {"status": "insufficient_data",
                "reasons": reasons, "runs": [], "cadence": cadence,
                "history_provenance": provenance,
                "history_since": since, "history_until": until}

    # A cumulative counter is modelled as its per-interval rate, never as the
    # running total; reset/wrap and oversized-gap intervals are omitted by the
    # same Phase-2 rules the data-analysis page uses.
    frame, counter_meta = derived_metrics.forecast_input_frame(
        frame, metric,
        max_gap_seconds=max(float(settings.max_rate_gap_seconds),
                            (quality.observed_interval_seconds or 0) * 1.5))

    # Time-indexed series for chronological integrity (index = timestamps).
    valid = frame.dropna(subset=["value"]).copy()
    if "imputed" in valid and valid["imputed"].any():
        valid = valid[~valid["imputed"]]  # only real observations are fitted
    series = pd.Series(valid["value"].to_numpy(),
                       index=pd.DatetimeIndex(valid["collected_at"]))
    split = evaluation.chronological_split(
        series, test_frac=0.2, val_frac=0.15,
        min_train=max(12, settings.min_points_forecast // 2), min_test=5)
    if not split.has_test:
        return {"status": "insufficient_data",
                "reasons": [f"forecast: holdout test segment needs >= 5 "
                            f"samples, split produced {len(split.test)}. "
                            f"Series has {len(series)} usable samples."],
                "runs": [], "cadence": cadence, "history_provenance": provenance,
                "history_since": since, "history_until": until}

    interval = quality.observed_interval_seconds
    origin = series.index.max()
    comparison_group = str(uuid.uuid4())
    unit = (frame["unit"].dropna().iloc[-1]
            if not frame.empty and frame["unit"].notna().any() else None)
    coverage = (settings.band_nominal_coverage if band_coverage is None
                else min(0.999, max(0.5, float(band_coverage))))

    runs: list[dict] = []
    ok_runs: list[dict] = []      # the same dict objects, post-processed below
    test_actuals = [float(a) for a in split.test.to_numpy(dtype=float)]
    for name in models:
        fn = FORECAST_MODELS.get(name)
        if fn is None:
            runs.append({"model": name, "status": "unknown_model"})
            continue
        # A model only ever receives the extra arguments it declares, so adding
        # a model cannot make an existing call pass an unexpected keyword.
        supplied = {"interval_seconds": interval,
                    "season_hours": settings.season_hours,
                    "val": split.val}
        kwargs = {k: supplied[k] for k in MODEL_KWARGS.get(name, ())
                  if supplied.get(k) is not None}
        # ---- holdout evaluation (fit on train, predict test) ------------
        try:
            eval_res = fn(split.train, horizon,
                          future_timestamps=None, test=split.test, **kwargs)
        except Exception as exc:  # model failure -> reported, never hidden
            runs.append({"model": name, "status": "error",
                         "reason": f"{type(exc).__name__}: {exc}"})
            continue
        if not eval_res.ok:
            # An honest refusal is recorded (and persisted) with its reason:
            # "this model is not defensible here" is a result, not a gap.
            runs.append({"model": name, "status": "skipped",
                         "reason": eval_res.reason,
                         "model_version": eval_res.model_version,
                         "config": eval_res.config})
            continue
        pairs = [(a, p) for a, p in zip(test_actuals, eval_res.test_pred)
                 if math.isfinite(a) and not _isnan(p)]
        act = [a for a, _ in pairs]
        prd = [p for _, p in pairs]
        abs_errors = [abs(a - p) for a, p in pairs]
        mae = evaluation.mae(act, prd)
        rmse = evaluation.rmse(act, prd)
        r2v = evaluation.r2(act, prd)
        mape = evaluation.mape(act, prd)
        smape = evaluation.smape(act, prd)
        mase = evaluation.mase(act, prd, split.train)
        n_eval = len(prd)

        # ---- future forecast: refit on ALL in-time data ------------------
        full = pd.Series(series.to_numpy(), index=series.index)
        fut_kwargs = dict(kwargs)
        fut_kwargs.pop("val", None)  # refit on full history incl. val/test
        future_error = None
        try:
            steps = [origin + pd.Timedelta(seconds=interval * (h + 1))
                     for h in range(horizon)] if interval else []
            future_res = fn(full, horizon,
                            future_timestamps=steps or None, **fut_kwargs)
        except Exception as exc:  # a failed refit is reported, never hidden
            future_res = None
            future_error = f"{type(exc).__name__}: {exc}"
        raw_future = list(future_res.future) if future_res else []
        # Published bands come from the errors this model really made on its
        # holdout, not from an untested normal approximation. ``raw_future`` is
        # kept alongside so the UI can show what the approximation claimed.
        future, band_meta = apply_holdout_bands(raw_future, abs_errors,
                                               coverage=coverage,
                                               mode=band_mode)
        run_row = {
            "model": name,
            "model_version": eval_res.model_version,
            "config": eval_res.config,
            "status": "ok",
            "mae": mae, "rmse": rmse, "r2": r2v, "n_eval": n_eval,
            "mape": mape, "smape": smape, "mase": mase,
            # filled in after the loop, once the naive reference exists
            "skill": None,
            # holdout predictions + their timestamps, kept in-memory for the
            # UI and for the identical-timestamp comparison guarantee
            "test_pred": eval_res.test_pred,
            "test_timestamps": list(split.test.index),
            "scored_timestamps": [ts for ts, (a, p) in zip(
                split.test.index, zip(test_actuals, eval_res.test_pred))
                if math.isfinite(a) and not _isnan(p)],
            "n_train": len(split.train), "n_val": len(split.val),
            "n_test": len(split.test),
            "train_start": series.index.min(), "train_end": split.train_end,
            "val_end": split.val_end, "test_end": split.test_end,
            "origin_at": _to_dt(origin),
            "horizon_steps": horizon,
            "interval_seconds": interval,
            "future": future,
            "band": band_meta,
            "_raw_future": raw_future,
            "band_nominal_coverage": coverage,
            "n_holdout_errors": len(abs_errors),
        }
        if not raw_future and interval is None:
            run_row["future_note"] = (
                "sampling interval unknown - future steps cannot be "
                "timestamped, so none are published")
        elif future_error is not None:
            run_row["future_error"] = future_error
            run_row["future_note"] = (
                "the full-history refit failed, so no future forecast is "
                "published for this model; the holdout evaluation above is "
                "still the real measured result")
        elif not raw_future:
            run_row["future_note"] = (
                "the full-history refit produced no future points, so none "
                "are published")
        runs.append(run_row)
        ok_runs.append(run_row)

    # All model rankings and skills use the intersection of finite predictions.
    # A direct-horizon model or seasonal baseline may have a shorter valid
    # window; candidates are therefore compared on exactly the same timestamps.
    common_idx = evaluation.common_finite_indices(
        test_actuals, [r["test_pred"] for r in ok_runs])
    common_actual = [test_actuals[i] for i in common_idx]
    common_timestamps = [split.test.index[i] for i in common_idx]
    for r in ok_runs:
        common_pred = [float(r["test_pred"][i]) for i in common_idx]
        r.update({
            "n_eval": len(common_idx),
            "mae": evaluation.mae(common_actual, common_pred),
            "rmse": evaluation.rmse(common_actual, common_pred),
            "r2": evaluation.r2(common_actual, common_pred),
            "mape": evaluation.mape(common_actual, common_pred),
            "smape": evaluation.smape(common_actual, common_pred),
            "mase": evaluation.mase(common_actual, common_pred, split.train),
            "scored_timestamps": common_timestamps,
            "n_holdout_errors": len(common_idx),
        })
        errors = [abs(a - p) for a, p in zip(common_actual, common_pred)]
        r["future"], r["band"] = apply_holdout_bands(
            r["_raw_future"], errors, coverage=coverage, mode=band_mode)

    # ---- cross-model results on one shared holdout ---------------------
    naive_row = next((r for r in ok_runs if r["model"] == "naive"), None)
    for r in ok_runs:
        if r is naive_row:
            r["skill"] = 0.0      # the baseline's skill against itself, by definition
        elif naive_row is None:
            r["skill"] = None     # no reference to measure improvement against
        else:
            aligned = [(a, r["test_pred"][i], naive_row["test_pred"][i])
                       for i, a in zip(common_idx, common_actual)]
            r["skill"] = evaluation.skill_vs_naive(
                [x[0] for x in aligned], [x[1] for x in aligned],
                [x[2] for x in aligned])
    scored = [r for r in ok_runs if r.get("mae") is not None]
    best_row = (min(scored, key=lambda r: (r["mae"], r["model"]))
                if scored else None)
    best_model = best_row["model"] if best_row else None
    ensemble_result = (build_ensemble(ok_runs, test_actuals,
                                      train=split.train,
                                      coverage=coverage,
                                      band_mode=band_mode)
                       if ensemble else
                       {"model": ENSEMBLE_NAME, "status": "disabled",
                        "reason": "ensemble blending not requested"})

    # ---- persistence (after the cross-model pass, so stored rows are complete)
    if persist:
        for r in runs:
            if r.get("status") == "ok":
                run = repository.add_forecast_run(
                    session,
                    comparison_group=comparison_group,
                    device_name=device_name, interface_name=interface_name,
                    metric=metric, target_unit=unit, data_source=data_source,
                    model_name=r["model"],
                    model_version=r["model_version"],
                    model_config_json=json.dumps(r["config"], sort_keys=True),
                    horizon_steps=horizon, interval_seconds=interval,
                    origin_at=r["origin_at"],
                    n_train=r["n_train"], n_val=r["n_val"], n_test=r["n_test"],
                    train_start=_to_dt(r["train_start"]),
                    train_end=_to_dt(r["train_end"]),
                    val_end=_to_dt(r["val_end"]),
                    test_end=_to_dt(r["test_end"]),
                    mae=r["mae"], rmse=r["rmse"], r2=r["r2"],
                    n_eval=r["n_eval"],
                    mape=r["mape"], smape=r["smape"], mase=r["mase"],
                    skill=r["skill"],
                    band_meta_json=json.dumps(r["band"], sort_keys=True,
                                              default=str),
                    status="ok", message=None,
                )
                if r["future"]:
                    rows = [{"target_timestamp": _to_dt(f["target_timestamp"]),
                             "step": f["step"],
                             "predicted_value": f["predicted_value"],
                             "lower_bound": f.get("lower_bound"),
                             "upper_bound": f.get("upper_bound")}
                            for f in r["future"]]
                    repository.add_forecast_predictions(session, run, rows)
                r["run_id"] = run.id
            elif r.get("status") == "skipped":
                repository.add_forecast_run(
                    session, comparison_group=comparison_group,
                    device_name=device_name, interface_name=interface_name,
                    metric=metric, target_unit=unit,
                    data_source=data_source, model_name=r["model"],
                    model_version=r["model_version"],
                    model_config_json=json.dumps(r["config"], sort_keys=True),
                    horizon_steps=horizon, interval_seconds=interval,
                    origin_at=_to_dt(origin),
                    n_train=len(split.train), n_val=len(split.val),
                    n_test=len(split.test),
                    status="insufficient_data",
                    message=r["reason"])
        session.commit()
    for r in runs:
        r.pop("_raw_future", None)
    return {"status": "ok", "comparison_group": comparison_group,
            "cadence": cadence,
            "runs": runs, "split": split, "quality": quality,
            "unit": unit, "counter": counter_meta,
            "history_provenance": provenance,
            "history_since": since, "history_until": until,
            "best_model": best_model, "best_criterion": "holdout MAE",
            "ensemble": ensemble_result,
            "identical_holdout": bool(ok_runs) and len({
                tuple(pd.Timestamp(ts).isoformat()
                      for ts in r["scored_timestamps"])
                for r in ok_runs}) <= 1,
            "band_nominal_coverage": coverage, "band_mode": band_mode,
            "models_requested": list(models)}


def _isnan(x) -> bool:
    if x is None:
        return True
    try:
        return not math.isfinite(float(x))
    except (TypeError, ValueError):
        return True


def _to_dt(x) -> dt.datetime:
    """Convert a pandas Timestamp (possibly sub-microsecond precision from
    float-interval arithmetic) to a plain datetime without discarding ns
    silently (pandas 3.0 warns otherwise)."""
    ts = pd.Timestamp(x)
    return (ts.to_pydatetime() if ts.unit in ("s", "ms", "us")
            else ts.round("ms").to_pydatetime())


def backfill_actuals(session: Session, run_id: int,
                     tolerance_seconds: float = 1.0) -> int:
    """Fill actual_value/abs_error for predictions whose target timestamp
    now has an observation. Observations are never modified."""
    run = session.get(repository.ForecastRun, run_id)
    if run is None:
        return 0
    from database.models import MetricSample
    filled = 0
    for pred in repository.get_unbackfilled_predictions(session, run_id):
        stmt = (
            select(MetricSample)
            .where(
                MetricSample.device_name == run.device_name,
                MetricSample.metric == run.metric,
                MetricSample.data_source == run.data_source,
                MetricSample.value.is_not(None),
                MetricSample.collected_at >= pred.target_timestamp
                - dt.timedelta(seconds=tolerance_seconds),
                MetricSample.collected_at <= pred.target_timestamp
                + dt.timedelta(seconds=tolerance_seconds),
            )
            .order_by(MetricSample.collected_at.desc())
        )
        if run.interface_name is None:
            stmt = stmt.where(MetricSample.interface_name.is_(None))
        else:
            stmt = stmt.where(MetricSample.interface_name == run.interface_name)
        sample = session.scalars(stmt).first()
        if sample is not None:
            pred.actual_value = float(sample.value)
            pred.abs_error = abs(float(sample.value) - pred.predicted_value)
            pred.backfilled_at = db_utcnow()
            filled += 1
    if filled:
        session.commit()
    return filled


def forecast_warnings(runs: list[dict], threshold: float,
                      direction: str = "above") -> list[dict]:
    """Rule-based **forecast-based warning** (never a confirmed incident).

    Flags predicted future points expected to cross a configured threshold
    within the horizon, alongside the model's own holdout error so the UI
    can show the limitation right next to the warning. Each warning also
    carries a ``certainty``: ``certain`` only when the whole published band is
    past the threshold, otherwise ``possible``.
    """
    warnings: list[dict] = []
    for run in runs:
        if run.get("status") != "ok":
            continue
        for f in run.get("future") or []:
            v = f["predicted_value"]
            crossed = v >= threshold if direction == "above" else v <= threshold
            if crossed:
                lo = f.get("lower_bound")
                hi = f.get("upper_bound")
                # The band is the error the model actually measured on its own
                # holdout: a crossing that survives it is a far stronger
                # statement than the point estimate alone.
                band_clear = (lo is not None and hi is not None
                              and (lo >= threshold if direction == "above"
                                   else hi <= threshold))
                warnings.append({
                    "kind": "forecast-based warning",
                    "model": run["model"], "step": f["step"],
                    "target_timestamp": f["target_timestamp"],
                    "predicted_value": v, "threshold": threshold,
                    "direction": direction,
                    "certainty": "certain" if band_clear else "possible",
                    "lower_bound": lo, "upper_bound": hi,
                    "eta_seconds": _eta_seconds(run, f),
                    "model_mae": run.get("mae"),
                    "model_rmse": run.get("rmse"),
                    "model_skill": run.get("skill"),
                    "note": ("Warning is based on a forecast, not an "
                             "observed breach; see model error."),
                })
    return warnings


def _eta_seconds(run: dict, step: dict) -> float | None:
    """Seconds from the forecast origin to a published step (None if unknown)."""
    interval = run.get("interval_seconds")
    if not interval:
        return None
    try:
        return float(interval) * int(step.get("step") or 0)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ ensemble
def _inverse_error_weights(maes: list[float]) -> list[float]:
    """Weights proportional to ``1/(MAE_i + ridge)``, normalised to sum to 1.

    The ridge (10% of the best member's error) stops a member with a near-zero
    holdout error from absorbing all the weight, which would quietly reduce the
    "ensemble" to that single model. When every member is equally skilled the
    weights fall back to equal weighting.
    """
    eps = max(1e-12, 0.1 * min(maes))
    raw = [1.0 / (m + eps) for m in maes]
    total = sum(raw) or 1.0
    return [w / total for w in raw]


def build_ensemble(ok_runs: list[dict], test_actuals: list[float], *,
                   train: pd.Series | None = None,
                   coverage: float = 0.95,
                   band_mode: str = BAND_MODE) -> dict:
    """Inverse-MAE weighted blend of the compared models, on the same holdout.

    Blending is only meaningful when every member was evaluated on identical
    timestamps (the comparison protocol already guarantees this) and each one
    produced a future path; otherwise the result says so instead of inventing a
    number. A member with no prediction at a given step is masked out and the
    remaining weights are renormalised, so a gap removes evidence rather than
    fabricating a zero.

    Returns an in-memory dict only. It is deliberately NOT persisted as a
    ``forecast_runs`` row: a stored run must be a real model fit with a real
    config, and a weight vector over other runs is a comparison result.
    """
    members = [r for r in ok_runs
               if r.get("mae") is not None and r.get("future")
               and r.get("test_pred")]
    if len(members) < 2:
        return {"model": ENSEMBLE_NAME, "status": "not_applicable",
                "reason": ("an ensemble needs at least two compared models with "
                           "a holdout MAE and a future path; found "
                           f"{len(members)}")}
    ts_ref = list(members[0]["test_timestamps"])
    if any(list(m["test_timestamps"]) != ts_ref for m in members[1:]):
        return {"model": ENSEMBLE_NAME, "status": "not_applicable",
                "reason": ("members were not evaluated on identical holdout "
                           "timestamps - blending their errors would be "
                           "meaningless")}
    common_scored = set(members[0].get("scored_timestamps", ts_ref))
    for member in members[1:]:
        common_scored.intersection_update(
            member.get("scored_timestamps", ts_ref))
    weights = _inverse_error_weights([m["mae"] for m in members])

    blend_pred: list[float] = []
    for j in range(len(ts_ref)):
        if ts_ref[j] not in common_scored:
            blend_pred.append(float("nan"))
            continue
        num = den = 0.0
        for m, w in zip(members, weights):
            p = m["test_pred"][j] if j < len(m["test_pred"]) else None
            if _isnan(p):
                continue
            num += w * float(p)
            den += w
        blend_pred.append(num / den if den else float("nan"))

    pooled: dict[int, dict] = {}
    for m, w in zip(members, weights):
        for f in m["future"]:
            try:
                s = int(f.get("step") or 0)
            except (TypeError, ValueError):
                continue
            cell = pooled.setdefault(s, {"target_timestamp":
                                         f["target_timestamp"],
                                         "step": s, "num": 0.0, "den": 0.0})
            if _isnan(f.get("predicted_value")):
                continue
            cell["num"] += w * float(f["predicted_value"])
            cell["den"] += w
    raw_future = [{"target_timestamp": c["target_timestamp"], "step": c["step"],
                   "predicted_value": c["num"] / c["den"]}
                  for c in (pooled[k] for k in sorted(pooled)) if c["den"]]

    pairs = [(a, p) for a, p in zip(test_actuals, blend_pred) if not _isnan(p)]
    act = [a for a, _ in pairs]
    prd = [p for _, p in pairs]
    abs_errors = [abs(a - p) for a, p in pairs]
    future, band_meta = apply_holdout_bands(raw_future, abs_errors,
                                           coverage=coverage, mode=band_mode)
    naive_member = next((m for m in members if m["model"] == "naive"), None)
    skill = None
    if naive_member is not None:
        aligned = [(a, p, n) for a, p, n in
                   zip(test_actuals, blend_pred, naive_member["test_pred"])
                   if not _isnan(p) and not _isnan(n)]
        skill = evaluation.skill_vs_naive([x[0] for x in aligned],
                                         [x[1] for x in aligned],
                                         [x[2] for x in aligned])
    mae = evaluation.mae(act, prd)
    best_member = min(members, key=lambda m: m["mae"])
    return {
        "model": ENSEMBLE_NAME, "model_version": "1.0", "status": "ok",
        "method": "inverse holdout-MAE weights over the compared models",
        "members": [{"model": m["model"], "mae": m["mae"], "weight": w}
                    for m, w in zip(members, weights)],
        "mae": mae,
        "rmse": evaluation.rmse(act, prd),
        "r2": evaluation.r2(act, prd),
        "mape": evaluation.mape(act, prd),
        "smape": evaluation.smape(act, prd),
        "mase": (evaluation.mase(act, prd, train) if train is not None
                 else None),
        "skill": skill,
        "n_eval": len(prd),
        "test_pred": blend_pred,
        "test_timestamps": ts_ref,
        "future": future,
        "band": band_meta,
        "band_nominal_coverage": coverage,
        "best_member": best_member["model"],
        "best_member_mae": best_member["mae"],
        "beats_best_member": (mae is not None
                              and mae <= best_member["mae"] + 1e-12),
        "persisted": False,
        "note": ("in-memory comparison result; never stored as a forecast "
                 "run, so every persisted row stays a single real model fit"),
    }


# ------------------------------------------------------------ threshold ETA
def threshold_crossing(runs: list[dict], threshold: float,
                       direction: str = "above") -> list[dict]:
    """When each model's forecast reaches a threshold - two separate answers.

    ``possible`` means the central prediction has crossed; ``certain`` means the
    entire published band has crossed, i.e. the call survives the error the
    model actually measured on its own holdout. Reporting only the first would
    overstate confidence and only the second would hide an early signal, so both
    steps are returned per model while ``step``/``eta_seconds`` follow the
    conservative reading. Empty list when nothing crosses inside the horizon.
    """
    out: list[dict] = []
    for run in runs:
        if run.get("status") != "ok":
            continue
        possible = certain = None
        for f in sorted(run.get("future") or [],
                        key=lambda x: int(x.get("step") or 0)):
            v = f.get("predicted_value")
            if v is None:
                continue
            hit = (float(v) >= threshold if direction == "above"
                   else float(v) <= threshold)
            if hit and possible is None:
                possible = f
            lo, hi = f.get("lower_bound"), f.get("upper_bound")
            if certain is None and lo is not None and hi is not None:
                if (float(lo) >= threshold if direction == "above"
                        else float(hi) <= threshold):
                    certain = f
            if possible is not None and certain is not None:
                break
        if possible is None and certain is None:
            continue
        chosen = certain if certain is not None else possible
        out.append({
            "model": run["model"],
            "certainty": "certain" if certain is not None else "possible",
            "step": chosen["step"],
            "target_timestamp": chosen["target_timestamp"],
            "predicted_value": float(chosen["predicted_value"]),
            "lower_bound": chosen.get("lower_bound"),
            "upper_bound": chosen.get("upper_bound"),
            "threshold": float(threshold), "direction": direction,
            "first_possible_step": (possible or {}).get("step"),
            "first_certain_step": (certain or {}).get("step"),
            "eta_seconds": _eta_seconds(run, chosen),
            "model_mae": run.get("mae"),
            "band_kind": (run.get("band") or {}).get("kind"),
            "note": ("earliest band-clear crossing" if certain is not None
                     else "central prediction only: the model's own error band "
                          "still overlaps the threshold at this step"),
        })
    return sorted(out, key=lambda r: (int(r["step"] or 0), r["model"]))


# -------------------------------------------------------------- band outcome
def realized_coverage(session: Session, run_id: int) -> dict:
    """Did the published bands actually hold? Measured, not assumed.

    Uses only persisted predictions whose target timestamp has since been
    observed (backfilled by ``backfill_actuals``); unverified steps are counted
    separately instead of being scored as hits, so zero verified steps give a
    coverage of None, never a comfortable 100%.
    """
    rows = repository.get_forecast_predictions(session, run_id)
    result = realized_band_coverage(rows)
    result["run_id"] = run_id
    result["n_total"] = len(rows)
    verified = [(float(r.actual_value), float(r.predicted_value)) for r in rows
                if r.actual_value is not None]
    result["mae"] = (evaluation.mae([a for a, _ in verified],
                                    [p for _, p in verified])
                     if len(verified) >= 2 else None)
    result["bias"] = (sum(p - a for a, p in verified) / len(verified)
                      if verified else None)
    nominal = None
    result["band_kind"] = None
    result["holdout_measured_coverage"] = None
    run = session.get(repository.ForecastRun, run_id)
    if run is not None and getattr(run, "band_meta_json", None):
        try:
            meta = json.loads(run.band_meta_json)
        except (TypeError, ValueError):
            meta = {}
        nominal = meta.get("nominal_coverage")
        result["band_kind"] = meta.get("kind")
        result["holdout_measured_coverage"] = meta.get("measured_coverage")
    result["nominal_coverage"] = nominal
    if nominal is not None and result["coverage"] is not None:
        result["coverage_gap"] = result["coverage"] - float(nominal)
    else:
        result["coverage_gap"] = None
    return result


def model_ranking(runs: list[dict],
                  ensemble: dict | None = None) -> list[dict]:
    """Tidy comparison table for the UI, best holdout MAE first.

    Models that refused keep their row with the reason: an absent model is part
    of the answer, not a missing line in the table.
    """
    rows: list[dict] = []
    for r in runs:
        status = r.get("status")
        rows.append({
            "model": r.get("model"),
            "status": status,
            "mae": r.get("mae"),
            "rmse": r.get("rmse"),
            "r2": r.get("r2"),
            "mape": r.get("mape"),
            "smape": r.get("smape"),
            "mase": r.get("mase"),
            "skill": r.get("skill"),
            "n_eval": r.get("n_eval"),
            "band": ((r.get("band") or {}).get("kind")
                     if status == "ok" else None),
            "reason": r.get("reason"),
            "persisted": bool(r.get("run_id")),
        })
    if ensemble and ensemble.get("status") == "ok":
        rows.append({
            "model": ENSEMBLE_NAME + " (blend)",
            "status": ensemble["status"],
            "mae": ensemble.get("mae"), "rmse": ensemble.get("rmse"),
            "r2": ensemble.get("r2"), "mape": ensemble.get("mape"),
            "smape": ensemble.get("smape"), "mase": ensemble.get("mase"),
            "skill": ensemble.get("skill"), "n_eval": ensemble.get("n_eval"),
            "band": (ensemble.get("band") or {}).get("kind"),
            "reason": "in-memory blend, not a stored run",
            "persisted": False,
        })
    scored = sorted((r for r in rows if r["mae"] is not None),
                    key=lambda r: r["mae"])
    rest = [r for r in rows if r["mae"] is None]
    for i, r in enumerate(scored, start=1):
        r["rank"] = i
    for r in rest:
        r["rank"] = None
    return scored + rest
