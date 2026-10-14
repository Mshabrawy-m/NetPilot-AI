"""Network/device status rules (documented in topology_service)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.topology_service import compute_network_status


def d(status: str) -> SimpleNamespace:
    return SimpleNamespace(status=status)


@pytest.mark.parametrize("statuses,expected", [
    ([], "unknown"),                       # nothing registered
    (["up", "up", "up", "up", "up"], "healthy"),                # all up
    (["up", "up", "up", "up", "down"], "degraded"),             # 4/5 >= ceil(5/2)=3
    (["up", "up", "up", "down", "down"], "degraded"),           # exactly ceil(5/2)=3
    (["up", "up", "down", "down", "down"], "down"),             # 2 < ceil(5/2)=3
    (["down", "down"], "down"),
    (["unknown", "unknown"], "down"),      # none up
    (["up", "down", "down"], "down"),      # 1 < ceil(3/2)=2
])
def test_documented_status_rules(statuses, expected):
    result = compute_network_status([d(s) for s in statuses])
    assert result.overall == expected


def test_status_counts_partition_all_devices():
    result = compute_network_status([d("up"), d("down"), d("unknown")])
    assert (result.total, result.up, result.down, result.unknown) == (3, 1, 1, 1)
    assert result.up + result.down + result.unknown == result.total


def test_topology_service_reads_from_database(session):
    from database import repository
    from services.topology_service import get_topology

    repository.upsert_device(session, name="R1", type_="router", status="up")
    repository.upsert_device(session, name="PC1", type_="host", status="down")
    repository.upsert_link(session, "R1", "eth2", "PC1", "eth1", status="down")
    session.commit()

    topo = get_topology(session)
    assert {n["id"] for n in topo["nodes"]} == {"R1", "PC1"}
    assert topo["edges"][0]["status"] == "down"
    assert topo["status"].overall == "degraded"
