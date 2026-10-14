"""Reusable EDA computations (Phase 2 Scope C).

Pure functions over a prepared metric frame (``analytics.time_series``
layout) plus the ``SeriesQuality`` report: the Data Analysis page charts
*these* database-derived results - nothing here is hard-coded or
demonstration data. Every output is gated: distributions, correlations
and multi-series comparisons are only produced when enough compatible
observations exist; otherwise the function returns an explicit
``{"available": False, "reason": ...}`` instead of a misleading chart.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from analytics import data_quality
from config import get_settings

#: Minimum aligned observations before a Pearson correlation is reported.
MIN_CORR_POINTS = 12


def summary_stats(frame: pd.DataFrame) -> dict:
    """Core descriptive statistics over non-null values only."""
    values = pd.to_numeric(frame["value"], errors="coerce").dropna() \
        if not frame.empty else pd.Series(dtype=float)
    if values.empty:
        return {"available": False,
                "reason": "no non-null values in the selected range"}
    q = {p: float(values.quantile(p)) for p in (0.05, 0.25, 0.5, 0.75, 0.95)}
    return {
        "available": True,
        "n": int(len(values)),
        "mean": float(values.mean()),
        "std": float(values.std(ddof=1)) if len(values) >= 2 else None,
        "min": float(values.min()),
        "max": float(values.max()),
        "median": q[0.5],
        "p05": q[0.05], "p25": q[0.25], "p75": q[0.75], "p95": q[0.95],
        "unit": (frame["unit"].dropna().iloc[-1]
                 if not frame.empty and frame["unit"].notna().any() else None),
    }


def histogram(frame: pd.DataFrame, bins: int = 20) -> dict:
    """Value distribution, only when the ANALYSIS readiness gate passes."""
    values = pd.to_numeric(frame["value"], errors="coerce").dropna() \
        if not frame.empty else pd.Series(dtype=float)
    min_pts = int(get_settings().min_points_analysis)
    if len(values) < min_pts:
        return {"available": False,
                "reason": f"distribution needs >= {min_pts} values, "
                          f"has {len(values)}"}
    counts, edges = np.histogram(values.to_numpy(), bins=min(bins, len(values)))
    return {"available": True,
            "counts": [int(c) for c in counts],
            "edges": [float(e) for e in edges],
            "n": int(len(values))}


def metric_definition_of_compatible(frame: pd.DataFrame) -> dict:
    """Units present in one frame - comparison needs a single unit."""
    units = sorted({str(u) for u in frame["unit"].dropna().unique()} \
                   if not frame.empty else [])
    return {"units": units, "compatible": len(units) <= 1}


def group_comparison(frame: pd.DataFrame, by: str) -> dict:
    """Per-group (device/interface) stats for ONE metric.

    Comparison is only produced when every group shares the same unit
    (compatible metric definitions) and each group has enough values.
    """
    if frame.empty:
        return {"available": False, "reason": "no data selected"}
    compat = metric_definition_of_compatible(frame)
    if not compat["compatible"]:
        return {"available": False,
                "reason": ("groups use different units "
                           f"({', '.join(compat['units'])}); metric "
                           "definitions are not comparable")}
    min_pts = int(get_settings().min_points_analysis)
    rows, refused = [], []
    for name, grp in frame.dropna(subset=["value"]).groupby(by, dropna=True):
        if len(grp) < min_pts:
            refused.append(f"{name} ({len(grp)} values)")
            continue
        rows.append({
            "group": name, "n": int(len(grp)),
            "mean": float(grp["value"].mean()),
            "median": float(grp["value"].median()),
            "p95": float(grp["value"].quantile(0.95)),
            "max": float(grp["value"].max()),
            "unit": compat["units"][0] if compat["units"] else None,
        })
    if not rows:
        return {"available": False,
                "reason": f"no group reaches {min_pts} values "
                          f"(refused: {', '.join(refused)})"}
    return {"available": True, "rows": rows,
            "insufficient_groups": refused}


def time_of_day_profile(frame: pd.DataFrame, min_hours: int = 6) -> dict:
    """Mean/median per UTC hour - only when the range spans enough hours."""
    valid = frame.dropna(subset=["value"]) if not frame.empty else frame
    if valid.empty:
        return {"available": False, "reason": "no non-null values"}
    ts = pd.DatetimeIndex(valid["collected_at"])
    hours = ts.hour
    n_hours = int(pd.Series(hours).nunique())
    if n_hours < min_hours:
        return {"available": False,
                "reason": (f"selection spans only {n_hours} distinct UTC "
                           f"hours; diurnal profile needs >= {min_hours}")}
    prof = valid.assign(_hour=hours).groupby("_hour")["value"]
    out = pd.DataFrame({
        "mean": prof.mean(), "median": prof.median(), "count": prof.count(),
    }).reset_index().rename(columns={"_hour": "utc_hour"})
    return {"available": True, "profile": out}


def pairwise_correlation(named_series: dict[str, pd.Series],
                         method: str = "pearson",
                         min_points: int = MIN_CORR_POINTS) -> list[dict]:
    """Correlate series only where time-aligned overlap is meaningful.

    Series are indexed by timestamp; alignment uses the intersection of
    exact timestamps (same collection cycle). Pairs with fewer than
    ``min_points`` co-located observations are refused, not padded.
    """
    results: list[dict] = []
    names = list(named_series)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            sa, sb = named_series[a], named_series[b]
            joined = pd.concat([sa, sb], axis=1, join="inner").dropna()
            if len(joined) < min_points:
                results.append({
                    "pair": (a, b), "available": False,
                    "reason": (f"only {len(joined)} time-aligned points; "
                               f"correlation needs >= {min_points}")})
                continue
            x = joined.iloc[:, 0].to_numpy(dtype=float)
            y = joined.iloc[:, 1].to_numpy(dtype=float)
            if np.std(x) == 0 or np.std(y) == 0:
                results.append({
                    "pair": (a, b), "available": False, "n": int(len(joined)),
                    "reason": "one series is constant; correlation "
                              "undefined"})
                continue
            r = float(np.corrcoef(x, y)[0, 1]) \
                if method == "pearson" else _spearman(x, y)
            results.append({"pair": (a, b), "available": True,
                            "method": method, "r": r,
                            "n": int(len(joined))})
    return results


def _spearman(x: np.ndarray, y: np.ndarray) -> float:
    rx = pd.Series(x).rank().to_numpy()
    ry = pd.Series(y).rank().to_numpy()
    return float(np.corrcoef(rx, ry)[0, 1])


def gap_summary(quality: data_quality.SeriesQuality) -> list[dict]:
    """Missing-data / sampling-gap rows for the quality panel."""
    rows = [{
        "item": "Stored samples", "value": quality.n_rows,
    }, {
        "item": "Non-null values", "value": quality.n_non_null,
    }, {
        "item": "Null (unavailable) values", "value": quality.n_null_values,
    }, {
        "item": "Duplicate timestamps (deduplicated)",
        "value": quality.n_duplicates,
    }, {
        "item": "Observed median interval (s)",
        "value": _round(quality.observed_interval_seconds),
    }, {
        "item": f"Gaps > {data_quality.GAP_FACTOR}x interval",
        "value": quality.gap_count,
    }, {
        "item": "Longest gap (s)", "value": _round(quality.longest_gap_seconds),
    }, {
        "item": "Completeness vs expected schedule",
        "value": quality.completeness_pct,
    }, {
        "item": "Stale (no recent observation)",
        "value": "yes" if quality.is_stale else "no",
    }]
    return rows


def insert_gap_breaks(frame: pd.DataFrame, factor: float = 2.0) -> pd.DataFrame:
    """Return a copy with NaN 'break' rows inserted where the sampling gap
    exceeds ``factor`` x the median interval, so charts show a visible
    discontinuity instead of a misleading line drawn through missing time
    (spec Scope C). Break rows carry no value - nothing is imputed."""
    if frame.empty:
        return frame.copy()
    out = frame.sort_values("collected_at").reset_index(drop=True)
    diffs = out["collected_at"].diff()
    med = diffs.dropna().median()
    if pd.isna(med) or med <= pd.Timedelta(0):
        return out
    rows: list = []
    for i in range(len(out)):
        if i > 0 and diffs.iloc[i] > factor * med:
            brk = out.iloc[i].copy()
            brk["value"] = math.nan
            brk["collected_at"] = (
                out["collected_at"].iloc[i - 1] + diffs.iloc[i] / 2)
            rows.append(brk)
        rows.append(out.iloc[i])
    return pd.DataFrame(rows)


def _round(x, nd=1):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) \
        else round(float(x), nd)
