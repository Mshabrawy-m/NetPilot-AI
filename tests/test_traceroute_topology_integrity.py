"""Regression: traceroute topology writes must tolerate stale duplicate devices.

This is a real-repository test: it creates the application's actual ORM schema
(so the unique ``ix_devices_name`` index exists, exactly as on Neon), seeds the
duplicate rows that real collection runs left behind, and drives three
discovery cycles through the real write path. Nothing in ``database.repository``
is mocked.
"""
from __future__ import annotations

import datetime as dt
import itertools
import logging

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from database import repository
from database.models import Base, Device, MetricSample
from network.traceroute import DiscoveredPath
from services import topology_service
from tools.live_collector import _sync_traceroute_topology


@pytest.fixture()
def session_factory():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, future=True)
    try:
        yield factory
    finally:
        engine.dispose()


def _seed_stale_topology(factory) -> None:
    """Reproduce the rows earlier live runs left on Neon (never deleted)."""
    with factory() as session:
        # Friendly labels are inserted first: a naive same-IP lookup sees them
        # first, which is exactly the condition that used to rename them into a
        # colliding raw-IP row.
        repository.upsert_device(
            session, name="gateway", type_="router",
            mgmt_ip="192.168.1.1", lab_ip="192.168.1.1", status="up")
        repository.upsert_device(
            session, name="cloudflare-dns", type_="router",
            mgmt_ip="1.1.1.1", lab_ip="1.1.1.1", status="up")
        # Stale raw-IP duplicates and old synthetic placeholders.
        session.add(Device(name="192.168.1.1", type="router",
                           mgmt_ip="192.168.1.1", lab_ip="192.168.1.1",
                           status="up"))
        session.add(Device(name="1.1.1.1", type="router",
                           mgmt_ip="1.1.1.1", lab_ip="1.1.1.1",
                           status="up"))
        session.add(Device(name="unresponsive hops (3)", type="router",
                           status="unknown"))
        session.add(Device(name="unresponsive hops (5)", type="router",
                           status="unknown"))
        session.commit()


def _discovered_path(cycle: int) -> DiscoveredPath:
    """Three cycles with varying hops, sharing the duplicated endpoints."""
    if cycle == 0:
        hops = ["192.168.1.1", "10.50.0.1", "10.36.22.29"]
        destination = "1.1.1.1"
    elif cycle == 1:
        hops = ["192.168.1.1", "10.50.0.2"]
        destination = "8.8.8.8"
    else:
        hops = ["192.168.1.1", "10.50.0.1", "10.36.22.29"]
        destination = "1.1.1.1"

    placeholder = f"unresponsive-path:{destination}"
    devices = [
        {"name": hop, "type": "router", "lab_ip": hop, "status": "up"}
        for hop in hops
    ]
    devices.append({
        "name": placeholder, "type": "router", "lab_ip": None,
        "status": "unknown",
    })
    devices.append({
        "name": destination, "type": "router", "lab_ip": destination,
        "status": "up",
    })

    links = [
        {"device_a": left, "device_b": right, "status": "up"}
        for left, right in itertools.pairwise(hops)
    ]
    links.append({
        "device_a": hops[-1], "device_b": placeholder, "status": "up"})
    links.append({
        "device_a": placeholder, "device_b": destination, "status": "up"})

    collected_at = dt.datetime(2026, 1, 1, 0, cycle, 0)
    metrics = [{
        "device_name": hop,
        "interface_name": None,
        "metric": "traceroute.hop_1_rtt_ms",
        "value": 1.0,
        "unit": "ms",
        "data_source": "live",
        "collected_at": collected_at,
    } for hop in hops]
    return DiscoveredPath(
        destination=destination, devices=devices, links=links, metrics=metrics)


def test_three_traceroute_cycles_tolerate_stale_duplicates(session_factory,
                                                           caplog):
    caplog.set_level(logging.ERROR, logger="netpilot.live_collector")
    _seed_stale_topology(session_factory)

    for cycle in range(3):
        collected_at = dt.datetime(2026, 1, 1, 0, cycle, 0)
        discovered = _discovered_path(cycle)

        # 1. Samples are written first, in their own transaction.
        with session_factory() as session:
            repository.add_metric_samples_bulk(session, [{
                "device_name": "gateway",
                "interface_name": None,
                "metric": "traceroute.test_sample",
                "value": float(cycle),
                "unit": "count",
                "data_source": "live",
                "collected_at": collected_at,
            }])
            session.commit()

        # 2. Topology is written in a fresh session. On the pre-fix code the
        #    friendly->raw-IP rename collides here with ix_devices_name.
        with session_factory() as session:
            _sync_traceroute_topology(session, discovered, collected_at, [])
            session.commit()

        with session_factory() as session:
            stored = session.scalar(
                select(func.count(MetricSample.id))
                .where(MetricSample.metric == "traceroute.test_sample"))
            assert stored == cycle + 1, "samples must survive every cycle"

    assert not [record for record in caplog.records
                if record.levelno >= logging.ERROR], (
        "topology writes must not log IntegrityError/PendingRollbackError")

    with session_factory() as session:
        devices = list(repository.get_devices(session))
        names = [device.name for device in devices]

        assert len(names) == len(set(names))
        assert names.count("gateway") == 1
        assert names.count("cloudflare-dns") == 1
        assert names.count("192.168.1.1") == 1
        assert names.count("1.1.1.1") == 1
        assert names.count("unresponsive hops (3)") == 1
        assert names.count("unresponsive hops (5)") == 1
        assert names.count("unresponsive-path:1.1.1.1") == 1
        assert names.count("unresponsive-path:8.8.8.8") == 1

        # The friendly row kept its name and the stale duplicate was tolerated,
        # not replaced by a third row for the same IP.
        gateway = repository.get_device_by_name(session, "gateway")
        assert gateway is not None and gateway.name == "gateway"
        assert sum(
            1 for device in devices
            if "192.168.1.1" in (device.mgmt_ip, device.lab_ip)) == 2
        assert sum(
            1 for device in devices
            if "1.1.1.1" in (device.mgmt_ip, device.lab_ip)) == 2

        links = list(repository.get_links(session))
        normalized = {
            tuple(sorted(((link.device_a, link.if_a),
                          (link.device_b, link.if_b))))
            for link in links
        }
        assert len(normalized) == len(links), "links must dedupe by endpoints"

        status = topology_service.compute_network_status(devices)
        assert status.down == 0
        assert status.overall != "degraded"
