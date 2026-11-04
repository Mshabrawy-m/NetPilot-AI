"""Stage A6-3 - daily rollups integrated into the forecasting pipeline.

Covers the combination contract that SQLite exercises faithfully *and* the
safety properties the spec demands: no raw/rollup overlap on a UTC day boundary,
strict Demo/Live isolation, no fabricated values, an actionable result when a
window mixes sampling granularities, and byte-identical raw-only behaviour so
existing forecast/detection paths do not regress.
"""
from __future__ import annotations

import datetime as dt
import math

import pytest

from analytics import time_series
from database import repository
from database.models import MetricDailyRollup
from services import anomaly_service, forecast_service

BASE = dt.datetime(2026, 1, 1)
STEP_S = 1200                                   # 20-minute raw grid
MODELS = ("naive", "seasonal_naive", "holt_damped", "holt_winters")


def _add_raw(session, *, device, metric, value, source="live", iface=None,
             at=BASE, unit="ms"):
    repository.add_metric_sample(
        session, device_name=device, metric=metric, value=value, unit=unit,
        interface_name=iface, data_source=source, collected_at=at)


def _add_rollup(session, *, day, device, metric, value, source="live",
                iface=None, unit="ms", sample_count=5):
    session.add(MetricDailyRollup(
        bucket_date=day, device_name=device, interface_name=iface,
        interface_key=iface or "", metric=metric, data_source=source,
        sample_count=sample_count, avg_value=value,
        min_value=(None if value is None else float(value) - 1),
        max_value=(None if value is None else float(value) + 1),
        unit=unit,
        first_seen=dt.datetime.combine(day, dt.time(0, 5)),
        last_seen=dt.datetime.combine(day, dt.time(23, 55))))


def seed_raw(session, values, *, device="R1", metric="rtt", source="live",
             iface=None, step_s=STEP_S, start=BASE):
    for i, v in enumerate(values):
        _add_raw(session, device=device, metric=metric, value=v,
                 source=source, iface=iface,
                 at=start + dt.timedelta(seconds=i * step_s))
    session.commit()


def seed_rollups(session, values, *, device="R1", metric="rtt", source="live",
                 iface=None, start_day=dt.date(2026, 1, 1)):
    for i, v in enumerate(values):
        _add_rollup(session, day=start_day + dt.timedelta(days=i),
                    device=device, metric=metric, value=v, source=source,
                    iface=iface)
    session.commit()


# ---------------------------------------------------------------- raw only
def test_raw_only_history_matches_load_metric_frame(session):
    seed_raw(session, [50.0 + i for i in range(20)])
    plain = time_series.load_metric_frame(
        session, "R1", "rtt", data_source="live")
    combined = time_series.load_metric_frame_with_rollups(
        session, "R1", "rtt", data_source="live")
    # No rollups exist -> identical rows, chronological order preserved.
    assert list(combined["collected_at"]) == list(plain["collected_at"])
    assert list(combined["value"]) == list(plain["value"])
    prov = time_series.frame_provenance(combined)
    assert prov["rollup"] == 0 and prov["raw"] == 20
    assert prov["mixes_granularity"] is False


def test_raw_only_forecast_regression_and_provenance(session):
    seasonal = [50.0 + 2.0 * (i // 3) + [0.0, 8.0, -4.0][i % 3]
                for i in range(36)]
    seed_raw(session, seasonal)
    result = forecast_service.run_forecast_comparison(
        session, device_name="R1", metric="rtt", interface_name=None,
        data_source="live", horizon=6, models=("naive", "holt_damped"),
        persist=True)
    assert result["status"] == "ok"
    assert result["history_provenance"]["rollup"] == 0
    assert result["history_provenance"]["raw"] == 36


# ------------------------------------------------------------- rollup only
def test_rollup_only_history_is_built_from_rollups(session):
    seed_rollups(session, [40.0 + i for i in range(40)])
    combined = time_series.load_metric_frame_with_rollups(
        session, "R1", "rtt", data_source="live")
    assert len(combined) == 40
    assert all(p == time_series.ORIGIN_ROLLUP
               for p in combined["provenance"])
    # Each rollup lands at its UTC-day midnight, value = daily mean.
    assert combined["collected_at"].iloc[0] == dt.datetime(2026, 1, 1)
    assert combined["value"].iloc[0] == pytest.approx(40.0)


def test_rollup_only_history_forecasts_on_daily_grid(session):
    seed_rollups(session, [50.0 + (i % 5) for i in range(40)])
    result = forecast_service.run_forecast_comparison(
        session, device_name="R1", metric="rtt", interface_name=None,
        data_source="live", horizon=3, models=("naive", "holt_damped"),
        persist=False)
    assert result["status"] == "ok"
    assert result["history_provenance"]["rollup"] == 40
    assert result["history_provenance"]["raw"] == 0
    # Daily interval is what the model measured, not the collect interval.
    assert result["quality"].observed_interval_seconds == 86400.0


# --------------------------------------------------------- combination
def test_combination_orders_and_never_overlaps(session):
    # Rollups for 10 older days; raw starting 2026-02-01 (day 31).
    seed_rollups(session, [10.0 + i for i in range(10)],
                 start_day=dt.date(2026, 1, 1))
    seed_raw(session, [70.0 + i for i in range(12)],
             start=dt.datetime(2026, 2, 1, 9, 0, 0))
    combined = time_series.load_metric_frame_with_rollups(
        session, "R1", "rtt", data_source="live")
    assert len(combined) == 22
    assert not combined["collected_at"].duplicated().any()
    # Strictly chronological, every rollup precedes every raw sample.
    ts = list(combined["collected_at"])
    assert ts == sorted(ts)
    last_rollup = combined[combined["provenance"] == "rollup"]["collected_at"].max()
    first_raw = combined[combined["provenance"] == "raw"]["collected_at"].min()
    assert last_rollup < first_raw
    prov = time_series.frame_provenance(combined)
    assert prov["rollup"] == 10 and prov["raw"] == 12


def test_boundary_excludes_the_day_that_still_has_raw(session):
    # A rollup exists for 2026-01-05, but raw samples also live on that day.
    seed_rollups(session, [10.0, 20.0, 30.0], start_day=dt.date(2026, 1, 3))
    seed_raw(session, [99.0], start=dt.datetime(2026, 1, 5, 12, 0, 0))
    combined = time_series.load_metric_frame_with_rollups(
        session, "R1", "rtt", data_source="live")
    rollup_days = list(
        combined[combined["provenance"] == "rollup"]["collected_at"])
    # The day that still has raw (Jan 5) must NOT be represented by its rollup.
    assert dt.datetime(2026, 1, 5) not in rollup_days
    assert dt.datetime(2026, 1, 3) in rollup_days
    assert dt.datetime(2026, 1, 4) in rollup_days
    assert combined["collected_at"].tolist().count(
        dt.datetime(2026, 1, 5, 12, 0, 0)) == 1


# --------------------------------------------------------- isolation
def test_demo_forecast_never_pulls_live_rollups(session):
    # A live rollup and a demo raw series share device/metric names.
    seed_rollups(session, [100.0, 200.0], source="live",
                 start_day=dt.date(2026, 1, 1))
    seed_raw(session, [1.0, 2.0, 3.0], source="demo",
             start=dt.datetime(2026, 3, 1))
    demo = time_series.load_metric_frame_with_rollups(
        session, "R1", "rtt", data_source="demo")
    assert all(p == time_series.ORIGIN_RAW for p in demo["provenance"])
    assert len(demo) == 3
    # The live rollup values (100/200) are unreachable from a demo read.
    assert demo["value"].max() == pytest.approx(3.0)


def test_live_rollups_are_visible_only_to_live_reads(session):
    seed_rollups(session, [10.0, 20.0], source="live",
                 start_day=dt.date(2026, 1, 1))
    live = time_series.load_metric_frame_with_rollups(
        session, "R1", "rtt", data_source="live")
    assert len(live) == 2
    assert all(p == time_series.ORIGIN_ROLLUP for p in live["provenance"])


# --------------------------------------------------------- missing values
def test_rollup_with_null_mean_is_missing_not_zero(session):
    seed_rollups(session, [None], start_day=dt.date(2026, 1, 1))
    combined = time_series.load_metric_frame_with_rollups(
        session, "R1", "rtt", data_source="live")
    assert len(combined) == 1
    assert math.isnan(float(combined["value"].iloc[0]))


def test_insufficient_history_returns_actionable_status(session):
    seed_rollups(session, [10.0, 20.0], start_day=dt.date(2026, 1, 1))
    result = forecast_service.run_forecast_comparison(
        session, device_name="R1", metric="rtt", interface_name=None,
        data_source="live", horizon=3, models=("naive",), persist=False)
    assert result["status"] == "insufficient_data"
    assert any("insufficient data" in r or "usable" in r
               for r in result["reasons"])


# ------------------------------------------------- mixed granularity honesty
def test_mixed_granularity_is_actionable_not_misleading(session):
    # Older daily rollups plus a fine recent raw grid -> mixed sampling levels.
    seed_rollups(session, [10.0 + i for i in range(15)],
                 start_day=dt.date(2026, 1, 1))
    seed_raw(session, [50.0 + i for i in range(40)],
             start=dt.datetime(2026, 2, 1))
    combined = time_series.load_metric_frame_with_rollups(
        session, "R1", "rtt", data_source="live")
    assert time_series.frame_provenance(combined)["mixes_granularity"] is True
    result = forecast_service.run_forecast_comparison(
        session, device_name="R1", metric="rtt", interface_name=None,
        data_source="live", horizon=3, models=("naive",), persist=False)
    assert result["status"] == "insufficient_data"
    assert any("granularit" in r for r in result["reasons"])


# ------------------------------------------------- path isolation
def test_include_rollups_flag_keeps_detection_raw_only(session):
    seed_rollups(session, [10.0, 20.0], start_day=dt.date(2026, 1, 1))
    seed_raw(session, [90.0], start=dt.datetime(2026, 2, 1))
    # Default (detection/calibration): raw loader, no provenance column.
    raw_frame, _ = anomaly_service.load_ready_series(
        session, "R1", "rtt", None, "live")
    assert "provenance" not in raw_frame.columns
    # Forecast opt-in: combined loader carries provenance and the rollup days.
    comb_frame, _ = anomaly_service.load_ready_series(
        session, "R1", "rtt", None, "live", include_rollups=True)
    assert "provenance" in comb_frame.columns
    assert int((comb_frame["provenance"] == "rollup").sum()) == 2


def test_run_forecast_can_opt_out_of_rollups(session):
    seed_rollups(session, [10.0, 20.0], start_day=dt.date(2026, 1, 1))
    seed_raw(session, [50.0 + i for i in range(40)],
             start=dt.datetime(2026, 2, 1))
    result = forecast_service.run_forecast_comparison(
        session, device_name="R1", metric="rtt", interface_name=None,
        data_source="live", horizon=3, models=("naive", "holt_damped"),
        include_rollups=False, persist=False)
    assert result["history_provenance"]["rollup"] == 0
    assert result["status"] == "ok"
