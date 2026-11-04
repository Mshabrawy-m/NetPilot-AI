"""Data-access layer. All SQL/Orrm access goes through this module.

The Streamlit UI and the services call these functions; nothing else writes
to the database directly (requirement: data access separated from the UI).
"""
from __future__ import annotations

import datetime as dt
from typing import Iterable, Sequence

from sqlalchemy import Date, delete, func, select
from sqlalchemy.orm import Session

from database.models import (
    AnomalyEvent,
    CollectionRun,
    ConnectivityTest,
    Device,
    DigitalTwinSnapshotBinding,
    ForecastPrediction,
    ForecastRun,
    Incident,
    Interface,
    Link,
    MetricDailyRollup,
    MetricSample,
    db_utcnow,
)


# --------------------------------------------------------------------------
# Devices & interfaces
# --------------------------------------------------------------------------
def upsert_device(
    session: Session,
    name: str,
    type_: str = "unknown",
    mgmt_ip: str | None = None,
    lab_ip: str | None = None,
    status: str = "unknown",
    container: str | None = None,
) -> Device:
    """Insert or update a device by name (idempotent inventory sync)."""
    device = session.scalar(select(Device).where(Device.name == name))
    if device is None:
        device = Device(name=name)
        session.add(device)
    device.type = type_
    if mgmt_ip is not None:
        device.mgmt_ip = mgmt_ip
    if lab_ip is not None:
        device.lab_ip = lab_ip
    device.status = status
    if container is not None:
        device.container = container
    device.updated_at = db_utcnow()
    session.flush()
    return device


def upsert_interface(
    session: Session, device: Device, name: str,
    ip_cidr: str | None = None, status: str | None = None,
) -> Interface:
    iface = session.scalar(
        select(Interface).where(Interface.device_id == device.id,
                                Interface.name == name)
    )
    if iface is None:
        iface = Interface(device_id=device.id, name=name)
        session.add(iface)
    if ip_cidr is not None:
        iface.ip_cidr = ip_cidr
    if status is not None:
        iface.status = status
    session.flush()
    return iface


def get_devices(session: Session) -> Sequence[Device]:
    return session.scalars(select(Device).order_by(Device.name)).all()


def get_device_by_name(session: Session, name: str) -> Device | None:
    return session.scalar(select(Device).where(Device.name == name))


def set_device_status(session: Session, name: str, status: str) -> None:
    device = session.scalar(select(Device).where(Device.name == name))
    if device is not None:
        device.status = status
        device.updated_at = db_utcnow()


# --------------------------------------------------------------------------
# Links
# --------------------------------------------------------------------------
def upsert_link(
    session: Session, device_a: str, if_a: str,
    device_b: str, if_b: str, subnet: str | None = None,
    status: str | None = None,
) -> Link:
    link = session.scalar(
        select(Link).where(
            Link.device_a == device_a, Link.if_a == if_a,
            Link.device_b == device_b, Link.if_b == if_b,
        )
    )
    if link is None:
        link = Link(device_a=device_a, if_a=if_a, device_b=device_b, if_b=if_b)
        session.add(link)
    if subnet is not None:
        link.subnet = subnet
    if status is not None:
        link.status = status
    session.flush()
    return link


def get_links(session: Session) -> Sequence[Link]:
    return session.scalars(select(Link).order_by(Link.id)).all()


# --------------------------------------------------------------------------
# Metric samples (time-series)
# --------------------------------------------------------------------------
def add_metric_sample(
    session: Session, device_name: str, metric: str,
    value: float | None, unit: str | None = None,
    interface_name: str | None = None,
    data_source: str = "live",
    collected_at: dt.datetime | None = None,
) -> MetricSample:
    """Store one measurement. ``value`` may legitimately be None (metric not
    available) - we store NULL instead of inventing a number."""
    sample = MetricSample(
        device_name=device_name,
        interface_name=interface_name,
        metric=metric,
        value=value,
        unit=unit,
        data_source=data_source,
    )
    if collected_at is not None:
        sample.collected_at = collected_at
    session.add(sample)
    session.flush()
    return sample


def add_metric_samples(session: Session, samples: Iterable[dict]) -> int:
    count = 0
    for s in samples:
        add_metric_sample(session, **s)
        count += 1
    return count


def get_metric_series(
    session: Session, device_name: str, metric: str,
    since: dt.datetime | None = None, until: dt.datetime | None = None,
    interface_name: str | None = None,
    data_source: str | None = None,
    unit: str | None = None, unit_is_null: bool = False,
) -> Sequence[MetricSample]:
    stmt = (
        select(MetricSample)
        .where(MetricSample.device_name == device_name,
               MetricSample.metric == metric)
        .order_by(MetricSample.collected_at)
    )
    if since is not None:
        stmt = stmt.where(MetricSample.collected_at >= since)
    if until is not None:
        stmt = stmt.where(MetricSample.collected_at <= until)
    if interface_name is not None:
        stmt = stmt.where(MetricSample.interface_name == interface_name)
    if data_source is not None:
        stmt = stmt.where(MetricSample.data_source == data_source)
    if unit_is_null:
        stmt = stmt.where(MetricSample.unit.is_(None))
    elif unit is not None:
        stmt = stmt.where(MetricSample.unit == unit)
    return session.scalars(stmt).all()


def get_latest_metric_times(
    session: Session, *, data_source: str | None = None,
) -> dict[str, dt.datetime]:
    """Latest stored sample time per device (optionally for one source).

    Used by the read-only change-impact analysis to label telemetry as fresh,
    stale or missing. A sample whose value is NULL still counts as an
    observation timestamp; no measurement value is inferred.
    """
    stmt = (
        select(MetricSample.device_name,
               func.max(MetricSample.collected_at))
        .group_by(MetricSample.device_name)
    )
    if data_source is not None:
        stmt = stmt.where(MetricSample.data_source == data_source)
    return {name: latest for name, latest in session.execute(stmt).all()
            if name and latest is not None}


def get_available_metrics(
    session: Session, device_name: str | None = None,
    non_null_only: bool = True,
) -> list[tuple[str, str | None, str | None, str]]:
    """Distinct (device, metric, unit, data_source) tuples that have at
    least one non-null value (default) - the UI must not offer metrics for
    which no actual data exists."""
    stmt = (
        select(
            MetricSample.device_name,
            MetricSample.metric,
            MetricSample.unit,
            MetricSample.data_source,
        ).distinct()
        .order_by(MetricSample.device_name, MetricSample.metric)
    )
    if non_null_only:
        stmt = stmt.where(MetricSample.value.is_not(None))
    if device_name:
        stmt = stmt.where(MetricSample.device_name == device_name)
    return [tuple(row) for row in session.execute(stmt).all()]


def get_series_inventory(
    session: Session, *, data_source: str | None = None,
    device_name: str | None = None, min_samples: int = 1,
) -> list[dict]:
    """Every stored series of one provenance, with how much data it holds.

    A "series" is a distinct (device, metric, interface) triple; ``unit`` and
    ``data_source`` travel with it so a caller can group without a second query.
    ``n_samples`` counts NON-NULL values only, because a NULL is a metric that
    was not available - it is not an observation and must not make a thin series
    look analysis-ready. Ordered richest-first so a bounded batch pass spends its
    cap on the series that can actually answer.
    """
    stmt = (
        select(
            MetricSample.device_name,
            MetricSample.metric,
            MetricSample.interface_name,
            MetricSample.unit,
            MetricSample.data_source,
            func.count(MetricSample.id).label("n_samples"),
        )
        .where(MetricSample.value.is_not(None))
        .group_by(MetricSample.device_name, MetricSample.metric,
                  MetricSample.interface_name, MetricSample.unit,
                  MetricSample.data_source)
        .having(func.count(MetricSample.id) >= max(1, int(min_samples)))
        .order_by(func.count(MetricSample.id).desc(),
                  MetricSample.device_name, MetricSample.metric)
    )
    if data_source:
        stmt = stmt.where(MetricSample.data_source == data_source)
    if device_name:
        stmt = stmt.where(MetricSample.device_name == device_name)
    return [
        {
            "device_name": row.device_name,
            "metric": row.metric,
            "interface_name": row.interface_name,
            "unit": row.unit,
            "data_source": row.data_source,
            "n_samples": int(row.n_samples),
        }
        for row in session.execute(stmt).all()
    ]


def get_rollup_series_inventory(
    session: Session, *, data_source: str | None = None,
    device_name: str | None = None, min_days: int = 1,
) -> list[dict]:
    """Every aggregated series present in ``metric_daily_rollups`` (Stage A6-4).

    This is the rollup-side counterpart to :func:`get_series_inventory`, added
    so a series that survives only as daily rollups (raw history shrunk by
    A6-1 retention) is still discoverable for forecasting. A "series" is a
    distinct (device, metric, interface) triple; ``n_days`` counts the daily
    buckets that exist and ``n_source_samples`` sums the raw sample counts the
    rollups were built from. Ordered richest-first.

    Rollups are ``data_source='live'``-only by construction (A6-2), so passing
    ``data_source='demo'`` correctly returns nothing - Demo never surfaces a
    rollup it does not own. No aggregation semantics are changed here; this is
    a read-only enumeration of what the rollup job already stored.
    """
    stmt = (
        select(
            MetricDailyRollup.device_name,
            MetricDailyRollup.metric,
            MetricDailyRollup.interface_name,
            MetricDailyRollup.unit,
            MetricDailyRollup.data_source,
            func.count(MetricDailyRollup.bucket_date).label("n_days"),
            func.coalesce(func.sum(MetricDailyRollup.sample_count), 0).label(
                "n_source_samples"),
            func.max(MetricDailyRollup.bucket_date).label("latest_date"),
        )
        .group_by(MetricDailyRollup.device_name, MetricDailyRollup.metric,
                  MetricDailyRollup.interface_name, MetricDailyRollup.unit,
                  MetricDailyRollup.data_source)
        .having(func.count(MetricDailyRollup.bucket_date)
                >= max(1, int(min_days)))
        .order_by(func.count(MetricDailyRollup.bucket_date).desc(),
                  MetricDailyRollup.device_name, MetricDailyRollup.metric)
    )
    if data_source:
        stmt = stmt.where(MetricDailyRollup.data_source == data_source)
    if device_name:
        stmt = stmt.where(MetricDailyRollup.device_name == device_name)
    return [
        {
            "device_name": row.device_name,
            "metric": row.metric,
            "interface_name": row.interface_name,
            "unit": row.unit,
            "data_source": row.data_source,
            "n_days": int(row.n_days),
            "n_source_samples": int(row.n_source_samples or 0),
            "latest_date": row.latest_date,
        }
        for row in session.execute(stmt).all()
    ]


def latest_metric_values(
    session: Session, device_name: str,
) -> Sequence[MetricSample]:
    """Most recent value per (metric, interface) for one device."""
    rows: list[MetricSample] = []
    pairs = session.execute(
        select(MetricSample.interface_name, MetricSample.metric)
        .where(MetricSample.device_name == device_name)
        .distinct()
    ).all()
    for if_name, metric in pairs:
        stmt = (
            select(MetricSample)
            .where(MetricSample.device_name == device_name,
                   MetricSample.metric == metric)
        )
        if if_name is None:
            stmt = stmt.where(MetricSample.interface_name.is_(None))
        else:
            stmt = stmt.where(MetricSample.interface_name == if_name)
        row = session.scalars(stmt.order_by(MetricSample.collected_at.desc()).limit(1)).first()
        if row is not None:
            rows.append(row)
    return sorted(rows, key=lambda r: (r.metric, r.interface_name or ""))


def latest_metric_values_for_source(
    session: Session, device_name: str, data_source: str,
) -> Sequence[MetricSample]:
    """Most recent value per (metric, interface) for one device WITHIN a single
    ``data_source``.

    Additive helper introduced to close the Phase 3 mixed Demo/Live gap (audit
    finding F-2). ``latest_metric_values`` above selects one cross-source
    "latest" per (metric, interface); when a demo and a live sample for the
    same metric share a timestamp, only one survives and a caller that
    post-filters by source can lose that source's own value. This variant
    scopes the query to a single ``data_source`` up front, so it never depends
    on the other source's rows. The shared ``latest_metric_values`` is left
    unchanged for the Phase 1/2 pages.
    """
    rows: list[MetricSample] = []
    pairs = session.execute(
        select(MetricSample.interface_name, MetricSample.metric)
        .where(MetricSample.device_name == device_name,
               MetricSample.data_source == data_source)
        .distinct()
    ).all()
    for if_name, metric in pairs:
        stmt = (
            select(MetricSample)
            .where(MetricSample.device_name == device_name,
                   MetricSample.metric == metric,
                   MetricSample.data_source == data_source)
        )
        if if_name is None:
            stmt = stmt.where(MetricSample.interface_name.is_(None))
        else:
            stmt = stmt.where(MetricSample.interface_name == if_name)
        row = session.scalars(stmt.order_by(MetricSample.collected_at.desc()).limit(1)).first()
        if row is not None:
            rows.append(row)
    return sorted(rows, key=lambda r: (r.metric, r.interface_name or ""))


# --------------------------------------------------------------------------
# Connectivity tests
# --------------------------------------------------------------------------
def add_connectivity_test(
    session: Session, source: str, destination: str, test_type: str,
    success: bool, detail: str | None = None,
    avg_latency_ms: float | None = None, packet_loss_pct: float | None = None,
    path_json: str | None = None, data_source: str = "live",
    tested_at: dt.datetime | None = None,
) -> ConnectivityTest:
    record = ConnectivityTest(
        source=source, destination=destination, test_type=test_type,
        success=success, detail=detail, avg_latency_ms=avg_latency_ms,
        packet_loss_pct=packet_loss_pct, path_json=path_json,
        data_source=data_source,
    )
    if tested_at is not None:
        record.tested_at = tested_at
    session.add(record)
    session.flush()
    return record


def get_connectivity_history(
    session: Session, limit: int = 100,
    destination: str | None = None, test_type: str | None = None,
) -> Sequence[ConnectivityTest]:
    stmt = select(ConnectivityTest).order_by(ConnectivityTest.tested_at.desc()).limit(limit)
    if destination:
        stmt = stmt.where(ConnectivityTest.destination == destination)
    if test_type:
        stmt = stmt.where(ConnectivityTest.test_type == test_type)
    return session.scalars(stmt).all()


# --------------------------------------------------------------------------
# Collection runs / incidents
# --------------------------------------------------------------------------
def start_collection_run(session: Session, data_source: str) -> CollectionRun:
    run = CollectionRun(started_at=db_utcnow(), data_source=data_source)
    session.add(run)
    session.flush()
    return run


def finish_collection_run(
    session: Session, run: CollectionRun, status: str,
    devices_total: int = 0, devices_up: int = 0,
    error_message: str | None = None,
) -> CollectionRun:
    # The run row may have been created in an earlier session (the collector
    # opens the row before probing so failures are still recorded). Attach it
    # to this session before updating.
    run = session.merge(run)
    run.finished_at = db_utcnow()
    run.status = status
    run.devices_total = devices_total
    run.devices_up = devices_up
    run.error_message = error_message
    session.flush()
    return run


def get_last_successful_run(session: Session) -> CollectionRun | None:
    return session.scalars(
        select(CollectionRun)
        .where(CollectionRun.finished_at.is_not(None),
               CollectionRun.status.in_(("ok", "partial")))
        .order_by(CollectionRun.finished_at.desc())
        .limit(1)
    ).first()


def get_recent_runs(session: Session, limit: int = 20) -> Sequence[CollectionRun]:
    return session.scalars(
        select(CollectionRun).order_by(CollectionRun.started_at.desc()).limit(limit)
    ).all()


def open_incident(session: Session, device_name: str | None, kind: str,
                  detail: str | None = None) -> Incident:
    inc = Incident(device_name=device_name, kind=kind, detail=detail)
    session.add(inc)
    session.flush()
    return inc


def resolve_open_incidents(session: Session, device_name: str, kind: str) -> None:
    for inc in session.scalars(
        select(Incident).where(Incident.device_name == device_name,
                               Incident.kind == kind,
                               Incident.resolved_at.is_(None))
    ).all():
        inc.resolved_at = db_utcnow()


def get_open_incidents(session: Session) -> Sequence[Incident]:
    return session.scalars(
        select(Incident).where(Incident.resolved_at.is_(None))
        .order_by(Incident.opened_at.desc())
    ).all()


# --------------------------------------------------------------------------
# Maintenance helpers
# --------------------------------------------------------------------------
def purge_metric_samples(session: Session, device_name: str | None = None) -> int:
    stmt = delete(MetricSample)
    if device_name:
        stmt = stmt.where(MetricSample.device_name == device_name)
    result = session.execute(stmt)
    return int(result.rowcount or 0)


def delete_expired_live_samples(
    session: Session, *, cutoff_utc: dt.datetime, batch_size: int,
) -> int:
    """Delete raw **live** telemetry older than ``cutoff_utc``, in bounded batches.

    This is the retention-safe counterpart to :func:`purge_metric_samples`. Two
    predicates are hard-coded and non-negotiable (Stage A6 safety):

    * ``data_source == 'live'`` - Demo history is structurally unreachable here.
    * ``collected_at < cutoff_utc`` - strict, so a row exactly at the cutoff is
      kept. ``cutoff_utc`` must be a *naive UTC* value computed by the caller
      from ``db_utcnow()`` so it lines up with the naive-UTC ``collected_at``
      column (never ``now() - interval``, which would mix ``timestamptz``).

    Each batch is its own committed transaction using an ``id`` keyset (portable
    to both SQLite and PostgreSQL), so a long run never holds a write lock across
    the whole table, and a mid-run failure rolls back only the in-flight batch.
    It never calls ``purge_metric_samples``. Returns the total rows deleted.
    """
    if batch_size <= 0:
        raise ValueError(f"batch_size must be positive, got {batch_size!r}")
    total = 0
    while True:
        batch_ids = (
            select(MetricSample.id)
            .where(
                MetricSample.data_source == "live",
                MetricSample.collected_at < cutoff_utc,
            )
            .order_by(MetricSample.id)
            .limit(batch_size)
        )
        # Both predicates are repeated on the DELETE itself as defense in depth:
        # even if the keyset subquery were ever changed, the deletion stays
        # scoped to expired live rows and can never reach demo or fresh data.
        stmt = delete(MetricSample).where(
            MetricSample.data_source == "live",
            MetricSample.collected_at < cutoff_utc,
            MetricSample.id.in_(batch_ids),
        )
        try:
            result = session.execute(stmt)
            deleted = int(result.rowcount or 0)
            session.commit()
        except Exception:
            session.rollback()
            raise
        total += deleted
        if deleted == 0:
            break
    return total


def _rollup_timestamp(value):
    """Coerce an aggregate min()/max() result over a DATETIME column to naive UTC.

    SQLite returns the stored ISO *string* for ``func.min/max(collected_at)``
    (no ORM type coercion on a labeled aggregate), whereas PostgreSQL returns a
    real ``datetime``. Normalizing here keeps ``upsert_daily_rollups`` portable
    and means the same code path is exercised under the offline SQLite tests.
    """
    if value is None or isinstance(value, dt.datetime):
        return value
    text = str(value)
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    return dt.datetime.fromisoformat(text)


def _rollup_bucket_date(value) -> dt.date:
    """Coerce the projected day-truncation expression to a ``datetime.date``.

    The ``Date`` result type already converts SQLite's ``date(...)`` text and
    psycopg2's ``DATE`` into a ``date``; the extra shapes handled here are a
    defensive belt so a driver variation can never write a wrong type into the
    ``Date`` column.
    """
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value)[:10])


# The ON CONFLICT target: the NOT NULL surrogate ``interface_key`` (NULL folded
# to '') makes the upsert NULL-safe identically on SQLite and PostgreSQL, without
# relying on PG15-only NULLS NOT DISTINCT semantics.
_ROLLUP_CONFLICT_COLUMNS = (
    "bucket_date", "device_name", "interface_key", "metric", "data_source",
)


def _rollup_bucket_expr(dialect_name: str):
    """Return the UTC-day bucket expression for the *active* dialect.

    Stage A6-2 requirement 5 says to convert the aggregation bucket explicitly to
    DATE, but the correct DATE conversion is engine-specific and neither generic
    form is portable:

    * PostgreSQL uses the canonical ``CAST(collected_at AS DATE)`` - a documented,
      unambiguous date conversion of the naive-UTC timestamp (``date(col)`` on PG
      is only a functional-cast alias and is not assumed).
    * SQLite uses ``date(collected_at)`` - its truncation function - because
      SQLite gives a generic ``CAST(x AS DATE)`` numeric affinity and returns the
      integer year (e.g. ``2026``) rather than a calendar day. This is why the
      offline SQLite suite cannot simply reuse the PostgreSQL spelling.

    Both are labelled ``bucket_date`` and typed ``Date`` so the projected value is
    a Python ``datetime.date`` on each engine, and both are used for the projection
    *and* the GROUP BY, so the stored ``bucket_date`` and the grouping key can
    never disagree - and a window wider than one day would still bucket per
    calendar day.
    """
    col = MetricSample.collected_at
    if dialect_name == "sqlite":
        return func.date(col, type_=Date).label("bucket_date")
    return func.cast(col, Date).label("bucket_date")


def rollup_uniqueness_strategy_info(session: Session) -> dict:
    """Report the NULL-safe uniqueness strategy against the *configured* server.

    Stage A6-2 requirement 4: the choice must be justified by the PostgreSQL
    version actually in use, never assumed. ``dialect.server_version_info`` is a
    cached attribute captured at engine construction (it opens no new connection)
    and the pg dialect parses ``server_version`` into it, so this reads the real
    configured release. The returned ``chosen`` is always the engine-independent
    NOT NULL surrogate ``interface_key``: it behaves identically whatever the
    answer is, which is why a PG14 deployment and a PG16 deployment get the same
    schema and the same ON CONFLICT target.
    """
    from database.models import pg_supports_nulls_not_distinct

    bind = session.get_bind()
    dialect = bind.dialect
    version_info = getattr(dialect, "server_version_info", None)
    major = version_info[0] if version_info else None
    return {
        "dialect": dialect.name,
        "server_version_info": tuple(version_info) if version_info else None,
        "pg_supports_nulls_not_distinct": pg_supports_nulls_not_distinct(major),
        "chosen": "surrogate_interface_key",
        "version_independent": True,
    }


def upsert_daily_rollups(
    session: Session, *, day_start: dt.datetime, day_end: dt.datetime,
) -> int:
    """Aggregate one complete UTC day of raw **live** samples into rollups.

    Rebuilds the window ``[day_start, day_end)`` (normally a single calendar day)
    *idempotently*: every affected (day, device, interface, metric) key is
    recomputed from the raw rows and replaced via ``ON CONFLICT DO UPDATE`` -
    never accumulated - so re-running a completed day reproduces the same
    numbers, and a historical day can be re-derived when late telemetry arrives.
    Returns the number of rollup rows written (0 for an empty window; no row is
    fabricated for a day with no live data).

    Stage A6-2 safety properties:
    * Only ``data_source == 'live'`` rows are aggregated; Demo history never
      reaches the rollups, preserving the demo/live boundary.
    * The bucket is an explicit SQL DATE conversion chosen per dialect
      (PostgreSQL ``CAST(collected_at AS DATE)``, SQLite ``date(collected_at)``)
      that is both projected and grouped, so the stored ``bucket_date`` is the
      database's own UTC day derivation, not a value the Python layer assumes.
    * ``count`` tallies raw measurement points; ``avg/min/max`` ignore NULL values
      via SQL aggregates, so a day whose readings were all unavailable yields NULL
      statistics - never an invented 0. ``unit`` is carried through (max, ignoring
      NULLs). ``first_seen/last_seen`` are the real observed min/max timestamps.
    * The whole day is ONE transaction: any failure rolls the entire rebuild back,
      so a partial day is never left committed.
    * ``interface_key`` is the conflict target, giving NULL-safe uniqueness
      (multiple device-level rows with a NULL interface collapse into one).
    """
    # Naive-UTC contract is checked FIRST so a timezone-aware argument fails with
    # a clear ValueError instead of a confusing naive/aware comparison TypeError.
    if day_start.tzinfo is not None or day_end.tzinfo is not None:
        raise ValueError("upsert_daily_rollups expects naive UTC datetimes")
    if day_end <= day_start:
        raise ValueError("day_end must be after day_start")

    # The dialect decides BOTH the DATE-bucket spelling and the upsert operator,
    # so it is resolved once here and reused below.
    dialect_name = session.get_bind().dialect.name
    bucket_date = _rollup_bucket_expr(dialect_name)

    aggregate = (
        select(
            bucket_date,
            MetricSample.device_name,
            MetricSample.interface_name,
            MetricSample.metric,
            func.count().label("sample_count"),
            func.avg(MetricSample.value).label("avg_value"),
            func.min(MetricSample.value).label("min_value"),
            func.max(MetricSample.value).label("max_value"),
            func.max(MetricSample.unit).label("unit"),
            func.min(MetricSample.collected_at).label("first_seen"),
            func.max(MetricSample.collected_at).label("last_seen"),
        )
        .where(
            MetricSample.data_source == "live",
            MetricSample.collected_at >= day_start,
            MetricSample.collected_at < day_end,
        )
        # GROUP BY folds NULL interfaces into one group on both engines, which
        # lines up 1:1 with the interface_key surrogate used for uniqueness.
        .group_by(
            bucket_date,
            MetricSample.device_name,
            MetricSample.interface_name,
            MetricSample.metric,
        )
    )
    rows = session.execute(aggregate).all()

    if dialect_name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as _insert
    elif dialect_name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as _insert
    else:
        raise ValueError(f"unsupported dialect for rollups: {dialect_name!r}")

    now = db_utcnow()
    written = 0
    try:
        for r in rows:
            stmt = _insert(MetricDailyRollup).values(
                bucket_date=_rollup_bucket_date(r.bucket_date),
                device_name=r.device_name,
                interface_name=r.interface_name,
                interface_key=r.interface_name or "",
                metric=r.metric,
                data_source="live",
                sample_count=int(r.sample_count or 0),
                avg_value=r.avg_value,
                min_value=r.min_value,
                max_value=r.max_value,
                unit=r.unit,
                first_seen=_rollup_timestamp(r.first_seen),
                last_seen=_rollup_timestamp(r.last_seen),
                updated_at=now,
            )
            # ``stmt.excluded`` refers to the row we tried to insert, so the
            # conflict UPDATE replaces the aggregate from the *fresh* computation
            # (replace, never accumulate) - the basis of idempotent rebuilds.
            excluded = stmt.excluded
            stmt = stmt.on_conflict_do_update(
                index_elements=list(_ROLLUP_CONFLICT_COLUMNS),
                set_={
                    "interface_name": excluded.interface_name,
                    "sample_count": excluded.sample_count,
                    "avg_value": excluded.avg_value,
                    "min_value": excluded.min_value,
                    "max_value": excluded.max_value,
                    "unit": excluded.unit,
                    "first_seen": excluded.first_seen,
                    "last_seen": excluded.last_seen,
                    "updated_at": excluded.updated_at,
                },
            )
            session.execute(stmt)
            written += 1
        session.commit()
    except Exception:
        session.rollback()
        raise
    return written


# ==========================================================================
# Phase 2 - analytics persistence (additive; Phase 1 helpers untouched).
# Raw metric_samples are NEVER modified by anything below: forecasts and
# detections live in their own tables, predictions never overwrite
# observations (spec Scope E).
# ==========================================================================
def find_open_anomaly_event(
    session: Session, *, device_name: str, metric: str, detector: str,
    data_source: str, interface_name: str | None,
    after_last_seen: dt.datetime,
) -> AnomalyEvent | None:
    """Open event for the same series/detector whose window has not expired.

    ``after_last_seen`` is the lower bound for ``last_seen_at`` used for
    grouping (events whose last anomaly is older than the group window are
    NOT reused - a new episode becomes a new event row).
    """
    stmt = (
        select(AnomalyEvent)
        .where(
            AnomalyEvent.device_name == device_name,
            AnomalyEvent.metric == metric,
            AnomalyEvent.detector == detector,
            AnomalyEvent.data_source == data_source,
            AnomalyEvent.status == "open",
            AnomalyEvent.last_seen_at >= after_last_seen,
        )
        .order_by(AnomalyEvent.last_seen_at.desc())
    )
    if interface_name is None:
        stmt = stmt.where(AnomalyEvent.interface_name.is_(None))
    else:
        stmt = stmt.where(AnomalyEvent.interface_name == interface_name)
    return session.scalars(stmt).first()


def create_anomaly_event(session: Session, **fields) -> AnomalyEvent:
    event = AnomalyEvent(**fields)
    session.add(event)
    session.flush()
    return event


def update_anomaly_event(session: Session, event: AnomalyEvent,
                         **fields) -> AnomalyEvent:
    event = session.merge(event)
    for key, value in fields.items():
        setattr(event, key, value)
    session.flush()
    return event


def resolve_anomaly_event(session: Session, event: AnomalyEvent,
                          resolved_at: dt.datetime) -> AnomalyEvent:
    event = session.merge(event)
    event.status = "resolved"
    event.resolved_at = resolved_at
    session.flush()
    return event


def get_anomaly_events(
    session: Session, *, device_name: str | None = None,
    metric: str | None = None, detector: str | None = None,
    data_source: str | None = None, status: str | None = None,
    since: dt.datetime | None = None, limit: int = 200,
) -> list[AnomalyEvent]:
    stmt = select(AnomalyEvent).order_by(AnomalyEvent.last_seen_at.desc())
    if device_name:
        stmt = stmt.where(AnomalyEvent.device_name == device_name)
    if metric:
        stmt = stmt.where(AnomalyEvent.metric == metric)
    if detector:
        stmt = stmt.where(AnomalyEvent.detector == detector)
    if data_source:
        stmt = stmt.where(AnomalyEvent.data_source == data_source)
    if status:
        stmt = stmt.where(AnomalyEvent.status == status)
    if since is not None:
        stmt = stmt.where(AnomalyEvent.last_seen_at >= since)
    return list(session.scalars(stmt.limit(limit)).all())


def add_forecast_run(session: Session, **fields) -> ForecastRun:
    run = ForecastRun(**fields)
    session.add(run)
    session.flush()
    return run


def add_forecast_predictions(session: Session, run: ForecastRun,
                             rows: list[dict]) -> int:
    """Store predictions for one run. Each dict needs target_timestamp,
    step, predicted_value and optional bounds; actuals start NULL."""
    count = 0
    for r in rows:
        pred = ForecastPrediction(
            run_id=run.id,
            target_timestamp=r["target_timestamp"],
            step=r.get("step", 1),
            predicted_value=float(r["predicted_value"]),
            lower_bound=r.get("lower_bound"),
            upper_bound=r.get("upper_bound"),
        )
        session.add(pred)
        count += 1
    session.flush()
    return count


def get_forecast_runs(
    session: Session, *, device_name: str | None = None,
    metric: str | None = None, data_source: str | None = None,
    comparison_group: str | None = None, limit: int = 50,
) -> list[ForecastRun]:
    stmt = select(ForecastRun).order_by(ForecastRun.created_at.desc())
    if device_name:
        stmt = stmt.where(ForecastRun.device_name == device_name)
    if metric:
        stmt = stmt.where(ForecastRun.metric == metric)
    if data_source:
        stmt = stmt.where(ForecastRun.data_source == data_source)
    if comparison_group:
        stmt = stmt.where(ForecastRun.comparison_group == comparison_group)
    return list(session.scalars(stmt.limit(limit)).all())


def get_forecast_predictions(session: Session, run_id: int) -> list[ForecastPrediction]:
    return list(session.scalars(
        select(ForecastPrediction)
        .where(ForecastPrediction.run_id == run_id)
        .order_by(ForecastPrediction.target_timestamp)
    ).all())


def get_unbackfilled_predictions(session: Session,
                                 run_id: int) -> list[ForecastPrediction]:
    return list(session.scalars(
        select(ForecastPrediction)
        .where(ForecastPrediction.run_id == run_id,
               ForecastPrediction.actual_value.is_(None))
        .order_by(ForecastPrediction.target_timestamp)
    ).all())


# ==========================================================================
# Phase 3 - AI run history & audit trail (additive; Phase 1/2 helpers
# untouched). Secrets are never stored here.
# ==========================================================================
from database.models import (  # noqa: E402  (kept after Phase 1/2 imports)
    AIEvidence,
    AIRun,
    AIStatusTransition,
)


def create_ai_run(session: Session, *, run_uuid: str, request_text: str,
                  data_source: str, model: str | None = None,
                  prompt_version: str | None = None,
                  source_language: str | None = None) -> AIRun:
    run = AIRun(
        run_uuid=run_uuid, request_text=request_text, data_source=data_source,
        model=model, prompt_version=prompt_version,
        source_language=source_language, status="received",
    )
    session.add(run)
    session.flush()
    return run


def update_ai_run(session: Session, run: AIRun, **fields) -> AIRun:
    run = session.merge(run)
    for key, value in fields.items():
        setattr(run, key, value)
    run.updated_at = db_utcnow()
    session.flush()
    return run


def get_ai_run_by_uuid(session: Session, run_uuid: str) -> AIRun | None:
    return session.scalar(select(AIRun).where(AIRun.run_uuid == run_uuid))


def get_ai_runs(session: Session, *, data_source: str | None = None,
                status: str | None = None, request_type: str | None = None,
                limit: int = 25) -> list[AIRun]:
    stmt = select(AIRun).order_by(AIRun.created_at.desc())
    if data_source:
        stmt = stmt.where(AIRun.data_source == data_source)
    if status:
        stmt = stmt.where(AIRun.status == status)
    if request_type:
        stmt = stmt.where(AIRun.request_type == request_type)
    return list(session.scalars(stmt.limit(limit)).all())


def add_status_transition(session: Session, run: AIRun, status: str,
                          detail: str | None = None) -> AIStatusTransition:
    run = session.merge(run)
    existing = session.scalar(
        select(func.max(AIStatusTransition.step_order))
        .where(AIStatusTransition.run_id == run.id)
    )
    step = (int(existing) + 1) if existing is not None else 0
    t = AIStatusTransition(run_id=run.id, step_order=step, status=status,
                           detail=detail)
    session.add(t)
    session.flush()
    return t


def get_status_transitions(session: Session, run_id: int) -> list[AIStatusTransition]:
    return list(session.scalars(
        select(AIStatusTransition)
        .where(AIStatusTransition.run_id == run_id)
        .order_by(AIStatusTransition.step_order)
    ).all())


def add_ai_evidence(session: Session, run: AIRun, **fields) -> AIEvidence:
    run = session.merge(run)
    ev = AIEvidence(run_id=run.id, **fields)
    session.add(ev)
    session.flush()
    return ev


def get_ai_evidence(session: Session, run_id: int) -> list[AIEvidence]:
    return list(session.scalars(
        select(AIEvidence).where(AIEvidence.run_id == run_id)
        .order_by(AIEvidence.id)
    ).all())


def set_human_review(session: Session, run: AIRun, outcome: str) -> AIRun:
    run = session.merge(run)
    run.human_review_outcome = outcome
    run.updated_at = db_utcnow()
    session.flush()
    return run


# ==========================================================================
# v2 Slice 1 - alerting rules & fired events (additive; no prior helper is
# touched). Fired events de-duplicate per (rule, series, source) while an
# open event's window has not expired, mirroring the anomaly-event pattern.
# ==========================================================================
from database.models import (  # noqa: E402
    AlertEvent,
    AlertRule,
)


def create_alert_rule(session: Session, **fields) -> AlertRule:
    """Insert an alert rule. ``condition``/``severity`` are validated."""
    from database.models import ALERT_CONDITIONS, ALERT_SEVERITIES
    condition = fields.get("condition", "above")
    if condition not in ALERT_CONDITIONS:
        raise ValueError(f"condition must be one of {ALERT_CONDITIONS}")
    severity = fields.get("severity", "warning")
    if severity not in ALERT_SEVERITIES:
        raise ValueError(f"severity must be one of {ALERT_SEVERITIES}")
    rule = AlertRule(**fields)
    session.add(rule)
    session.flush()
    return rule


def get_alert_rules(session: Session, *,
                    enabled_only: bool = False) -> list[AlertRule]:
    stmt = select(AlertRule).order_by(AlertRule.name)
    if enabled_only:
        stmt = stmt.where(AlertRule.enabled.is_(True))
    return list(session.scalars(stmt).all())


def set_alert_rule_enabled(session: Session, rule: AlertRule,
                           enabled: bool) -> AlertRule:
    rule = session.merge(rule)
    rule.enabled = enabled
    session.flush()
    return rule


def find_open_alert_event(
    session: Session, *, rule_id: int, device_name: str, metric: str,
    interface_name: str | None, data_source: str, after_last_seen: dt.datetime,
) -> AlertEvent | None:
    """Open/ack event for the same rule+series+source within the window."""
    stmt = (
        select(AlertEvent)
        .where(
            AlertEvent.rule_id == rule_id,
            AlertEvent.device_name == device_name,
            AlertEvent.metric == metric,
            AlertEvent.data_source == data_source,
            AlertEvent.status.in_(("open", "acknowledged")),
            AlertEvent.last_fired_at >= after_last_seen,
        )
        .order_by(AlertEvent.last_fired_at.desc())
    )
    if interface_name is None:
        stmt = stmt.where(AlertEvent.interface_name.is_(None))
    else:
        stmt = stmt.where(AlertEvent.interface_name == interface_name)
    return session.scalars(stmt).first()


def record_alert_firing(
    session: Session, rule: AlertRule, sample, *,
    fired_at: dt.datetime, summary: str | None = None,
    window_minutes: int = 60,
) -> AlertEvent:
    """Record one firing of ``rule`` for ``sample``.

    Reuses the open event when one exists within ``window_minutes``
    (increments ``occurrences`` and refreshes the latest observation) and
    otherwise opens a new event. The event's ``data_source`` is taken from
    the sample so live and demo provenance can never merge.
    """
    existing = find_open_alert_event(
        session, rule_id=rule.id, device_name=sample.device_name,
        metric=sample.metric, interface_name=sample.interface_name,
        data_source=sample.data_source,
        after_last_seen=fired_at - dt.timedelta(minutes=window_minutes),
    )
    if existing is not None:
        existing.last_fired_at = fired_at
        existing.occurrences += 1
        existing.value = sample.value
        existing.threshold = rule.threshold
        existing.severity = rule.severity
        if summary is not None:
            existing.summary = summary
        session.flush()
        return existing

    event = AlertEvent(
        rule_id=rule.id,
        rule_name=rule.name,
        device_name=sample.device_name,
        interface_name=sample.interface_name,
        metric=sample.metric,
        value=sample.value,
        threshold=rule.threshold,
        condition=rule.condition,
        severity=rule.severity,
        data_source=sample.data_source,
        status="open",
        first_fired_at=fired_at,
        last_fired_at=fired_at,
        occurrences=1,
        summary=summary,
    )
    session.add(event)
    session.flush()
    return event


def acknowledge_alert_event(session: Session, event: AlertEvent,
                            at: dt.datetime) -> AlertEvent:
    event = session.merge(event)
    event.status = "acknowledged"
    event.acknowledged_at = at
    session.flush()
    return event


def resolve_alert_event(session: Session, event: AlertEvent,
                        at: dt.datetime) -> AlertEvent:
    event = session.merge(event)
    event.status = "resolved"
    event.resolved_at = at
    session.flush()
    return event


def get_alert_events(
    session: Session, *, status: str | None = None,
    severity: str | None = None, device_name: str | None = None,
    data_source: str | None = None, since: dt.datetime | None = None,
    limit: int = 200,
) -> list[AlertEvent]:
    stmt = select(AlertEvent).order_by(AlertEvent.last_fired_at.desc())
    if status:
        stmt = stmt.where(AlertEvent.status == status)
    if severity:
        stmt = stmt.where(AlertEvent.severity == severity)
    if device_name:
        stmt = stmt.where(AlertEvent.device_name == device_name)
    if data_source:
        stmt = stmt.where(AlertEvent.data_source == data_source)
    if since is not None:
        stmt = stmt.where(AlertEvent.last_fired_at >= since)
    return list(session.scalars(stmt.limit(limit)).all())


def count_open_alerts(session: Session) -> int:
    return int(session.scalar(
        select(func.count(AlertEvent.id)).where(
            AlertEvent.status.in_(("open", "acknowledged")))
    ) or 0)


# ==========================================================================
# v2 Slice 2 - user accounts (additive). Only a salted password HASH is
# stored here; ``services.auth`` owns hashing/verification.
# ==========================================================================
from database.models import USER_ROLES, User  # noqa: E402


def create_user(session: Session, *, username: str, password_hash: str,
                role: str = "viewer", is_active: bool = True) -> User:
    if role not in USER_ROLES:
        raise ValueError(f"role must be one of {USER_ROLES}")
    if not username or not password_hash:
        raise ValueError("username and password_hash are required")
    user = User(username=username, password_hash=password_hash,
                role=role, is_active=is_active)
    session.add(user)
    session.flush()
    return user


def get_user_by_name(session: Session, username: str) -> User | None:
    return session.scalar(select(User).where(User.username == username))


def list_users(session: Session) -> list[User]:
    return list(session.scalars(select(User).order_by(User.username)).all())


def count_users(session: Session) -> int:
    return int(session.scalar(select(func.count(User.id))) or 0)


def set_last_login(session: Session, user: User, at: dt.datetime) -> User:
    user = session.merge(user)
    user.last_login = at
    session.flush()
    return user


def set_user_active(session: Session, user: User, is_active: bool) -> User:
    user = session.merge(user)
    user.is_active = is_active
    session.flush()
    return user


# ==========================================================================
# v2 Slice 7 - change requests (additive). The repository only persists the
# approval/apply audit; the lifecycle rules live in services.change_service.
# Config lists / validation findings are stored as JSON text columns.
# ==========================================================================
import json  # noqa: E402

from database.models import (  # noqa: E402
    CHANGE_RISK_LEVELS,
    CHANGE_STATUSES,
    ChangeAuditEvent,
    ChangeRequest,
)


def _dump(obj):
    return None if obj is None else json.dumps(obj, ensure_ascii=False,
                                               default=str, sort_keys=True)


def create_change_request(
    session: Session, *, request_uuid: str, device_name: str,
    data_source: str = "live", description: str | None = None,
    base_config=None, proposed_config=None, rollback_config=None,
    diff: str | None = None, validation=None, risk_level: str = "low",
    status: str = "draft", proposed_by: str | None = None,
    proposed_by_authenticated: bool = False,
    expires_at: dt.datetime | None = None,
    rejection_reason: str | None = None,
    rejected_at: dt.datetime | None = None,
) -> ChangeRequest:
    if status not in CHANGE_STATUSES:
        raise ValueError(f"status must be one of {CHANGE_STATUSES}")
    if risk_level not in CHANGE_RISK_LEVELS:
        raise ValueError(f"risk_level must be one of {CHANGE_RISK_LEVELS}")
    if not request_uuid or not device_name:
        raise ValueError("request_uuid and device_name are required")
    req = ChangeRequest(
        request_uuid=request_uuid, device_name=device_name,
        data_source=data_source, description=description,
        base_config_json=_dump(base_config),
        proposed_config_json=_dump(proposed_config),
        rollback_config_json=_dump(rollback_config),
        diff=diff, validation_json=_dump(validation),
        risk_level=risk_level, status=status, proposed_by=proposed_by,
        proposed_by_authenticated=bool(proposed_by_authenticated),
        expires_at=expires_at, rejection_reason=rejection_reason,
        rejected_at=rejected_at,
    )
    session.add(req)
    session.flush()
    return req


def get_change_request(session: Session, request_uuid: str) -> ChangeRequest | None:
    return session.scalar(
        select(ChangeRequest).where(ChangeRequest.request_uuid == request_uuid))


def list_change_requests(
    session: Session, *, status: str | None = None,
    device_name: str | None = None, data_source: str | None = None,
    limit: int = 100,
) -> list[ChangeRequest]:
    stmt = select(ChangeRequest).order_by(ChangeRequest.created_at.desc())
    if status:
        stmt = stmt.where(ChangeRequest.status == status)
    if device_name:
        stmt = stmt.where(ChangeRequest.device_name == device_name)
    if data_source:
        stmt = stmt.where(ChangeRequest.data_source == data_source)
    return list(session.scalars(stmt.limit(limit)).all())


def update_change_request(session: Session, req: ChangeRequest, **fields
                          ) -> ChangeRequest:
    req = session.merge(req)
    status = fields.get("status")
    if status is not None and status not in CHANGE_STATUSES:
        raise ValueError(f"status must be one of {CHANGE_STATUSES}")
    json_fields = {"base_config", "proposed_config", "rollback_config",
                    "validation"}
    for key, value in fields.items():
        if key in json_fields:
            setattr(req, f"{key}_json", _dump(value))
        elif hasattr(req, key):
            setattr(req, key, value)
    session.flush()
    return req


def add_change_audit_event(
    session: Session, req: ChangeRequest, *, action: str,
    actor: str | None = None, actor_authenticated: bool = False,
    from_status: str | None = None, to_status: str | None = None,
    reason: str | None = None, at: dt.datetime | None = None,
) -> ChangeAuditEvent:
    """Append one audit row. Actor is only recorded when authenticated."""
    req = session.merge(req)
    event = ChangeAuditEvent(
        request_id=req.id, action=action,
        actor=actor if actor_authenticated and actor else None,
        actor_authenticated=bool(actor_authenticated and actor),
        from_status=from_status, to_status=to_status, reason=reason,
        at=at or db_utcnow(),
    )
    session.add(event)
    session.flush()
    return event


def get_change_audit_events(
    session: Session, request_id: int,
) -> list[ChangeAuditEvent]:
    return list(session.scalars(
        select(ChangeAuditEvent)
        .where(ChangeAuditEvent.request_id == request_id)
        .order_by(ChangeAuditEvent.at, ChangeAuditEvent.id)
    ).all())


# --------------------------------------------------------------------------
# Digital Twin snapshot bindings (Phase D4)
# --------------------------------------------------------------------------
def add_dt_snapshot_binding(
    session: Session, *,
    binding_uuid: str,
    schema_version: str,
    binding_version: str,
    topology_ref: str,
    topology_source: str,
    topology_fingerprint: str,
    change_fingerprint: str | None,
    device_name: str | None,
    request_uuid: str | None,
    scenario: str | None,
    captured_at: dt.datetime,
    max_age_seconds: int | None,
    device_count: int,
    link_count: int,
) -> DigitalTwinSnapshotBinding:
    """Insert one immutable binding row and flush it (caller commits).

    No update/delete counterpart exists: the registry is append-only. A
    duplicate ``binding_uuid`` is rejected by the unique constraint and must be
    resolved by the service, never silently overwritten here.
    """
    row = DigitalTwinSnapshotBinding(
        binding_uuid=binding_uuid,
        schema_version=schema_version,
        binding_version=binding_version,
        topology_ref=topology_ref,
        topology_source=topology_source,
        topology_fingerprint=topology_fingerprint,
        change_fingerprint=change_fingerprint,
        device_name=device_name,
        request_uuid=request_uuid,
        scenario=scenario,
        captured_at=captured_at,
        max_age_seconds=max_age_seconds,
        device_count=device_count,
        link_count=link_count,
    )
    session.add(row)
    session.flush()
    return row


def get_dt_snapshot_binding(
    session: Session, binding_uuid: str,
) -> DigitalTwinSnapshotBinding | None:
    return session.scalar(
        select(DigitalTwinSnapshotBinding)
        .where(DigitalTwinSnapshotBinding.binding_uuid == binding_uuid))


def list_dt_snapshot_bindings(
    session: Session, *, topology_ref: str | None = None,
    request_uuid: str | None = None, limit: int = 100,
) -> list[DigitalTwinSnapshotBinding]:
    stmt = select(DigitalTwinSnapshotBinding).order_by(
        DigitalTwinSnapshotBinding.captured_at.desc(),
        DigitalTwinSnapshotBinding.id.desc())
    if topology_ref:
        stmt = stmt.where(
            DigitalTwinSnapshotBinding.topology_ref == topology_ref)
    if request_uuid:
        stmt = stmt.where(
            DigitalTwinSnapshotBinding.request_uuid == request_uuid)
    return list(session.scalars(stmt.limit(limit)).all())


# ==========================================================================
# Phase 5 - AI Lab experiment history (additive; nothing above is altered).
#
# A lab experiment records its protocol, metrics and warnings. It never
# duplicates raw samples, and no lab frame is written into metric_samples or
# anomaly_events, so stored experiment rows and stored measurements stay
# separate concerns.
# ==========================================================================
from database.models import LabExperiment  # noqa: E402


def add_lab_experiment(session: Session, **fields) -> LabExperiment:
    """Insert one experiment row and flush it (caller commits).

    Field names are passed through unchanged so the service owns validation;
    an unknown key raises ``TypeError`` instead of silently dropping metadata.
    """
    row = LabExperiment(**fields)
    session.add(row)
    session.flush()
    return row


def get_lab_experiments(
    session: Session, *, task: str | None = None,
    source_type: str | None = None, status: str | None = None,
    dataset_fingerprint: str | None = None, limit: int = 50,
) -> list[LabExperiment]:
    """Most recent experiments first, with a bounded result set."""
    stmt = select(LabExperiment).order_by(
        LabExperiment.created_at.desc(), LabExperiment.id.desc())
    if task:
        stmt = stmt.where(LabExperiment.task == task)
    if source_type:
        stmt = stmt.where(LabExperiment.source_type == source_type)
    if status:
        stmt = stmt.where(LabExperiment.status == status)
    if dataset_fingerprint:
        stmt = stmt.where(
            LabExperiment.dataset_fingerprint == dataset_fingerprint)
    return list(session.scalars(stmt.limit(max(1, int(limit)))).all())


def get_lab_experiment(session: Session,
                       experiment_uuid: str) -> LabExperiment | None:
    return session.scalar(
        select(LabExperiment)
        .where(LabExperiment.experiment_uuid == experiment_uuid))


def count_lab_experiments(session: Session) -> int:
    return int(session.scalar(select(func.count(LabExperiment.id))) or 0)
