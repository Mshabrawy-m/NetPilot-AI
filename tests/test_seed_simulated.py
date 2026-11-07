"""Tests for the one-command simulated-source seed tool.

The seed reuses the AI Lab simulator and writes through the same tables as the
native collector. Every structural row must carry the reserved ``sim:``
namespace and every measurement must be tagged ``data_source='simulated'`` -
never ``live``. Re-running the tool is a no-op for measurements.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from analytics import lab_simulator
from database import connection, repository, source_scope
from database.models import Incident
from network.device_inventory import load_inventory
from tools import seed_simulated


def _dataset() -> lab_simulator.LabDataset:
    return lab_simulator.simulate(
        duration_hours=0.1, interval_seconds=60, seed=11, events={},
        start=dt.datetime(2026, 1, 1))


def test_seed_writes_only_labelled_simulated_rows(db_url):
    session = connection.get_session()
    try:
        summary = seed_simulated.seed_simulated(
            session, dataset=_dataset(), inventory=load_inventory())
        session.commit()

        assert summary["skipped"] is False
        assert summary["metrics"] > 0
        assert summary["devices"] == len(load_inventory().nodes)
        assert summary["links"] > 0

        devices = list(repository.get_devices(session))
        assert devices
        assert all(source_scope.is_simulated_name(d.name) for d in devices)
        assert source_scope.filter_devices(devices, source_scope.LIVE) == []

        links = list(repository.get_links(session))
        assert links
        assert all(source_scope.is_simulated_name(link.device_a)
                   and source_scope.is_simulated_name(link.device_b)
                   for link in links)

        sources = {source for (_d, _m, _u, source)
                   in repository.get_available_metrics(session)}
        assert sources == {source_scope.SIMULATED}

        runs = repository.get_recent_runs(session, limit=10)
        assert [run.data_source for run in runs] == [source_scope.SIMULATED]
    finally:
        session.close()


def test_seed_is_idempotent_for_measurements(db_url):
    session = connection.get_session()
    try:
        first = seed_simulated.seed_simulated(
            session, dataset=_dataset(), inventory=load_inventory())
        session.commit()
        before = len(repository.get_available_metrics(session))

        second = seed_simulated.seed_simulated(
            session, dataset=_dataset(), inventory=load_inventory())
        session.commit()

        assert second["skipped"] is True
        assert second["metrics"] == 0
        assert second["incidents"] == 0
        assert len(repository.get_available_metrics(session)) == before
        assert first["metrics"] > 0
    finally:
        session.close()


def test_seed_incidents_come_from_simulator_ground_truth(session):
    fake = SimpleNamespace(provenance={"events": [{
        "event_id": "evt-1", "type": "packet_loss",
        "class_label": "packet_loss", "device": "R1",
        "metric": "ping.loss_pct", "interface": "eth0",
        "start": "2026-01-01T00:10:00", "end": "2026-01-01T00:20:00",
        "n_points": 10, "meaning": "elevated loss percentage",
    }]})

    created = seed_simulated._seed_incidents(session, fake)
    session.commit()

    assert created == 1
    incident = session.query(Incident).one()
    assert incident.device_name == "sim:R1"
    assert incident.kind == "simulated_packet_loss"
    assert "NOT real telemetry" in (incident.detail or "")
    assert incident.resolved_at is not None

    # Re-running does not duplicate the ground-truth incident.
    assert seed_simulated._seed_incidents(session, fake) == 0
