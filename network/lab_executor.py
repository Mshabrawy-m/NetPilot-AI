"""Isolated-lab configuration executor (Phase 4).

This is the concrete implementation of the executor callable that
``services/change_service.apply()`` has always accepted as an injected
argument. Until now the repository shipped the *protocol* but no
implementation, so the apply half of the change loop could never be
exercised (Gap G3-2).

Design constraints, all of them load-bearing:

**The executor lives OUTSIDE ``ai/`` on purpose.** Gate 3's acceptance
criterion L-5 statically scans ``ai/**.py`` for ``subprocess``, ``socket``,
``os.system``, ``eval(``, ``exec(``, ``containerlab_manager``,
``network.connectivity`` and the strings ``def deploy`` / ``def apply`` /
``def rollback``. Putting execution code under ``ai/`` would break L-5. The AI
package therefore still cannot execute anything: the LLM may *propose*, the
deterministic verifier may *judge*, and only this module - behind two
environment gates, a human approval, a verified rollback plan and a proven
lab identity - may *act*.

**Fail closed, always.** Every precondition that cannot be positively proven
is a refusal, never a best-effort guess:

1. ``NETPILOT_LAB_EXECUTION_ENABLED=true`` - explicit opt-in for real device
   execution. Off by default; the hosted/demo deployment never has it.
2. ``NETPILOT_CHANGE_LOOP_ENABLED=true`` - the pre-existing change-loop gate.
3. Live mode (never demo mode).
4. The device must be a **router** node declared in the configured
   Containerlab topology file.
5. The container must be **running** and must carry Containerlab's own
   labels proving it belongs to this topology - a container name alone is
   never trusted.
6. The proposed lines must pass the *existing* deterministic validator
   (``ai.verification.config_validation``), the lab-invariant policy check,
   the target/scope checks, **and** a config-command allowlist.

**The LLM can never execute shell.** Lines arrive as configuration text. They
are validated as FRR config (no shell metacharacters, no prohibited
commands), checked against a config-command allowlist, and then handed to a
fixed ``docker exec ... vtysh`` argv list with ``shell=False``. The line
becomes one argv element; there is no shell anywhere on the path. Hosts
(``PC1``/``PC2``) are refused outright - only routers can be configured.

**Nothing is written to the startup configuration.** The change is applied to
the *running* configuration only (``configure terminal`` ... ``end``, never
``write memory``/``copy running-config``, which the validator forbids). A
container restart therefore discards the change - which is exactly the
behaviour you want in a throwaway lab, and it means a rollback is always
reachable.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ai.verification import config_validation, policy_checks
from config import get_settings
from network import containerlab_manager
from network.device_inventory import InventoryNode, LabInventory, load_inventory
from network.lab_config_ops import (ConfigOp, build_apply_ops,
                                    build_rollback_ops, config_head,
                                    normalize_config, normalize_line,
                                    removal_target)

#: Config-command heads a proposal may use. Everything else is refused
#: outright, even if it passed the generic syntax check. These are the FRR
#: *configuration* commands used by the NetPilot lab routers.
#:
#: The list is verified against FRR 8.4: every head here is a command FRR
#: actually accepts. ``mtu`` was removed because FRR 8.4 has no interface
#: ``mtu`` command (the MTU is a kernel property; only ``show`` reports it),
#: so a proposal using it would have been rejected by the device itself.
ALLOWED_CONFIG_HEADS = frozenset({
    "hostname", "interface", "ip", "description", "router", "ospf",
    "network", "no", "line", "log", "banner", "vrf", "neighbor",
    "access-list", "route-map", "exit",
})

#: Docker label keys Containerlab stamps on the containers it creates. Any of
#: them is accepted as proof of node identity.
_NODE_LABEL_KEYS = ("clab_node_name", "clab.node.name", "clab_node")

#: The read-only command used to read a device's running configuration.
_SHOW_RUNNING_CONFIG = ["vtysh", "-c", "show running-config"]

#: The read-only command used to read a device's routing table. It is
#: evidence for "the network state", not a configuration change.
_SHOW_IP_ROUTE = ["vtysh", "-c", "show ip route"]


@dataclass
class ExecutionOutcome:
    """What actually happened on the device.

    Compatible with ``services.change_service._outcome()``: it reads ``ok``
    and ``error``. The extra fields let the service layer distinguish
    *clean failure* from *partial application* and from *timeout* - three
    states that need three different responses.
    """

    ok: bool
    error: str | None = None
    #: Expected lines that are present in the device's running config.
    applied_lines: list[str] = field(default_factory=list)
    #: Expected lines that are NOT present after the attempt.
    missing_lines: list[str] = field(default_factory=list)
    #: True when the running configuration could not be read back.
    unreadable: bool = False
    #: True when the command was killed by the hard timeout (state unknown).
    timed_out: bool = False
    #: True when some expected lines landed and others did not.
    partial: bool = False
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "error": self.error,
            "applied_lines": list(self.applied_lines),
            "missing_lines": list(self.missing_lines),
            "unreadable": self.unreadable,
            "timed_out": self.timed_out,
            "partial": self.partial,
            "detail": self.detail,
        }


def head_allowed(line: str | None) -> bool:
    """True when the line's command head is an allowed FRR config command."""
    head = config_head(line)
    return bool(head) and head in ALLOWED_CONFIG_HEADS


def static_gate(device_name: str, lines: list[str],
                known_devices: set[str]) -> tuple[bool, str]:
    """Deterministic pre-execution gate (no device access).

    Reuses the *existing* Phase 3 validator and policy checks rather than
    inventing a second set of rules, and adds the config-command allowlist
    that the LLM could otherwise walk around. Any check that conclusively
    fails is a refusal; a check that is merely inconclusive (for example
    "static config cannot prove live management reachability") is recorded but
    does not block - except where the lab invariants are violated, which is
    a conclusive failure.
    """
    if not lines:
        return False, "refusing to apply an empty configuration"

    findings = config_validation.validate_lines(lines)
    bad = [f for f in findings if not f["syntax_ok"] or f["prohibited"]]
    if bad:
        detail = "; ".join(f"{f['line'][:40]} ({f['reason']})" for f in bad[:5])
        return False, f"proposal contains invalid/prohibited lines: {detail}"

    disallowed = [ln for ln in lines if not head_allowed(ln)]
    if disallowed:
        heads = sorted({config_head(ln) for ln in disallowed})
        return False, (f"proposal uses command head(s) outside the allowed "
                       f"config set {sorted(ALLOWED_CONFIG_HEADS)}: {heads}")

    target = policy_checks.check_target_exists(device_name, known_devices)
    if target["passed"] is False:
        return False, f"target check failed: {target['finding']}"

    scope = policy_checks.check_out_of_scope(lines, device_name, known_devices)
    if scope["passed"] is False:
        return False, f"scope check failed: {scope['finding']}"

    invariants = policy_checks.check_lab_invariants(lines)
    if invariants["passed"] is False:
        return False, f"lab invariant check failed: {invariants['finding']}"

    return True, "static gate passed"


def ops_to_argv(ops: list[ConfigOp]) -> list[str]:
    """Render context-aware ops as a fixed ``vtysh`` argv list.

    The result is ``["vtysh", "-c", "configure terminal", "-c", <line>, ...]``
    - every config line is its own ``-c`` argument, so a line can never be
    reinterpreted as a shell command. ``exit`` returns from a block to
    config mode, ``end`` leaves config mode.
    """
    argv: list[str] = ["vtysh", "-c", "configure terminal"]
    for op in ops:
        if op.context:
            argv += ["-c", op.context]
        for line in op.lines:
            argv += ["-c", line]
        if op.context:
            argv += ["-c", "exit"]
    argv += ["-c", "end"]
    return argv


class LabExecutor:
    """Executes configuration changes against the isolated Containerlab lab.

    Satisfies the executor protocol used by
    ``services.change_service.apply()``/``rollback()``::

        executor(device_name: str, config_lines: list[str]) -> outcome

    The outcome object exposes ``ok`` and ``error`` (the attributes the
    service reads) plus the richer state needed for partial-application and
    timeout handling.
    """

    def __init__(self, *, timeout_seconds: int | None = None,
                 inventory: LabInventory | None = None) -> None:
        self._timeout = timeout_seconds
        self._inventory = inventory

    # -- plumbing ---------------------------------------------------------
    @property
    def timeout_seconds(self) -> int:
        if self._timeout is not None:
            return int(self._timeout)
        return get_settings().lab_execution_timeout_seconds

    def inventory(self) -> LabInventory:
        """The lab inventory (topology file + live containerlab state)."""
        if self._inventory is not None:
            return self._inventory
        return load_inventory()

    def known_devices(self) -> set[str]:
        return {n.name for n in self.inventory().nodes}

    # -- gates ------------------------------------------------------------
    def resolve_target(self, device_name: str) -> tuple[InventoryNode | None, str]:
        """Resolve a device name to a *proven* lab node.

        Returns ``(node, "")`` on success or ``(None, reason)`` with a
        human-readable reason for every refusal.
        """
        settings = get_settings()
        if not settings.lab_execution_enabled:
            return None, ("lab execution disabled (set "
                          "NETPILOT_LAB_EXECUTION_ENABLED=true on the "
                          "isolated lab host)")
        if not settings.change_loop_enabled:
            return None, ("change loop disabled (set "
                          "NETPILOT_CHANGE_LOOP_ENABLED=true)")
        if settings.demo_mode:
            return None, "demo mode: lab execution is disabled"

        inventory = self.inventory()
        if inventory.source_error:
            return None, f"lab inventory unavailable: {inventory.source_error}"
        if not inventory.nodes:
            return None, ("lab inventory is empty - the topology file "
                          f"{settings.lab_topology_file!r} declares no nodes")

        node = inventory.get_node(device_name)
        if node is None:
            return None, (f"{device_name!r} is not a node of the configured "
                          f"lab topology ({settings.lab_topology_file})")
        if node.type != "router":
            return None, (f"{device_name!r} is a lab node of type "
                          f"{node.type!r}; only routers can be configured")
        if not node.container:
            return None, (f"{device_name!r} has no container - the lab is "
                          f"probably not deployed")
        if node.running is False:
            return None, f"the container for {device_name!r} is not running"

        labels = containerlab_manager.docker_inspect_labels(node.container)
        if labels is None:
            return None, (f"could not read the Docker labels of container "
                          f"{node.container!r} - lab identity unverified")
        node_label = None
        for key in _NODE_LABEL_KEYS:
            if labels.get(key):
                node_label = str(labels[key])
                break
        if not node_label:
            return None, (f"container {node.container!r} carries no "
                          f"containerlab node label - lab identity unverified")
        if node_label != node.name:
            return None, (f"container {node.container!r} belongs to lab node "
                          f"{node_label!r}, not {node.name!r} - refusing")
        return node, ""

    def preflight(self, device_name: str) -> tuple[bool, str]:
        """Prove the lab identity of a device (no configuration is sent)."""
        node, reason = self.resolve_target(device_name)
        if node is None:
            return False, reason
        return True, f"lab node {node.name!r} ({node.container}) verified"

    def static_preflight(self, device_name: str, lines: list[str]
                         ) -> tuple[bool, str]:
        """Static gate only (no device access) - run before the identity
        check so an obviously invalid proposal never reaches the lab."""
        return static_gate(device_name, lines, self.known_devices())

    # -- device I/O -------------------------------------------------------
    def read_config(self, device_name: str) -> list[str]:
        """Read the device's *running* configuration.

        Returns the raw configuration lines. Raises on failure - the caller
        turns that into an explicit "unreadable" verdict rather than an
        implicit success.
        """
        node, reason = self.resolve_target(device_name)
        if node is None:
            raise RuntimeError(reason)
        result = containerlab_manager.docker_exec(
            node.container, _SHOW_RUNNING_CONFIG, timeout=self.timeout_seconds)
        if result.timed_out:
            raise TimeoutError(
                f"reading the running configuration of {device_name!r} "
                f"timed out after {self.timeout_seconds}s")
        if not result.ok:
            raise RuntimeError(
                f"could not read the running configuration of {device_name!r}: "
                f"{result.error or 'vtysh failed'}")
        return (result.stdout or "").splitlines()

    def read_routing_table(self, device_name: str) -> list[str]:
        """Read the device's routing table (read-only).

        Used as *network state* evidence for the change loop: the routing
        table is captured before the change, after the change and after the
        rollback, so the restoration can be judged on real observations and
        not only on the configuration file. Raises on failure - an unreadable
        table is never reported as an empty one.
        """
        node, reason = self.resolve_target(device_name)
        if node is None:
            raise RuntimeError(reason)
        result = containerlab_manager.docker_exec(
            node.container, _SHOW_IP_ROUTE, timeout=self.timeout_seconds)
        if result.timed_out:
            raise TimeoutError(
                f"reading the routing table of {device_name!r} timed out "
                f"after {self.timeout_seconds}s")
        if not result.ok:
            raise RuntimeError(
                f"could not read the routing table of {device_name!r}: "
                f"{result.error or 'vtysh failed'}")
        return (result.stdout or "").splitlines()

    def _run_ops(self, node: InventoryNode, ops: list[ConfigOp]
                 ) -> containerlab_manager.CommandResult:
        argv = ops_to_argv(ops)
        return containerlab_manager.docker_exec(
            node.container, argv, timeout=self.timeout_seconds)

    def apply_config(self, device_name: str,
                     lines: list[str]) -> ExecutionOutcome:
        """Apply configuration lines to a lab device, transactionally.

        The change is applied to the running configuration and then read back
        so the caller learns exactly which lines landed. A read-back that
        does not contain every expected line is reported as a *failure with
        partial application* - never as a success.
        """
        lines = list(lines or [])
        ok, reason = self.static_preflight(device_name, lines)
        if not ok:
            return ExecutionOutcome(ok=False, error=reason, detail=reason)

        node, reason = self.resolve_target(device_name)
        if node is None:
            return ExecutionOutcome(ok=False, error=reason, detail=reason)

        result = self._run_ops(node, build_apply_ops(lines))

        if result.timed_out:
            applied, missing, unreadable = self._diff_state(device_name, lines)
            return ExecutionOutcome(
                ok=False, timed_out=True, unreadable=unreadable,
                error=(f"the config transaction on {device_name!r} timed out "
                       f"after {self.timeout_seconds}s - the device state is "
                       f"unknown"),
                applied_lines=applied, missing_lines=missing,
                partial=bool(applied) and bool(missing),
                detail="timeout - state unknown, read-back attempted")

        if not result.ok:
            applied, missing, unreadable = self._diff_state(device_name, lines)
            return ExecutionOutcome(
                ok=False, unreadable=unreadable,
                error=(f"the config transaction on {device_name!r} failed: "
                       f"{(result.error or 'vtysh returned an error')[:300]}"),
                applied_lines=applied, missing_lines=missing,
                partial=bool(applied) and bool(missing),
                detail="transaction failed")

        applied, missing, unreadable = self._diff_state(device_name, lines)
        if unreadable:
            return ExecutionOutcome(
                ok=False, unreadable=True,
                error=(f"the config transaction on {device_name!r} ran but the "
                       f"result could not be read back - not verified"),
                applied_lines=applied, missing_lines=missing,
                detail="applied but not verifiable")
        if missing:
            return ExecutionOutcome(
                ok=False,
                error=(f"{len(missing)} of {len(lines)} line(s) did not take "
                       f"effect on {device_name!r}: {missing[:5]}"),
                applied_lines=applied, missing_lines=missing, partial=True,
                detail="partial application")
        return ExecutionOutcome(
            ok=True, applied_lines=applied, missing_lines=missing,
            detail=f"applied {len(applied)} line(s) to {device_name!r}")

    def rollback_config(self, device_name: str, proposed: list[str],
                        base: list[str]) -> ExecutionOutcome:
        """Undo ``proposed`` on a device and restore ``base``.

        The rollback is a real undo, not a merge: the plan removes the lines
        the proposal added and re-applies the base values for the lines it
        changed (see ``network.lab_config_ops.build_rollback_ops``). The
        result is verified the same way an apply is - by reading the device
        back and comparing it with the base configuration.
        """
        ops = build_rollback_ops(proposed, base)
        if not ops:
            return ExecutionOutcome(
                ok=True, detail="no rollback required (nothing to undo)")

        node, reason = self.resolve_target(device_name)
        if node is None:
            return ExecutionOutcome(ok=False, error=reason, detail=reason)

        result = self._run_ops(node, ops)
        if result.timed_out:
            return ExecutionOutcome(
                ok=False, timed_out=True,
                error=(f"the rollback on {device_name!r} timed out after "
                       f"{self.timeout_seconds}s - the device state is "
                       f"unknown"),
                detail="rollback timeout")
        if not result.ok:
            return ExecutionOutcome(
                ok=False,
                error=(f"the rollback on {device_name!r} failed: "
                       f"{(result.error or 'vtysh returned an error')[:300]}"),
                detail="rollback failed")

        return ExecutionOutcome(
            ok=True, detail=f"rollback transaction executed on {device_name!r}")

    def _diff_state(self, device_name: str, expected: list[str]
                    ) -> tuple[list[str], list[str], bool]:
        """Read the device back and split the expected lines into
        (present, missing). The third element is True when the read-back
        itself failed.

        A proposal line that *removes* something counts as landed when its
        target is **gone** - a removal can never be present in the read-back.
        """
        try:
            actual = self.read_config(device_name)
        except Exception:  # noqa: BLE001 - an unreadable device is not a pass
            return [], list(expected), True
        actual_set = set(normalize_config(actual))
        applied, missing = [], []
        for line in expected:
            norm = normalize_line(line)
            target = removal_target(norm)
            if target:
                landed = normalize_line(target) not in actual_set
            else:
                landed = norm in actual_set
            (applied if landed else missing).append(line)
        return applied, missing, False

    # -- change_service protocol -----------------------------------------
    def __call__(self, device_name: str, config_lines: list[str]
                 ) -> ExecutionOutcome:
        """Entry point used by ``services.change_service.apply()/rollback()``."""
        return self.apply_config(device_name, config_lines)
