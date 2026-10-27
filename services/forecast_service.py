"""Forecast orchestration and persistence (Phase 2 Scope E service).

Protocol implemented here (spec §8):

1. Load one series, apply the data-quality FORECAST readiness gate -
   insufficient data returns a status, never a forecast.
2. Chronological train|val|test split (never shuffled).
3. Each model is fitted on TRAIN only (tuning may use train+val), then
   predicts every TEST timestamp - all models are compared on the same
   identical target timestamps and the same horizon.
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

from analytics import data_quality, evaluation
from services.anomaly_service import load_ready_series
from analytics.forecasting import (FORECAST_MODELS, MODEL_KWARGS,
                                   apply_holdout_bands,
                                   realized_band_coverage)
from config import get_settings
from database import repository
from database.models import db_utcnow

#: Models compared when the caller does not choose. ``seasonal_naive`` and
#: ``holt_winters`` are listed because both refuse out loud (a recorded skip
#: with the reason) when the sampling interval or the seasonal history is not
#: defensible - they never produce a quiet, wrong answer.
DEFAULT_MODELS = ("naive", "seasonal_naive", "holt_damped", "holt_winters")

#: Name under which the in-memory ensemble is reported (never persisted).
ENSEMBLE_NAME = "ensemble_inverse_mae"

#: How the service re-cuts published bands. ``empirical_sqrt_h`` uses the
#: half-width measured on the model's one-step holdout errors and grows it with
#: sqrt(h), because multi-step error does usually grow; a flat empirical band
#: would be over-confident late in the horizon.
BAND_MODE = "empirical_sqrt_h"


def run_forecast_comparison(
    session: Session, *,
    device_name: str, metric: str,
    interface_name: str | None, data_source: str,
    horizon: int | None = None,
    models: tuple[str, ...] = DEFAULT_MODELS,
    persist: bool = True,
    device_level_only: bool = False,
    since: dt.datetime | None = None,
    band_mode: str = BAND_MODE,
    band_coverage: float | None = None,
    ensemble: bool = True,
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
                 if not _isnan(p)]
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
        try:
            steps = [origin + pd.Timedelta(seconds=interval * (h + 1))
                     for h in range(horizon)] if interval else []
            future_res = fn(full, horizon,
                            future_timestamps=steps or None, **fut_kwargs)
        except Exception:
            future_res = None
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
            "n_train": len(split.train), "n_val": len(split.val),
            "n_test": len(split.test),
            "train_start": series.index.min(), "train_end": split.train_end,
            "val_end": split.val_end, "test_end": split.test_end,
            "origin_at": _to_dt(origin),
            "horizon_steps": horizon,
            "interval_seconds": interval,
            "future": future,
            "band": band_meta,
            "band_nominal_coverage": coverage,
            "n_holdout_errors": len(abs_errors),
        }
        if not raw_future and interval is None:
            run_row["future_note"] = (
                "sampling interval unknown - future steps cannot be "
                "timestamped, so none are published")
        runs.append(run_row)
        ok_runs.append(run_row)

    # ---- cross-model results, all on the identical holdout timestamps ----
    naive_row = next((r for r in ok_runs if r["model"] == "naive"), None)
    for r in ok_runs:
        if r is naive_row:
            r["skill"] = 0.0      # the baseline's skill against itself, by definition
        elif naive_row is None:
            r["skill"] = None     # no reference to measure improvement against
        else:
            aligned = [(a, p, n) for a, p, n in
                       zip(test_actuals, r["test_pred"],
                           naive_row["test_pred"])
                       if not _isnan(p) and not _isnan(n)]
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
    return {"status": "ok", "comparison_group": comparison_group,
            "runs": runs, "split": split, "quality": quality,
            "unit": unit,
            "best_model": best_model, "best_criterion": "holdout MAE",
            "ensemble": ensemble_result,
            "band_nominal_coverage": coverage, "band_mode": band_mode,
            "models_requested": list(models)}


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
    weights = _inverse_error_weights([m["mae"] for m in members])

    blend_pred: list[float] = []
    for j in range(len(ts_ref)):
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
