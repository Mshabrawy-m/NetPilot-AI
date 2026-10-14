"""Anomaly event lifecycle service (Phase 2 Scope D persistence layer).

Bridges ``analytics.anomaly_detection`` and the database:

* runs detectors only on series whose data-quality gate is ready;
* groups consecutive anomaly points for the same
  (device, interface, metric, detector, data_source) into ONE event
  (``last_seen_at`` within the configured group window) so a continuous
  anomaly does not create a new incident every collection cycle;
* resolution rule (spec §7): an event is resolved only by an actual
  *normal observation* after the anomaly - a missing/stale sample never
  resolves anything;
* every stored event carries the detector configuration snapshot and a
  plain-language explanation for the UI.
"""
from __future__ import annotations

import datetime as dt
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from analytics import data_quality
from analytics.anomaly_detection import AnomalyPoint, detect_frame
from analytics.time_series import dedupe_frame, load_metric_frame
from config import get_settings
from database import repository
from database.models import AnomalyEvent, db_utcnow

#: How far back a detection run looks at most (bounded query, spec §12).
DETECTION_LOOKBACK_LIMIT = 5000


def load_ready_series(
    session: Session, device_name: str, metric: str,
    interface_name: str | None, data_source: str,
    purpose: str = data_quality.DETECTION,
    since: dt.datetime | None = None,
    device_level_only: bool = False,
) -> tuple:
    """Load + quality-gate one series. Returns (frame, SeriesQuality)."""
    frame = load_metric_frame(
        session, device_name, metric, interface_name=interface_name,
        data_source=data_source, since=since,
        max_points=DETECTION_LOOKBACK_LIMIT,
        device_level_only=device_level_only)
    frame = dedupe_frame(frame)
    quality = data_quality.summarize_quality(
        frame, device_name, metric, interface_name=interface_name,
        expected_interval_seconds=float(
            get_settings().collect_interval_seconds),
        now=db_utcnow())
    return frame, quality


def ingest_anomaly_points(
    session: Session, points: list[AnomalyPoint],
    group_window_seconds: float | None = None,
) -> dict:
    """Persist detection points with deduplication/grouping.

    Returns counts ``{"created": n, "updated": n, "skipped": n}``.
    Grouping: a point joins the newest open event for the same key when
    ``point.timestamp - event.last_seen_at <= group_window``. Joining
    escalates severity, refreshes peak value/baseline/thresholds (by
    largest |deviation|), increments occurrences and appends the source
    sample id. Points older than an event's last_seen (out-of-order
    re-runs) still update it but never move ``first_seen_at`` backwards.
    """
    window = (group_window_seconds
              if group_window_seconds is not None
              else float(get_settings().anomaly_group_window_seconds))
    created = updated = skipped = 0
    for p in points:
        anchor = p.timestamp.to_pydatetime() if hasattr(p.timestamp, "to_pydatetime") else p.timestamp
        existing = repository.find_open_anomaly_event(
            session,
            device_name=p.device_name, metric=p.metric,
            detector=p.detector, data_source=p.data_source,
            interface_name=p.interface_name,
            after_last_seen=anchor - dt.timedelta(seconds=window),
        )
        if existing is not None:
            fields: dict = {
                "last_seen_at": max(existing.last_seen_at, anchor),
                "occurrences": int(existing.occurrences or 1) + 1,
            }
            if (p.deviation or 0) >= (existing.deviation or 0):
                fields.update(
                    observed_value=p.observed,
                    baseline_value=p.baseline,
                    threshold_upper=p.threshold_upper,
                    threshold_lower=p.threshold_lower,
                    deviation=p.deviation,
                    explanation=p.explanation,
                )
            order = {"info": 0, "warning": 1, "critical": 2}
            if order.get(p.severity, 1) > order.get(existing.severity, 1):
                fields["severity"] = p.severity
            if p.sample_id is not None:
                try:
                    ids = json.loads(existing.source_samples_json or "[]")
                except json.JSONDecodeError:
                    ids = []
                ids = ids[-50:] + [p.sample_id]  # bounded link list
                fields["source_samples_json"] = json.dumps(sorted(set(ids)))
            repository.update_anomaly_event(session, existing, **fields)
            updated += 1
            continue
        if existing is None and _recently_resolved(session, p, anchor, window):
            # A just-resolved event for the same key inside the group window
            # means the detector keeps seeing the same episode; skip noise.
            skipped += 1
            continue
        repository.create_anomaly_event(
            session,
            device_name=p.device_name, interface_name=p.interface_name,
            metric=p.metric, detector=p.detector,
            detector_version=p.detector_version,
            detector_config_json=json.dumps(p.detector_config, sort_keys=True),
            data_source=p.data_source,
            first_seen_at=anchor, last_seen_at=anchor, occurrences=1,
            observed_value=p.observed, baseline_value=p.baseline,
            threshold_upper=p.threshold_upper,
            threshold_lower=p.threshold_lower, deviation=p.deviation,
            severity=p.severity, status="open",
            explanation=p.explanation,
            source_samples_json=json.dumps([p.sample_id]
                                           if p.sample_id is not None else []),
        )
        created += 1
    session.commit()
    return {"created": created, "updated": updated, "skipped": skipped}


def _recently_resolved(session: Session, p: AnomalyPoint,
                       anchor: dt.datetime, window: float) -> bool:
    stmt = select(AnomalyEvent).where(
        AnomalyEvent.device_name == p.device_name,
        AnomalyEvent.metric == p.metric,
        AnomalyEvent.detector == p.detector,
        AnomalyEvent.data_source == p.data_source,
        AnomalyEvent.status == "resolved",
        AnomalyEvent.resolved_at >= anchor - dt.timedelta(seconds=window),
    ).limit(1)
    if p.interface_name is None:
        stmt = stmt.where(AnomalyEvent.interface_name.is_(None))
    else:
        stmt = stmt.where(AnomalyEvent.interface_name == p.interface_name)
    return session.scalars(stmt).first() is not None


def run_detection(
    session: Session, *, device_name: str, metric: str,
    interface_name: str | None, data_source: str,
    detectors: tuple[str, ...] | None = None,
    window: int | None = None,
    mad_threshold: float | None = None,
    zscore_threshold: float | None = None,
    device_level_only: bool = False,
) -> dict:
    """Full pipeline for one series: load -> gate -> detect -> persist.

    Refuses to run on data that is not detection-ready and says why -
    never a confident result on thin evidence.
    """
    settings = get_settings()
    frame, quality = load_ready_series(
        session, device_name, metric, interface_name, data_source,
        device_level_only=device_level_only)
    if not quality.ready(data_quality.DETECTION):
        return {"status": "insufficient_data",
                "reasons": quality.reasons_for(data_quality.DETECTION)
                or quality.reasons,
                "points": [], "persisted": {}}
    points = detect_frame(
        frame, device_name=device_name, metric=metric,
        interface_name=interface_name,
        detectors=detectors or ("rolling_mad", "rolling_zscore"),
        window=window or settings.detector_window,
        mad_threshold=mad_threshold or settings.mad_threshold,
        zscore_threshold=zscore_threshold or settings.zscore_threshold,
    )
    persisted = ingest_anomaly_points(session, points)
    # Resolution: the newest REAL observation that no detector flagged is
    # evidence of normality; it resolves open events that ended before it.
    # Missing/stale data never reaches this path (no observation, no proof).
    flagged_ts = {p.timestamp for p in points}
    normal = [ts for ts in frame.loc[frame["value"].notna(),
                                     "collected_at"] if ts not in flagged_ts]
    resolved = 0
    if normal:
        resolved = resolve_events_on_normal_observation(
            session, device_name=device_name, metric=metric,
            interface_name=interface_name, data_source=data_source,
            normal_timestamp=max(normal))
    return {"status": "ok", "points": points, "persisted": persisted,
            "resolved": resolved,
            "quality": quality, "n_anomalies": len(points),
            "n_series_points": int(frame["value"].notna().sum())}


def resolve_events_on_normal_observation(
    session: Session, *, device_name: str, metric: str,
    interface_name: str | None, data_source: str,
    normal_timestamp: dt.datetime,
) -> int:
    """Resolution rule (spec): only an actual normal observation resolves.

    A point is "normal" for this purpose when it is *after* the event's
    last anomaly and was NOT flagged by any currently open detector for
    that key. Missing/stale data never calls this function.
    """
    resolved = 0
    for event in repository.get_anomaly_events(
            session, device_name=device_name, metric=metric,
            data_source=data_source, status="open"):
        if (event.interface_name or None) != (interface_name or None):
            continue
        if event.last_seen_at >= normal_timestamp:
            continue  # observation is not after the anomaly
        repository.resolve_anomaly_event(
            session, event, resolved_at=normal_timestamp)
        resolved += 1
    if resolved:
        session.commit()
    return resolved


def list_events(session: Session, **filters) -> list[AnomalyEvent]:
    return repository.get_anomaly_events(session, **filters)
