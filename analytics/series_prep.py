"""Regular-grid preparation for forecast backtests (backlog item A).

Raw collector samples arrive roughly every ``collect_interval_seconds`` but
with jitter, duplicate timestamps (overlapping collector runs) and gaps.  A
forecast model that assumes a regular grid cannot be evaluated honestly on that
raw stream, so every series is normalised here BEFORE any model sees it:

1. rows are ordered by timestamp and duplicate timestamps are collapsed
   deterministically (the last written row wins, matching
   :func:`analytics.time_series.dedupe_frame`);
2. values are aggregated onto a fixed grid (default 30 s, configurable);
3. a gap of at most ``max_gap_steps`` missing steps between two observed
   values is linearly interpolated and labelled ``imputed=True``;
4. a longer gap stays ``NaN`` forever - the grid marks it as missing and no
   model, plot or metric is allowed to draw a line across it.

Nothing here touches the database or mutates the caller's frame.  The returned
``series_hash`` is a stable digest of the deduplicated observations plus the
grid parameters, so the UI can cache a backtest keyed by the exact series it
was computed from.
"""
from __future__ import annotations

import hashlib
import itertools
import math

import numpy as np
import pandas as pd

DEFAULT_INTERVAL_SECONDS = 30.0
DEFAULT_MAX_GAP_STEPS = 3
#: Documented minimum usable points for any forecast/backtest result.
MIN_POINTS = 30


def _as_naive_utc(values: pd.Series) -> pd.Series:
    """Return a tz-naive UTC datetime series (the DB stores naive UTC)."""
    values = pd.to_datetime(values, errors="coerce")
    if getattr(values.dt, "tz", None) is not None:
        values = values.dt.tz_convert("UTC").dt.tz_localize(None)
    return values


def _series_hash(observations: pd.DataFrame, interval_seconds: float,
                 max_gap_steps: int) -> str:
    digest = hashlib.sha1()
    digest.update(
        f"v1|{interval_seconds:.6f}|{int(max_gap_steps)}|"
        f"{len(observations)}|".encode())
    for ts, value in zip(observations["collected_at"],
                         observations["value"]):
        stamp = "" if pd.isna(ts) else str(int(pd.Timestamp(ts).value))
        number = "nan" if pd.isna(value) else format(float(value), ".12g")
        digest.update(f"{stamp}:{number};".encode())
    return digest.hexdigest()


def insufficient_data_message(n_points: int, floor: int = MIN_POINTS) -> str:
    """The one message the UI and service both show below the data floor."""
    return (f"Not enough data: this series has {int(n_points)} usable "
            f"point(s); at least {int(floor)} are required for a "
            "rolling-origin backtest.")


def prepare_regular_series(
    frame: pd.DataFrame,
    *,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    max_gap_steps: int = DEFAULT_MAX_GAP_STEPS,
    time_col: str = "collected_at",
    value_col: str = "value",
) -> dict:
    """Resample one series onto a fixed grid, filling only short gaps.

    Returns a dict with the prepared ``frame`` (columns ``collected_at``,
    ``value``, ``imputed``, ``gap_steps``), the pandas ``series`` indexed by the
    grid, provenance counts, the stable ``series_hash`` and the effective
    parameters.  An empty/valueless input yields an empty prepared frame rather
    than an exception; the caller reports it as insufficient data.
    """
    interval = float(interval_seconds)
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("interval_seconds must be a positive number")
    if int(max_gap_steps) < 0:
        raise ValueError("max_gap_steps must be >= 0")
    max_gap_steps = int(max_gap_steps)

    empty = {
        "frame": pd.DataFrame({
            "collected_at": pd.Series(dtype="datetime64[ns]"),
            "value": pd.Series(dtype="float64"),
            "imputed": pd.Series(dtype="bool"),
            "gap_steps": pd.Series(dtype="int64"),
        }),
        "series": pd.Series(dtype="float64"),
        "interval_seconds": interval,
        "max_gap_steps": max_gap_steps,
        "n_raw_rows": 0, "n_duplicates": 0, "n_points": 0,
        "n_observed": 0, "n_imputed": 0, "n_missing": 0,
        "n_long_gaps": 0, "unit": None, "data_source": None,
        "series_hash": _series_hash(pd.DataFrame(
            {"collected_at": [], "value": []}), interval, max_gap_steps),
    }
    if frame is None or frame.empty or time_col not in frame:
        return empty

    work = pd.DataFrame({
        "collected_at": _as_naive_utc(frame[time_col]),
        "value": pd.to_numeric(frame[value_col], errors="coerce"),
    })
    work = work.dropna(subset=["collected_at"])
    if work.empty:
        return empty
    work = work.sort_values("collected_at", kind="mergesort")
    before = len(work)
    work = work.drop_duplicates("collected_at", keep="last")
    n_duplicates = before - len(work)

    first = pd.Timestamp(work["collected_at"].iloc[0])
    last = pd.Timestamp(work["collected_at"].iloc[-1])
    interval_ns = round(interval * 1_000_000_000)
    start = pd.Timestamp((int(first.value) // interval_ns) * interval_ns)
    end = pd.Timestamp((int(last.value) // interval_ns) * interval_ns)
    n_points = round((end - start).total_seconds() / interval) + 1
    grid = pd.date_range(start, periods=n_points,
                         freq=pd.Timedelta(seconds=interval))

    seconds = (work["collected_at"] - start).dt.total_seconds().to_numpy()
    bin_index = np.floor(seconds / interval + 1e-9).astype(int)
    bin_index = np.clip(bin_index, 0, n_points - 1)
    grouped = pd.Series(work["value"].to_numpy(dtype=float),
                        index=bin_index).groupby(level=0).mean()
    values = np.full(n_points, np.nan, dtype=float)
    values[grouped.index.to_numpy(dtype=int)] = grouped.to_numpy(dtype=float)

    imputed = np.zeros(n_points, dtype=bool)
    gap_steps = np.zeros(n_points, dtype=np.int64)
    finite = np.flatnonzero(np.isfinite(values))
    n_long_gaps = 0
    if finite.size >= 2:
        for left_i, right_i in itertools.pairwise(finite):
            missing = int(right_i - left_i - 1)
            if missing <= 0:
                continue
            if missing <= max_gap_steps:
                values[left_i + 1:right_i] = np.linspace(
                    values[left_i], values[right_i], missing + 2)[1:-1]
                imputed[left_i + 1:right_i] = True
            else:
                n_long_gaps += 1
                gap_steps[left_i + 1:right_i] = missing

    prepared = pd.DataFrame({
        "collected_at": grid,
        "value": values,
        "imputed": imputed,
        "gap_steps": gap_steps,
    })
    unit = None
    source = None
    if "unit" in frame and frame["unit"].notna().any():
        unit = frame["unit"].dropna().iloc[-1]
    if "data_source" in frame and frame["data_source"].notna().any():
        source = frame["data_source"].dropna().iloc[-1]

    return {
        "frame": prepared,
        "series": pd.Series(values, index=grid, name="value"),
        "interval_seconds": interval,
        "max_gap_steps": max_gap_steps,
        "n_raw_rows": before,
        "n_duplicates": n_duplicates,
        "n_points": n_points,
        "n_observed": int(np.isfinite(values).sum()),
        "n_imputed": int(imputed.sum()),
        "n_missing": int((~np.isfinite(values)).sum()),
        "n_long_gaps": n_long_gaps,
        "unit": unit,
        "data_source": source,
        "series_hash": _series_hash(work, interval, max_gap_steps),
    }


def finite_segments(values) -> list[tuple[int, int]]:
    """Half-open ``[start, end)`` runs of finite values in a 1-D sequence.

    Models are fitted inside one run only; a long NaN gap ends a run and is
    never bridged, which is the executable form of the "never fabricate across
    a gap" rule.
    """
    array = np.asarray(values, dtype=float)
    finite = np.isfinite(array)
    segments: list[tuple[int, int]] = []
    start = None
    for i, ok in enumerate(finite):
        if ok and start is None:
            start = i
        elif not ok and start is not None:
            segments.append((start, i))
            start = None
    if start is not None:
        segments.append((start, len(array)))
    return segments
