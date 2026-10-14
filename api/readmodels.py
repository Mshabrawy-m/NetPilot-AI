"""Read-only view models for the REST/WS API (Slice 6).

Kept as plain functions over a SQLAlchemy session so they are directly
unit-testable without HTTP, and shared by both the REST routes and the
WebSocket stream. Every value is derived from stored rows - nothing is
invented - and datetimes are serialised to ISO-8601 UTC strings.
"""
from __future__ import annotations

from analytics import graph
from database import repository
from services import nlquery

API_VERSION = "1"


def _iso(x) -> str | None:
    return x.isoformat() if x is not None else None


def health(session) -> dict:
    from database.connection import check_connection
    from config import get_settings
    ok, detail = check_connection()
    return {
        "status": "ok" if ok else "degraded",
        "database": detail,
        "api_version": API_VERSION,
        "demo_mode": get_settings().demo_mode,
    }


def devices(session) -> dict:
    rows = repository.get_devices(session)
    return {
        "count": len(rows),
        "items": [{
            "name": d.name, "type": d.type, "status": d.status,
            "lab_ip": d.lab_ip, "mgmt_ip": d.mgmt_ip,
            "container": d.container, "updated_at": _iso(d.updated_at),
        } for d in rows],
    }


def device_detail(session, name: str, data_source: str = "live") -> dict:
    d = repository.get_device_by_name(session, name)
    if d is None:
        return {"found": False, "name": name}
    latest = repository.latest_metric_values_for_source(session, name, data_source)
    return {
        "found": True,
        "device": {
            "name": d.name, "type": d.type, "status": d.status,
            "lab_ip": d.lab_ip, "mgmt_ip": d.mgmt_ip,
            "updated_at": _iso(d.updated_at),
        },
        "data_source": data_source,
        "metrics": [{
            "metric": r.metric, "interface": r.interface_name,
            "value": r.value, "unit": r.unit,
            "collected_at": _iso(r.collected_at)} for r in latest],
    }


def alerts(session, status: str | None = "open",
           data_source: str | None = None) -> dict:
    events = repository.get_alert_events(
        session, status=status, data_source=data_source, limit=200)
    return {
        "count": len(events),
        "open_total": repository.count_open_alerts(session),
        "items": [{
            "id": e.id, "rule": e.rule_name, "device": e.device_name,
            "interface": e.interface_name, "metric": e.metric,
            "value": e.value, "threshold": e.threshold,
            "condition": e.condition, "severity": e.severity,
            "status": e.status, "data_source": e.data_source,
            "first_fired_at": _iso(e.first_fired_at),
            "last_fired_at": _iso(e.last_fired_at),
            "occurrences": e.occurrences,
        } for e in events],
    }


def topology(session) -> dict:
    g = graph.build_from_session(session)
    return {"connectivity": graph.connectivity_summary(g),
            "links": [{"a": l.device_a, "a_if": l.if_a,
                       "b": l.device_b, "b_if": l.if_b,
                       "subnet": l.subnet, "status": l.status}
                      for l in repository.get_links(session)]}


def risk(session) -> dict:
    g = graph.build_from_session(session)
    return graph.single_points_of_failure(g)


def ask(session, question: str, data_source: str = "live") -> dict:
    return nlquery.answer(session, question, data_source=data_source)
