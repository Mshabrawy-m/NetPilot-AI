"""Offline tests for the deterministic NL data query (Slice 5b)."""
import datetime as dt

from database import repository
from services import nlquery


def _seed_chain(session):
    # R1 - R2 - R3 linear topology; R3 is down.
    repository.upsert_device(session, "R1", type_="router", status="up")
    repository.upsert_device(session, "R2", type_="router", status="up")
    repository.upsert_device(session, "R3", type_="router", status="down")
    repository.upsert_link(session, "R1", "eth0", "R2", "eth0")
    repository.upsert_link(session, "R2", "eth1", "R3", "eth0")
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    for name, rtt in (("R1", 5.0), ("R2", 40.0), ("R3", 12.0)):
        repository.add_metric_sample(
            session, name, "ping.avg_rtt_ms", rtt, unit="ms",
            data_source="live", collected_at=now)
    session.commit()


def test_devices_down_intent(session):
    _seed_chain(session)
    res = nlquery.answer(session, "which devices are down?", data_source="live")
    assert res["intent"] == "devices_down"
    assert res["understood"]
    assert [r["device"] for r in res["rows"]] == ["R3"]


def test_device_count_intent(session):
    _seed_chain(session)
    res = nlquery.answer(session, "how many devices do we have", data_source="live")
    assert res["intent"] == "device_count"
    assert "3 device" in res["answer"]


def test_reachability_finds_path(session):
    _seed_chain(session)
    res = nlquery.answer(session, "can R1 reach R3?", data_source="live")
    assert res["intent"] == "reachability"
    assert res["entities"][:2] == ["R1", "R3"]
    assert [r["device"] for r in res["rows"]] == ["R1", "R2", "R3"]


def test_spof_detects_middle_node(session):
    _seed_chain(session)
    res = nlquery.answer(session, "what are the single points of failure",
                         data_source="live")
    assert res["intent"] == "spof"
    assert "R2" in [r["name"] for r in res["rows"] if r["kind"] == "device SPOF"]


def test_worst_latency_ranks_by_metric(session):
    _seed_chain(session)
    res = nlquery.answer(session, "which device has the worst latency",
                         data_source="live")
    assert res["intent"] == "metric_extreme"
    assert res["answer"].startswith("worst latency: R2")


def test_unknown_question_is_not_fabricated(session):
    _seed_chain(session)
    res = nlquery.answer(session, "what is the meaning of life", data_source="live")
    assert res["understood"] is False
    assert res["rows"] == []


def test_alerts_intent_with_no_alerts(session):
    _seed_chain(session)
    res = nlquery.answer(session, "are there open alerts", data_source="live")
    assert res["intent"] == "alerts"
    assert res["understood"]
    assert res["rows"] == []
