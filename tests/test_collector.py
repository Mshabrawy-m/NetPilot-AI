"""Metrics collector tests with injected fake probes.

The collector is tested against a stubbed lab: ping/counter/route readers
are function parameters, so we can feed it realistic structured results
without Docker. This verifies real behaviour: values that exist are stored,
unavailable metrics are stored as NULL, statuses/incidents/run-records are
derived correctly, and nothing is invented.
"""
from __future__ import annotations

import pytest

from database import repository
from database.connection import get_session
from network import containerlab_manager, metrics_collector
from network.connectivity import PingResult
from network.device_inventory import (
    InventoryInterface, InventoryLink, InventoryNode, LabInventory,
)


@pytest.fixture()
def fake_inventory():
    r1 = InventoryNode(name="R1", type="router", loopback_ip="10.1.1.1",
                       mgmt_ip="172.20.0.1", container="R1", running=True,
                       interfaces=[InventoryInterface("eth1", "10.0.12.1/24")])
    pc1 = InventoryNode(name="PC1", type="host", mgmt_ip="172.20.0.2",
                        container="PC1", running=True,
                        interfaces=[InventoryInterface("eth1", "10.0.21.21/24")])
    link = InventoryLink("R1", "eth1", "PC1", "eth1", subnet="10.0.12.0/24")
    return LabInventory(nodes=[r1, pc1], links=[link])


def _fake_ping(up_names):
    def ping(target, inventory):
        node = inventory.validate_destination(target)
        if node.name in up_names:
            return PingResult(destination=target, success=True,
                              packets_sent=3, packets_received=3,
                              loss_pct=0.0, avg_rtt_ms=1.5)
        return PingResult(destination=target, success=False,
                          packets_sent=3, packets_received=0,
                          loss_pct=100.0, avg_rtt_ms=None,
                          error="Destination host unreachable")
    return ping


def _fake_counters(container, iface):
    if container == "PC1":
        return None                      # counter source unavailable
    return {"rx_bytes": 12345.0, "tx_bytes": 6789.0, "rx_errors": 0.0,
            "tx_errors": 0.0, "rx_dropped": 2.0, "tx_dropped": 0.0,
            "operstate": 1.0}


def _fake_routes(container):
    return {"frr.routes_total": 7.0}


def _fake_inlab_ping(container, destination, inventory):
    # Validate like the real implementation does, then fabricate a
    # structured *test* result (allowed in unit tests - not stored as a
    # claimed real measurement).
    inventory.validate_destination(destination)
    return PingResult(destination=destination, success=True,
                      packets_sent=2, packets_received=2,
                      loss_pct=0.0, avg_rtt_ms=0.5)


@pytest.fixture()
def fake_docker(monkeypatch):
    monkeypatch.setattr(containerlab_manager, "availability",
                        lambda: {"docker": True, "containerlab": True})
    return monkeypatch


def test_collect_once_stores_real_measurements(db_url, fake_inventory, fake_docker):
    summary = metrics_collector.collect_once(
        inventory=fake_inventory,
        host_ping=_fake_ping({"R1"}),
        inlab_ping=_fake_inlab_ping,
        counter_reader=_fake_counters,
        route_reader=_fake_routes,
        data_source="live",
    )
    assert summary.devices_total == 2
    assert summary.devices_up == 1
    assert summary.status == "partial"      # one probe failed

    session = get_session()
    try:
        devices = {d.name: d for d in repository.get_devices(session)}
        assert devices["R1"].status == "up"
        assert devices["PC1"].status == "down"
        assert devices["R1"].type == "router"      # type must not be clobbered

        r1_metrics = {(m.metric, m.interface_name): m.value for m in
                      repository.get_metric_series(session, "R1", "ping.avg_rtt_ms")}
        assert ("ping.avg_rtt_ms", None) in r1_metrics
        assert r1_metrics[("ping.avg_rtt_ms", None)] == 1.5

        rx = repository.get_metric_series(session, "R1", "if.rx_bytes",
                                          interface_name="eth1")
        assert len(rx) == 1 and rx[0].value == 12345.0
        routes = repository.get_metric_series(session, "R1", "frr.routes_total")
        assert routes[0].value == 7.0

        # In-lab ping measured inside the container (fake probe injected).
        inlab = repository.get_metric_series(session, "PC1",
                                             "inlab_ping.avg_rtt_ms")
        assert inlab and inlab[0].value == 0.5

        # PC1 unreachable: RTT stored as NULL (no invented latency),
        # counters recorded as unavailable (NULL operstate sample), never
        # fabricated values.
        pc_rtt = repository.get_metric_series(session, "PC1", "ping.avg_rtt_ms")
        assert pc_rtt[0].value is None
        pc_op = repository.get_metric_series(session, "PC1", "if.operstate")
        assert pc_op and pc_op[0].value is None

        # Incident opened for the down device.
        incidents = repository.get_open_incidents(session)
        assert [i.device_name for i in incidents] == ["PC1"]

        # Link status derived from endpoint statuses.
        links = repository.get_links(session)
        assert links[0].status == "down"

        run = repository.get_last_successful_run(session)
        assert run is not None and run.status == "partial"
        assert run.data_source == "live"
    finally:
        session.close()


def test_collect_once_rejects_no_lab(db_url, tmp_path, fake_docker):
    """Empty inventory (e.g. topology missing) -> honest error run."""
    empty = LabInventory(nodes=[], links=[], source_error="Cannot read topology")
    summary = metrics_collector.collect_once(inventory=empty)
    assert summary.status == "error"
    session = get_session()
    try:
        runs = repository.get_recent_runs(session)
        assert runs[0].status == "error"
        assert "topology" in (runs[0].error_message or "").lower()
    finally:
        session.close()


def test_incident_resolves_when_device_recovers(db_url, fake_inventory, fake_docker):
    # Cycle 1: PC1 down -> incident opened.
    metrics_collector.collect_once(inventory=fake_inventory,
                                   host_ping=_fake_ping({"R1"}),
                                   inlab_ping=_fake_inlab_ping,
                                   counter_reader=_fake_counters,
                                   route_reader=_fake_routes)
    session = get_session()
    assert len(repository.get_open_incidents(session)) == 1
    session.close()

    # Cycle 2: everyone up -> incident resolved automatically.
    metrics_collector.collect_once(inventory=fake_inventory,
                                   host_ping=_fake_ping({"R1", "PC1"}),
                                   inlab_ping=_fake_inlab_ping,
                                   counter_reader=_fake_counters,
                                   route_reader=_fake_routes)
    session = get_session()
    try:
        assert repository.get_open_incidents(session) == []
        run = repository.get_last_successful_run(session)
        assert run.status == "ok"
    finally:
        session.close()
