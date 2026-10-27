"""Time-series forecasting baselines (Phase 2 Scope E).

Small, explainable model set - all original implementations:

* ``naive``           - last observed value (always available baseline).
* ``seasonal_naive``  - value one season earlier; only offered when the
  sampling interval is known and >= 2 full seasons of history exist
  (otherwise refused as not defensible).
* ``holt_damped``     - damped-trend exponential smoothing (Holt's linear
  trend with damping factor phi), fitted by a DETERMINISTIC grid search on
  training 1-step error - pure NumPy, no external statsmodels dependency
  (its compiled statespace extensions are blocked by this machine's
  Windows App Control policy, same class of issue as the Phase 1
  SQLAlchemy pin).
* ``holt_winters``    - additive seasonal Holt-Winters with a damped trend
  (level + damped trend + a within-season profile), tuned by the same kind
  of deterministic grid. It refuses outright when the sampling interval is
  unknown or fewer than two complete seasons exist, because a seasonal model
  fitted on one season would be inventing a rhythm.
* ``ridge_lag``       - scikit-learn Ridge on trailing lag features: one
  direct model per horizon step, with the feature scaler fitted on TRAIN
  rows only. Holdout evaluation is one-step-ahead (features use true
  in-time history); future steps after the first are recursive (each
  prediction is fed back as history), so multi-step accuracy is not
  guaranteed. This is a small explainable baseline, not a production
  forecaster.

Protocol honoured by the service layer (see services/forecast_service.py):
chronological train|val|test splits, tuning on train+val only, models
compared on identical test timestamps, forecast origin = last observation,
future forecasts refit on all available (in-time) data.

Uncertainty bands are a documented approximation, NOT calibrated prediction
intervals: each model scales its own training-residual sigma by sqrt(h).
The sigma source differs by model (naive: first differences; holt: one-step
residuals; ridge: residuals pooled across all fitted direct-horizon
models), so bands are indicative only and no per-horizon coverage is
claimed.

``apply_holdout_bands`` is the one deliberate upgrade to that story, and it is
the *service* that calls it (never a model, so the baselines above keep their
documented sqrt(h) behaviour): once a model has made real one-step errors on
held-out data, those errors give an empirical half-width that replaces the
normal-approximation width, together with the coverage actually measured on
the holdout. With no holdout to measure, the approximation is kept and labelled
``normal_sqrt_h_unmeasured`` rather than quietly narrowed. ``realized_band_coverage``
closes the loop later, against observations that arrived after the prediction.
"""
from __future__ import annotations

import dataclasses
import math

import numpy as np
import pandas as pd

Z95 = 1.96  # documented normal-approximation multiplier for ~95% bands


@dataclasses.dataclass
class ForecastResult:
    model_name: str
    model_version: str
    config: dict
    ok: bool
    reason: str | None = None
    # holdout predictions aligned to split.test (set by the service)
    test_pred: list[float] = dataclasses.field(default_factory=list)
    # future predictions, step 1..horizon relative to forecast origin
    future: list[dict] = dataclasses.field(default_factory=list)
    # 1-step training residual std, also used for the sqrt(h) bands
    train_mape_guard: float | None = None


def _result(name: str, version: str, config: dict, ok: bool,
            reason: str | None = None) -> ForecastResult:
    return ForecastResult(model_name=name, model_version=version,
                          config=config, ok=ok, reason=reason)


# --------------------------------------------------------------------- naive
def naive_forecast(train: pd.Series, horizon: int,
                   future_timestamps: list[pd.Timestamp] | None = None,
                   test: pd.Series | None = None) -> ForecastResult:
    """Last observed value repeated; band sigma from 1-step differences."""
    if len(train) < 1:
        return _result("naive", "1.0", {}, False, "no training data")
    last = float(train.iloc[-1])
    diffs = train.diff().dropna()
    sigma = float(diffs.std(ddof=1)) if len(diffs) >= 2 else 0.0
    res = _result("naive", "1.0", {"anchor": "last value"}, True)
    res.train_mape_guard = sigma
    if test is not None:
        res.test_pred = [last] * len(test)
    if future_timestamps:
        res.future = [{
            "target_timestamp": ts, "step": h + 1, "predicted_value": last,
            "lower_bound": last - Z95 * sigma * math.sqrt(h + 1),
            "upper_bound": last + Z95 * sigma * math.sqrt(h + 1),
        } for h, ts in enumerate(future_timestamps)]
    return res


# -------------------------------------------------------------- seasonal naive
def seasonal_naive_forecast(train: pd.Series, horizon: int,
                            interval_seconds: float | None,
                            future_timestamps: list[pd.Timestamp] | None = None,
                            test: pd.Series | None = None,
                            season_hours: float = 24.0) -> ForecastResult:
    """Refuses to run unless >= 2 full seasons on a known regular grid."""
    cfg = {"season_hours": season_hours}
    if interval_seconds is None or interval_seconds <= 0:
        return _result("seasonal_naive", "1.0", cfg, False,
                       "sampling interval unknown - seasonality not defensible")
    season_len = int(round(season_hours * 3600 / interval_seconds))
    cfg["season_len"] = season_len
    if season_len < 2:
        return _result("seasonal_naive", "1.0", cfg, False,
                       "season shorter than 2 samples - not defensible")
    if len(train) < 2 * season_len + 1:
        return _result("seasonal_naive", "1.0", cfg, False,
                       f"needs >= {2 * season_len + 1} samples for "
                       "2 full seasons, has " + str(len(train)))
    values = train.to_numpy(dtype=float)
    if test is not None:
        # predict test[j] from value m steps earlier inside the full series
        n = len(values)
        m = season_len
        # test positions map to indices n .. n+len(test)-1 of the whole
        # series; their seasonal reference is that index minus m.
        full_len = n + len(test)
        res_test = []
        for k in range(n, full_len):
            ref = k - m
            res_test.append(float(values[ref]) if 0 <= ref < n else float("nan"))
        res = _result("seasonal_naive", "1.0", cfg, True)
        res.test_pred = res_test
    else:
        res = _result("seasonal_naive", "1.0", cfg, True)
    # residual sigma on training data (one-step seasonal errors)
    errs = values[season_len:] - values[:-season_len]
    sigma = float(np.std(errs, ddof=1)) if len(errs) >= 2 else 0.0
    res.train_mape_guard = sigma
    if future_timestamps:
        res.future = []
        for h, ts in enumerate(future_timestamps, start=1):
            ref_idx = len(values) - season_len + ((h - 1) % season_len)
            if 0 <= ref_idx < len(values):
                v = float(values[ref_idx])
            else:
                v = float(values[-1])
            res.future.append({
                "target_timestamp": ts, "step": h, "predicted_value": v,
                "lower_bound": v - Z95 * sigma * math.sqrt(h),
                "upper_bound": v + Z95 * sigma * math.sqrt(h),
            })
    return res


# ---------------------------------------------------------------- damped Holt
def _holt_fit(y: np.ndarray, alpha: float, beta: float, phi: float):
    """Return (level, trend, one-step errors) for a damped Holt path."""
    n = len(y)
    level = y[0]
    trend = y[1] - y[0] if n >= 2 else 0.0
    errors: list[float] = []
    for t in range(1, n):
        forecast = level + phi * trend
        err = y[t] - forecast
        errors.append(err)
        prev_level = level
        level = level + alpha * err
        trend = phi * trend + beta * (level - prev_level)
    return level, trend, np.array(errors)


def holt_damped_forecast(train: pd.Series, horizon: int,
                        future_timestamps: list[pd.Timestamp] | None = None,
                        val: pd.Series | None = None,
                        test: pd.Series | None = None) -> ForecastResult:
    """Damped-trend exponential smoothing with deterministic grid tuning.

    alpha/beta/phi are chosen by minimising 1-step SSE on TRAIN (grid fixed
    here - no randomness, fully reproducible). If ``val`` is supplied the
    grid is scored on validation 1-step errors instead (still never on
    test). Forecast: level + trend * sum(phi^1..phi^h). Bands use training
    1-step residual sigma with sqrt(h) scaling (documented approximation).
    """
    y = train.to_numpy(dtype=float)
    if len(y) < 6:
        return _result("holt_damped", "1.0", {}, False,
                       "need >= 6 training samples for trend smoothing")
    alphas = [0.05, 0.1, 0.2, 0.35, 0.5]
    betas = [0.01, 0.05, 0.1, 0.2]
    phis = [0.85, 0.9, 0.95, 0.98]
    best = (float("inf"), alphas[0], betas[0], phis[0])
    for a in alphas:
        for b in betas:
            for p in phis:
                _lvl, _tr, errs = _holt_fit(y, a, b, p)
                sse = float(np.sum(errs ** 2))
                if sse < best[0] - 1e-12:  # deterministic tie -> first grid cell
                    best = (sse, a, b, p)
    sse, alpha, beta, phi = best
    cfg = {"alpha": alpha, "beta": beta, "phi": phi,
           "tuned_on": "validation" if val is not None and len(val) >= 2
           else "train"}
    if cfg["tuned_on"] == "validation":
        # refit grid using train+val for scoring but keep train-only fit for
        # test evaluation honesty: score on val 1-step errors continuing from
        # the train-fitted state.
        yv = pd.concat([train, val]).to_numpy(dtype=float)
        best = (float("inf"), alpha, beta, phi)
        for a in alphas:
            for b in betas:
                for p in phis:
                    _lvl, _tr, errs = _holt_fit(yv, a, b, p)
                    n_tr = len(y) - 1
                    sse_v = float(np.sum(errs[n_tr:] ** 2))
                    if sse_v < best[0] - 1e-12:
                        best = (sse_v, a, b, p)
        _, alpha, beta, phi = best
        cfg.update({"alpha": alpha, "beta": beta, "phi": phi})

    level, trend, errs = _holt_fit(y, alpha, beta, phi)
    sigma = float(np.std(errs, ddof=1)) if len(errs) >= 2 else 0.0
    res = _result("holt_damped", "1.0", cfg, True)
    res.train_mape_guard = sigma

    if test is not None:
        # Multi-step from end of train; val (if any) is *not* used for the
        # test predictions beyond what training already contained: to keep
        # comparison fair the service refits identically across models.
        res.test_pred = []
        lv, tr = level, trend
        for _ in range(len(test)):
            res.test_pred.append(float(lv + tr * phi))
            lv = lv + tr * phi  # advance state (naive recursion on errors)
            tr = phi * tr
    if future_timestamps:
        res.future = []
        cumulative = 0.0
        for h, ts in enumerate(future_timestamps, start=1):
            cumulative += phi ** h
            v = level + trend * cumulative
            res.future.append({
                "target_timestamp": ts, "step": h, "predicted_value": float(v),
                "lower_bound": float(v - Z95 * sigma * math.sqrt(h)),
                "upper_bound": float(v + Z95 * sigma * math.sqrt(h)),
            })
    return res


# ------------------------------------------------- Holt-Winters (additive)
def _season_length(season_hours: float,
                  interval_seconds: float | None) -> int | None:
    """Samples per season on a known regular grid, or None when it is not
    defensible to speak of seasonality at all."""
    if interval_seconds is None or interval_seconds <= 0:
        return None
    m = int(round(season_hours * 3600.0 / interval_seconds))
    return m if m >= 2 else None


def _hw_init(y: np.ndarray, m: int):
    """Causal initialisation from the FIRST two complete seasons only.

    level  = mean of season 1, trend = (mean(season2) - mean(season1)) / m,
    seasonal[j] = mean deviation of position j from its own season mean,
    re-centred to sum to zero (so the seasonal profile never shifts the level).
    Nothing after index 2*m is looked at, so the recursion start is honest.
    """
    s1 = float(np.mean(y[:m]))
    s2 = float(np.mean(y[m:2 * m]))
    level = s1
    trend = (s2 - s1) / m
    seasonal = np.zeros(m)
    for s in range(2):
        block = y[s * m:(s + 1) * m]
        base = float(np.mean(block))
        seasonal += block - base
    seasonal -= seasonal.sum() / m
    return level, trend, seasonal


def _hw_fit(y: np.ndarray, m: int, alpha: float, beta: float, gamma: float,
            phi: float):
    """Additive damped Holt-Winters recursion; returns state + 1-step errors.

    The recursion starts at index ``2*m`` (the initialisation budget) so every
    error is a genuine strictly-past one-step forecast, and the seasonal term
    for the current period is updated with its own error - the classic Winters
    update, never a look-ahead.
    """
    level, trend, seasonal = _hw_init(y, m)
    errors: list[float] = []
    for t in range(2 * m, len(y)):
        j = t % m
        forecast = level + phi * trend + seasonal[j]
        err = float(y[t]) - forecast
        errors.append(err)
        prev_level = level
        level = level + alpha * err
        trend = phi * trend + beta * (level - prev_level)
        seasonal[j] = seasonal[j] + gamma * err
    return level, trend, seasonal, np.array(errors)


def holt_winters_forecast(train: pd.Series, horizon: int,
                          interval_seconds: float | None = None,
                          future_timestamps: list[pd.Timestamp] | None = None,
                          test: pd.Series | None = None,
                          season_hours: float = 24.0) -> ForecastResult:
    """Additive seasonal Holt-Winters with a damped trend.

    Fills the gap the other baselines leave: ``holt_damped`` tracks level and
    trend but is blind to a daily rhythm, and ``seasonal_naive`` copies last
    season's value but cannot follow a trend. This model carries level, a
    damped trend and a seasonal profile at once.

    Refuses (honestly) unless the sampling interval is known and at least two
    complete seasons plus a recursion budget exist - a seasonal model fitted on
    one season would be inventing a rhythm. Parameters come from a DETERMINISTIC
    grid on training 1-step SSE (same reproducibility guarantee as
    ``holt_damped``; no randomness, no external smoothing library).

    Multi-step paths (holdout evaluation and future steps) recurse from the
    forecast origin without consuming any observed value at or after it, so
    step h uses the seasonal cell of position ``origin + h`` only.
    """
    cfg = {"season_hours": season_hours, "trend": "damped additive"}
    m = _season_length(season_hours, interval_seconds)
    if m is None:
        return _result("holt_winters", "1.0", cfg, False,
                       "sampling interval unknown or too coarse for a season "
                       "- seasonality not defensible")
    cfg["season_len"] = m
    y = train.to_numpy(dtype=float)
    need = 2 * m + 4
    if len(y) < need:
        return _result("holt_winters", "1.0", cfg, False,
                       f"needs >= {need} samples (2 full seasons plus a fit "
                       f"budget) for a {m}-step season, has {len(y)}")
    alphas = [0.05, 0.15, 0.3, 0.5]
    betas = [0.01, 0.05, 0.1]
    gammas = [0.02, 0.08, 0.2]
    phis = [0.85, 0.9, 0.98]
    best = (float("inf"), alphas[0], betas[0], gammas[0], phis[0])
    for a in alphas:
        for b in betas:
            for g in gammas:
                for p in phis:
                    _lv, _tr, _se, errs = _hw_fit(y, m, a, b, g, p)
                    if errs.size == 0:
                        continue
                    sse = float(np.sum(errs ** 2))
                    if sse < best[0] - 1e-12:  # deterministic tie -> first cell
                        best = (sse, a, b, g, p)
    _sse, alpha, beta, gamma, phi = best
    cfg.update({"alpha": alpha, "beta": beta, "gamma": gamma, "phi": phi,
                "tuned_on": "train"})

    level, trend, seasonal, errs = _hw_fit(y, m, alpha, beta, gamma, phi)
    sigma = float(np.std(errs, ddof=1)) if len(errs) >= 2 else 0.0
    res = _result("holt_winters", "1.0", cfg, True)
    res.train_mape_guard = sigma
    n = len(y)

    def path(step: int) -> float:
        cumulative = sum(phi ** i for i in range(1, step + 1))
        return float(level + trend * cumulative
                     + seasonal[(n + step - 1) % m])

    if test is not None:
        res.test_pred = [path(j + 1) for j in range(len(test))]
    if future_timestamps:
        res.future = []
        for h, ts in enumerate(future_timestamps, start=1):
            v = path(h)
            res.future.append({
                "target_timestamp": ts, "step": h, "predicted_value": v,
                "lower_bound": v - Z95 * sigma * math.sqrt(h),
                "upper_bound": v + Z95 * sigma * math.sqrt(h),
            })
    return res


# ------------------------------------------------------------- ridge on lags
def ridge_lag_forecast(train: pd.Series, horizon: int,
                       future_timestamps: list[pd.Timestamp] | None = None,
                       test: pd.Series | None = None,
                       max_lags: int = 12) -> ForecastResult:
    """Direct multi-horizon Ridge on trailing lag features (sklearn).

    Leakage rules: the lag window always ends at the forecast origin
    (training target index) and the feature StandardScaler is fitted on
    TRAIN rows only. For test evaluation, features for test step j use the
    true observed values preceding it (values are in-time at prediction).
    Only the 1-step model is used for that one-step-ahead evaluation; the
    future multi-step path feeds earlier predictions back as history and is
    therefore recursive. The reported sigma pools residuals from every
    direct-horizon model, so it is an indicative scale, not a calibrated
    multi-step interval.
    """
    try:
        from sklearn.linear_model import Ridge
        from sklearn.preprocessing import StandardScaler
    except Exception as exc:  # pragma: no cover - env dependent
        return _result("ridge_lag", "1.0", {}, False,
                       f"scikit-learn unavailable: {exc}")
    y = train.to_numpy(dtype=float)
    p = min(max_lags, max(2, len(y) // 6))
    min_rows = 2 * p
    if len(y) < min_rows + horizon:
        return _result("ridge_lag", "1.0", {"lags": p}, False,
                       f"need >= {min_rows + horizon} samples for a "
                       f"{p}-lag design, has {len(y)}")
    cfg = {"lags": p, "alpha": 1.0, "design": "direct multi-horizon"}
    res = _result("ridge_lag", "1.0", cfg, True)

    def build(target: np.ndarray, origin_block: np.ndarray, h: int):
        """Rows of ``p`` consecutive lags ending at index i, aligned to the
        true value ``h`` steps after the window ends (index i+p+h-1)."""
        n = len(origin_block) - p - h + 1
        if n <= p:
            return None, None
        X = np.lib.stride_tricks.sliding_window_view(origin_block, p)[:n]
        t = target[p + h - 1: p + h - 1 + n]
        return X, t

    models: list[tuple] = []
    train_errs: list[float] = []
    for h in range(1, max(horizon, 1) + 1):
        X, t = build(y, y, h)
        if X is None or len(X) < p + 2:
            models.append((None, None))
            continue
        scaler = StandardScaler().fit(X)
        model = Ridge(alpha=1.0).fit(scaler.transform(X), t)
        models.append((scaler, model))
        train_errs.extend((model.predict(scaler.transform(X)) - t).tolist())
    sigma = float(np.std(train_errs, ddof=1)) if len(train_errs) >= 2 else 0.0
    res.train_mape_guard = sigma

    if test is not None:
        hist = list(y)
        res.test_pred = []
        for j in range(len(test)):
            h = 1  # direct one-step model, features = true in-time history
            scaler, model = models[0] if models else (None, None)
            if scaler is None:
                res.test_pred.append(float("nan"))
            else:
                feats = np.array([hist[-p:]]).reshape(1, -1)
                pred = float(model.predict(scaler.transform(feats))[0])
                res.test_pred.append(pred)
            hist.append(float(test.iloc[j]))
    if future_timestamps:
        hist = list(y)
        res.future = []
        for h, ts in enumerate(future_timestamps, start=1):
            scaler, model = models[min(h, len(models)) - 1] if models else (None, None)
            if scaler is None:
                v = float(hist[-1])
            else:
                feats = np.array([hist[-p:]]).reshape(1, -1)
                v = float(model.predict(scaler.transform(feats))[0])
            res.future.append({
                "target_timestamp": ts, "step": h, "predicted_value": v,
                "lower_bound": v - Z95 * sigma * math.sqrt(h),
                "upper_bound": v + Z95 * sigma * math.sqrt(h),
            })
            hist.append(v)  # recursive: uses own prediction as new history
    return res


#: Registry used by the forecast service; naive is always available.
FORECAST_MODELS = {
    "naive": naive_forecast,
    "seasonal_naive": seasonal_naive_forecast,
    "holt_damped": holt_damped_forecast,
    "holt_winters": holt_winters_forecast,
    "ridge_lag": ridge_lag_forecast,
}

#: Extra keyword arguments each registry entry accepts from the service.
#: Keeps ``FORECAST_MODELS[name](train, horizon, **kwargs_for(name))`` honest:
#: a model that needs a sampling interval is never called without one. Both
#: seasonal-aware models receive ``season_hours`` so a deployment has ONE
#: documented season definition, not two that silently disagree.
MODEL_KWARGS = {
    "seasonal_naive": ("interval_seconds", "season_hours"),
    "holt_winters": ("interval_seconds", "season_hours"),
    "holt_damped": ("val",),
    "ridge_lag": ("max_lags",),
}


# ---------------------------------------------------------- band calibration
def holdout_band_width(abs_errors, coverage: float = 0.95) -> float | None:
    """Nearest-rank empirical half-width for a requested nominal coverage.

    ``abs_errors`` are absolute errors the model actually made on data it never
    fitted (the held-out test segment). Returns None when there is nothing to
    measure - the caller then keeps the documented normal-approximation band
    instead of silently shipping a zero-width one.
    """
    vals = sorted(float(e) for e in (abs_errors or [])
                  if e is not None and not (isinstance(e, float) and math.isnan(e)))
    if not vals:
        return None
    if not 0.0 < coverage < 1.0:
        coverage = 0.95
    k = max(1, min(len(vals), math.ceil(coverage * len(vals))))
    return vals[k - 1]


def measure_band_coverage(future: list[dict], abs_errors) -> float | None:
    """Share of holdout errors the step-1 band would have contained.

    Holdout errors are one-step by construction, so the reference width is the
    half-width of step 1 - the same base every band scheme starts from. This is
    a *measurement on the model's own holdout*, not a claim about the future:
    on a handful of points it is a coarse number and is reported as such.
    Returns None when there is nothing to check against.
    """
    vals = [float(e) for e in (abs_errors or [])
            if e is not None and not (isinstance(e, float) and math.isnan(e))]
    if not vals or not future:
        return None
    half = _half_width(future[0])
    if half is None:
        return None
    hits = sum(1.0 for e in vals if e <= half + 1e-12)
    return hits / len(vals)


def _half_width(step: dict) -> float | None:
    """Half-width of a published band, or None when it is not a number."""
    if step.get("upper_bound") is None or step.get("predicted_value") is None:
        return None
    try:
        return abs(float(step["upper_bound"]) - float(step["predicted_value"]))
    except (TypeError, ValueError):
        return None


def apply_holdout_bands(future: list[dict], abs_errors,
                        coverage: float = 0.95,
                        mode: str = "empirical") -> tuple[list[dict], dict]:
    """Re-cut published bands from the model's real holdout errors.

    The sqrt(h) normal-approximation bands the models emit by default are a
    documented approximation. When a holdout exists this replaces them with an
    empirical (split-conformal style) half-width measured on errors the model
    actually made:

    * ``empirical``         - constant half-width (the classic conformal choice:
      marginal coverage under exchangeability of the errors);
    * ``empirical_sqrt_h``  - the same width grown by sqrt(h), because multi-step
      errors usually do grow with the horizon and a flat band is likely too
      narrow late in the horizon;
    * ``normal_sqrt_h``     - leave the model's own approximation untouched.

    Never mutates the caller's list and never changes a predicted value. Returns
    ``(new_future, band_meta)``; when nothing can be calibrated (no holdout
    errors, no future steps, or an unknown mode) the original future comes back
    unchanged and ``band_meta["kind"]`` says why.
    """
    meta = {"mode": mode, "nominal_coverage": coverage, "kind": "normal_sqrt_h",
            "half_width": None, "n_errors": 0, "measured_coverage": None}
    errors = [float(e) for e in (abs_errors or [])
              if e is not None and not (isinstance(e, float) and math.isnan(e))]
    meta["n_errors"] = len(errors)
    if not future:
        meta["kind"] = "no-forecast-steps"
        return list(future or []), meta
    if mode not in ("empirical", "empirical_sqrt_h"):
        meta["measured_coverage"] = measure_band_coverage(future, errors)
        return [dict(f) for f in future], meta
    width = holdout_band_width(errors, coverage) if errors else None
    if width is None:
        # Nothing measured: keep the documented approximation, say so plainly.
        meta["kind"] = "normal_sqrt_h_unmeasured"
        meta["measured_coverage"] = None
        return [dict(f) for f in future], meta
    recalibrated = []
    for f in future:
        h = int(f.get("step") or 1)
        half = width * math.sqrt(h) if mode == "empirical_sqrt_h" else width
        v = float(f["predicted_value"])
        g = dict(f)
        g["lower_bound"] = v - half
        g["upper_bound"] = v + half
        g["band_half_width"] = half
        recalibrated.append(g)
    meta.update({"kind": mode, "half_width": width,
                 "measured_coverage": measure_band_coverage(recalibrated,
                                                            errors)})
    return recalibrated, meta


def realized_band_coverage(rows) -> dict:
    """Check published bands against observations that later arrived.

    ``rows`` are persisted prediction records exposing ``predicted_value``,
    ``lower_bound``, ``upper_bound`` and ``actual_value`` (anything without a
    backfilled actual is skipped, never guessed). Returns ``{"n_checked": n,
    "n_inside": k, "coverage": k/n or None, "n_unverified": m}`` - the honest
    "did the band actually hold?" number for the predictions that time has since
    confirmed. Zero checked rows yields coverage None, not 100%.
    """
    checked = inside = unverified = 0
    for r in rows:
        actual = getattr(r, "actual_value", None)
        if actual is None:
            unverified += 1
            continue
        lo = getattr(r, "lower_bound", None)
        hi = getattr(r, "upper_bound", None)
        if lo is None or hi is None:
            unverified += 1
            continue
        checked += 1
        if float(lo) <= float(actual) <= float(hi):
            inside += 1
    return {
        "n_checked": checked,
        "n_inside": inside,
        "coverage": (inside / checked) if checked else None,
        "n_unverified": unverified,
    }
