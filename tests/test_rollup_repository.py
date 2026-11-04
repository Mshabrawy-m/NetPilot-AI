"""Stage A6-2 rollup repository tests (offline SQLite + PG-dialect structural).

SQLite cannot fully exercise PostgreSQL's NULL-uniqueness semantics, so the
NULL-safe guarantee is asserted two ways: behaviorally on SQLite (the surrogate
``interface_key`` folds NULL to '') and *structurally* by compiling the ON
CONFLICT target against the PostgreSQL dialect. A real PostgreSQL integration
test remains pending (see the final report).
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest
from sqlalchemy import Select, func, select
from sqlalchemy.dialects import postgresql, registry

from database import repository
from database.models import MetricDailyRollup

DAY = dt.datetime(2026, 10, 5)   # a fully completed UTC day in the past


def _add(session, *, device, metric, value, source="live", iface=None,
         at=DAY, unit="ms"):
    repository.add_metric_sample(
        session, device_name=device, metric=metric, value=value, unit=unit,
        interface_name=iface, data_source=source, collected_at=at)


def _window(day=DAY):
    return day, day + dt.timedelta(days=1)


def test_aggregation_stats(session):
    for v in (1.0, 2.0, 3.0):
        _add(session, device="r1", metric="m", value=v,
             at=DAY + dt.timedelta(hours=1))
    session.commit()

    s, e = _window()
    written = repository.upsert_daily_rollups(session, day_start=s, day_end=e)
    assert written == 1

    row = session.scalar(select(MetricDailyRollup))
    assert row.sample_count == 3
    assert row.min_value == 1.0 and row.max_value == 3.0
    assert round(float(row.avg_value), 3) == 2.0
    assert row.data_source == "live"
    assert row.bucket_date == DAY.date()
    assert row.unit == "ms"
    assert row.first_seen == DAY + dt.timedelta(hours=1)


def test_null_values_not_fabricated(session):
    _add(session, device="r1", metric="m", value=None, at=DAY)
    _add(session, device="r1", metric="m", value=None, at=DAY + dt.timedelta(hours=2))
    session.commit()

    s, e = _window()
    assert repository.upsert_daily_rollups(session, day_start=s, day_end=e) == 1
    row = session.scalar(select(MetricDailyRollup))
    assert row.sample_count == 2                 # two measurement points counted
    assert row.avg_value is None and row.min_value is None and row.max_value is None


def test_live_only_demo_excluded(session):
    _add(session, device="r1", metric="m", value=1.0, source="live", at=DAY)
    _add(session, device="r1", metric="m", value=9.0, source="demo", at=DAY)
    session.commit()

    s, e = _window()
    repository.upsert_daily_rollups(session, day_start=s, day_end=e)
    rows = session.scalars(select(MetricDailyRollup)).all()
    assert len(rows) == 1
    assert rows[0].sample_count == 1 and rows[0].max_value == 1.0
    assert rows[0].data_source == "live"


def test_utc_day_boundaries(session):
    s, e = _window()
    _add(session, device="r", metric="m", value=1.0, at=s)                          # included
    _add(session, device="r", metric="m", value=2.0, at=e - dt.timedelta(microseconds=1))  # included
    _add(session, device="r", metric="m", value=3.0, at=e)                          # next day -> excluded
    session.commit()

    repository.upsert_daily_rollups(session, day_start=s, day_end=e)
    row = session.scalar(select(MetricDailyRollup))
    assert row.sample_count == 2 and row.max_value == 2.0


def test_null_interface_single_row_and_idempotent(session):
    _add(session, device="r", metric="m", value=1.0, iface=None, at=DAY)
    _add(session, device="r", metric="m", value=2.0, iface=None, at=DAY + dt.timedelta(hours=3))
    session.commit()

    s, e = _window()
    assert repository.upsert_daily_rollups(session, day_start=s, day_end=e) == 1
    rows = session.scalars(select(MetricDailyRollup)).all()
    assert len(rows) == 1
    assert rows[0].interface_name is None and rows[0].interface_key == ""

    # Rebuild the same completed day: replace, never duplicate or double-count.
    assert repository.upsert_daily_rollups(session, day_start=s, day_end=e) == 1
    rows = session.scalars(select(MetricDailyRollup)).all()
    assert len(rows) == 1
    assert rows[0].sample_count == 2


def test_distinct_interfaces_separate_rows(session):
    _add(session, device="r", metric="m", value=1.0, iface="eth0", at=DAY)
    _add(session, device="r", metric="m", value=2.0, iface="eth1", at=DAY)
    _add(session, device="r", metric="m", value=3.0, iface=None, at=DAY)
    session.commit()

    s, e = _window()
    assert repository.upsert_daily_rollups(session, day_start=s, day_end=e) == 3
    keys = sorted(r.interface_key for r in session.scalars(select(MetricDailyRollup)))
    assert keys == ["", "eth0", "eth1"]


def test_late_arriving_rebuild(session):
    s, e = _window()
    assert repository.upsert_daily_rollups(session, day_start=s, day_end=e) == 0

    _add(session, device="r", metric="m", value=5.0, at=DAY + dt.timedelta(hours=5))
    session.commit()
    assert repository.upsert_daily_rollups(session, day_start=s, day_end=e) == 1
    row = session.scalar(select(MetricDailyRollup))
    assert row.sample_count == 1 and row.max_value == 5.0

    _add(session, device="r", metric="m", value=7.0, at=DAY + dt.timedelta(hours=6))
    session.commit()
    repository.upsert_daily_rollups(session, day_start=s, day_end=e)
    row = session.scalar(select(MetricDailyRollup))
    assert row.sample_count == 2 and row.min_value == 5.0 and row.max_value == 7.0


def test_empty_window_creates_nothing(session):
    _add(session, device="r", metric="m", value=1.0, source="demo", at=DAY)
    session.commit()

    s, e = _window()
    assert repository.upsert_daily_rollups(session, day_start=s, day_end=e) == 0
    assert session.scalars(select(MetricDailyRollup)).all() == []


def test_failed_aggregation_rolls_back(session):
    _add(session, device="r1", metric="m", value=1.0, at=DAY)
    _add(session, device="r2", metric="m", value=2.0, at=DAY)
    session.commit()

    s, e = _window()
    session.commit = lambda: (_ for _ in ()).throw(RuntimeError("commit failed"))
    with pytest.raises(RuntimeError):
        repository.upsert_daily_rollups(session, day_start=s, day_end=e)

    assert session.scalar(
        select(func.count()).select_from(MetricDailyRollup)) == 0


def test_rejects_bad_window_arguments(session):
    with pytest.raises(ValueError):
        repository.upsert_daily_rollups(session, day_start=DAY, day_end=DAY)
    aware = dt.datetime(2026, 10, 5, tzinfo=dt.timezone.utc)
    with pytest.raises(ValueError):
        repository.upsert_daily_rollups(session, day_start=aware, day_end=DAY)


# ------------------------------------------------------- explicit DATE bucket
def test_bucket_expr_is_the_correct_date_form_per_dialect():
    """The bucket conversion is a dialect-appropriate DATE, asserted as SQL text.

    ``bucket_date`` may not be an artifact of the Python window argument: the day
    boundary must be truncated inside the SQL. The two engines need *different*
    spellings (SQLite's generic ``CAST(.. AS DATE)`` collapses to the integer
    year), so the repository picks per dialect. Asserting the rendered SQL is the
    only faithful way to prove the PostgreSQL form without a live PG server.
    """
    pg_sql = str(repository._rollup_bucket_expr("postgresql").compile(
        dialect=postgresql.dialect())).lower()
    assert "cast(metric_samples.collected_at as date)" in pg_sql

    sqlite_sql = str(repository._rollup_bucket_expr("sqlite").compile(
        dialect=registry.load("sqlite")())).lower()
    assert "date(metric_samples.collected_at)" in sqlite_sql


def test_bucket_is_projected_and_grouped(session):
    """The chosen bucket appears in BOTH the projection and the GROUP BY."""
    _add(session, device="r", metric="m", value=1.0, at=DAY + dt.timedelta(hours=7))
    session.commit()

    captured = []
    real_execute = session.execute

    def spy(stmt, *a, **k):
        captured.append(stmt)
        return real_execute(stmt, *a, **k)

    session.execute = spy
    s, e = _window()
    repository.upsert_daily_rollups(session, day_start=s, day_end=e)

    target = next(
        st for st in captured
        if isinstance(st, Select)
        and (st.column_descriptions or [{}])[0].get("name") == "bucket_date")
    # The offline engine is SQLite, so the executed bucket is date(collected_at);
    # it must be grouped on that same expression, never on the raw timestamp.
    sql = str(target.compile()).lower()
    group_by = sql.split("group by", 1)[1]
    assert "date(metric_samples.collected_at)" in sql
    assert "date(metric_samples.collected_at)" in group_by


def test_bucket_date_drives_the_written_row(session):
    """The value stored in ``bucket_date`` comes from the projected DATE."""
    s, e = _window()
    _add(session, device="r", metric="m", value=1.0, at=s + dt.timedelta(hours=23))
    _add(session, device="r", metric="m", value=2.0, at=e - dt.timedelta(minutes=1))
    session.commit()

    assert repository.upsert_daily_rollups(session, day_start=s, day_end=e) == 1
    row = session.scalar(select(MetricDailyRollup))
    assert row.bucket_date == DAY.date()
    assert row.first_seen == s + dt.timedelta(hours=23)
    assert row.last_seen == e - dt.timedelta(minutes=1)


# ------------------------------------------- NULL-safe uniqueness vs PG version
class _ProbeSession:
    """Minimal session stand-in exposing only ``get_bind().dialect``."""

    def __init__(self, *, name="postgresql", version=None):
        # Mirrors a real dialect: never None, empty when the server cannot be
        # inspected (the non-PostgreSQL case).
        self.dialect = SimpleNamespace(
            name=name,
            server_version_info=() if version is None else (version,))

    def get_bind(self):
        return self


def test_rollup_strategy_prefers_surrogate_on_any_pg_version():
    # PG 16 (the packaged reference server) *could* use NULLS NOT DISTINCT...
    info = repository.rollup_uniqueness_strategy_info(_ProbeSession(version=16))
    assert info["pg_supports_nulls_not_distinct"] is True
    # ...and PG 14 could not, yet the schema the runner relies on is identical:
    # the version-independent NOT NULL surrogate is always the chosen strategy.
    old = repository.rollup_uniqueness_strategy_info(_ProbeSession(version=14))
    assert old["pg_supports_nulls_not_distinct"] is False
    assert info["chosen"] == old["chosen"] == "surrogate_interface_key"
    assert info["version_independent"] is True
    # An uninspectable server is never assumed to be new.
    unknown = repository.rollup_uniqueness_strategy_info(_ProbeSession())
    assert unknown["server_version_info"] is None
    assert unknown["pg_supports_nulls_not_distinct"] is False


def test_rollup_strategy_on_the_real_test_engine(session):
    info = repository.rollup_uniqueness_strategy_info(session)
    assert info["dialect"] == "sqlite"
    assert info["chosen"] == "surrogate_interface_key"


# ------------------------------------------------ PostgreSQL-dialect structural test
def test_pg_on_conflict_target_is_nullsafe_surrogate_key():
    """The PG upsert conflict target must be ``interface_key`` (NOT NULL), not the
    nullable ``interface_name``. This is the version-independent NULL-safety
    guarantee SQLite alone cannot demonstrate."""
    rows = [SimpleNamespace(
        bucket_date=DAY.date(),
        device_name="d", interface_name=None, metric="m", sample_count=2,
        avg_value=1.5, min_value=1.0, max_value=2.0, unit="ms",
        first_seen=DAY, last_seen=DAY + dt.timedelta(hours=1))]

    class FakeSession:
        def __init__(self):
            self.executed = []

        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        def execute(self, stmt):
            self.executed.append(stmt)
            if isinstance(stmt, Select):
                return SimpleNamespace(all=lambda: rows)
            return None

        def commit(self):
            pass

        def rollback(self):
            pass

    fs = FakeSession()
    n = repository.upsert_daily_rollups(
        fs, day_start=DAY, day_end=DAY + dt.timedelta(days=1))
    assert n == 1

    inserts = [s for s in fs.executed if not isinstance(s, Select)]
    assert inserts
    sql = str(inserts[-1].compile(dialect=postgresql.dialect())).lower()
    assert "on conflict" in sql
    target = sql.split("on conflict", 1)[1].split("do update", 1)[0]
    assert "interface_key" in target
    assert "interface_name" not in target

    # The PostgreSQL aggregate must convert the bucket to DATE with the canonical
    # CAST (not the SQLite date() spelling): this proves the production-only DATE
    # form without a live server. A real PostgreSQL integration run still remains
    # pending (see the final report).
    aggregate = next(s for s in fs.executed if isinstance(s, Select))
    agg_sql = str(aggregate.compile(dialect=postgresql.dialect())).lower()
    assert "cast(metric_samples.collected_at as date)" in agg_sql
    assert "group by" in agg_sql and "as date)" in agg_sql.split("group by", 1)[1]
