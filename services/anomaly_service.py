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

Three orchestration helpers sit on top of that pipeline:

* ``scan_ready_series`` - one bounded batch run over the stored inventory
  (capped by ``NETPILOT_SCAN_MAX_SERIES``, each series still gated);
* ``event_summary`` - aggregate open/resolved/severity/detector/duration view
  of the event store;
* ``calibrate_detectors`` / ``calibrate_series`` - score detector sensitivities
  against labels the caller supplies. Only meaningful on documented synthetic
  fixtures: no live network provides anomaly labels, so the returned rows carry
  that caveat next to every precision/recall number.
"""
from __future__ import annotations

import datetime as dt
import json

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from analytics import data_quality, evaluation
from analytics.anomaly_detection import (
    DEFAULT_DETECTORS, DETECTORS, INTERVAL_REQUIRED, AnomalyPoint,
    detect_frame)
from analytics.time_series import dedupe_frame, load_metric_frame
from config import get_settings
from database import repository
from database.models import AnomalyEvent, db_utcnow

#: How far back a detection run looks at most (bounded query, spec §12).
DETECTION_LOOKBACK_LIMIT = 5000

#: Candidate sensitivity values explored by ``calibrate_detectors`` when the
#: caller does not supply its own grid. Presented as a search space, never as
#: a recommendation: the labels decide, this list only bounds the work.
CALIBRATION_GRID = (2.0, 2.5, 3.0, 3.5, 4.0, 5.0)

#: Which ``detect_frame`` keyword carries each detector's sensitivity, so the
#: calibration pass can retune any detector without special-casing its name.
THRESHOLD_KWARG = {
    "rolling_mad": "mad_threshold",
    "rolling_zscore": "zscore_threshold",
    "ewma": "ewma_threshold",
    "cusum": "cusum_limit",
    "seasonal_residual": "seasonal_threshold",
}


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
    ewma_threshold: float | None = None,
    cusum_drift: float | None = None,
    cusum_limit: float | None = None,
    seasonal_threshold: float | None = None,
    device_level_only: bool = False,
) -> dict:
    """Full pipeline for one series: load -> gate -> detect -> persist.

    Refuses to run on data that is not detection-ready and says why - never a
    confident result on thin evidence. A detector that *cannot* form a verdict on
    this series (the seasonal one without a known sampling interval) is reported
    as an explicit abstention in ``notes``: "nothing flagged" and "no opinion"
    are different statements and are never conflated here.
    """
    settings = get_settings()
    chosen = tuple(detectors) if detectors else DEFAULT_DETECTORS
    unknown = [d for d in chosen if d not in DETECTORS]
    if unknown:
        return {"status": "unknown_detector", "reasons":
                [f"unknown detector(s): {', '.join(unknown)}. "
                 f"available: {', '.join(sorted(DETECTORS))}"],
                "points": [], "persisted": {}, "notes": []}
    frame, quality = load_ready_series(
        session, device_name, metric, interface_name, data_source,
        device_level_only=device_level_only)
    interval = quality.observed_interval_seconds
    notes: list[str] = []
    if not quality.ready(data_quality.DETECTION):
        return {"status": "insufficient_data",
                "reasons": quality.reasons_for(data_quality.DETECTION)
                or quality.reasons,
                "points": [], "persisted": {}, "notes": notes,
                "detectors": list(chosen)}
    for d in chosen:
        if d in INTERVAL_REQUIRED and not interval:
            notes.append(
                f"{d}: abstained - the sampling interval is unknown here, so "
                "no seasonal baseline is defensible (this is not a clean "
                "result)")
    points = detect_frame(
        frame, device_name=device_name, metric=metric,
        interface_name=interface_name,
        detectors=chosen,
        window=window or settings.detector_window,
        mad_threshold=mad_threshold or settings.mad_threshold,
        zscore_threshold=zscore_threshold or settings.zscore_threshold,
        ewma_threshold=ewma_threshold or settings.ewma_threshold,
        cusum_drift=cusum_drift or settings.cusum_drift_k,
        cusum_limit=cusum_limit or settings.cusum_threshold_h,
        seasonal_threshold=seasonal_threshold or settings.mad_threshold,
        interval_seconds=interval,
        season_hours=settings.season_hours,
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
    by_detector: dict[str, int] = {}
    for p in points:
        by_detector[p.detector] = by_detector.get(p.detector, 0) + 1
    return {"status": "ok", "points": points, "persisted": persisted,
            "resolved": resolved,
            "quality": quality, "n_anomalies": len(points),
            "n_series_points": int(frame["value"].notna().sum()),
            "detectors": list(chosen), "by_detector": by_detector,
            "interval_seconds": interval, "notes": notes}


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


# ------------------------------------------------------------ batch scanning
def scan_ready_series(
    session: Session, *, data_source: str,
    device_name: str | None = None,
    detectors: tuple[str, ...] | None = None,
    window: int | None = None,
    max_series: int | None = None,
    min_samples: int | None = None,
) -> dict:
    """Bounded batch detection over the stored series of one data source.

    The work is capped twice on purpose: the inventory is enumerated by
    ``repository.get_series_inventory`` (so the loop is over what actually
    exists, never over a Cartesian product of devices and metrics) and then
    truncated to ``settings.scan_max_series`` series per call. Every series goes
    through the same readiness gate as an interactive run, so thin series are
    reported as skipped with their reasons instead of being judged anyway.

    Only device-level series (``interface_name`` NULL) are scanned: an interface
    fan-out would multiply the same click by every port of every device, and the
    per-interface series remain available through the interactive run.
    """
    settings = get_settings()
    cap = int(max_series or settings.scan_max_series)
    cap = max(1, min(cap, settings.scan_max_series))
    floor = int(min_samples or settings.min_points_detection)
    inventory = [row for row in repository.get_series_inventory(
        session, data_source=data_source, device_name=device_name,
        min_samples=floor)
        if row["interface_name"] is None]
    candidates = len(inventory)
    targets = inventory[:cap]
    results: list[dict] = []
    skipped: list[dict] = []
    totals = {"created": 0, "updated": 0, "skipped": 0, "resolved": 0,
              "points": 0, "series_ok": 0}
    by_detector: dict[str, int] = {}
    notes: list[str] = []
    for row in targets:
        res = run_detection(
            session, device_name=row["device_name"], metric=row["metric"],
            interface_name=None, data_source=data_source,
            detectors=detectors, window=window, device_level_only=True)
        key = f"{row['device_name']}/{row['metric']}"
        if res["status"] != "ok":
            skipped.append({"series": key, "status": res["status"],
                            "reasons": res.get("reasons") or []})
            continue
        totals["series_ok"] += 1
        for name in ("created", "updated", "skipped"):
            totals[name] += int((res["persisted"] or {}).get(name, 0))
        totals["resolved"] += int(res.get("resolved") or 0)
        totals["points"] += len(res["points"])
        for det, n in (res.get("by_detector") or {}).items():
            by_detector[det] = by_detector.get(det, 0) + n
        for note in res.get("notes") or []:
            notes.append(f"{key}: {note}")
        results.append({
            "series": key, "n_points": len(res["points"]),
            "n_samples": res["n_series_points"],
            "by_detector": res.get("by_detector") or {},
            "persisted": res["persisted"], "resolved": res.get("resolved"),
        })
    if candidates > cap:
        notes.append(
            f"scan capped at {cap} of {candidates} ready series "
            f"(NETPILOT_SCAN_MAX_SERIES): {candidates - cap} series were not "
            "evaluated by this click")
    return {"status": "ok", "data_source": data_source,
            "scanned": totals["series_ok"], "capped": candidates > cap,
            "candidates": candidates, "limit": cap,
            "results": results, "skipped": skipped,
            "by_detector": by_detector, "totals": totals, "notes": notes}


# --------------------------------------------------------------- event report
def event_summary(
    session: Session, *, data_source: str | None = None,
    device_name: str | None = None, metric: str | None = None,
    detector: str | None = None, since: dt.datetime | None = None,
    limit: int = 1000,
) -> dict:
    """Aggregate view of the event store (events, not raw points).

    Answers "how much is open, how severe, which detector and which metric, and
    how long do episodes actually last". Durations are measured per event as
    ``last_seen_at - first_seen_at`` and resolution as
    ``resolved_at - first_seen_at``; an event that is still open contributes to
    the open set and never to the resolution statistics (no guessed "still
    ongoing = resolved" numbers).
    """
    events = repository.get_anomaly_events(
        session, device_name=device_name, metric=metric, detector=detector,
        data_source=data_source, since=since, limit=limit)
    by_severity: dict[str, int] = {}
    by_status: dict[str, int] = {}
    by_detector: dict[str, dict[str, int]] = {}
    by_metric: dict[str, dict[str, int]] = {}
    recurring: list[dict] = []
    durations: list[float] = []
    resolutions: list[float] = []
    occurrences = 0
    newest_open: dt.datetime | None = None
    for e in events:
        by_severity[e.severity] = by_severity.get(e.severity, 0) + 1
        by_status[e.status] = by_status.get(e.status, 0) + 1
        occurrences += int(e.occurrences or 1)
        for bucket, key in ((by_detector, e.detector), (by_metric, e.metric)):
            cell = bucket.setdefault(key, {"events": 0, "open": 0,
                                           "occurrences": 0})
            cell["events"] += 1
            cell["occurrences"] += int(e.occurrences or 1)
            if e.status == "open":
                cell["open"] += 1
        if e.status == "open":
            if newest_open is None or e.last_seen_at > newest_open:
                newest_open = e.last_seen_at
        episode = (e.last_seen_at - e.first_seen_at).total_seconds()
        durations.append(max(0.0, episode))
        if e.status == "resolved" and e.resolved_at is not None:
            resolutions.append(
                max(0.0, (e.resolved_at - e.first_seen_at).total_seconds()))
        if int(e.occurrences or 1) >= 2:
            recurring.append({
                "device_name": e.device_name, "metric": e.metric,
                "interface_name": e.interface_name, "detector": e.detector,
                "severity": e.severity, "status": e.status,
                "occurrences": int(e.occurrences or 1),
                "first_seen_at": e.first_seen_at, "last_seen_at": e.last_seen_at,
            })
    recurring.sort(key=lambda r: -r["occurrences"])
    return {
        "n_events": len(events),
        "truncated": len(events) >= limit,
        "by_severity": by_severity,
        "by_status": by_status,
        "open": by_status.get("open", 0),
        "resolved": by_status.get("resolved", 0),
        "critical_open": sum(1 for e in events
                             if e.status == "open" and e.severity == "critical"),
        "total_occurrences": occurrences,
        "by_detector": by_detector,
        "by_metric": by_metric,
        "top_recurring": recurring[:10],
        "median_episode_seconds": _median(durations),
        "max_episode_seconds": max(durations) if durations else None,
        "median_resolution_seconds": _median(resolutions),
        "newest_open_at": newest_open,
        "filters": {"device_name": device_name, "metric": metric,
                    "detector": detector, "data_source": data_source,
                    "since": since},
    }


def _median(values: list[float]) -> float | None:
    """Median of a list, None when it is empty (never 0 for 'no data')."""
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


# ------------------------------------------------------------- calibration
def calibrate_detectors(
    frame: pd.DataFrame, *, device_name: str, metric: str,
    interface_name: str | None = None,
    labelled_anomalies, detectors: tuple[str, ...] = DEFAULT_DETECTORS,
    thresholds=None, window: int | None = None,
    interval_seconds: float | None = None,
    season_hours: float | None = None,
) -> list[dict]:
    """Score detector sensitivities against LABELLED ground truth.

    This is calibration on labels the caller owns - in practice a documented
    synthetic fixture (``evaluation/`` or a test). No live network has anomaly
    labels here, so nothing returned may be presented as real-world accuracy;
    the returned rows carry that warning themselves because a number that looks
    like precision is the dangerous part.

    Each ``detector``/``threshold`` pair is scored with
    ``analytics.evaluation.score_detection`` (point matching on timestamps).
    A detector that produced no verdict at any threshold - typically
    ``seasonal_residual`` without a sampling interval - is reported with
    ``scoreable: False`` and its abstention note instead of a perfect-looking
    0.0 precision.
    """
    labels = set(pd.to_datetime(pd.Series(list(labelled_anomalies or []))).tolist())
    grid = tuple(thresholds) if thresholds else CALIBRATION_GRID
    window = window or get_settings().detector_window
    season_hours = season_hours or get_settings().season_hours
    normal_count = int(frame["value"].notna().sum()) - len(labels)
    rows: list[dict] = []
    for name in detectors:
        if name not in DETECTORS:
            rows.append({"detector": name, "threshold": None,
                         "scoreable": False, "f1": None,
                         "note": "unknown detector - not available in "
                                 f"{sorted(DETECTORS)}"})
            continue
        kwarg = THRESHOLD_KWARG.get(name)
        if kwarg is None:
            rows.append({"detector": name, "threshold": None,
                         "scoreable": False, "f1": None,
                         "note": "detector has no single sensitivity knob to "
                                 "calibrate; its configuration is fixed"})
            continue
        scored_any = False
        for value in grid:
            points = detect_frame(
                frame, device_name=device_name, metric=metric,
                interface_name=interface_name, detectors=(name,),
                window=window, interval_seconds=interval_seconds,
                season_hours=season_hours, **{kwarg: float(value)})
            flagged = {p.timestamp for p in points}
            score = evaluation.score_detection(flagged, labels,
                                               total_normal=max(0, normal_count))
            rows.append({
                "detector": name, "threshold": float(value),
                "scoreable": True,
                "true_positives": score.true_positives,
                "false_positives": score.false_positives,
                "false_negatives": score.false_negatives,
                "precision": score.precision, "recall": score.recall,
                "f1": score.f1, "false_alarm_rate": score.false_alarm_rate,
                "n_flagged": len(flagged),
                "labels_available": bool(labels),
                "note": ("scored against caller-supplied labels only; no live "
                         "network labels exist, so this is not real-world "
                         "accuracy") if labels else
                        "no labels supplied: counts only, no precision/recall",
            })
            scored_any = scored_any or bool(flagged)
        if not scored_any and name in INTERVAL_REQUIRED and not interval_seconds:
            for r in rows:
                if r["detector"] == name:
                    r["scoreable"] = False
                    r["note"] = ("abstained at every threshold: the sampling "
                                 "interval is unknown, so this detector has no "
                                 "opinion (not a clean result)")
    ranked = sorted((r for r in rows if r.get("scoreable")
                     and r.get("f1") is not None),
                    key=lambda r: (-r["f1"], r["false_positives"],
                                   r["threshold"]))
    for i, r in enumerate(ranked, start=1):
        r["rank"] = i
    return rows


def calibrate_series(
    session: Session, *, device_name: str, metric: str,
    interface_name: str | None, data_source: str,
    labelled_anomalies, detectors: tuple[str, ...] = DEFAULT_DETECTORS,
    thresholds=None, window: int | None = None,
    device_level_only: bool = False,
) -> dict:
    """Load one ready series and calibrate detectors on its labels."""
    frame, quality = load_ready_series(
        session, device_name, metric, interface_name, data_source,
        purpose=data_quality.DETECTION, device_level_only=device_level_only)
    if not quality.ready(data_quality.DETECTION):
        return {"status": "insufficient_data",
                "reasons": quality.reasons_for(data_quality.DETECTION)
                or quality.reasons, "rows": []}
    rows = calibrate_detectors(
        frame, device_name=device_name, metric=metric,
        interface_name=interface_name,
        labelled_anomalies=labelled_anomalies, detectors=detectors,
        thresholds=thresholds, window=window,
        interval_seconds=quality.observed_interval_seconds)
    best = sorted((r for r in rows if r.get("scoreable")
                   and r.get("f1") is not None),
                  key=lambda r: (-r["f1"], r["false_positives"]))
    return {"status": "ok", "rows": rows,
            "best": best[0] if best else None,
            "quality": quality,
            "interval_seconds": quality.observed_interval_seconds}
