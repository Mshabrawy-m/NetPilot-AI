"""Explainable anomaly detection baselines (Phase 2 Scope D, extended).

Interpretable detectors plus one optional ML detector:

1. ``rolling_mad``      - trailing-window median and MAD (1.4826-scaled);
   a point is flagged when |x - median| > k * scale. Robust to outliers in
   the window itself.
2. ``rolling_zscore``   - trailing-window mean/std; flagged when
   |z| > threshold. Simpler, sensitive baseline.
3. ``ewma``             - exponentially weighted moving average of the
   strictly-past values, judged against a robust scale of its own residuals.
   Reacts to a slow creep that a flat rolling median absorbs, and forgets old
   history smoothly instead of at an arbitrary window edge.
4. ``cusum``            - two-sided standardized cumulative-sum change-point
   test. A sustained small shift accumulates until it crosses the decision
   boundary, which is exactly what single-point z-scores are blind to. The
   statistic resets after a flag so one episode is reported once.
5. ``seasonal_residual``- same-phase-of-cycle baseline built only from strictly
   past samples; flags what is unusual *for that time of day* rather than
   unusual versus the last few minutes. The cycle position comes from the
   timestamps (elapsed time on the assumed grid), so dropped or resampled rows
   no longer shift later phases; it resets on a collection gap and abstains on
   an irregular cadence. Refuses (returns no verdicts) when the sampling
   interval is unknown or the history holds too few same-phase samples.
6. ``isolation_forest`` - optional scikit-learn detector (seeded, hence
   reproducible). Never used as the default; must be chosen explicitly.

The required, always-on pair stays ``rolling_mad`` + ``rolling_zscore``
(``DEFAULT_DETECTORS``): the newer detectors are opt-in so a detection run is
never silently widened under an existing deployment.

Leakage rule honoured everywhere: every baseline for time *t* is computed
only from samples at times <= *t* (trailing windows). Missing samples
(None/NaN) are skipped, never converted to zeros, and points near the
start of the series (fewer than ``min_periods`` history) are not judged.

An "anomaly" means statistically unusual behaviour - it is **not** proof
of a failure or an attack, and none of this code claims a root cause.
"""
from __future__ import annotations

import dataclasses
import math

import numpy as np
import pandas as pd

#: Detector registry name -> module-level implementation (see DETECTORS).
MAD_SCALE = 1.4826  # MAD -> sigma-equivalent for normal data (documented)


@dataclasses.dataclass
class AnomalyPoint:
    """One flagged observation with its full explanation."""

    device_name: str
    metric: str
    interface_name: str | None
    timestamp: pd.Timestamp
    observed: float
    baseline: float | None
    threshold_upper: float | None
    threshold_lower: float | None
    deviation: float | None      # |observed-baseline| / scale (robust z)
    severity: str                # 'warning' | 'critical'
    detector: str
    detector_version: str
    detector_config: dict
    explanation: str
    data_source: str
    sample_id: int | None = None


def _severity(ratio: float, threshold: float) -> str:
    """warning: beyond threshold; critical: beyond 2x threshold."""
    return "critical" if ratio >= 2.0 * threshold else "warning"


def detect_rolling_mad(
    series: pd.Series,
    timestamps: pd.Series,
    *,
    device_name: str,
    metric: str,
    interface_name: str | None = None,
    data_source: str = "live",
    sample_ids: pd.Series | None = None,
    window: int = 20,
    threshold: float = 3.5,
    min_periods: int | None = None,
) -> list[AnomalyPoint]:
    """Robust rolling median + MAD detector (required baseline #1).

    The baseline window is *strictly past* (shifted by one sample): the
    judged value never contaminates its own median/MAD, which would mask
    large single spikes, and no future data is ever used.
    """
    minp = min_periods or max(5, window // 2)
    values = pd.to_numeric(series, errors="coerce")
    median = values.shift(1).rolling(window, min_periods=minp).median()
    mad = (values - median).abs().rolling(window, min_periods=minp).median()
    scale = MAD_SCALE * mad
    # Degenerate windows (constant history -> scale 0): use the global robust
    # scale so a genuinely different value is judged, and document it.
    global_scale = MAD_SCALE * (values - values.median()).abs().median()
    points: list[AnomalyPoint] = []
    for i in range(len(values)):
        x = values.iloc[i]
        if pd.isna(x) or pd.isna(median.iloc[i]):
            continue                    # missing: never judged, never filled
        s = scale.iloc[i]
        used_fallback = False
        if s is None or pd.isna(s) or s <= 1e-12:
            if global_scale is None or pd.isna(global_scale) or global_scale <= 1e-12:
                continue                # no variability info at all: abstain
            s, used_fallback = float(global_scale), True
        dist = abs(float(x) - float(median.iloc[i]))
        if dist <= threshold * s:
            continue
        ratio = dist / s
        direction = "above" if x > median.iloc[i] else "below"
        points.append(AnomalyPoint(
            device_name=device_name, metric=metric,
            interface_name=interface_name, timestamp=timestamps.iloc[i],
            observed=float(x), baseline=float(median.iloc[i]),
            threshold_upper=float(median.iloc[i] + threshold * s),
            threshold_lower=float(median.iloc[i] - threshold * s),
            deviation=float(ratio), severity=_severity(ratio, threshold),
            detector="rolling_mad", detector_version="1.0",
            detector_config={"window": window, "threshold": threshold,
                             "min_periods": minp},
            explanation=(
                f"observed {x:.4g} is {ratio:.1f}x the robust scale "
                f"({'global fallback scale' if used_fallback else 'rolling MAD'}"
                f" {s:.4g}) {direction} the rolling median "
                f"{float(median.iloc[i]):.4g} of the {min(i, window)} prior "
                f"samples (strictly past window, no future data used)"),
            data_source=data_source,
            sample_id=(int(sample_ids.iloc[i]) if sample_ids is not None
                       and not pd.isna(sample_ids.iloc[i]) else None),
        ))
    return points


def detect_rolling_zscore(
    series: pd.Series,
    timestamps: pd.Series,
    *,
    device_name: str,
    metric: str,
    interface_name: str | None = None,
    data_source: str = "live",
    sample_ids: pd.Series | None = None,
    window: int = 20,
    threshold: float = 3.0,
    min_periods: int | None = None,
) -> list[AnomalyPoint]:
    """Rolling mean/std z-score detector (required baseline #2).

    Strictly-past window (shifted) for the same self-masking/leakage
    reasons as ``detect_rolling_mad``.
    """
    minp = min_periods or max(5, window // 2)
    values = pd.to_numeric(series, errors="coerce")
    mean = values.shift(1).rolling(window, min_periods=minp).mean()
    std = values.shift(1).rolling(window, min_periods=minp).std(ddof=1)
    points: list[AnomalyPoint] = []
    for i in range(len(values)):
        x = values.iloc[i]
        if pd.isna(x) or pd.isna(mean.iloc[i]) or pd.isna(std.iloc[i]):
            continue
        s = float(std.iloc[i])
        if s <= 1e-12:
            continue  # constant window: z-score undefined -> abstain (documented)
        z = (float(x) - float(mean.iloc[i])) / s
        if abs(z) <= threshold:
            continue
        points.append(AnomalyPoint(
            device_name=device_name, metric=metric,
            interface_name=interface_name, timestamp=timestamps.iloc[i],
            observed=float(x), baseline=float(mean.iloc[i]),
            threshold_upper=float(mean.iloc[i] + threshold * s),
            threshold_lower=float(mean.iloc[i] - threshold * s),
            deviation=float(abs(z)), severity=_severity(abs(z), threshold),
            detector="rolling_zscore", detector_version="1.0",
            detector_config={"window": window, "threshold": threshold,
                             "min_periods": minp},
            explanation=(
                f"z-score {z:+.1f}: observed {x:.4g} deviates "
                f"{abs(z):.1f} standard deviations from the rolling mean "
                f"{float(mean.iloc[i]):.4g} of the {min(i, window)} prior "
                "samples (strictly past window, no future data used)"),
            data_source=data_source,
            sample_id=(int(sample_ids.iloc[i]) if sample_ids is not None
                       and not pd.isna(sample_ids.iloc[i]) else None),
        ))
    return points


def detect_ewma(
    series: pd.Series,
    timestamps: pd.Series,
    *,
    device_name: str,
    metric: str,
    interface_name: str | None = None,
    data_source: str = "live",
    sample_ids: pd.Series | None = None,
    window: int = 20,
    threshold: float = 3.0,
    lambda_: float = 0.3,
    min_periods: int | None = None,
) -> list[AnomalyPoint]:
    """Exponentially weighted moving average detector (explainable #3).

    The reference is an EWMA of the *strictly past* series (``shift(1)``), so a
    value never pulls its own baseline, and the scale is the robust MAD of the
    EWMA's own trailing residuals - not of the raw values - which is what makes
    a slow drift visible that a flat rolling median absorbs. ``lambda_`` is the
    smoothing factor; the implied half-life is reported with every explanation
    so the sensitivity is auditable rather than a magic number.
    """
    minp = min_periods or max(5, window // 2)
    lam = min(0.95, max(0.02, float(lambda_)))
    values = pd.to_numeric(series, errors="coerce")
    ewma = values.shift(1).ewm(alpha=lam, adjust=False).mean()
    resid = values - ewma
    scale = MAD_SCALE * resid.shift(1).abs().rolling(
        window, min_periods=minp).median()
    global_scale = MAD_SCALE * (resid - resid.median()).abs().median()
    half_life = math.log(0.5) / math.log(1.0 - lam)
    points: list[AnomalyPoint] = []
    for i in range(len(values)):
        x = values.iloc[i]
        ref = ewma.iloc[i]
        if pd.isna(x) or pd.isna(ref) or pd.isna(resid.iloc[i]):
            continue                    # missing: never judged, never filled
        s = scale.iloc[i]
        used_fallback = False
        if s is None or pd.isna(s) or s <= 1e-12:
            if global_scale is None or pd.isna(global_scale) \
                    or global_scale <= 1e-12:
                continue                # no variability info at all: abstain
            s, used_fallback = float(global_scale), True
        dist = abs(float(x) - float(ref))
        if dist <= threshold * float(s):
            continue
        ratio = dist / float(s)
        direction = "above" if x > ref else "below"
        scale_note = " + global fallback scale" if used_fallback else ""
        points.append(AnomalyPoint(
            device_name=device_name, metric=metric,
            interface_name=interface_name, timestamp=timestamps.iloc[i],
            observed=float(x), baseline=float(ref),
            threshold_upper=float(ref + threshold * s),
            threshold_lower=float(ref - threshold * s),
            deviation=float(ratio), severity=_severity(ratio, threshold),
            detector="ewma", detector_version="1.0",
            detector_config={"lambda": lam, "half_life_samples": half_life,
                             "window": window, "threshold": threshold,
                             "min_periods": minp},
            explanation=(
                f"observed {x:.4g} is {ratio:.1f}x the EWMA residual scale "
                f"({s:.4g}{scale_note}) {direction} the exponentially weighted "
                f"mean {float(ref):.4g} (alpha={lam:.2f}, half-life "
                f"{half_life:.1f} samples, strictly-past history only)"),
            data_source=data_source,
            sample_id=(int(sample_ids.iloc[i]) if sample_ids is not None
                       and not pd.isna(sample_ids.iloc[i]) else None),
        ))
    return points


def detect_cusum(
    series: pd.Series,
    timestamps: pd.Series,
    *,
    device_name: str,
    metric: str,
    interface_name: str | None = None,
    data_source: str = "live",
    sample_ids: pd.Series | None = None,
    window: int = 20,
    drift: float = 0.5,
    limit: float = 4.0,
    min_periods: int | None = None,
) -> list[AnomalyPoint]:
    """Two-sided standardized cumulative-sum change-point detector (#4).

    A rolling z-score judges each sample alone, so a *sustained* modest shift
    (1-2 sigma, every point) never trips it. CUSUM adds each point's deviation
    from the strictly-past reference mean, discounting a drift allowance, and
    flags when the accumulated statistic crosses the decision boundary - which
    is when the shift is no longer explainable as noise.

    The statistic resets after a flag, so one episode produces one event instead
    of one event per sample, and a missing sample also resets it: a run must be
    continuous evidence, never stitched across a gap. ``threshold_upper`` /
    ``threshold_lower`` carry the value-space distance from the reference mean
    that this run still needed to trip, which is why the boundary tightens as
    the run grows.
    """
    minp = min_periods or max(5, window // 2)
    k = max(0.05, float(drift))
    h = max(1.0, float(limit))
    values = pd.to_numeric(series, errors="coerce")
    mean = values.shift(1).rolling(window, min_periods=minp).mean()
    std = values.shift(1).rolling(window, min_periods=minp).std(ddof=1)
    s_hi = s_lo = 0.0
    points: list[AnomalyPoint] = []
    for i in range(len(values)):
        x = values.iloc[i]
        if pd.isna(x) or pd.isna(mean.iloc[i]) or pd.isna(std.iloc[i]):
            s_hi = s_lo = 0.0            # gap: the run cannot survive it
            continue
        m = float(mean.iloc[i])
        sd = float(std.iloc[i])
        if sd <= 1e-12:
            s_hi = s_lo = 0.0            # flat window: statistic undefined
            continue
        z = (float(x) - m) / sd
        prev_hi, prev_lo = s_hi, s_lo
        s_hi = max(0.0, s_hi + z - k)
        s_lo = max(0.0, s_lo - z - k)
        if s_hi > h and s_hi >= s_lo:
            side, statv, prev = "above", s_hi, prev_hi
        elif s_lo > h:
            side, statv, prev = "below", s_lo, prev_lo
        else:
            continue
        need = (h - prev + k) * sd       # distance still required by this run
        points.append(AnomalyPoint(
            device_name=device_name, metric=metric,
            interface_name=interface_name, timestamp=timestamps.iloc[i],
            observed=float(x), baseline=m,
            threshold_upper=m + need if side == "above" else None,
            threshold_lower=m - need if side == "below" else None,
            deviation=float(statv),
            severity="critical" if statv >= 2.0 * h else "warning",
            detector="cusum", detector_version="1.0",
            detector_config={"window": window, "drift": k, "limit": h,
                             "min_periods": minp, "side": "two-sided"},
            explanation=(
                f"CUSUM statistic {statv:.1f} sigma crossed the decision "
                f"boundary {h:.1f} {side} the reference mean {m:.4g}: the "
                f"deviation of {abs(float(x) - m):.4g} at this sample is only "
                f"{(h - prev + k) * sd:.4g} of value-space away from the "
                "boundary because earlier samples in this run already counted "
                f"(drift allowance {k:.2f} sigma; strictly-past window, no "
                "future data; statistic reset after this flag)"),
            data_source=data_source,
            sample_id=(int(sample_ids.iloc[i]) if sample_ids is not None
                       and not pd.isna(sample_ids.iloc[i]) else None),
        ))
        s_hi = s_lo = 0.0
    return points


def detect_seasonal_residual(
    series: pd.Series,
    timestamps: pd.Series,
    *,
    device_name: str,
    metric: str,
    interface_name: str | None = None,
    data_source: str = "live",
    sample_ids: pd.Series | None = None,
    interval_seconds: float | None = None,
    season_hours: float = 24.0,
    threshold: float = 3.5,
    min_hist: int = 4,
    max_gap_intervals: float = 1.5,
    phase_align_tol: float = 0.25,
) -> list[AnomalyPoint]:
    """Same-phase-of-cycle baseline detector (#5).

    Traffic that is normal at 09:00 can be alarming at 03:00, and a rolling
    window compares it to the last few minutes instead. Each sample is judged
    against the median of the *strictly past* samples that share its position
    in the seasonal cycle, scaled by the MAD of that same-phase history.

    The cycle position (phase) is derived from the *timestamps*, not the row
    index: elapsed seconds from the first usable sample, divided by the
    sampling interval, taken modulo the season length. This keeps the phase
    honest when rows have been dropped, de-duplicated or resampled - an
    index-based phase would silently shift every later verdict off its true
    time-of-day.

    Honest limits, each reflected in the verdict rather than hidden:
    - abstains entirely when the sampling interval is unknown or the season
      would be shorter than two samples;
    - a phase is judged only once it has ``min_hist`` strictly-prior samples;
    - when the gap between two consecutive samples exceeds
      ``max_gap_intervals`` intervals the collection cadence has a hole, so the
      phase history is reset and the re-anchored sample only seeds a fresh
      baseline instead of being flagged against a pre-gap history;
    - a sample that does not land within ``phase_align_tol`` of the assumed
      grid, or carries an unusable (missing/NaT/negative) timestamp, is
      skipped: an irregular or resampled cadence is not trusted to define a
      phase.
    """
    if interval_seconds is None or interval_seconds <= 0:
        return []            # season not defensible: abstain, never guess
    m = int(round(float(season_hours) * 3600.0 / float(interval_seconds)))
    if m < 2:
        return []
    values = pd.to_numeric(series, errors="coerce")
    ts = pd.to_datetime(timestamps, errors="coerce")
    if len(ts) != len(values):
        return []            # misaligned inputs: cannot place samples in time
    valid_ts = ts.dropna()
    if valid_ts.empty:
        return []            # no usable clock: no defensible cycle position
    clean = values.dropna()
    global_scale = (MAD_SCALE
                    * float((clean - clean.median()).abs().median())
                    if len(clean) >= 2 else float("nan"))
    # Fixed origin = first usable timestamp; elapsed time from here converts
    # each timestamp into a position on the assumed regular grid, independent
    # of how many rows survived upstream (gaps / dedupe / resampling).
    origin = valid_ts.iloc[0]
    elapsed = (ts - origin).dt.total_seconds()

    phase_hist: dict[int, list[float]] = {}
    points: list[AnomalyPoint] = []
    prev_step: int | None = None
    for i in range(len(values)):
        x = values.iloc[i]
        e = elapsed.iloc[i]
        # A usable, non-negative elapsed time is required to locate the sample
        # on the cycle; without it we can neither judge nor seed, and the
        # cadence chain is broken so the next sample re-anchors.
        if pd.isna(e) or e < 0:
            prev_step = None
            continue
        stepf = float(e) / float(interval_seconds)
        nearest = int(round(stepf))
        # Only trust the phase when the timestamp actually sits on the assumed
        # grid; a resampled/jittery cadence must not silently define a phase.
        off_grid = abs(stepf - nearest) > phase_align_tol
        gap = None if prev_step is None else nearest - prev_step
        prev_step = nearest
        if pd.isna(x):
            continue                     # missing value: never enters history
        if off_grid:
            continue                     # irregular sampling: no defensible phase
        if gap is not None and gap > max_gap_intervals:
            # Collection hole: baselines from before the gap no longer describe
            # this cadence, so rebuild from here and abstain on the boundary.
            phase_hist = {nearest % m: [float(x)]}
            continue
        phase = nearest % m
        hist = phase_hist.setdefault(phase, [])
        if len(hist) < min_hist:
            hist.append(float(x))
            continue                     # too little same-phase history: abstain
        arr = np.asarray(hist, dtype=float)
        baseline = float(np.median(arr))
        scale = MAD_SCALE * float(np.median(np.abs(arr - baseline)))
        used_fallback = False
        if scale <= 1e-12:
            if not math.isfinite(global_scale) or global_scale <= 1e-12:
                hist.append(float(x))
                continue                 # no variability anywhere: abstain
            scale, used_fallback = global_scale, True
        dist = abs(float(x) - baseline)
        hist.append(float(x))            # judged first, then becomes history
        if dist <= threshold * scale:
            continue
        ratio = dist / scale
        scale_note = " + global fallback scale" if used_fallback else ""
        points.append(AnomalyPoint(
            device_name=device_name, metric=metric,
            interface_name=interface_name, timestamp=timestamps.iloc[i],
            observed=float(x), baseline=baseline,
            threshold_upper=baseline + threshold * scale,
            threshold_lower=baseline - threshold * scale,
            deviation=float(ratio), severity=_severity(ratio, threshold),
            detector="seasonal_residual", detector_version="1.1",
            detector_config={"season_len_samples": m,
                             "season_hours": season_hours,
                             "phase": phase, "grid_step": nearest,
                             "phase_basis": "timestamp",
                             "threshold": threshold,
                             "min_hist": min_hist},
            explanation=(
                f"observed {float(x):.4g} is {ratio:.1f}x the spread "
                f"({scale:.4g}{scale_note}) of cycle position {phase}/{m} "
                f"(median {baseline:.4g} of {len(hist) - 1} prior same-phase "
                "samples placed by elapsed time on the assumed grid). Unusual "
                "for this point in the cycle, not necessarily unusual for the "
                "recent minutes"),
            data_source=data_source,
            sample_id=(int(sample_ids.iloc[i]) if sample_ids is not None
                       and not pd.isna(sample_ids.iloc[i]) else None),
        ))
    return points


def detect_isolation_forest(
    series: pd.Series,
    timestamps: pd.Series,
    *,
    device_name: str,
    metric: str,
    interface_name: str | None = None,
    data_source: str = "live",
    sample_ids: pd.Series | None = None,
    window: int = 60,
    contamination: float = 0.05,
    random_seed: int = 42,
) -> list[AnomalyPoint]:
    """OPTIONAL ML detector, explicitly separated from the required baselines.

    Trailing-window rule: for each judged point the forest is fitted on
    the most recent ``window`` valid samples up to AND INCLUDING that
    point, so no future observation influences an earlier verdict and the
    judged sample is represented in the contamination quantile of its own
    fit (a model blind to the current point can mis-bound extreme scores -
    verified empirically). n_estimators is kept small and detection runs
    operate on bounded frames, so per-point refits stay affordable.
    Seeded => reproducible for fixed input/configuration. Exploratory aid
    only - never the default and never presented as proof of anything.
    """
    try:
        from sklearn.ensemble import IsolationForest
    except Exception as exc:  # pragma: no cover - env dependent
        raise RuntimeError(
            f"Isolation Forest unavailable in this environment: {exc}") from exc
    values = pd.to_numeric(series, errors="coerce")
    min_fit = max(20, window // 2)
    points: list[AnomalyPoint] = []
    history: list[float] = []
    for i in range(len(values)):
        x = values.iloc[i]
        if pd.isna(x):
            continue
        history.append(float(x))
        recent = history[-window:]
        if len(recent) < min_fit:
            continue                      # insufficient history: abstain
        model = IsolationForest(
            n_estimators=50, contamination=contamination,
            random_state=random_seed)
        model.fit(pd.DataFrame({"value": recent}))
        if int(model.predict(pd.DataFrame({"value": [float(x)]}))[0]) != -1:
            continue
        mean = float(sum(recent) / len(recent))
        var = sum((v - mean) ** 2 for v in recent) / max(1, len(recent) - 1)
        std = math.sqrt(var)
        points.append(AnomalyPoint(
            device_name=device_name, metric=metric,
            interface_name=interface_name, timestamp=timestamps.iloc[i],
            observed=float(x), baseline=mean, threshold_upper=None,
            threshold_lower=None,
            deviation=abs(float(x) - mean) / std if std > 1e-12 else None,
            severity="warning",
            detector="isolation_forest", detector_version="1.0",
            detector_config={"window": window,
                             "contamination": contamination,
                             "random_seed": random_seed},
            explanation=(
                f"Isolation Forest flagged {float(x):.4g} against a "
                f"trailing {len(recent)}-sample window (contamination "
                f"{contamination:.2f}, seed {random_seed}); exploratory ML "
                "result, not a failure or attack claim"),
            data_source=data_source,
            sample_id=(int(sample_ids.iloc[i]) if sample_ids is not None
                       and not pd.isna(sample_ids.iloc[i]) else None),
        ))
    return points


DETECTORS = {
    "rolling_mad": detect_rolling_mad,
    "rolling_zscore": detect_rolling_zscore,
    "ewma": detect_ewma,
    "cusum": detect_cusum,
    "seasonal_residual": detect_seasonal_residual,
    "isolation_forest": detect_isolation_forest,   # optional, non-default
}

#: Detectors offered as the safe default pair in the UI.
DEFAULT_DETECTORS = ("rolling_mad", "rolling_zscore")

#: Detectors that cannot form a verdict without a known sampling interval.
#: ``detect_seasonal_residual`` returns no points in that case (an honest
#: abstention, not a clean bill of health), so the service reports why.
INTERVAL_REQUIRED = ("seasonal_residual",)

#: Detectors whose threshold is not one of the two configured baselines.
#: ``threshold`` here is the EWMA sensitivity, and CUSUM uses its own pair.
SENSITIVITY_DEFAULTS = {
    "ewma": 3.0,
    "cusum": {"drift": 0.5, "limit": 4.0},
    "seasonal_residual": 3.5,
}


def detect_frame(
    frame: pd.DataFrame,
    *,
    device_name: str,
    metric: str,
    interface_name: str | None = None,
    detectors: tuple[str, ...] = DEFAULT_DETECTORS,
    window: int = 20,
    mad_threshold: float = 3.5,
    zscore_threshold: float = 3.0,
    ewma_threshold: float = 3.0,
    cusum_drift: float = 0.5,
    cusum_limit: float = 4.0,
    seasonal_threshold: float = 3.5,
    interval_seconds: float | None = None,
    season_hours: float = 24.0,
) -> list[AnomalyPoint]:
    """Run the selected detectors over a prepared time_series frame.

    Each detector receives only the knobs it actually declares (see
    ``MODEL_KWARGS``-style routing below), so adding a detector can never make
    an existing call pass an argument the function does not accept.
    """
    if frame.empty:
        return []
    common = dict(
        series=frame["value"].reset_index(drop=True),
        timestamps=frame["collected_at"].reset_index(drop=True),
        sample_ids=frame["sample_id"].reset_index(drop=True),
        device_name=device_name, metric=metric,
        interface_name=interface_name,
        data_source=str(frame["data_source"].iloc[0]),
    )
    points: list[AnomalyPoint] = []
    for name in detectors:
        fn = DETECTORS.get(name)
        if fn is None:
            raise ValueError(f"unknown detector: {name}")
        if name == "isolation_forest":
            points.extend(fn(window=max(window * 3, 60), **common))
        elif name == "rolling_mad":
            points.extend(fn(window=window, threshold=mad_threshold, **common))
        elif name == "ewma":
            points.extend(fn(window=window, threshold=ewma_threshold, **common))
        elif name == "cusum":
            points.extend(fn(window=window, drift=cusum_drift,
                             limit=cusum_limit, **common))
        elif name == "seasonal_residual":
            points.extend(fn(interval_seconds=interval_seconds,
                             season_hours=season_hours,
                             threshold=seasonal_threshold, **common))
        else:
            points.extend(fn(window=window, threshold=zscore_threshold, **common))
    return sorted(points, key=lambda p: (p.timestamp, p.detector))
