"""Stage A6-1 repository tests: scoped live-only retention delete (offline SQLite).

Proves the two mandatory predicates, the demo-safety, batch looping, the strict
cutoff boundary, per-batch durability, the SQL text, and that the unscoped
``purge_metric_samples`` is never used.
"""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from database import repository
from database.models import MetricSample, db_utcnow


def _seed(session, *, device, metric, value, source, age_days):
    ts = db_utcnow() - dt.timedelta(days=age_days)
    return repository.add_metric_sample(
        session, device_name=device, metric=metric, value=value, unit="ms",
        interface_name=None, data_source=source, collected_at=ts)


def test_scoped_to_live_and_age(session):
    _seed(session, device="r1", metric="m", value=1.0, source="live", age_days=40)
    _seed(session, device="r1", metric="m", value=2.0, source="live", age_days=1)
    _seed(session, device="r1", metric="m", value=3.0, source="demo", age_days=40)
    session.commit()

    cutoff = db_utcnow() - dt.timedelta(days=30)
    deleted = repository.delete_expired_live_samples(
        session, cutoff_utc=cutoff, batch_size=5000)

    assert deleted == 1
    remaining = {(r.data_source, r.value) for r in
                 session.scalars(select(MetricSample)).all()}
    # only the old LIVE row is gone; fresh live and old demo both survive
    assert remaining == {("live", 2.0), ("demo", 3.0)}


def test_demo_only_table_deletes_nothing(session):
    _seed(session, device="d", metric="m", value=1.0, source="demo", age_days=999)
    _seed(session, device="d", metric="m", value=2.0, source="demo", age_days=1000)
    session.commit()

    deleted = repository.delete_expired_live_samples(
        session, cutoff_utc=db_utcnow(), batch_size=1000)
    assert deleted == 0
    assert len(session.scalars(select(MetricSample)).all()) == 2


def test_batching_loops_until_exhausted(session):
    for i in range(7):
        _seed(session, device=f"d{i}", metric="m", value=float(i),
              source="live", age_days=45)
    _seed(session, device="keep", metric="m", value=99.0, source="live", age_days=2)
    session.commit()

    cutoff = db_utcnow() - dt.timedelta(days=30)
    # batch_size 3 forces multiple independent transactions (3 + 3 + 1 + 0)
    deleted = repository.delete_expired_live_samples(
        session, cutoff_utc=cutoff, batch_size=3)

    assert deleted == 7
    remaining = session.scalars(select(MetricSample)).all()
    assert [r.value for r in remaining] == [99.0]


def test_boundary_row_at_exact_cutoff_is_kept(session):
    ts = db_utcnow() - dt.timedelta(days=30)
    repository.add_metric_sample(
        session, device_name="b", metric="m", value=5.0,
        data_source="live", collected_at=ts)
    session.commit()

    deleted = repository.delete_expired_live_samples(
        session, cutoff_utc=ts, batch_size=100)
    assert deleted == 0            # strict `<` keeps a row exactly at cutoff
    assert len(session.scalars(select(MetricSample)).all()) == 1


def test_rejects_non_positive_batch_size(session):
    with pytest.raises(ValueError):
        repository.delete_expired_live_samples(
            session, cutoff_utc=db_utcnow(), batch_size=0)


def test_never_calls_purge_metric_samples(session, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("retention must not use purge_metric_samples")
    monkeypatch.setattr(repository, "purge_metric_samples", _boom)

    _seed(session, device="x", metric="m", value=1.0, source="live", age_days=40)
    session.commit()
    deleted = repository.delete_expired_live_samples(
        session, cutoff_utc=db_utcnow() - dt.timedelta(days=30), batch_size=100)
    assert deleted == 1


def test_generated_delete_sql_carries_both_predicates(session):
    stmts = []
    real = session.execute

    def spy(stmt, *a, **k):
        stmts.append(stmt)
        return real(stmt, *a, **k)

    session.execute = spy
    try:
        repository.delete_expired_live_samples(
            session, cutoff_utc=db_utcnow(), batch_size=100)
    finally:
        session.execute = real

    assert stmts, "expected at least one DELETE to have executed"
    # Render literal binds so the two mandatory predicates are visible as text
    # (they are bound parameters in the normal execution path).
    dialect = session.get_bind().dialect
    sql = str(stmts[0].compile(
        dialect=dialect, compile_kwargs={"literal_binds": True})).lower()
    assert "delete from metric_samples" in sql
    assert "data_source = 'live'" in sql
    assert "collected_at <" in sql
