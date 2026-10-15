"""Tests for device/link records and time-series storage (spec 6)."""
from __future__ import annotations

import datetime as dt
import threading

import pytest

from database import repository


def test_create_and_read_device(session):
    device = repository.upsert_device(
        session, name="R1", type_="router", mgmt_ip="172.20.0.2",
        lab_ip="10.1.1.1", status="up")
    session.commit()
    assert device.id is not None

    devices = repository.get_devices(session)
    assert [d.name for d in devices] == ["R1"]
    assert devices[0].mgmt_ip == "172.20.0.2"
    assert devices[0].status == "up"


def test_upsert_device_is_idempotent(session):
    repository.upsert_device(session, name="R2", type_="router", status="up")
    session.commit()
    repository.upsert_device(session, name="R2", type_="router", status="down")
    session.commit()
    devices = repository.get_devices(session)
    assert len(devices) == 1
    assert devices[0].status == "down"


def test_create_and_read_link(session):
    repository.upsert_device(session, name="R1")
    repository.upsert_device(session, name="R2")
    repository.upsert_link(session, "R1", "eth1", "R2", "eth1",
                           subnet="10.0.12.0/24", status="up")
    session.commit()
    links = repository.get_links(session)
    assert len(links) == 1
    assert links[0].device_a == "R1"
    assert links[0].subnet == "10.0.12.0/24"

    # Upsert must not duplicate.
    repository.upsert_link(session, "R1", "eth1", "R2", "eth1", status="down")
    session.commit()
    assert len(repository.get_links(session)) == 1
    assert repository.get_links(session)[0].status == "down"


def test_metric_sample_time_series_roundtrip(session):
    base = dt.datetime(2026, 1, 1, 12, 0, 0)
    for i in range(10):
        repository.add_metric_sample(
            session, "R1", "ping.avg_rtt_ms", 1.0 + i, unit="ms",
            collected_at=base + dt.timedelta(minutes=i))
    # A null sample (metric unavailable) must be storable and readable.
    repository.add_metric_sample(session, "R1", "ping.avg_rtt_ms", None,
                                 unit="ms", collected_at=base + dt.timedelta(minutes=10))
    session.commit()

    series = repository.get_metric_series(session, "R1", "ping.avg_rtt_ms")
    assert len(series) == 11
    assert series[0].value == 1.0
    assert series[-1].value is None      # availability preserved, not faked

    windowed = repository.get_metric_series(
        session, "R1", "ping.avg_rtt_ms",
        since=base + dt.timedelta(minutes=5),
        until=base + dt.timedelta(minutes=8))
    assert [s.value for s in windowed] == [6.0, 7.0, 8.0, 9.0]


def test_available_metrics_excludes_all_null(session):
    repository.add_metric_sample(session, "R1", "frr.routes_total", None,
                                 unit="count")
    repository.add_metric_sample(session, "R1", "ping.avg_rtt_ms", 2.5,
                                 unit="ms")
    session.commit()
    metrics = repository.get_available_metrics(session, "R1")
    names = {m for _, m, _, _ in metrics}
    assert names == {"ping.avg_rtt_ms"}   # no data -> not offered in UI


def test_latest_metric_values_per_interface(session):
    base = dt.datetime(2026, 1, 1)
    repository.add_metric_sample(session, "R2", "if.rx_bytes", 100.0,
                                 interface_name="eth1",
                                 collected_at=base)
    repository.add_metric_sample(session, "R2", "if.rx_bytes", 250.0,
                                 interface_name="eth1",
                                 collected_at=base + dt.timedelta(minutes=1))
    repository.add_metric_sample(session, "R2", "if.rx_bytes", 50.0,
                                 interface_name="eth2",
                                 collected_at=base)
    session.commit()
    latest = repository.latest_metric_values(session, "R2")
    by_iface = {m.interface_name: m.value for m in latest}
    assert by_iface == {"eth1": 250.0, "eth2": 50.0}


def test_connectivity_test_records(session):
    repository.add_connectivity_test(
        session, source="collector-host", destination="R1", test_type="ping",
        success=True, avg_latency_ms=1.2, packet_loss_pct=0.0,
        data_source="live")
    repository.add_connectivity_test(
        session, source="collector-host", destination="PC1", test_type="ping",
        success=False, detail="timed out", data_source="live")
    session.commit()
    history = repository.get_connectivity_history(session)
    assert len(history) == 2
    failed = [h for h in history if not h.success]
    assert failed[0].destination == "PC1"
    assert failed[0].avg_latency_ms is None


def test_reset_removes_all_rows(session):
    repository.upsert_device(session, name="R9")
    repository.add_metric_sample(session, "R9", "m", 1.0)
    session.commit()
    from database.connection import reset_lab_data
    reset_lab_data()
    assert repository.get_devices(session) == []
    assert repository.get_metric_series(session, "R9", "m") == []


# --------------------------------------------------------------------------
# init_db() robustness (Streamlit Community Cloud boots several sessions at
# once; create_all is a non-atomic check-then-create).
# --------------------------------------------------------------------------
def test_init_db_is_safe_under_concurrent_callers(tmp_path, monkeypatch):
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "false")
    monkeypatch.setenv("DATABASE_URL",
                       f"sqlite:///{(tmp_path / 'race.db').as_posix()}")
    from database import connection

    errors: list[BaseException] = []

    def worker() -> None:
        try:
            connection.init_db()
        except BaseException as exc:  # pragma: no cover - regression only
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, f"concurrent init_db raised: {errors!r}"


def test_init_db_retries_past_already_exists(tmp_path, monkeypatch):
    """A writer that wins the race must not fail the loser's page render."""
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "false")
    monkeypatch.setenv("DATABASE_URL",
                       f"sqlite:///{(tmp_path / 'retry.db').as_posix()}")
    from sqlalchemy.exc import OperationalError
    from database import connection
    from database.models import Base

    real_create_all = Base.metadata.create_all
    calls = {"n": 0}

    def flaky_create_all(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("CREATE TABLE devices", {},
                                   Exception("table devices already exists"))
        return real_create_all(*args, **kwargs)

    monkeypatch.setattr(Base.metadata, "create_all", flaky_create_all)
    connection.init_db()            # swallows the race, then succeeds
    assert calls["n"] == 2

    # A genuine error must still propagate rather than be masked as a race.
    def boom(*args, **kwargs):
        raise OperationalError("CREATE TABLE devices", {},
                               Exception("attempt to write a readonly database"))

    monkeypatch.setattr(Base.metadata, "create_all", boom)
    with pytest.raises(OperationalError):
        connection.init_db()
