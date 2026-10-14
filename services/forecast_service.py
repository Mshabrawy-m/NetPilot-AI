"""Forecast orchestration and persistence (Phase 2 Scope E service).

Protocol implemented here (spec §8):

1. Load one series, apply the data-quality FORECAST readiness gate -
   insufficient data returns a status, never a forecast.
2. Chronological train|val|test split (never shuffled).
3. Each model is fitted on TRAIN only (tuning may use train+val), then
   predicts every TEST timestamp - all models are compared on the same
   identical target timestamps and the same horizon.
4. MAE/RMSE (and R^2 when meaningful) are computed against the held-out
   test actuals; nothing is reported when the test segment is too short.
5. Future forecasts come from a refit on ALL in-time data up to the
   forecast origin; predictions persist in ``forecast_predictions``,
   strictly separate from observed ``metric_samples``.
6. Actuals/errors are backfilled once real observations exist - a
   prediction never overwrites an observation.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import uuid

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from analytics import data_quality, evaluation
from services.anomaly_service import load_ready_series
from analytics.forecasting import FORECAST_MODELS
from config import get_settings
from database import repository
from database.models import db_utcnow

DEFAULT_MODELS = ("naive", "seasonal_naive", "holt_damped")


def run_forecast_comparison(
    session: Session, *,
    device_name: str, metric: str,
    interface_name: str | None, data_source: str,
    horizon: int | None = None,
    models: tuple[str, ...] = DEFAULT_MODELS,
    persist: bool = True,
    device_level_only: bool = False,
    since: dt.datetime | None = None,
) -> dict:
    settings = get_settings()
    horizon = horizon or settings.forecast_horizon_default
    frame, quality = load_ready_series(
        session, device_name, metric, interface_name, data_source,
        purpose=data_quality.FORECAST, device_level_only=device_level_only,
        since=since)
    if not quality.ready(data_quality.FORECAST):
        return {"status": "insufficient_data",
                "reasons": quality.reasons_for(data_quality.FORECAST)
                or quality.reasons, "runs": []}

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
                "runs": []}

    interval = quality.observed_interval_seconds
    origin = series.index.max()
    comparison_group = str(uuid.uuid4())
    unit = (frame["unit"].dropna().iloc[-1]
            if not frame.empty and frame["unit"].notna().any() else None)

    runs: list[dict] = []
    for name in models:
        fn = FORECAST_MODELS.get(name)
        if fn is None:
            runs.append({"model": name, "status": "unknown_model"})
            continue
        kwargs = {}
        if name == "seasonal_naive":
            kwargs["interval_seconds"] = interval
        if name == "holt_damped":
            kwargs["val"] = split.val
        # ---- holdout evaluation (fit on train, predict test) ------------
        try:
            eval_res = fn(split.train, horizon,
                          future_timestamps=None, test=split.test, **kwargs)
        except Exception as exc:  # model failure -> reported, never hidden
            runs.append({"model": name, "status": "error",
                         "reason": f"{type(exc).__name__}: {exc}"})
            continue
        if not eval_res.ok:
            runs.append({"model": name, "status": "skipped",
                         "reason": eval_res.reason})
            if persist:
                repository.add_forecast_run(
                    session, comparison_group=comparison_group,
                    device_name=device_name, interface_name=interface_name,
                    metric=metric, target_unit=unit,
                    data_source=data_source, model_name=name,
                    model_version=eval_res.model_version,
                    model_config_json=json.dumps(eval_res.config,
                                                 sort_keys=True),
                    horizon_steps=horizon, interval_seconds=interval,
                    origin_at=_to_dt(origin),
                    n_train=len(split.train), n_val=len(split.val),
                    n_test=len(split.test),
                    status="insufficient_data",
                    message=eval_res.reason)
            continue
        preds = [p for p in eval_res.test_pred if not _isnan(p)]
        actuals = [a for a, p in zip(split.test.to_numpy(), eval_res.test_pred)
                   if not _isnan(p)]
        mae = evaluation.mae(actuals, preds)
        rmse = evaluation.rmse(actuals, preds)
        r2v = evaluation.r2(actuals, preds)
        n_eval = len(preds)

        # ---- future forecast: refit on ALL in-time data ------------------
        full = pd.Series(series.to_numpy(), index=series.index)
        fut_kwargs = dict(kwargs)
        fut_kwargs.pop("val", None)  # refit on full history incl. val/test
        try:
            steps = [origin + pd.Timedelta(seconds=interval * (h + 1))
                     for h in range(horizon)] if interval else []
            future_res = fn(full, horizon,
                            future_timestamps=steps or None, **fut_kwargs)
        except Exception:
            future_res = None
        run_row = {
            "model": name,
            "model_version": eval_res.model_version,
            "config": eval_res.config,
            "status": "ok",
            "mae": mae, "rmse": rmse, "r2": r2v, "n_eval": n_eval,
            # holdout predictions + their timestamps, kept in-memory for the
            # UI and for the identical-timestamp comparison guarantee
            "test_pred": eval_res.test_pred,
            "test_timestamps": list(split.test.index),
            "n_train": len(split.train), "n_val": len(split.val),
            "n_test": len(split.test),
            "train_start": series.index.min(), "train_end": split.train_end,
            "val_end": split.val_end, "test_end": split.test_end,
            "origin_at": _to_dt(origin),
            "horizon_steps": horizon,
            "interval_seconds": interval,
            "future": future_res.future if future_res else [],
        }
        runs.append(run_row)
        if persist:
            run = repository.add_forecast_run(
                session,
                comparison_group=comparison_group,
                device_name=device_name, interface_name=interface_name,
                metric=metric, target_unit=unit, data_source=data_source,
                model_name=name,
                model_version=run_row["model_version"],
                model_config_json=json.dumps(run_row["config"], sort_keys=True),
                horizon_steps=horizon, interval_seconds=interval,
                origin_at=run_row["origin_at"],
                n_train=run_row["n_train"], n_val=run_row["n_val"],
                n_test=run_row["n_test"],
                train_start=_to_dt(run_row["train_start"]),
                train_end=_to_dt(run_row["train_end"]),
                val_end=_to_dt(run_row["val_end"]),
                test_end=_to_dt(run_row["test_end"]),
                mae=mae, rmse=rmse, r2=r2v, n_eval=n_eval,
                status="ok", message=None,
            )
            if run_row["future"]:
                rows = [{"target_timestamp": _to_dt(f["target_timestamp"]),
                         "step": f["step"],
                         "predicted_value": f["predicted_value"],
                         "lower_bound": f.get("lower_bound"),
                         "upper_bound": f.get("upper_bound")}
                        for f in run_row["future"]]
                repository.add_forecast_predictions(session, run, rows)
            run_row["run_id"] = run.id
    if persist:
        session.commit()
    return {"status": "ok", "comparison_group": comparison_group,
            "runs": runs, "split": split, "quality": quality,
            "unit": unit}


def _isnan(x) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


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
    can show the limitation right next to the warning.
    """
    warnings: list[dict] = []
    for run in runs:
        if run.get("status") != "ok":
            continue
        for f in run.get("future") or []:
            v = f["predicted_value"]
            crossed = v >= threshold if direction == "above" else v <= threshold
            if crossed:
                warnings.append({
                    "kind": "forecast-based warning",
                    "model": run["model"], "step": f["step"],
                    "target_timestamp": f["target_timestamp"],
                    "predicted_value": v, "threshold": threshold,
                    "direction": direction,
                    "model_mae": run.get("mae"),
                    "model_rmse": run.get("rmse"),
                    "note": ("Warning is based on a forecast, not an "
                             "observed breach; see model error."),
                })
    return warnings
