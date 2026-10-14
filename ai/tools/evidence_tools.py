"""Analytics evidence (anomalies, forecasts, incidents, collection errors)
plus the bounded ``gather_evidence`` aggregator that assigns stable ``uid``
references the diagnosis/plan cite. All reads are source-filtered.
"""
from __future__ import annotations

import datetime as dt

from ai.tools import (connectivity_tools, telemetry_tools, topology_tools)
from ai.tools._common import make_record
from config import get_settings
from database import repository


def anomaly_records(session, *, data_source: str, now: dt.datetime,
                    max_rows: int, max_bytes: int,
                    device_name: str | None = None) -> list[dict]:
    events = repository.get_anomaly_events(
        session, data_source=data_source, device_name=device_name,
        limit=max_rows)
    out = []
    for e in events:
        out.append(make_record(
            kind="anomaly", tool="evidence_anomalies", data_source=data_source,
            device_name=e.device_name, observed_at=e.last_seen_at,
            content=(f"anomaly {e.severity}/{e.status}: {e.metric} on "
                     f"{e.device_name} ({e.detector}); {e.explanation or ''}"),
            source_ref=[f"anomaly_event:{e.id}"], max_bytes=max_bytes))
    return out


def forecast_records(session, *, data_source: str, now: dt.datetime,
                     max_rows: int, max_bytes: int) -> list[dict]:
    runs = repository.get_forecast_runs(
        session, data_source=data_source, limit=max_rows)
    out = []
    for r in runs:
        out.append(make_record(
            kind="forecast", tool="evidence_forecasts", data_source=data_source,
            device_name=r.device_name, observed_at=r.created_at,
            content=(f"forecast {r.model_name} {r.metric}: status={r.status} "
                     f"mae={r.mae} rmse={r.rmse} horizon={r.horizon_steps}"),
            source_ref=[f"forecast_run:{r.id}"], max_bytes=max_bytes))
    return out


def incident_records(session, *, now: dt.datetime, max_rows: int,
                     max_bytes: int) -> list[dict]:
    out = []
    for i in repository.get_open_incidents(session)[:max_rows]:
        out.append(make_record(
            kind="incident", tool="evidence_incidents", data_source="live",
            device_name=i.device_name, observed_at=i.opened_at,
            content=f"open incident {i.kind}: {i.detail or ''}",
            source_ref=[f"incident:{i.id}"], max_bytes=max_bytes))
    return out


def collection_error_records(session, *, now: dt.datetime, max_rows: int,
                             max_bytes: int) -> list[dict]:
    out = []
    for r in repository.get_recent_runs(session, limit=max_rows):
        if r.status not in ("error", "partial"):
            continue
        out.append(make_record(
            kind="collection_error", tool="evidence_collection",
            data_source=r.data_source, observed_at=r.finished_at or now,
            content=(f"collection run {r.status}: "
                     f"{(r.error_message or '')[:200]}"),
            source_ref=[f"collection_run:{r.id}"], max_bytes=max_bytes))
    return out


def known_devices(session) -> set[str]:
    """Device names the lab/app actually knows (from the persisted inventory).

    Used by the Config Agent and the deterministic verifier to reject unknown
    targets. Never trusts a name that only appears in LLM text or logs."""
    return {d.name for d in repository.get_devices(session)}


def gather_evidence(session, *, data_source: str,
                    intent: dict | None = None,
                    now: dt.datetime | None = None,
                    max_rows: int | None = None,
                    max_bytes: int | None = None) -> list[dict]:
    """Compose bounded, provenance-tagged evidence for one run.

    The tools enforce their own limits (rows/bytes) and the total list is
    capped again here; every record receives a stable ``uid`` (ev-1…)."""
    settings = get_settings()
    now = now or dt.datetime.utcnow()
    max_rows = max_rows or settings.ai_evidence_max_rows
    max_bytes = max_bytes or settings.ai_evidence_max_bytes
    targets = {t for t in (intent or {}).get("targets", []) if isinstance(t, str)}

    records: list[dict] = []
    records += topology_tools.device_status_records(
        session, data_source=data_source, now=now, max_rows=max_rows,
        max_bytes=max_bytes)
    records += topology_tools.link_records(
        session, data_source=data_source, now=now, max_rows=max_rows,
        max_bytes=max_bytes)
    records += connectivity_tools.recent_tests(
        session, data_source=data_source, now=now, max_rows=max_rows,
        max_bytes=max_bytes)
    records += anomaly_records(
        session, data_source=data_source, now=now, max_rows=max_rows,
        max_bytes=max_bytes)
    records += incident_records(session, now=now, max_rows=max_rows,
                                max_bytes=max_bytes)
    records += collection_error_records(session, now=now, max_rows=max_rows,
                                        max_bytes=max_bytes)
    # Per-target telemetry (only for devices the lab actually has).
    valid = known_devices(session)
    for dev in sorted(targets & valid)[:max_rows]:
        records += telemetry_tools.latest_metrics(
            session, device_name=dev, data_source=data_source, now=now,
            max_rows=max_rows, max_bytes=max_bytes)

    # Global cap + uid assignment.
    records = records[:max_rows] if max_rows else records
    for i, rec in enumerate(records, start=1):
        rec["uid"] = f"ev-{i}"
    return records
