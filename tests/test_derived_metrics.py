"""Phase 2 Scope B tests: derived rates from cumulative counters.

All fixtures are SYNTHETIC counter sequences that verify the documented
formula and rejection rules; they are not real traffic measurements.
"""
from __future__ import annotations

import datetime as dt
import math

import pandas as pd

from analytics import derived_metrics as dm

BASE = dt.datetime(2026, 1, 1, 12, 0, 0)


def counter_frame(values, step_s=60, start=BASE):
    n = len(values)
    return pd.DataFrame({
        "sample_id": list(range(1, n + 1)),
        "collected_at": pd.to_datetime(
            [start + dt.timedelta(seconds=i * step_s) for i in range(n)]),
        "value": [None if v is None else float(v) for v in values],
        "unit": "bytes", "data_source": "live", "imputed": False,
    })


def test_rate_formula_and_unit_conversion():
    # 1000 bytes over 60 s -> 1000*8/60 bps
    rates = dm.counter_frame_to_rates(counter_frame([0.0, 1000.0]),
                                      max_gap_seconds=300)
    row = rates.iloc[0]
    assert row["value"] == pytest_approx(1000 * 8 / 60)
    assert row["unit"] == "bps"
    assert row["status"] == dm.RATE_OK
    # traceable to its two source samples
    assert row["start_sample_id"] == 1 and row["end_sample_id"] == 2


def pytest_approx(x):
    import pytest
    return pytest.approx(x, rel=1e-9)


def test_counter_decrease_is_reset_candidate_not_traffic():
    rates = dm.counter_frame_to_rates(counter_frame([5000.0, 100.0]),
                                      max_gap_seconds=300)
    row = rates.iloc[0]
    assert math.isnan(row["value"])          # interval omitted
    assert row["status"] == dm.RATE_RESET    # labelled, never a negative rate


def test_zero_elapsed_rejected_and_negative_cannot_occur_after_sort():
    frame = counter_frame([100.0, 200.0, 300.0], step_s=60)
    # force the second interval to zero elapsed time (tied timestamps)
    frame.loc[2, "collected_at"] = frame.loc[1, "collected_at"]
    rates = dm.counter_frame_to_rates(frame, max_gap_seconds=300)
    assert rates.iloc[0]["status"] == dm.RATE_OK
    assert rates.iloc[1]["status"] == dm.RATE_BAD_ELAPSED
    assert math.isnan(rates.iloc[1]["value"])

    # Out-of-order input is sorted chronologically first (documented
    # guarantee), so a negative elapsed interval can never be produced -
    # the displaced sample surfaces as a counter-decrease interval instead.
    frame2 = counter_frame([100.0, 200.0, 300.0], step_s=60)
    frame2.loc[2, "collected_at"] = (frame2.loc[1, "collected_at"]
                                     - pd.Timedelta(seconds=5))
    rates2 = dm.counter_frame_to_rates(frame2, max_gap_seconds=300)
    assert (rates2["elapsed_seconds"] > 0).all()
    assert dm.RATE_RESET in set(rates2["status"])


def test_large_gap_marked_unreliable_but_not_ok():
    # elapsed 600s with max gap 300 -> computed but flagged unreliable
    rates = dm.counter_frame_to_rates(counter_frame([0.0, 6000.0], step_s=600),
                                      max_gap_seconds=300)
    row = rates.iloc[0]
    assert row["status"] == dm.RATE_GAP
    assert row["value"] == 6000 * 8 / 600  # 80 bps, flagged unreliable
    assert not math.isnan(row["value"])


def test_null_counter_breaks_chain_without_zero_substitution():
    frame = counter_frame([1000.0, None, 3000.0], step_s=60)
    rates = dm.counter_frame_to_rates(frame, max_gap_seconds=300)
    assert len(rates) == 1                       # only the valid pair
    assert rates.iloc[0]["start_sample_id"] == 1
    assert rates.iloc[0]["end_sample_id"] == 3
    # elapsed is the full 120s across the hole - honest, not per-slot guess
    assert rates.iloc[0]["elapsed_seconds"] == 120.0


def test_insufficient_samples_returns_empty():
    assert dm.counter_frame_to_rates(counter_frame([5.0])).empty
    assert dm.counter_frame_to_rates(counter_frame([])).empty


def test_utilization_requires_known_capacity():
    assert dm.utilization_pct(500.0, 1000.0) == 50.0
    assert dm.utilization_pct(500.0, None) is None    # never assumed
    assert dm.utilization_pct(500.0, 0.0) is None
    assert dm.utilization_pct(float("nan"), 1000.0) is None


def test_rolling_summaries_need_full_window():
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    out = dm.rolling_summaries(s, window=3)
    assert math.isnan(out.loc[0, "mean"])
    assert out.loc[2, "mean"] == 2.0
    assert out.loc[4, "median"] == 4.0
    assert out.loc[4, "min"] == 3.0 and out.loc[4, "max"] == 5.0


def test_derive_wrapper_counter_vs_non_counter():
    res = dm.derive_for_counter_metric(counter_frame([0.0, 8000.0]),
                                       "if.rx_bytes", max_gap_seconds=300)
    assert res["rate_metric"] == "if.rx_bps" and res["unit"] == "bps"
    assert res["rates"].iloc[0]["value"] == 8000 * 8 / 60
    assert res["utilization"] is False  # capacity unknown

    bad = dm.derive_for_counter_metric(counter_frame([1.0, 2.0]),
                                       "ping.avg_rtt_ms")
    assert bad["unsupported"] is True   # RTT is not a counter - refuse


def test_errors_derive_per_second_not_bps():
    res = dm.derive_for_counter_metric(counter_frame([10.0, 70.0]),
                                       "if.rx_errors", max_gap_seconds=300)
    assert res["unit"] == "count/s"
    assert res["rates"].iloc[0]["value"] == 60 / 60  # 1/s
