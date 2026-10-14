"""Alert service - run a rule-evaluation cycle (v2 Slice 1).

Thin orchestration between the pure matcher (``analytics.alerts``) and the
data layer (``database.repository``). For one ``data_source`` it loads the
enabled rules, reads each device's latest per-source metric values (using the
F-2 source-scoped helper so live/demo provenance never collapses), records
every firing with de-duplication, and returns the touched events.

Contains no SQL itself - all data access goes through the repository.
"""
from __future__ import annotations

import datetime as dt

from analytics import alerts
from database import repository


def run_cycle(session, *, data_source: str = "live",
              fired_at: dt.datetime | None = None,
              window_minutes: int = 60) -> list:
    """Evaluate all enabled rules against the latest telemetry.

    Returns the list of AlertEvent rows that fired (created or refreshed).
    Commits once at the end so the caller controls the transaction boundary.
    """
    fired_at = fired_at or repository.db_utcnow()
    rules = repository.get_alert_rules(session, enabled_only=True)
    if not rules:
        return []

    touched: list = []
    for device in repository.get_devices(session):
        samples = repository.latest_metric_values_for_source(
            session, device.name, data_source)
        for firing in alerts.evaluate(rules, samples):
            summary = alerts.summarize(firing["rule"], firing["sample"])
            event = repository.record_alert_firing(
                session, firing["rule"], firing["sample"],
                fired_at=fired_at, summary=summary,
                window_minutes=window_minutes)
            touched.append(event)
    session.commit()
    return touched
