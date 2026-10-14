"""Inventory parsing + allowlist validation tests (spec 6)."""
from __future__ import annotations

from pathlib import Path

import pytest

from network import containerlab_manager, device_inventory
from network.device_inventory import DestinationNotAllowed, LabInventory

TOPOLOGY = str(Path(__file__).resolve().parents[1] / "lab" / "topology.clab.yml")


@pytest.fixture(autouse=True)
def no_containerlab(monkeypatch):
    """Inventory tests must not depend on Docker/containerlab here."""
    monkeypatch.setattr(containerlab_manager, "inspect_nodes",
                        lambda *a, **k: [])
    return monkeypatch


def test_topology_parses_all_nodes_and_links():
    inv = device_inventory.load_inventory(TOPOLOGY)
    assert inv.source_error is None
    assert sorted(inv.node_names) == ["PC1", "PC2", "R1", "R2", "R3"]
    assert len(inv.links) == 4
    r1 = inv.get_node("R1")
    assert r1.type == "router"
    assert r1.loopback_ip == "10.1.1.1"
    assert {i.name for i in r1.interfaces} == {"eth1", "eth2"}
    pc1 = inv.get_node("PC1")
    assert pc1.type == "host"
    assert pc1.lab_ip == "10.0.21.21"


def test_link_subnets_derived_from_interface_labels():
    inv = device_inventory.load_inventory(TOPOLOGY)
    subnets = {l.subnet for l in inv.links}
    assert "10.0.12.0/24" in subnets
    assert "10.0.21.0/24" in subnets


@pytest.mark.parametrize("allowed", [
    "R1", "PC2", "10.1.1.1", "10.0.21.21", "10.0.13.3",
])
def test_allowlist_accepts_lab_members(allowed):
    inv = device_inventory.load_inventory(TOPOLOGY)
    node = inv.validate_destination(allowed)
    assert node.name in inv.node_names


@pytest.mark.parametrize("rejected", [
    "8.8.8.8",                      # outside the lab
    "192.168.1.1",                  # outside the lab
    "example.com",                  # arbitrary name
    "",                             # empty
    "R1; rm -rf /",                 # shell-injection attempt
    "R1 && ping 8.8.8.8",           # command chaining attempt
    "10.1.1.9",                     # plausible but not in inventory
    "A" * 200,                      # oversized input
])
def test_allowlist_rejects_everything_else(rejected):
    inv = device_inventory.load_inventory(TOPOLOGY)
    with pytest.raises(DestinationNotAllowed):
        inv.validate_destination(rejected)


def test_unreadable_topology_reports_error(tmp_path):
    inv = device_inventory.load_inventory(str(tmp_path / "missing.yml"))
    assert inv.nodes == []
    assert inv.source_error is not None
