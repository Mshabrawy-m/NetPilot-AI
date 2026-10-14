"""Data-quality summary and readiness gates (Phase 2 Scope A).

Produces an explainable quality report per prepared series and simple
readiness flags for analysis / anomaly detection / forecasting. Short or
irregular series are reported as *not ready* with concrete reasons - the
pipeline must show an "insufficient data" status rather than return
confident-looking numbers built on almost nothing.
"""
from __future__ import annotations

import dataclasses
import datetime as dt

import pandas as pd

from analytics.time_series import median_interval_seconds
from config import get_settings

#: A sampling interval is "irregular" when the inter-observation gap exceeds
#: this multiple of the median/expected interval.
GAP_FACTOR = 3.0

ANALYSIS = "analysis"
DETECTION = "detection"
FORECAST = "forecast"


@dataclasses.dataclass
class SeriesQuality:
    """Data-quality report for one prepared metric series."""

    device_name: str
    metric: str
    interface_name: str | None
    data_source: str
    unit: str | None

    n_rows: int = 0                       # raw observations loaded
    n_duplicates: int = 0                 # rows collapsing to same timestamp
    n_non_null: int = 0                   # usable values
    n_null_values: int = 0                # stored-but-unavailable samples
    n_imputed: int = 0                    # labelled imputed values
    first_ts: dt.datetime | None = None
    last_ts: dt.datetime | None = None
    expected_interval_seconds: float | None = None   # configured schedule
    observed_interval_seconds: float | None = None   # median of real gaps
    gap_count: int = 0                    # sampling gaps > GAP_FACTOR*interval
    longest_gap_seconds: float | None = None
    missing_expected: int = 0             # expected minus observed slots
    completeness_pct: float | None = None
    stale_seconds: float | None = None    # age of newest observation
    is_stale: bool = False
    mixed_sources: bool = False           # demo + live rows in one series

    ready_for: dict[str, bool] = dataclasses.field(default_factory=dict)
    reasons: list[str] = dataclasses.field(default_factory=list)

    def ready(self, purpose: str) -> bool:
        return self.ready_for.get(purpose, False)

    def reasons_for(self, purpose: str) -> list[str]:
        return [r for r in self.reasons if r.startswith(purpose)]


def summarize_quality(
    frame: pd.DataFrame,
    device_name: str,
    metric: str,
    interface_name: str | None = None,
    expected_interval_seconds: float | None = None,
    now: dt.datetime | None = None,
    stale_after_seconds: float | None = None,
) -> SeriesQuality:
    """Build the quality report for one prepared frame (see time_series)."""
    settings = get_settings()
    sources = sorted(frame["data_source"].dropna().unique()) \
        if not frame.empty else []
    q = SeriesQuality(
        device_name=device_name,
        metric=metric,
        interface_name=interface_name,
        data_source="+".join(sources) if sources else "none",
        unit=(frame["unit"].dropna().iloc[-1]
              if not frame.empty and frame["unit"].notna().any() else None),
        mixed_sources=len(sources) > 1,
    )
    if frame.empty:
        q.reasons.append(f"{ANALYSIS}: no samples stored for this selection.")
        q.ready_for = {ANALYSIS: False, DETECTION: False, FORECAST: False}
        return q

    q.n_rows = int(len(frame))
    q.n_duplicates = int(frame["collected_at"].duplicated().sum())
    q.n_null_values = int(frame["value"].isna().sum())
    q.n_non_null = int(frame["value"].notna().sum())
    q.n_imputed = int(frame["imputed"].sum()) if "imputed" in frame else 0
    q.first_ts = frame["collected_at"].min()
    q.last_ts = frame["collected_at"].max()

    observed = median_interval_seconds(frame)
    q.observed_interval_seconds = observed
    q.expected_interval_seconds = (
        expected_interval_seconds
        if expected_interval_seconds is not None else observed
    )

    if q.expected_interval_seconds and q.first_ts is not None:
        span = (q.last_ts - q.first_ts).total_seconds()
        expected_slots = int(span // q.expected_interval_seconds) + 1
        q.gap_count, q.longest_gap_seconds = _gap_stats(
            frame, q.expected_interval_seconds)
        if expected_slots > 0:
            q.missing_expected = max(0, expected_slots - q.n_rows)
            q.completeness_pct = round(
                100.0 * q.n_rows / expected_slots, 1)

    reference_now = now or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    if q.last_ts is not None:
        q.stale_seconds = max(
            0.0, (reference_now - q.last_ts).total_seconds())
        stale_limit = (stale_after_seconds
                       if stale_after_seconds is not None
                       else max(4 * (q.expected_interval_seconds or 0), 1800))
        q.is_stale = q.stale_seconds > stale_limit

    # --- readiness gates -------------------------------------------------
    checks: list[tuple[str, bool, str]] = [
        (ANALYSIS, q.n_non_null >= settings.min_points_analysis,
         f"needs >= {settings.min_points_analysis} non-null samples, "
         f"has {q.n_non_null}"),
        (DETECTION, q.n_non_null >= settings.min_points_detection,
         f"needs >= {settings.min_points_detection} non-null samples, "
         f"has {q.n_non_null}"),
        (FORECAST, q.n_non_null >= settings.min_points_forecast,
         f"needs >= {settings.min_points_forecast} non-null samples, "
         f"has {q.n_non_null}"),
    ]
    for purpose, ok, why in checks:
        if not ok:
            q.reasons.append(f"{purpose}: insufficient data - {why}.")

    if q.n_non_null < 2:
        for purpose in (ANALYSIS, DETECTION, FORECAST):
            if not any(r.startswith(purpose) for r in q.reasons):
                q.reasons.append(f"{purpose}: fewer than 2 usable values.")
    elif q.observed_interval_seconds and q.expected_interval_seconds:
        # Extremely irregular sampling is unsuitable for forecasting.
        cv = _interval_cv(frame)
        if cv is not None and cv > 1.0:
            q.reasons.append(
                f"{FORECAST}: sampling is highly irregular "
                f"(interval CV {cv:.2f} > 1.0); forecasts assume a "
                "regular grid and would be misleading.")
            checks.append((FORECAST, False, "irregular sampling"))

    if q.mixed_sources:
        q.reasons.append(
            "warning: series mixes data sources "
            f"({q.data_source}); analyses must filter one source.")

    # FORECAST additionally needs a defensible regular interval.
    ready = {c[0]: c[1] for c in checks}
    if ready.get(FORECAST) and not q.observed_interval_seconds:
        ready[FORECAST] = False
        q.reasons.append(f"{FORECAST}: fewer than 2 distinct timestamps.")
    q.ready_for = ready
    return q


def _gap_stats(frame: pd.DataFrame, expected_interval: float
               ) -> tuple[int, float | None]:
    diffs = (frame["collected_at"].sort_values().diff().dropna()
             .dt.total_seconds())
    threshold = GAP_FACTOR * expected_interval
    gaps = diffs[diffs > threshold]
    return int(len(gaps)), (float(gaps.max()) if len(gaps) else None)


def _interval_cv(frame: pd.DataFrame) -> float | None:
    """Coefficient of variation of inter-observation intervals."""
    diffs = frame["collected_at"].sort_values().diff().dropna()
    diffs = diffs[diffs > pd.Timedelta(0)].dt.total_seconds()
    if len(diffs) < 2:
        return None
    mean = float(diffs.mean())
    if mean <= 0:
        return None
    return float(diffs.std(ddof=1)) / mean
