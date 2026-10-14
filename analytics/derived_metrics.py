"""Derived network metrics from cumulative counters (Phase 2 Scope B).

Computes per-interval rates/deltas from consecutive *valid* counter samples.
The rules follow the Phase 2 spec §5 exactly:

* ``rate_bps = (current_counter - previous_counter) * 8 / elapsed_seconds``
* negative deltas are **counter reset/wrap candidates** - the interval's
  rate is omitted (the source cannot distinguish a reset from a wrap on
  32/64-bit counters without extra evidence, so results are labelled
  ``counter_reset_or_wrap`` rather than guessed);
* zero or negative elapsed time rejects the interval;
* intervals longer than ``max_gap_seconds`` are marked unreliable;
* utilization percentage is only produced when a real interface capacity
  (bits/s) is supplied by the caller - it is never assumed;
* raw stored samples are never modified, and derived values are computed
  on demand (not persisted), so they can always be re-derived from and
  traced back to their source sample ids/timestamps.
"""
from __future__ import annotations

import dataclasses
import math

import pandas as pd

from config import get_settings

#: Documented interval outcomes. Only 'ok' values may be charted/analysed.
RATE_OK = "ok"
RATE_RESET = "counter_reset_or_wrap"
RATE_BAD_ELAPSED = "non_positive_elapsed"
RATE_GAP = "gap_too_large_unreliable"
RATE_INSUFFICIENT = "insufficient_samples"

#: Counter metrics understood by the derived-metric engine (Phase 1 names).
COUNTER_METRICS = {
    "if.rx_bytes": ("if.rx_bps", "if.rx_delta_bytes"),
    "if.tx_bytes": ("if.tx_bps", "if.tx_delta_bytes"),
    "if.rx_errors": ("if.rx_errors_per_sec", "if.rx_errors_delta"),
    "if.tx_errors": ("if.tx_errors_per_sec", "if.tx_errors_delta"),
    "if.rx_dropped": ("if.rx_dropped_per_sec", "if.rx_dropped_delta"),
    "if.tx_dropped": ("if.tx_dropped_per_sec", "if.tx_dropped_delta"),
}


@dataclasses.dataclass
class RateInterval:
    """One derived interval, traceable to its two source samples."""

    start_ts: pd.Timestamp | None
    end_ts: pd.Timestamp | None
    start_sample_id: int | None
    end_sample_id: int | None
    value: float | None          # rate or delta (None when rejected)
    unit: str
    status: str                  # one of the RATE_* constants
    detail: str | None = None


def counter_frame_to_rates(
    frame: pd.DataFrame,
    max_gap_seconds: float | None = None,
    bits_per_unit: float = 8.0,
    rate_unit: str = "bps",
) -> pd.DataFrame:
    """Turn a counter-value frame (time_series layout) into a rate frame.

    Columns of the returned frame: ``collected_at`` (end of interval),
    ``value`` (rate, NaN when the interval was rejected), ``status``,
    ``unit``, ``prev_value``, ``delta``, ``elapsed_seconds``,
    ``start_sample_id``, ``end_sample_id``.

    Only consecutive *non-null* counter readings produce candidate
    intervals; a NULL counter simply breaks the chain (the next rate uses
    the next valid pair) - missing data is never substituted with zero.
    """
    if max_gap_seconds is None:
        max_gap_seconds = float(get_settings().max_rate_gap_seconds)
    cols = ["collected_at", "value", "status", "unit", "prev_value",
            "delta", "elapsed_seconds", "start_sample_id", "end_sample_id"]
    valid = frame.dropna(subset=["value"]).sort_values(
        ["collected_at", "sample_id"]).reset_index(drop=True)
    if len(valid) < 2:
        return pd.DataFrame(columns=cols)

    rows: list[dict] = []
    for i in range(1, len(valid)):
        prev, cur = valid.iloc[i - 1], valid.iloc[i]
        elapsed = (cur["collected_at"] - prev["collected_at"]).total_seconds()
        delta = float(cur["value"]) - float(prev["value"])
        base = {
            "collected_at": cur["collected_at"],
            "prev_value": float(prev["value"]),
            "delta": delta,
            "elapsed_seconds": elapsed,
            "start_sample_id": int(prev["sample_id"]),
            "end_sample_id": int(cur["sample_id"]),
            "unit": rate_unit,
        }
        if elapsed <= 0:
            rows.append({**base, "value": math.nan, "status": RATE_BAD_ELAPSED,
                         "detail": "elapsed time is zero or negative"})
            continue
        if delta < 0:
            rows.append({**base, "value": math.nan, "status": RATE_RESET,
                         "detail": ("counter decreased; reset or wrap "
                                    "candidate - interval omitted")})
            continue
        if elapsed > max_gap_seconds:
            rows.append({**base, "value": (delta * bits_per_unit) / elapsed,
                         "status": RATE_GAP,
                         "detail": (f"interval {elapsed:.0f}s exceeds "
                                    f"max_gap {max_gap_seconds:.0f}s; "
                                    "unreliable")})
            continue
        rows.append({**base, "value": (delta * bits_per_unit) / elapsed,
                     "status": RATE_OK, "detail": None})
    return pd.DataFrame(rows)[cols]


def utilization_pct(rate_bps: float | None,
                    capacity_bps: float | None) -> float | None:
    """Utilization only when a valid capacity is known (spec §5)."""
    if rate_bps is None or math.isnan(rate_bps):
        return None
    if capacity_bps is None or capacity_bps <= 0:
        return None  # never assume interface capacity
    return 100.0 * rate_bps / capacity_bps


def rolling_summaries(series: pd.Series, window: int = 10) -> pd.DataFrame:
    """Rolling mean/median/min/max/std over ``window`` valid samples.

    ``min_periods=window`` keeps the earliest rows as NaN instead of
    computing summaries over almost nothing.
    """
    s = pd.to_numeric(series, errors="coerce")
    out = pd.DataFrame({
        "mean": s.rolling(window, min_periods=window).mean(),
        "median": s.rolling(window, min_periods=window).median(),
        "min": s.rolling(window, min_periods=window).min(),
        "max": s.rolling(window, min_periods=window).max(),
        "std": s.rolling(window, min_periods=window).std(),
    })
    return out


def derive_for_counter_metric(
    frame: pd.DataFrame,
    metric: str,
    max_gap_seconds: float | None = None,
    capacity_bps: float | None = None,
) -> dict:
    """Convenience wrapper used by services/UI.

    Returns ``{"rates": DataFrame, "rate_metric": str, "delta_metric": str,
    "unit": str, "utilization": bool}`` for a known counter metric, or an
    ``{"unsupported": True}`` result - never a fabricated derivation.
    """
    if metric not in COUNTER_METRICS:
        return {"unsupported": True,
                "reason": (f"{metric} is not a cumulative counter; no rate "
                           "can be derived from it.")}
    rate_metric, delta_metric = COUNTER_METRICS[metric]
    is_bytes = metric.endswith("_bytes")
    rates = counter_frame_to_rates(
        frame,
        max_gap_seconds=max_gap_seconds,
        bits_per_unit=8.0 if is_bytes else 1.0,
        rate_unit="bps" if is_bytes else "count/s",
    )
    if capacity_bps is not None and is_bytes:
        ok = rates["status"] == RATE_OK
        rates.loc[ok, "utilization_pct"] = rates.loc[ok, "value"].apply(
            lambda v: utilization_pct(v, capacity_bps))
    return {
        "rates": rates,
        "rate_metric": rate_metric,
        "delta_metric": delta_metric,
        "unit": "bps" if is_bytes else "count/s",
        "utilization": capacity_bps is not None and is_bytes,
    }
