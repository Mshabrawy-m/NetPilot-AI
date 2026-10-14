"""Lab device inventory.

The inventory is derived from the real source of truth - the Containerlab
topology file (``lab/topology.clab.yml``) - and, when Docker/Containerlab
are available, enriched with live container state from
``containerlab inspect``. Nothing here is hard-coded per device; changing
the topology file changes the inventory.

Safety: ``LabInventory.validate_destination`` implements the allowlist used
by every outbound network command. Only names/IPs that belong to a known
lab node may be targeted; anything else raises ``DestinationNotAllowed``.
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field

import yaml

from config import get_settings
from network import containerlab_manager


class DestinationNotAllowed(ValueError):
    """Raised when a requested target is not part of the approved lab."""


@dataclass
class InventoryInterface:
    name: str
    ip_cidr: str | None = None

    @property
    def ip(self) -> str | None:
        return self.ip_cidr.split("/")[0] if self.ip_cidr else None


@dataclass
class InventoryNode:
    name: str
    type: str                      # 'router' | 'host' | 'unknown'
    loopback_ip: str | None = None
    interfaces: list[InventoryInterface] = field(default_factory=list)
    mgmt_ip: str | None = None     # from containerlab inspect (live)
    container: str | None = None
    running: bool | None = None

    @property
    def lab_ip(self) -> str | None:
        """Best lab-internal address: loopback first, then first iface IP."""
        if self.loopback_ip:
            return self.loopback_ip
        for iface in self.interfaces:
            if iface.ip:
                return iface.ip
        return None

    def all_addresses(self) -> set[str]:
        addrs = {self.loopback_ip} if self.loopback_ip else set()
        addrs |= {i.ip for i in self.interfaces if i.ip}
        if self.mgmt_ip:
            addrs.add(self.mgmt_ip)
        return {a for a in addrs if a}


@dataclass
class InventoryLink:
    node_a: str
    if_a: str
    node_b: str
    if_b: str
    subnet: str | None = None


@dataclass
class LabInventory:
    nodes: list[InventoryNode]
    links: list[InventoryLink]
    source_error: str | None = None   # e.g. topology file unreadable

    @property
    def node_names(self) -> list[str]:
        return [n.name for n in self.nodes]

    def get_node(self, name: str) -> InventoryNode | None:
        for node in self.nodes:
            if node.name == name:
                return node
        return None

    def validate_destination(self, target: str) -> InventoryNode:
        """Allowlist check: target must be a lab node name or one of its
        documented addresses. Raises DestinationNotAllowed otherwise."""
        target = (target or "").strip()
        if not target or len(target) > 128:
            raise DestinationNotAllowed("Empty or oversized destination")
        # Reject anything that is not a bare name/IPv4 literal (defence in
        # depth against CLI-injection style inputs).
        node = self.get_node(target)
        if node is not None:
            return node
        try:
            ip = ipaddress.ip_address(target)
        except ValueError as exc:
            raise DestinationNotAllowed(
                f"Destination {target!r} is not a lab node name or IP"
            ) from exc
        for node in self.nodes:
            if str(ip) in node.all_addresses():
                return node
        raise DestinationNotAllowed(
            f"Destination {target!r} does not belong to the NetPilot lab"
        )


def _labels_to_node(name: str, labels: dict) -> InventoryNode:
    node = InventoryNode(
        name=name,
        type=str(labels.get("netpilot.type", "unknown")),
        loopback_ip=(str(labels["netpilot.loopback"]).split("/")[0]
                     if labels.get("netpilot.loopback") else None),
    )
    for key, value in labels.items():
        if key.startswith("netpilot.if."):
            iface_name = key.split("netpilot.if.", 1)[1]
            node.interfaces.append(
                InventoryInterface(name=iface_name, ip_cidr=str(value))
            )
    node.interfaces.sort(key=lambda i: i.name)
    return node


def load_inventory(topology_file: str | None = None) -> LabInventory:
    """Parse the containerlab topology, then enrich with live inspect data."""
    settings = get_settings()
    topology_file = topology_file or settings.lab_topology_file
    nodes: list[InventoryNode] = []
    links: list[InventoryLink] = []
    source_error: str | None = None

    try:
        with open(topology_file, "r", encoding="utf-8") as fh:
            topo = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError) as exc:
        return LabInventory(nodes=[], links=[],
                            source_error=f"Cannot read topology: {exc}")

    topology = (topo or {}).get("topology", {}) or {}
    raw_nodes = topology.get("nodes", {}) or {}
    for name, spec in raw_nodes.items():
        labels = (spec or {}).get("labels", {}) or {}
        nodes.append(_labels_to_node(str(name), labels))

    prefix = (topo or {}).get("prefix")
    prefix = "" if prefix is None else str(prefix)
    lab_name = str((topo or {}).get("name", settings.lab_name))
    for link_spec in topology.get("links", []) or []:
        eps = link_spec.get("endpoints") or link_spec.get("interfaces") or []
        if len(eps) != 2:
            continue
        a_node, _, a_if = str(eps[0]).rpartition(":")
        b_node, _, b_if = str(eps[1]).rpartition(":")
        subnet = None
        a = next((n for n in nodes if n.name == a_node), None)
        if a is not None:
            for iface in a.interfaces:
                if iface.name == a_if and iface.ip_cidr:
                    subnet = str(
                        ipaddress.ip_network(iface.ip_cidr, strict=False)
                    )
        links.append(InventoryLink(
            node_a=a_node, if_a=a_if, node_b=b_node, if_b=b_if, subnet=subnet,
        ))
        for node, ifname in ((a, a_if),
                             (next((n for n in nodes if n.name == b_node), None), b_if)):
            if node is None:
                continue
            if not any(i.name == ifname for i in node.interfaces):
                node.interfaces.append(InventoryInterface(name=ifname))

    # Enrich with live state when containerlab+docker are available.
    live = containerlab_manager.inspect_nodes(lab_name, topology_file)
    for rec in live:
        node_name = rec.get("node") or rec.get("name") or ""
        # Strip containerlab prefix if present in the runtime name.
        base = node_name.split("_")[-1] if prefix and "_" in node_name else node_name
        node = next((n for n in nodes if n.name in (node_name, base)), None)
        if node is not None:
            node.container = rec.get("container") or node_name
            node.running = str(rec.get("state", "")).lower() in {"running", "up"}
            ipv4 = rec.get("ipv4_address") or rec.get("address")
            if ipv4:
                node.mgmt_ip = str(ipv4).split("/")[0]

    return LabInventory(nodes=nodes, links=links, source_error=source_error)
