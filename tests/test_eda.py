"""Phase 2 Scope C tests: EDA core summaries (SYNTHETIC fixtures).

Verify the documented gating ("only where statistically meaningful")
and exact summary outputs. They are not evidence about real traffic.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from analytics import data_quality, eda

START = dt.datetime(2026, 1, 1, 0, 0, 0)


def make_frame(values, unit="bps", step_min=1, start=START, interfaces=None):
    rows = []
    for i, v in enumerate(values):
        rows.append({
            "sample_id": i + 1,
            "collected_at": start + dt.timedelta(minutes=step_min * i),
            "value": v, "unit": unit, "data_source": "live",
            "imputed": False,
            "interface": (interfaces[i] if interfaces else None),
        })
    return pd.DataFrame(rows)


# ------------------------------------------------------------ summary stats
def test_summary_stats_refuses_empty():
    res = eda.summary_stats(make_frame([]))
    assert res["available"] is False
    assert "non-null" in res["reason"]


def test_summary_stats_values_exact():
    f = make_frame([float(i) for i in range(11)])   # 0..10
    res = eda.summary_stats(f)
    assert res["available"] and res["n"] == 11
    assert res["mean"] == pytest.approx(5.0)
    assert res["median"] == pytest.approx(5.0)
    assert res["min"] == 0.0 and res["max"] == 10.0
    assert res["unit"] == "bps"
    # 5th percentile of 0..10 linear series sits near the low end
    assert res["p05"] == pytest.approx(0.5)


def test_summary_stats_ignores_nulls_never_treats_as_zero():
    f = make_frame([1.0, None, 3.0, None, 5.0])
    res = eda.summary_stats(f)
    assert res["n"] == 3 and res["min"] == 1.0   # nulls excluded, not 0
    assert res["mean"] == pytest.approx(3.0)


# --------------------------------------------------------------- histogram
def test_histogram_gate_and_counts():
    thin = make_frame([1.0] * 4)                 # < min_points_analysis (5)
    assert eda.histogram(thin)["available"] is False
    f = make_frame([float(i % 5) for i in range(20)])
    res = eda.histogram(f)
    assert res["available"]
    assert sum(res["counts"]) == 20
    assert len(res["edges"]) == len(res["counts"]) + 1


# ---------------------------------------------------------- group comparison
def test_group_comparison_refuses_incompatible_units():
    f = make_frame([float(i) for i in range(12)], unit="bps")
    f.loc[f.index[len(f) // 2:], "unit"] = "count/s"
    res = eda.group_comparison(f, by="interface")
    assert res["available"] is False
    assert "units" in res["reason"]


def test_group_comparison_requires_min_points_per_group():
    # interface A: 8 values (>=5 ok), B: 3 values -> B refused, A reported
    vals = [1.0] * 8 + [2.0] * 3
    ifaces = ["A"] * 8 + ["B"] * 3
    f = make_frame(vals, interfaces=ifaces)
    res = eda.group_comparison(f, by="interface")
    assert res["available"]
    groups = {r["group"] for r in res["rows"]}
    assert groups == {"A"}
    assert any(r.startswith("B") for r in res["insufficient_groups"])


def test_group_comparison_ok_when_compatible():
    vals = [float(i) for i in range(12)]
    ifaces = ["A"] * 6 + ["B"] * 6
    res = eda.group_comparison(make_frame(vals, interfaces=ifaces),
                               by="interface")
    assert res["available"] and len(res["rows"]) == 2


# ---------------------------------------------------------- time-of-day gate
def test_time_of_day_refuses_short_span():
    f = make_frame([float(i) for i in range(5)], step_min=5)  # <1h span
    res = eda.time_of_day_profile(f)
    assert res["available"] is False and "UTC hours" in res["reason"]


def test_time_of_day_profile_counts():
    # 12 hourly samples -> 12 distinct UTC hours
    f = make_frame([float(i) for i in range(12)], step_min=60)
    res = eda.time_of_day_profile(f)
    assert res["available"]
    prof = res["profile"]
    assert int(prof["count"].sum()) == 12


# ------------------------------------------------------------- correlation
def series(values, step_min=1):
    idx = pd.to_datetime([START + dt.timedelta(minutes=step_min * i)
                          for i in range(len(values))])
    return pd.Series([float(v) for v in values], index=idx)


def test_correlation_perfect_on_synthetic_linear_pair():
    a = series(range(15))
    b = 2 * a + 1
    res = eda.pairwise_correlation({"a": a, "b": b})
    assert res[0]["available"] and res[0]["r"] == pytest.approx(1.0)
    assert res[0]["n"] == 15


def test_correlation_refused_on_sparse_overlap():
    a = series(range(15))
    b = series(range(5), step_min=60)     # only timestamps 0..4h
    res = eda.pairwise_correlation({"a": a, "b": b})
    assert res[0]["available"] is False
    assert "time-aligned" in res[0]["reason"]


def test_correlation_refused_on_constant_series():
    a = series([1.0] * 15)
    b = series(range(15))
    res = eda.pairwise_correlation({"a": a, "b": b})
    assert res[0]["available"] is False and "constant" in res[0]["reason"]


# ---------------------------------------------------------------- gap summary
def test_gap_breaks_preserve_missing_periods_in_charts():
    # regular 1-min samples + a 30-min hole -> one NaN break row inserted
    vals = [1.0] * 5 + [2.0] * 5
    stamps = [START + dt.timedelta(minutes=i) for i in range(5)] + \
             [START + dt.timedelta(minutes=30 + i) for i in range(5)]
    f = make_frame(vals)
    f["collected_at"] = stamps
    out = eda.insert_gap_breaks(f, factor=2.0)
    assert len(out) == len(f) + 1               # exactly one break row
    assert out["value"].isna().sum() == 1       # break carries NO value
    brk_ts = out.loc[out["value"].isna(), "collected_at"].iloc[0]
    assert brk_ts == START + dt.timedelta(minutes=17)  # mid-hole


def test_gap_breaks_noop_on_regular_series():
    f = make_frame([float(i) for i in range(10)])
    assert len(eda.insert_gap_breaks(f)) == 10


# ---------------------------------------------------------------- gap summary
def test_gap_summary_reflects_quality_report():
    f = make_frame([1.0, 2.0, None, 4.0], step_min=1)
    q = data_quality.summarize_quality(
        f.drop(columns=["interface"]), "R1", "m",
        expected_interval_seconds=60.0, now=START + dt.timedelta(minutes=10))
    rows = {r["item"]: r["value"] for r in eda.gap_summary(q)}
    assert rows["Stored samples"] == 4
    assert rows["Non-null values"] == 3
    assert rows["Null (unavailable) values"] == 1
