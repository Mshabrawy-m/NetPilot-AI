"""Offline fakes for the isolated lab (no Docker, no Containerlab).

These stand in for the Containerlab lab so the change loop can be exercised
deterministically in CI. They model FRR's behaviour closely enough to make
the interesting cases meaningful:

* a transaction applies its commands in order, under the right context;
* ``no <context>`` removes a block and its sub-lines;
* ``no <line>`` removes that line;
* a transaction can fail, time out, or land only partially.

They are **only** used by tests. Nothing in ``network/`` or ``services/``
imports this module.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from network.containerlab_manager import CommandResult
from network.device_inventory import InventoryNode, LabInventory
from network.lab_config_ops import CONTEXT_HEADS as CONFIG_HEAD_RE

#: The pre-change configuration of a lab router, in FRR config-file order.
BASE_R2 = [
    "hostname R2",
    "interface lo",
    "ip address 10.1.1.2/32",
    "interface eth1",
    "ip address 10.0.12.2/24",
    "interface eth2",
    "ip address 10.0.21.2/24",
    "router ospf 100",
    "ospf router-id 10.1.1.2",
    "network 10.0.12.0/24 area 0",
]

#: A proposal that changes one address and adds another interface.
PROPOSED_R2 = [
    "hostname R2",
    "interface lo",
    "ip address 10.1.1.2/32",
    "interface eth1",
    "ip address 10.0.12.2/24",
    "interface eth2",
    "ip address 10.0.21.2/24",
    "interface eth3",
    "ip address 10.0.23.2/24",
    "router ospf 100",
    "ospf router-id 10.1.1.2",
    "network 10.0.12.0/24 area 0",
]


@dataclass
class FakeLab:
    """An in-memory stand-in for the Containerlab lab."""

    #: device name -> running configuration lines
    config: dict[str, list[str]] = field(default_factory=dict)
    #: device name -> routing table lines ("show ip route")
    routes: dict[str, list[str]] = field(default_factory=dict)
    #: every docker exec that was requested: (device, argv)
    calls: list[tuple[str, list[str]]] = field(default_factory=list)
    #: when set, the next transaction fails with this error
    fail_next: str | None = None
    #: when set, the next transaction times out
    timeout_next: bool = False
    #: when set, the next transaction applies only this many commands
    partial_next: int | None = None
    #: when set, reading the configuration raises
    unreadable: bool = False

    def docker_exec(self, container: str, argv: list[str],
                    timeout: int = 20) -> CommandResult:
        self.calls.append((container, list(argv)))
        if argv[:3] == ["vtysh", "-c", "show running-config"]:
            if self.unreadable:
                return CommandResult(False, -1, "", "simulated read failure")
            body = "\n".join(self.config.get(container, []))
            return CommandResult(True, 0, body, None)
        if argv[:3] == ["vtysh", "-c", "show ip route"]:
            # a read-only view of the network state
            body = "\n".join(self.routes.get(container, []))
            return CommandResult(True, 0, body, None)
        if self.timeout_next:
            self.timeout_next = False
            return CommandResult(False, -1, "", "simulated timeout",
                                 timed_out=True)
        commands = [argv[i + 1] for i, v in enumerate(argv) if v == "-c"]
        if self.fail_next is not None:
            error = self.fail_next
            self.fail_next = None
            # FRR applies the commands it could before the failing one.
            self._apply(container, commands[:1])
            return CommandResult(False, 1, "", error)
        if self.partial_next is not None:
            cut = self.partial_next
            self.partial_next = None
            self._apply(container, commands[:cut])
            return CommandResult(False, 1, "", "simulated partial failure")
        self._apply(container, commands)
        return CommandResult(True, 0, "", None)

    def docker_inspect_labels(self, container: str,
                              timeout: int = 15) -> dict[str, str] | None:
        return {"clab_node_name": container, "clab.topology": "netpilot"}

    # -- FRR-ish simulation ------------------------------------------------
    def _apply(self, device: str, commands: list[str]) -> None:
        lines = list(self.config.get(device, []))
        context: str | None = None
        for cmd in commands:
            norm = " ".join(str(cmd).split())
            if not norm or norm in ("configure terminal", "end", "exit"):
                if norm in ("end", "exit"):
                    context = None
                continue
            head = norm.split()[0].lower()
            if head == "no":
                lines = self._remove(lines, norm, context)
                continue
            if head in CONFIG_HEAD_RE:
                context = norm
            if norm not in lines:
                lines.append(norm)
        self.config[device] = lines

    @staticmethod
    def _remove(lines: list[str], norm: str, context: str | None) -> list[str]:
        target = norm[3:].strip()
        if not target:
            return lines
        head = target.split()[0].lower()
        if head in CONFIG_HEAD_RE:
            # remove the block header and everything under it
            out: list[str] = []
            dropping = False
            for line in lines:
                if line == target:
                    dropping = True
                    continue
                if dropping and " ".join(line.split()).split()[0].lower() in CONFIG_HEAD_RE:
                    dropping = False
                if not dropping:
                    out.append(line)
            return out
        out = []
        for line in lines:
            stripped = " ".join(line.split())
            if stripped == target:
                continue
            if head == "description" and stripped.startswith("description "):
                continue
            out.append(line)
        return out


def lab_inventory(*names: str, types: dict[str, str] | None = None,
                  running: bool = True) -> LabInventory:
    """An inventory of lab nodes as ``load_inventory`` would produce it."""
    types = types or {}
    nodes = []
    for name in names:
        nodes.append(InventoryNode(
            name=name, type=types.get(name, "router"),
            container=name, running=running))
    return LabInventory(nodes=nodes, links=[])


@dataclass
class FakeOutcome:
    """Mirrors ``network.lab_executor.ExecutionOutcome``."""

    ok: bool
    error: str | None = None
    applied_lines: list[str] = field(default_factory=list)
    missing_lines: list[str] = field(default_factory=list)
    unreadable: bool = False
    timed_out: bool = False
    partial: bool = False
    detail: str = ""
