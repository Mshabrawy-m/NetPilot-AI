"""Time-series access and preparation (Phase 2 Scope A).

Converts Phase 1 ``metric_samples`` rows into analysis-ready pandas frames
without ever mutating the stored data. Guarantees:

* rows are sorted chronologically; ties at the same timestamp are resolved
  deterministically (highest sample id wins - i.e. the last written row);
* a NULL stored value becomes ``NaN`` - a missing sample is never silently
  turned into a measured zero;
* any imputation is opt-in, bounded, and labelled via the ``imputed`` column;
* provenance is preserved: ``data_source`` stays on every row and mixing
  demo with live data requires an explicit, documented choice.
"""
from __future__ import annotations

import datetime as dt
import math

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from config import get_settings
from database.models import MetricSample

#: Canonical column layout of every prepared frame produced here.
FRAME_COLUMNS = ["sample_id", "collected_at", "value", "unit",
                 "data_source", "imputed"]


def load_metric_frame(
    session: Session,
    device_name: str,
    metric: str,
    interface_name: str | None = None,
    data_source: str | None = None,
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
    max_points: int | None = None,
    device_level_only: bool = False,
) -> pd.DataFrame:
    """Query one metric series into a preparation-ready DataFrame.

    ``max_points`` keeps the most recent N rows (bounded query size, spec
    §12). ``data_source=None`` deliberately means "whatever is stored" -
    pages must pass an explicit source; only tests/EDA tooling may leave it
    open, and callers should warn when both demo and live rows appear.
    ``device_level_only=True`` selects exclusively interface-NULL rows
    (device-level metrics) instead of the unfiltered mix.
    """
    limit = max_points or get_settings().analytics_max_points
    stmt = (
        select(MetricSample)
        .where(MetricSample.device_name == device_name,
               MetricSample.metric == metric)
        .order_by(MetricSample.collected_at.desc(), MetricSample.id.desc())
        .limit(limit)
    )
    if interface_name is not None:
        stmt = stmt.where(MetricSample.interface_name == interface_name)
    elif device_level_only:
        stmt = stmt.where(MetricSample.interface_name.is_(None))
    if data_source is not None:
        stmt = stmt.where(MetricSample.data_source == data_source)
    if since is not None:
        stmt = stmt.where(MetricSample.collected_at >= since)
    if until is not None:
        stmt = stmt.where(MetricSample.collected_at <= until)

    rows = session.scalars(stmt).all()[::-1]  # chronological again
    if not rows:
        return pd.DataFrame(columns=FRAME_COLUMNS).astype(
            {"sample_id": "int64", "collected_at": "datetime64[ns]",
             "value": "float64", "unit": "object",
             "data_source": "object", "imputed": "bool"})
    frame = pd.DataFrame({
        "sample_id": [r.id for r in rows],
        "collected_at": [r.collected_at for r in rows],
        "value": [None if r.value is None else float(r.value) for r in rows],
        "unit": [r.unit for r in rows],
        "data_source": [r.data_source for r in rows],
        "imputed": [False] * len(rows),
    })
    frame["value"] = frame["value"].astype("float64")  # None -> NaN
    return frame


def dedupe_frame(frame: pd.DataFrame, keep: str = "last") -> pd.DataFrame:
    """Collapse duplicate timestamps deterministically.

    ``keep='last'`` retains the highest ``sample_id`` per timestamp (the row
    written last); ``'mean'`` aggregates numeric duplicates. The choice is
    reported by the data-quality summary either way.
    """
    if frame.empty or not frame["collected_at"].duplicated().any():
        return frame.copy()
    if keep == "mean":
        out = (frame.groupby("collected_at", as_index=False)
               .agg(value=("value", "mean"), unit=("unit", "first"),
                    data_source=("data_source", "first"),
                    imputed=("imputed", "max")))
        out["sample_id"] = -1  # synthetic aggregate row
        return out[FRAME_COLUMNS]
    return (frame.sort_values(["collected_at", "sample_id"])
            .drop_duplicates("collected_at", keep="last")
            .reset_index(drop=True))


def median_interval_seconds(frame: pd.DataFrame) -> float | None:
    """Median spacing between successive observations (None if <2 rows)."""
    if len(frame) < 2:
        return None
    diffs = frame["collected_at"].diff().dropna().dt.total_seconds()
    diffs = diffs[diffs > 0]
    if diffs.empty:
        return None
    return float(diffs.median())


def resample_frame(
    frame: pd.DataFrame,
    rule: str = "5min",
    agg: str = "mean",
) -> pd.DataFrame:
    """Aggregate to fixed bins; empty bins stay empty (never filled).

    Output timestamps are bin labels (left edges). Rows produced here carry
    ``sample_id=-1`` and are labelled through the returned frame only -
    nothing is written to the database.
    """
    if frame.empty:
        return frame.copy()
    valid = frame.dropna(subset=["value"])
    if valid.empty:
        return frame.iloc[0:0].copy()
    ts = valid.set_index("collected_at")
    func = {"mean": "mean", "median": "median", "max": "max",
            "min": "min", "last": "last", "sum": "sum"}[agg]
    binned = ts["value"].resample(rule).apply(func)
    out = pd.DataFrame({
        "sample_id": -1,
        "collected_at": binned.index,
        "value": binned.to_numpy(),
        "unit": valid["unit"].iloc[-1],
        "data_source": valid["data_source"].iloc[-1],
        "imputed": False,  # aggregation of real values, not imputation
    })
    return out[FRAME_COLUMNS].dropna(subset=["value"]).reset_index(drop=True)


def interpolate_short_gaps(
    frame: pd.DataFrame,
    max_gap_seconds: float,
) -> pd.DataFrame:
    """Optional, bounded, LABELLED imputation of missing values.

    Only rows whose surrounding gap is <= ``max_gap_seconds`` are filled
    (time-aware linear interpolation between the bracketing known values).
    Filled rows get ``imputed=True``; long gaps are left as NaN - we never
    silently draw data across an outage. Rows with a *measured zero* are
    treated as known values (zero is not missing).
    """
    if frame.empty:
        return frame.copy()
    out = frame.sort_values(["collected_at", "sample_id"]).reset_index(
        drop=True).copy()
    known = out["value"].notna()
    if known.all() or known.sum() < 2:
        out.loc[~out["value"].notna(), "imputed"] = False
        return out
    # Seconds relative to the first observation: unit-safe across pandas
    # datetime resolutions (ns/us), unlike casting to int64.
    times = (out["collected_at"] - out["collected_at"].min()
             ).dt.total_seconds()
    filled = out["value"].copy()
    imputed = pd.Series(False, index=out.index)
    known_idx = out.index[known]
    for i in out.index[~known]:
        prev = known_idx[known_idx < i]
        nxt = known_idx[known_idx > i]
        if len(prev) == 0 or len(nxt) == 0:
            continue  # edge missing - stays missing
        p, n = prev[-1], nxt[0]
        gap = times[n] - times[p]
        if not (0 < gap <= max_gap_seconds):
            continue  # long gap: reported, never interpolated
        wp = (times[n] - times[i]) / gap
        wn = (times[i] - times[p]) / gap
        pv, nv = out.at[p, "value"], out.at[n, "value"]
        if any(math.isnan(v) for v in (pv, nv)):
            continue
        filled[i] = wp * pv + wn * nv
        imputed[i] = True
    out["value"] = filled
    out["imputed"] = out["imputed"] | imputed
    return out
