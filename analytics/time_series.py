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
from database.models import MetricDailyRollup, MetricSample

#: Canonical column layout of every prepared frame produced here.
FRAME_COLUMNS = ["sample_id", "collected_at", "value", "unit",
                 "data_source", "imputed"]

#: Provenance labels added by :func:`load_metric_frame_with_rollups`. The word
#: ``provenance`` is deliberate: ``origin`` already means "forecast origin" in
#: :mod:`analytics.forecasting`, so reusing it here would be ambiguous.
ORIGIN_RAW = "raw"
ORIGIN_ROLLUP = "rollup"
PROVENANCE_COLUMN = "provenance"


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


def _rollup_history_frame(
    session: Session,
    device_name: str,
    metric: str,
    *,
    interface_name: str | None = None,
    device_level_only: bool = False,
    data_source: str | None = None,
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
    before_day: dt.date | None = None,
    max_points: int | None = None,
) -> pd.DataFrame:
    """Load older daily-rollup observations as a ``FRAME_COLUMNS`` frame.

    Each rollup becomes one observation stamped at its UTC-day midnight
    (``bucket_date`` 00:00, naive UTC like every other timestamp here) with
    ``value = avg_value``. A NULL ``avg_value`` (a day whose readings were all
    unavailable) stays ``NaN`` - a rollup never invents a measurement.

    ``before_day`` (a ``datetime.date``) is the strict upper bound: only rollups
    whose ``bucket_date`` is *earlier than* that whole UTC day are returned, so
    a day still covered by raw samples is never duplicated by its rollup. When
    ``before_day`` is ``None`` the raw series is empty and every rollup inside
    the window is offered (rollup-only history).

    The interface / ``data_source`` filters mirror :func:`load_metric_frame`
    exactly, so Demo and Live can never bleed across this boundary: a
    ``data_source='demo'`` request matches no rollup (A6-2 aggregates live only).
    """
    limit = max_points or get_settings().analytics_max_points
    stmt = (
        select(MetricDailyRollup)
        .where(MetricDailyRollup.device_name == device_name,
               MetricDailyRollup.metric == metric)
        .order_by(MetricDailyRollup.bucket_date.desc())
        .limit(limit)
    )
    if interface_name is not None:
        stmt = stmt.where(MetricDailyRollup.interface_name == interface_name)
    elif device_level_only:
        stmt = stmt.where(MetricDailyRollup.interface_name.is_(None))
    if data_source is not None:
        stmt = stmt.where(MetricDailyRollup.data_source == data_source)
    if before_day is not None:
        stmt = stmt.where(MetricDailyRollup.bucket_date < before_day)
    if since is not None:
        stmt = stmt.where(MetricDailyRollup.bucket_date >= since.date())
    if until is not None:
        stmt = stmt.where(MetricDailyRollup.bucket_date <= until.date())

    rows = session.scalars(stmt).all()[::-1]  # chronological again
    if not rows:
        return _empty_provenance_frame()
    frame = pd.DataFrame({
        "sample_id": [-1] * len(rows),  # synthetic aggregate, not a raw id
        "collected_at": [dt.datetime.combine(r.bucket_date, dt.time())
                         for r in rows],
        "value": [None if r.avg_value is None else float(r.avg_value)
                  for r in rows],
        "unit": [r.unit for r in rows],
        "data_source": [r.data_source for r in rows],
        "imputed": [False] * len(rows),  # real aggregation, never imputation
        PROVENANCE_COLUMN: [ORIGIN_ROLLUP] * len(rows),
    })
    frame["value"] = frame["value"].astype("float64")  # None -> NaN
    return frame


def _empty_provenance_frame() -> pd.DataFrame:
    """Typed empty frame carrying every ``FRAME_COLUMNS`` column plus provenance."""
    frame = pd.DataFrame(columns=FRAME_COLUMNS + [PROVENANCE_COLUMN]).astype(
        {"sample_id": "int64", "collected_at": "datetime64[ns]",
         "value": "float64", "unit": "object", "data_source": "object",
         "imputed": "bool", PROVENANCE_COLUMN: "object"})
    return frame


def load_metric_frame_with_rollups(
    session: Session,
    device_name: str,
    metric: str,
    interface_name: str | None = None,
    data_source: str | None = None,
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
    max_points: int | None = None,
    device_level_only: bool = False,
    rollup_max_points: int | None = None,
) -> pd.DataFrame:
    """Combine recent raw samples with older daily-rollup history (Stage A6-3).

    Extends :func:`load_metric_frame` for the forecasting pipeline so long-term
    analytics survive once raw retention deletes old ``metric_samples``. The two
    regions are separated on a **UTC day boundary** so they can never overlap:

    * recent history comes from raw ``metric_samples`` (unchanged query and
      semantics - when no rollup is older than the raw window this returns
      exactly what :func:`load_metric_frame` returns, so existing forecasts and
      their tests are byte-identical);
    * strictly older whole UTC days (``bucket_date`` before the day of the
      oldest raw sample) come from ``metric_daily_rollups`` for the SAME
      ``data_source``.

    Guarantees: chronological order; one observation per timestamp (no
    duplicate/overlapping raw-and-rollup days); a ``provenance`` column tagging
    every row ``'raw'`` or ``'rollup'``; Demo/Live isolation inherited from the
    shared ``data_source`` filter; no fabricated values (a rollup whose mean is
    NULL stays ``NaN``). Because a combined raw-then-daily series mixes sampling
    granularity, callers must not silently feed it to a regular-grid model - the
    data-quality gate reports that case explicitly.
    """
    frame = load_metric_frame(
        session, device_name, metric, interface_name=interface_name,
        data_source=data_source, since=since, until=until,
        max_points=max_points, device_level_only=device_level_only)
    frame[PROVENANCE_COLUMN] = ORIGIN_RAW

    # Boundary = the UTC day of the OLDEST raw sample kept. Rollups strictly
    # before that day fill only what raw no longer covers, so raw is always
    # authoritative for (and after) its own first day.
    before_day = frame["collected_at"].min().date() if not frame.empty else None

    rollups = _rollup_history_frame(
        session, device_name, metric, interface_name=interface_name,
        device_level_only=device_level_only, data_source=data_source,
        since=since, until=until, before_day=before_day,
        max_points=rollup_max_points)
    if rollups.empty:
        return frame.reset_index(drop=True)
    combined = pd.concat([rollups, frame], ignore_index=True)
    combined = combined.sort_values(
        ["collected_at", "sample_id"]).reset_index(drop=True)
    return combined


def frame_provenance(frame: pd.DataFrame) -> dict:
    """Summarise raw-vs-rollup composition of a prepared frame.

    Returns ``{"raw": n, "rollup": m, "mixes_granularity": bool}``. ``mixes``
    is True only when both kinds are present, which is the signal the
    forecasting layer uses to warn that the grid spans two sampling levels.
    """
    if frame.empty or PROVENANCE_COLUMN not in frame:
        return {ORIGIN_RAW: 0, ORIGIN_ROLLUP: 0,
                "mixes_granularity": False}
    counts = frame[PROVENANCE_COLUMN].value_counts().to_dict()
    raw = int(counts.get(ORIGIN_RAW, 0))
    rollup = int(counts.get(ORIGIN_ROLLUP, 0))
    return {ORIGIN_RAW: raw, ORIGIN_ROLLUP: rollup,
            "mixes_granularity": bool(raw and rollup)}


def _raw_to_daily(frame: pd.DataFrame) -> pd.DataFrame:
    """Aggregate a raw sample frame into one UTC-day mean (Stage A6-4).

    A day is emitted only when it has at least one raw ROW. Its value is the
    mean over NON-NULL readings; a day whose readings were all unavailable
    becomes ``NaN`` - never a fabricated 0. The day is stamped at UTC midnight
    to match the rollup observation grain, and carries ``provenance='raw'``.
    """
    if frame.empty:
        return _empty_provenance_frame()
    work = frame.copy()
    work["_day"] = work["collected_at"].dt.normalize()
    rows: list[dict] = []
    for day, grp in work.groupby("_day", sort=True):
        non_null = grp["value"].dropna()
        unit_vals = grp["unit"].dropna()
        src_vals = grp["data_source"].dropna()
        rows.append({
            "sample_id": -1,
            "collected_at": day.to_pydatetime(),
            "value": float(non_null.mean()) if len(non_null) else math.nan,
            "unit": unit_vals.iloc[-1] if len(unit_vals) else None,
            "data_source": src_vals.iloc[-1] if len(src_vals) else None,
            "imputed": False,
            PROVENANCE_COLUMN: ORIGIN_RAW,
        })
    out = pd.DataFrame(rows, columns=FRAME_COLUMNS + [PROVENANCE_COLUMN])
    out["value"] = out["value"].astype("float64")
    return out


def _merge_daily_prefer_rollup(rollups: pd.DataFrame,
                               raw_daily: pd.DataFrame) -> pd.DataFrame:
    """Union two UTC-day frames, keeping exactly one observation per day.

    Rollup days are authoritative (they preserve the stored A6-2 daily mean),
    so any raw-derived day that collides with a rollup day is dropped. Because
    every day appears once, the merged frame has no duplicate timestamps and is
    a regular daily grid regardless of how fine the raw sampling was.
    """
    parts: list[pd.DataFrame] = []
    if not rollups.empty:
        parts.append(rollups)
    if not raw_daily.empty:
        if rollups.empty:
            parts.append(raw_daily)
        else:
            rollup_days = set(rollups["collected_at"].tolist())
            raw_only = raw_daily[~raw_daily["collected_at"].isin(rollup_days)]
            if not raw_only.empty:
                parts.append(raw_only)
    if not parts:
        return _empty_provenance_frame()
    combined = pd.concat(parts, ignore_index=True)
    return combined.sort_values("collected_at").reset_index(drop=True)


def load_metric_frame_daily(
    session: Session,
    device_name: str,
    metric: str,
    interface_name: str | None = None,
    data_source: str | None = None,
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
    max_points: int | None = None,
    device_level_only: bool = False,
    rollup_max_points: int | None = None,
) -> pd.DataFrame:
    """Combine raw samples and daily rollups on ONE uniform UTC-day grid.

    This is the Stage A6-4 *daily cadence*: it answers "can raw and rollup
    history be combined safely?" by refusing to mix sampling levels. Every day
    in the window is represented by a single observation:

    * a day that has a ``metric_daily_rollup`` uses the stored daily mean
      (provenance ``'rollup'``) - the A6-2 aggregate is never recomputed here;
    * a day that has only raw ``metric_samples`` gets their non-null mean
      (provenance ``'raw'``);
    * a day with neither is omitted (nothing is fabricated); a day whose raw
      values were all unavailable stays ``NaN`` (missing, not 0).

    Rollup preferred on overlap means one row per UTC day and no duplicate
    timestamps, so the series is a regular daily grid that the data-quality
    gate can accept. ``data_source``/interface filters mirror the other loaders,
    so Demo never reads a Live rollup (A6-2 makes rollups live-only).
    """
    raw = load_metric_frame(
        session, device_name, metric, interface_name=interface_name,
        data_source=data_source, since=since, until=until,
        max_points=max_points, device_level_only=device_level_only)
    raw_daily = _raw_to_daily(raw)
    rollups = _rollup_history_frame(
        session, device_name, metric, interface_name=interface_name,
        device_level_only=device_level_only, data_source=data_source,
        since=since, until=until, before_day=None,
        max_points=rollup_max_points)
    return _merge_daily_prefer_rollup(rollups, raw_daily)


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
