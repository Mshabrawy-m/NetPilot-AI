"""Explainable anomaly detection baselines (Phase 2 Scope D).

Two required, interpretable detectors plus one optional ML detector:

1. ``rolling_mad``      - trailing-window median and MAD (1.4826-scaled);
   a point is flagged when |x - median| > k * scale. Robust to outliers in
   the window itself.
2. ``rolling_zscore``   - trailing-window mean/std; flagged when
   |z| > threshold. Simpler, sensitive baseline.
3. ``isolation_forest`` - optional scikit-learn detector (seeded, hence
   reproducible). Never used as the default; must be chosen explicitly.

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
    "isolation_forest": detect_isolation_forest,   # optional, non-default
}

#: Detectors offered as the safe default pair in the UI.
DEFAULT_DETECTORS = ("rolling_mad", "rolling_zscore")


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
) -> list[AnomalyPoint]:
    """Run the selected detectors over a prepared time_series frame."""
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
        else:
            points.extend(fn(window=window, threshold=zscore_threshold, **common))
    return sorted(points, key=lambda p: (p.timestamp, p.detector))
