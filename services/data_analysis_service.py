"""Pure computation layer for the read-only Data Analysis page.

The page owns Streamlit widgets and Plotly figures. Every transformation,
readiness gate and derived table lives here so it can be tested without a
browser and reused by the live-data proof. Nothing in this module opens a
database session, writes a row, renders a widget or calls a provider.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd

from analytics import anomaly_detection, data_quality, eda
from analytics.derived_metrics import COUNTER_METRICS, derive_for_counter_metric
from analytics.time_series import (
    FRAME_COLUMNS,
    dedupe_frame,
    median_interval_seconds,
    resample_frame,
)
from config import get_settings

#: Chart rows above this bound are reduced deterministically; summaries,
#: anomalies and quality still use the full prepared series.
MAX_PLOT_POINTS = 5000
MIN_CORR_POINTS = eda.MIN_CORR_POINTS
RESAMPLE_OPTIONS = ("raw", "1min", "5min", "15min", "1h")
ANOMALY_COLUMNS = (
    "timestamp",
    "detector",
    "severity",
    "observed",
    "baseline",
    "threshold_upper",
    "threshold_lower",
    "deviation",
    "explanation",
)
BAND_COLUMNS = ("collected_at", "value", "rolling_mean", "upper", "lower")


@dataclass
class PreparedSeries:
    """Deduped source rows, computed rows and the bounded chart subset."""

    source: pd.DataFrame
    frame: pd.DataFrame
    plot: pd.DataFrame
    info: dict[str, object]


@dataclass
class AnalysisResult:
    """Everything the page needs for one provenance-scoped selection."""

    prepared: PreparedSeries
    quality: data_quality.SeriesQuality
    stats: dict[str, object]
    histogram: dict[str, object]
    profile: dict[str, object]
    band: pd.DataFrame
    anomalies: pd.DataFrame
    anomaly_notes: tuple[str, ...] = field(default_factory=tuple)
    comparison: dict[str, object] | None = None
    device_comparison: dict[str, object] | None = None
    correlations: tuple[dict[str, object], ...] = field(default_factory=tuple)
    counter: dict[str, object] | None = None


def _downsample(frame: pd.DataFrame, max_points: int) -> tuple[pd.DataFrame, int | None]:
    """Pick evenly spaced rows including the first and last observation."""
    count = len(frame)
    if count <= max_points:
        return frame.copy(), None
    positions = sorted({
        round(index * (count - 1) / (max_points - 1))
        for index in range(max_points)
    })
    sampled = frame.iloc[positions].copy()
    step = max(1, math.ceil(count / max_points))
    return sampled, step


def prepare_frame(
    frame: pd.DataFrame,
    *,
    resample: str = "raw",
    agg: str = "mean",
    max_points: int = MAX_PLOT_POINTS,
) -> PreparedSeries:
    """Dedupe, optionally resample, then bound the chart payload.

    The returned ``source`` is the deduped but unresampled series; it is used
    for quality and counter rates so resampling cannot hide gaps or rewrite
    cumulative counters.
    """
    if resample not in RESAMPLE_OPTIONS:
        raise ValueError(f"unsupported resample option: {resample}")
    limit = max(2, min(max_points, 100000))
    deduped = dedupe_frame(frame).sort_values("collected_at").reset_index(drop=True)
    source = deduped.copy()
    if resample == "raw":
        computed = deduped
    else:
        computed = resample_frame(deduped, rule=resample, agg=agg)
    computed = computed.sort_values("collected_at").reset_index(drop=True)
    plot, step = _downsample(computed, limit)
    info: dict[str, object] = {
        "input_rows": len(frame),
        "deduped_rows": len(source),
        "computed_rows": len(computed),
        "plot_rows": len(plot),
        "resample": resample,
        "agg": agg,
        "downsampled": step is not None,
        "downsample_step": step,
        "max_plot_points": limit,
    }
    return PreparedSeries(source=source, frame=computed, plot=plot, info=info)


def prepare_scope_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Dedupe a wide comparison frame that has no ``sample_id`` column."""
    if frame.empty:
        return frame.copy()
    keys = [name for name in ("collected_at", "device", "interface")
            if name in frame.columns]
    clean = frame.dropna(subset=["collected_at", "value"]).copy()
    if keys:
        clean = clean.drop_duplicates(subset=keys, keep="last")
    return clean.sort_values("collected_at").reset_index(drop=True)


def _series(frame: pd.DataFrame) -> pd.Series:
    valid = frame.dropna(subset=["collected_at", "value"])
    return pd.Series(
        valid["value"].to_numpy(dtype=float),
        index=pd.DatetimeIndex(valid["collected_at"]),
        name="value",
    )


def summarize(frame: pd.DataFrame) -> dict[str, object]:
    """Return summary, histogram and time-of-day readiness together."""
    return {
        "stats": eda.summary_stats(frame),
        "histogram": eda.histogram(frame),
        "profile": eda.time_of_day_profile(frame),
    }


def chart_series(result: AnalysisResult, factor: float = 2.0) -> pd.DataFrame:
    """Insert NaN breaks so Plotly never connects across real sampling gaps."""
    return eda.insert_gap_breaks(result.prepared.plot, factor=factor)


def quality_report(
    frame: pd.DataFrame,
    *,
    device_name: str,
    metric: str,
    interface_name: str | None,
    data_source: str,
    unit: str | None,
    expected_interval_seconds: float | None,
    now: pd.Timestamp | None = None,
) -> data_quality.SeriesQuality:
    """Compute the shared readiness gates for the deduped source series."""
    return data_quality.summarize_quality(
        frame,
        device_name,
        metric,
        interface_name=interface_name,
        expected_interval_seconds=expected_interval_seconds,
        now=now,
    )


def gap_report(quality: data_quality.SeriesQuality) -> list[dict[str, object]]:
    return eda.gap_summary(quality)


def rolling_band(
    frame: pd.DataFrame,
    *,
    window: int = 20,
    sigma: float = 2.0,
) -> pd.DataFrame:
    """Trailing mean +/- sigma standard deviations, never using future rows."""
    empty = pd.DataFrame(columns=list(BAND_COLUMNS))
    values = frame.dropna(subset=["collected_at", "value"])
    min_points = max(2, int(get_settings().min_points_analysis))
    if len(values) < min_points:
        return empty
    span = max(2, min(window, len(values)))
    rolling = values["value"].astype(float).rolling(
        span, min_periods=min_points)
    result = pd.DataFrame({
        "collected_at": values["collected_at"].to_numpy(),
        "value": values["value"].to_numpy(dtype=float),
        "rolling_mean": rolling.mean().to_numpy(),
        "upper": (rolling.mean() + sigma * rolling.std(ddof=0)).to_numpy(),
        "lower": (rolling.mean() - sigma * rolling.std(ddof=0)).to_numpy(),
    })
    return result[list(BAND_COLUMNS)]


def anomaly_report(
    frame: pd.DataFrame,
    *,
    quality: data_quality.SeriesQuality,
    device_name: str,
    metric: str,
    interface_name: str | None,
    detectors: tuple[str, ...] = anomaly_detection.DEFAULT_DETECTORS,
) -> tuple[pd.DataFrame, tuple[str, ...]]:
    """Run the read-only detectors, or state why they cannot form an opinion."""
    if not quality.ready(data_quality.DETECTION):
        reasons = quality.reasons_for(data_quality.DETECTION)
        detail = "; ".join(reasons) if reasons else "detection readiness gate not met"
        return (pd.DataFrame(columns=list(ANOMALY_COLUMNS)),
                (f"Anomaly detection skipped: {detail}",))
    settings = get_settings()
    interval = median_interval_seconds(frame)
    points = anomaly_detection.detect_frame(
        frame,
        device_name=device_name,
        metric=metric,
        interface_name=interface_name,
        detectors=detectors,
        window=settings.detector_window,
        mad_threshold=settings.mad_threshold,
        zscore_threshold=settings.zscore_threshold,
        interval_seconds=interval,
    )
    rows = [{
        "timestamp": point.timestamp,
        "detector": point.detector,
        "severity": point.severity,
        "observed": point.observed,
        "baseline": point.baseline,
        "threshold_upper": point.threshold_upper,
        "threshold_lower": point.threshold_lower,
        "deviation": point.deviation,
        "explanation": point.explanation,
    } for point in points]
    anomalies = pd.DataFrame(rows, columns=list(ANOMALY_COLUMNS))
    if points:
        note = ((f"{len(points)} detector point(s) from "
                 f"{', '.join(detectors)} on {len(frame)} prepared sample(s)."),)
    else:
        note = ((f"No anomalies flagged by {', '.join(detectors)} "
                 f"on {len(frame)} prepared sample(s)."),)
    return anomalies, note


def counter_report(frame: pd.DataFrame, metric: str) -> dict[str, object] | None:
    """Derive defensible rates from raw counters; never mutate raw samples."""
    if metric not in COUNTER_METRICS:
        return None
    return derive_for_counter_metric(frame, metric)


def correlation_report(
    primary_metric: str,
    primary_frame: pd.DataFrame,
    related: dict[str, pd.DataFrame],
) -> tuple[dict[str, object], ...]:
    """Pairwise correlations on exact-timestamp aligned observations."""
    series = {primary_metric: _series(primary_frame)}
    series.update({
        metric: _series(frame) for metric, frame in related.items()
    })
    return tuple(eda.pairwise_correlation(series))


def analyze(
    *,
    frame: pd.DataFrame,
    device_name: str,
    metric: str,
    interface_name: str | None,
    data_source: str,
    unit: str | None,
    expected_interval_seconds: float | None,
    now: pd.Timestamp | None = None,
    resample: str = "raw",
    comparison_frame: pd.DataFrame | None = None,
    device_frame: pd.DataFrame | None = None,
    related_frames: dict[str, pd.DataFrame] | None = None,
) -> AnalysisResult:
    """Compute one complete read-only analysis bundle."""
    prepared = prepare_frame(frame, resample=resample)
    quality = quality_report(
        prepared.source,
        device_name=device_name,
        metric=metric,
        interface_name=interface_name,
        data_source=data_source,
        unit=unit,
        expected_interval_seconds=expected_interval_seconds,
        now=now,
    )
    summaries = summarize(prepared.frame)
    anomalies, notes = anomaly_report(
        prepared.frame,
        quality=quality,
        device_name=device_name,
        metric=metric,
        interface_name=interface_name,
    )
    comparison = None
    if comparison_frame is not None and not comparison_frame.empty:
        comparison = eda.group_comparison(
            prepare_scope_frame(comparison_frame), by="interface")
    device_comparison = None
    if device_frame is not None and not device_frame.empty:
        device_comparison = eda.group_comparison(
            prepare_scope_frame(device_frame), by="device")
    correlations: tuple[dict[str, object], ...] = ()
    if related_frames:
        correlations = correlation_report(metric, prepared.frame, related_frames)
    return AnalysisResult(
        prepared=prepared,
        quality=quality,
        stats=summaries["stats"],
        histogram=summaries["histogram"],
        profile=summaries["profile"],
        band=rolling_band(prepared.frame),
        anomalies=anomalies,
        anomaly_notes=notes,
        comparison=comparison,
        device_comparison=device_comparison,
        correlations=correlations,
        counter=counter_report(prepared.source, metric),
    )


def summary_table(result: AnalysisResult) -> pd.DataFrame:
    """Render-ready summary table; ``available=False`` stays explicit."""
    stats = dict(result.stats)
    available = bool(stats.pop("available", False))
    if not available:
        return pd.DataFrame({"value": [str(stats.get("reason", "unavailable"))]})
    table = pd.DataFrame({"value": pd.Series(stats, dtype=object).astype(str)})
    table.index.name = "stat"
    return table.reset_index()


def quality_table(result: AnalysisResult) -> pd.DataFrame:
    return pd.DataFrame(gap_report(result.quality)).astype({"value": str})


def export_frames(result: AnalysisResult) -> dict[str, pd.DataFrame]:
    """Named CSV payloads for the page's download buttons."""
    exports = {
        "series.csv": result.prepared.source[list(FRAME_COLUMNS)],
        "summary.csv": summary_table(result),
        "quality.csv": quality_table(result),
        "anomalies.csv": result.anomalies,
    }
    if not result.band.empty:
        exports["rolling_band.csv"] = result.band
    if result.counter is not None and "rates" in result.counter:
        rates = result.counter["rates"]
        if isinstance(rates, pd.DataFrame) and not rates.empty:
            exports["derived_rates.csv"] = rates
    if result.comparison and result.comparison.get("available"):
        exports["interface_comparison.csv"] = pd.DataFrame(
            result.comparison["rows"])
    if result.device_comparison and result.device_comparison.get("available"):
        exports["device_comparison.csv"] = pd.DataFrame(
            result.device_comparison["rows"])
    if result.correlations:
        exports["correlations.csv"] = pd.DataFrame([
            {
                "pair": " ↔ ".join(str(part) for part in row["pair"]),
                "available": row["available"],
                "r": row.get("r"),
                "n": row.get("n"),
                "method": row.get("method", ""),
                "reason": row.get("reason", ""),
            }
            for row in result.correlations
        ])
    return exports


__all__ = [
    "MAX_PLOT_POINTS",
    "MIN_CORR_POINTS",
    "RESAMPLE_OPTIONS",
    "AnalysisResult",
    "PreparedSeries",
    "analyze",
    "anomaly_report",
    "chart_series",
    "correlation_report",
    "counter_report",
    "export_frames",
    "gap_report",
    "prepare_frame",
    "prepare_scope_frame",
    "quality_report",
    "quality_table",
    "rolling_band",
    "summarize",
    "summary_table",
]
