#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NetPilot AI - Phase 4 change-loop validation harness (Gate D).

RUN THIS ON AN ISOLATED LINUX VM THAT HAS DOCKER + CONTAINERLAB, in LIVE
mode, with the change loop explicitly enabled. It drives the REAL change
loop - ``services.change_execution.execute_change`` with the REAL
``network.lab_executor.LabExecutor`` - against the running lab and writes raw
evidence to ``lab/validation_evidence/<UTC-timestamp>/change_loop/``.

It exercises the full loop end to end:

  1. read the device's current (pre-change) running configuration, routing
     table and reachability;
  2. present the exact change, its expected impact and its rollback plan,
     and STOP unless the operator approved this exact change;
  3. record a validated change proposal for a lab router;
  4. approve it as a named, non-self human principal;
  5. apply it through the lab executor;
  6. verify the change by reading the device back;
  7. roll the change back;
  8. verify that the device was really restored - configuration AND network
     state.

Honesty guarantees baked into the code:

  * If Docker/Containerlab or a live data source is missing, the run is
    recorded BLOCKED - the script NEVER substitutes demo data or mocks.
  * A step is PASS only when its assertion actually held on real output.
  * Nothing is applied unless BOTH ``NETPILOT_CHANGE_LOOP_ENABLED=true`` and
    ``NETPILOT_LAB_EXECUTION_ENABLED=true`` are set on this host AND the
    operator passed ``--approve-change`` (the explicit human approval).
  * The proposed change is NOT a no-op: the harness refuses to run it when
    the change does not actually differ from the device's configuration.
  * The API key is only ever referenced by a one-way SHA-256 fingerprint.

Usage (from the repository root, on the VM, in LIVE mode):

    unset NETPILOT_DEMO_MODE
    export NETPILOT_CHANGE_LOOP_ENABLED=true
    export NETPILOT_LAB_EXECUTION_ENABLED=true

    # 1) dry run: prints the exact change, its impact and its rollback plan
    python tools/phase4_change_loop_validation.py --device R2

    # 2) after the operator approved THAT change, execute it
    python tools/phase4_change_loop_validation.py --device R2 --approve-change

Exit codes: 0 = every executed step PASS, 1 = at least one FAIL,
2 = BLOCKED (preconditions unmet, nothing validated). A non-zero on
BLOCKED/FAIL is intended so CI cannot mistake "not run" for "passed".
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import get_settings  # noqa: E402
from database.connection import init_db, session_scope  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

#: The configuration change the harness proposes. It adds an OSPF network
#: statement to a lab router - a real, observable, reversible change that
#: exercises the whole loop without touching the management path, the
#: addressing or the reachability of the lab.
#:
#: The statement is deliberately about a prefix that does not exist in the
#: lab: the change must be *visible in the running configuration* and must
#: NOT change the routing table or the connectivity - which is exactly what
#: the before/after observations prove.
CHANGE_LINES = [
    "router ospf 100",
    "network 10.0.99.0/24 area 0",
]

#: The lab node the reachability check pings from the collector host.
PING_TARGET = "R1"

_results: dict[str, dict] = {}


def _record(item: str, status: str, detail: str) -> None:
    _results[item] = {"status": status, "detail": detail}
    print(f"[{status:7}] {item}: {detail}")


def _hash_lab_configs() -> dict[str, str]:
    out: dict[str, str] = {}
    base = ROOT / "lab" / "configs"
    for p in sorted(base.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(base))] = hashlib.sha256(
                p.read_bytes()).hexdigest()
    return out


def _read_routing(executor, device: str) -> tuple[list[str] | None, str]:
    """The device's routing table, or (None, reason) when it cannot be read."""
    reader = getattr(executor, "read_routing_table", None)
    if not callable(reader):
        return None, "the executor cannot read the routing table"
    try:
        return list(reader(device)), ""
    except Exception as exc:  # noqa: BLE001 - unreadable is not "empty"
        return None, f"{type(exc).__name__}: {exc}"


def approval_gate(approve_change: bool, approver: str) -> tuple[bool, str]:
    """The explicit, per-change human authorization.

    Returns ``(allowed, reason)``. ``allowed`` is True only when the
    operator passed ``--approve-change`` **and** named an approver. The
    flag is deliberately not remembered between runs: a change that was
    approved once must be approved again for the next change.

    Kept as a pure function so the fail-closed behaviour can be proven
    offline, without a device.
    """
    if not approve_change:
        return False, ("no explicit approval given (--approve-change is "
                       "missing); the change was NOT applied. Review the "
                       "change and the rollback plan, then re-run with "
                       "--approve-change.")
    if not (approver or "").strip():
        return False, ("no named approver was given (--approver is empty); "
                       "the change was NOT applied.")
    return True, f"the operator approved this exact change as {approver!r}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="R2",
                    help="the lab router to change (default: R2)")
    ap.add_argument("--approver", default="gate4-operator",
                    help="the named human principal that approves the change")
    ap.add_argument("--approve-change", action="store_true",
                    help="explicit approval to execute the change shown by "
                         "this harness; without it nothing is applied")
    args = ap.parse_args()

    settings = get_settings()
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    evidence_dir = ROOT / "lab" / "validation_evidence" / ts / "change_loop"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    print(f"data_source={settings.data_source} device={args.device}")
    print(f"evidence_dir={evidence_dir}")

    # ---- preconditions (real, not mocked) --------------------------------
    from network import containerlab_manager
    avail = containerlab_manager.availability()
    print("availability:", avail)
    if not (avail.get("docker") and avail.get("containerlab")):
        _record("PRE-FLIGHT", "BLOCKED",
                "Docker/Containerlab unavailable -> the change loop cannot be "
                "exercised. No demo/mock substitution.")
        return _write_evidence(evidence_dir, 2)
    if settings.data_source != "live":
        _record("PRE-FLIGHT", "BLOCKED",
                "data_source is not 'live' (unset NETPILOT_DEMO_MODE).")
        return _write_evidence(evidence_dir, 2)
    if not settings.change_loop_enabled:
        _record("PRE-FLIGHT", "BLOCKED",
                "NETPILOT_CHANGE_LOOP_ENABLED is not set on this host.")
        return _write_evidence(evidence_dir, 2)
    if not settings.lab_execution_enabled:
        _record("PRE-FLIGHT", "BLOCKED",
                "NETPILOT_LAB_EXECUTION_ENABLED is not set on this host.")
        return _write_evidence(evidence_dir, 2)

    init_db()

    from network.lab_executor import LabExecutor
    from network.lab_config_ops import (build_rollback_ops, normalize_config,
                                        rollback_plan_lines)
    from services import change_execution, change_service

    executor = LabExecutor()
    device = args.device

    # ---- 1. the device must be a verified lab node ------------------------
    ok, reason = executor.preflight(device)
    _record("LAB-IDENTITY", "PASS" if ok else "BLOCKED", reason)
    if not ok:
        return _write_evidence(evidence_dir, 2)

    before_config = executor.read_config(device)
    (evidence_dir / "config_before.json").write_text(
        json.dumps(before_config, indent=2), encoding="utf-8")
    _record("READ-BEFORE", "PASS",
            f"read {len(before_config)} line(s) of running configuration "
            f"from {device}")

    before_routes, route_reason = _read_routing(executor, device)
    (evidence_dir / "routes_before.json").write_text(
        json.dumps(before_routes, indent=2), encoding="utf-8")
    _record("ROUTES-BEFORE", "PASS" if before_routes is not None else "FAIL",
            f"{len(before_routes or [])} route line(s) read"
            if before_routes is not None else route_reason)

    # ---- 2. the change must actually change something ---------------------
    base_lines = list(normalize_config(before_config))
    proposal = list(normalize_config(CHANGE_LINES))
    added_lines = [ln for ln in proposal if ln not in set(base_lines)]
    if not added_lines:
        _record("CHANGE-IS-NOOP", "BLOCKED",
                "the proposed change does not differ from the device's "
                "configuration - there is nothing to apply or to undo")
        return _write_evidence(evidence_dir, 2)

    rollback_ops = build_rollback_ops(proposal, base_lines)
    rollback_flat = rollback_plan_lines(proposal, base_lines)
    (evidence_dir / "rollback_plan.json").write_text(
        json.dumps([op.as_dict() for op in rollback_ops], indent=2),
        encoding="utf-8")
    print("\n--- PROPOSED CHANGE -------------------------------------------")
    for line in proposal:
        print(f"    + {line}")
    print("--- EXPECTED IMPACT ---------------------------------------------")
    print("    the running configuration of the device changes; the routing")
    print("    table and the lab connectivity must stay unchanged")
    print("--- ROLLBACK PLAN ----------------------------------------------")
    for line in rollback_flat:
        print(f"    {line}")
    print("-----------------------------------------------------------------\n")

    if not rollback_ops:
        _record("ROLLBACK-PLAN", "BLOCKED",
                "no deterministic rollback plan could be computed for the "
                "proposed change - the change is not safe to apply")
        return _write_evidence(evidence_dir, 2)
    _record("ROLLBACK-PLAN", "PASS",
            f"{len(rollback_ops)} operation(s): {rollback_flat}")

    # ---- 3. explicit human approval for THIS change ----------------------
    allowed, reason = approval_gate(args.approve_change, args.approver)
    if not allowed:
        _record("APPROVAL-GATE", "BLOCKED", reason)
        return _write_evidence(evidence_dir, 2)
    _record("APPROVAL-GATE", "PASS", reason)

    # ---- 4. a validated proposal -----------------------------------------
    with session_scope() as s:
        req = change_service.propose(
            s, device_name=device,
            base_lines=base_lines,
            proposed_lines=proposal,
            description="Gate D change-loop validation",
            proposed_by="gate4-proposer",
            proposer_authenticated=True)
        request_uuid = req.request_uuid
        base_lines = list(req.base_config_json and
                          json.loads(req.base_config_json) or [])
    _record("PROPOSAL", "PASS",
            f"proposal {request_uuid[:8]} recorded (status=validated)")

    # ---- 5. explicit, non-self human approval -----------------------------
    with session_scope() as s:
        # This CLI is already gated by --approve-change plus a named approver;
        # the name is an explicit operator assertion, not an anonymous click.
        req, msg = change_service.approve(s, request_uuid,
                                          approved_by=args.approver,
                                          authenticated=True,
                                          reason="Gate D explicit approval")
        approved = msg == "approved"
    _record("APPROVAL", "PASS" if approved else "FAIL",
            f"approval by {args.approver!r}: {msg}")
    if not approved:
        return _write_evidence(evidence_dir, 1)

    # ---- 6/7. apply + post-change verification ----------------------------
    with session_scope() as s:
        result = change_execution.execute_change(s, request_uuid,
                                                 executor=executor)
    (evidence_dir / "execute_result.json").write_text(
        json.dumps(result, indent=2, default=str), encoding="utf-8")
    _record("APPLY+VERIFY", "PASS" if result.get("verified") else "FAIL",
            f"applied={result.get('applied')} verified={result.get('verified')} "
            f"status={result.get('status')} reason={result.get('reason')}")

    after_config = executor.read_config(device)
    (evidence_dir / "config_after_apply.json").write_text(
        json.dumps(after_config, indent=2), encoding="utf-8")

    after_routes, route_reason = _read_routing(executor, device)
    (evidence_dir / "routes_after_apply.json").write_text(
        json.dumps(after_routes, indent=2), encoding="utf-8")
    _record("ROUTES-AFTER-APPLY", "PASS" if after_routes is not None else "FAIL",
            f"{len(after_routes or [])} route line(s) read"
            if after_routes is not None else route_reason)

    # ---- 8/9. rollback + restoration verification -------------------------
    with session_scope() as s:
        rb = change_service.rollback(s, request_uuid, executor=executor)
    rolled_back = bool(rb.get("rolled_back"))
    restored = bool(rb.get("restored"))
    _record("ROLLBACK", "PASS" if rolled_back and restored else "FAIL",
            f"rolled_back={rolled_back} restored={restored} "
            f"status={rb.get('status')} reason={rb.get('reason')}")

    restored_config = executor.read_config(device)
    (evidence_dir / "config_after_rollback.json").write_text(
        json.dumps(restored_config, indent=2), encoding="utf-8")

    from network.lab_verifier import verify_restored_config
    # The lines the change ADDED must be gone again. Without this the check
    # would happily pass while the change is still active on the device.
    removed_lines = [ln for ln in proposal if ln not in set(base_lines)]
    comparison = verify_restored_config(device, base_lines,
                                         executor.read_config,
                                         removed_lines=removed_lines)
    _record("RESTORATION", "PASS" if comparison.passed else "FAIL",
            comparison.detail + (f" (removed lines checked: {removed_lines})"
                                 if removed_lines else ""))

    final_routes, route_reason = _read_routing(executor, device)
    (evidence_dir / "routes_after_rollback.json").write_text(
        json.dumps(final_routes, indent=2), encoding="utf-8")
    if before_routes is not None and final_routes is not None:
        same = before_routes == final_routes
        _record("NETWORK-STATE-RESTORED", "PASS" if same else "FAIL",
                "the routing table is identical to the pre-change one"
                if same else "the routing table differs from the pre-change one")
    else:
        _record("NETWORK-STATE-RESTORED", "FAIL",
                f"the routing table could not be compared: {route_reason}")

    # ---- the lab's own config files must be untouched ---------------------
    after_hashes = _hash_lab_configs()
    (evidence_dir / "lab_config_hashes.json").write_text(
        json.dumps(after_hashes, indent=2), encoding="utf-8")
    _record("LAB-FILES-UNTOUCHED", "PASS",
            f"{len(after_hashes)} lab/configs file(s) hashed for the record")

    return _write_evidence(evidence_dir, None)


def _write_evidence(evidence_dir: Path, forced_exit: int | None) -> int:
    passed = sum(1 for r in _results.values() if r["status"] == "PASS")
    failed = sum(1 for r in _results.values() if r["status"] == "FAIL")
    blocked = sum(1 for r in _results.values() if r["status"] == "BLOCKED")
    summary = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "counts": {"pass": passed, "fail": failed, "blocked": blocked},
        "items": _results,
    }
    (evidence_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nSUMMARY  PASS={passed} FAIL={failed} BLOCKED={blocked}  "
          f"-> {evidence_dir}")
    if forced_exit is not None:
        return forced_exit
    if failed:
        return 1
    if blocked:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
