"""Data-access layer. All SQL/Orrm access goes through this module.

The Streamlit UI and the services call these functions; nothing else writes
to the database directly (requirement: data access separated from the UI).
"""
from __future__ import annotations

import datetime as dt
from typing import Iterable, Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from database.models import (
    AnomalyEvent,
    CollectionRun,
    ConnectivityTest,
    Device,
    ForecastPrediction,
    ForecastRun,
    Incident,
    Interface,
    Link,
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
    return session.scalars(stmt).all()


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
