"""Tests for the v2 alerting engine (analytics.alerts + repository layer).

Covers the pure matcher and the persisted lifecycle: rule creation, firing,
de-duplication within a window, acknowledge/resolve, and the hard rule that
live and demo provenance never merge into one event.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from analytics import alerts
from database import repository


# --------------------------------------------------------------------------
# Pure evaluation
# --------------------------------------------------------------------------
def _rule(**kw):
    base = dict(name="r", metric="ping.avg_rtt_ms", device_name=None,
                interface_name=None, condition="above", threshold=100.0,
                severity="warning", enabled=True)
    base.update(kw)
    return SimpleNamespace(**base)


def _sample(**kw):
    base = dict(device_name="R1", interface_name=None, metric="ping.avg_rtt_ms",
                value=150.0, data_source="live")
    base.update(kw)
    return SimpleNamespace(**base)


def test_matches_above_and_below():
    assert alerts.matches(_rule(condition="above", threshold=100), 150) is True
    assert alerts.matches(_rule(condition="above", threshold=100), 90) is False
    assert alerts.matches(_rule(condition="below", threshold=100), 50) is True
    assert alerts.matches(_rule(condition="below", threshold=100), 150) is False


def test_matches_none_value_is_false():
    assert alerts.matches(_rule(), None) is False


def test_evaluate_respects_scope_metric_and_disabled():
    rule = _rule(device_name="R1", metric="ping.avg_rtt_ms")
    firings = alerts.evaluate([rule], [
        _sample(device_name="R1", value=150),          # fires
        _sample(device_name="R2", value=150),          # out of device scope
        _sample(metric="if.rx_bytes", value=150),      # metric mismatch
        _sample(device_name="R1", value=50),           # below threshold
    ])
    assert len(firings) == 1
    assert firings[0]["sample"].device_name == "R1"


def test_evaluate_skips_disabled_rule():
    rule = _rule(enabled=False)
    assert alerts.evaluate([rule], [_sample(value=999)]) == []


def test_summarize_is_bounded_and_readable():
    s = alerts.summarize(_rule(condition="above", threshold=100),
                         _sample(value=150))
    assert "R1" in s and "150" in s and len(s) <= 300


# --------------------------------------------------------------------------
# Persistence + lifecycle (real DB via session fixture)
# --------------------------------------------------------------------------
def _now():
    return dt.datetime(2026, 10, 13, 12, 0, 0)


def test_create_rule_validates_condition(session):
    with pytest.raises(ValueError):
        repository.create_alert_rule(
            session, name="bad", metric="m", condition="sideways", threshold=1)


def test_fire_deduplicates_within_window(session):
    rule = repository.create_alert_rule(
        session, name="High RTT", metric="ping.avg_rtt_ms",
        device_name="R1", condition="above", threshold=100, severity="warning")
    session.commit()

    s1 = repository.add_metric_sample(session, device_name="R1",
                                      metric="ping.avg_rtt_ms", value=150,
                                      data_source="live")
    s2 = repository.add_metric_sample(session, device_name="R1",
                                      metric="ping.avg_rtt_ms", value=180,
                                      data_source="live")
    session.commit()

    t = _now()
    e1 = repository.record_alert_firing(session, rule, s1, fired_at=t,
                                        summary="first")
    e2 = repository.record_alert_firing(session, rule, s2,
                                        fired_at=t + dt.timedelta(minutes=5),
                                        summary="second")
    session.commit()

    # Same event reused, occurrence count and latest value refreshed.
    assert e1.id == e2.id
    assert e2.occurrences == 2
    assert e2.value == 180
    assert len(repository.get_alert_events(session)) == 1


def test_new_event_after_window_expiry(session):
    rule = repository.create_alert_rule(
        session, name="High RTT", metric="ping.avg_rtt_ms",
        condition="above", threshold=100)
    s = repository.add_metric_sample(session, device_name="R1",
                                     metric="ping.avg_rtt_ms", value=150,
                                     data_source="live")
    session.commit()
    t = _now()
    repository.record_alert_firing(session, rule, s, fired_at=t,
                                   window_minutes=60)
    repository.record_alert_firing(session, rule, s,
                                   fired_at=t + dt.timedelta(minutes=90),
                                   window_minutes=60)
    session.commit()
    assert len(repository.get_alert_events(session)) == 2


def test_provenance_never_merges_demo_and_live(session):
    rule = repository.create_alert_rule(
        session, name="High RTT", metric="ping.avg_rtt_ms",
        device_name="R1", condition="above", threshold=100)
    live = repository.add_metric_sample(session, device_name="R1",
                                        metric="ping.avg_rtt_ms", value=150,
                                        data_source="live")
    demo = repository.add_metric_sample(session, device_name="R1",
                                        metric="ping.avg_rtt_ms", value=160,
                                        data_source="demo")
    session.commit()
    t = _now()
    repository.record_alert_firing(session, rule, live, fired_at=t)
    repository.record_alert_firing(session, rule, demo, fired_at=t)
    session.commit()
    events = repository.get_alert_events(session)
    assert len(events) == 2
    assert {e.data_source for e in events} == {"live", "demo"}


def test_acknowledge_and_resolve_lifecycle(session):
    rule = repository.create_alert_rule(
        session, name="Down", metric="device.status", device_name="R1",
        condition="below", threshold=1, severity="critical")
    s = repository.add_metric_sample(session, device_name="R1",
                                     metric="device.status", value=0,
                                     data_source="live")
    session.commit()
    t = _now()
    ev = repository.record_alert_firing(session, rule, s, fired_at=t)
    session.commit()
    assert repository.count_open_alerts(session) == 1

    repository.acknowledge_alert_event(session, ev, t + dt.timedelta(minutes=1))
    session.commit()
    assert repository.count_open_alerts(session) == 1  # ack still open

    repository.resolve_alert_event(session, ev, t + dt.timedelta(minutes=2))
    session.commit()
    assert repository.count_open_alerts(session) == 0
    refreshed = repository.get_alert_events(session)[0]
    assert refreshed.status == "resolved"
    assert refreshed.resolved_at is not None


def test_service_run_cycle_end_to_end(session):
    from services import alert_service
    repository.upsert_device(session, name="R1", type_="router", status="up")
    repository.create_alert_rule(
        session, name="High RTT", metric="ping.avg_rtt_ms",
        device_name="R1", condition="above", threshold=100, severity="warning")
    repository.add_metric_sample(session, device_name="R1",
                                 metric="ping.avg_rtt_ms", value=200,
                                 data_source="live")
    session.commit()

    touched = alert_service.run_cycle(session, data_source="live",
                                      fired_at=_now())
    assert len(touched) == 1
    assert repository.count_open_alerts(session) == 1
    # Re-running the cycle de-duplicates into the same open event.
    alert_service.run_cycle(session, data_source="live",
                            fired_at=_now() + dt.timedelta(minutes=1))
    events = repository.get_alert_events(session)
    assert len(events) == 1
    assert events[0].occurrences == 2
