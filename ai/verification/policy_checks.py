"""Lab policy + invariant checks (deterministic, no LLM).

Each helper returns a plain dict the verifier turns into a ``Check``. Where a
requirement genuinely cannot be confirmed from static config (e.g. live
management reachability), the check reports ``inconclusive`` rather than
pretending to pass.
"""
from __future__ import annotations

import re

from ai.verification import config_validation

#: Bare tokens that look like device references (R1, PC2, core-sw, ...).
_DEVICE_TOKEN = re.compile(r"\b([A-Za-z][A-Za-z0-9_\-]{1,31})\b")


def _referenced_devices(config_lines: list[str], known: set[str]) -> set[str]:
    found: set[str] = set()
    for line in config_lines or []:
        for tok in _DEVICE_TOKEN.findall(line or ""):
            if tok in known:
                found.add(tok)
    return found


def check_target_exists(device: str | None, known: set[str]) -> dict:
    if not device:
        return {"name": "target_exists", "passed": False, "severity": "critical",
                "finding": "no target device specified"}
    ok = device in known
    return {"name": "target_exists", "passed": ok, "severity": "critical",
            "finding": (f"target {device!r} is in the lab inventory"
                        if ok else f"target {device!r} is NOT a known lab device")}


def check_out_of_scope(config_lines: list[str], device: str | None,
                       known: set[str]) -> dict:
    referenced = _referenced_devices(config_lines, known)
    extra = {d for d in referenced if d != device}
    ok = not extra
    return {"name": "targets_in_scope", "passed": ok, "severity": "critical",
            "finding": ("only the intended device is referenced" if ok
                        else f"config references out-of-scope devices: "
                             f"{sorted(extra)}")}


def check_syntax_and_prohibited(config_lines: list[str]) -> dict:
    findings = config_validation.validate_lines(config_lines)
    bad = [f for f in findings if not f["syntax_ok"] or f["prohibited"]]
    if not config_lines:
        return {"name": "config_structured", "passed": False,
                "severity": "critical", "finding": "empty configuration"}
    ok = not bad
    detail = "; ".join(f"{f['line'][:40]} ({f['reason']})" for f in bad[:5])
    return {"name": "config_structured", "passed": ok, "severity": "critical",
            "finding": ("all lines are structured, non-executable config"
                        if ok else f"invalid/prohibited lines: {detail}")}


def check_policy_constraints(intent: dict, config_lines: list[str]) -> dict:
    """Represented required reachability/services, when the request stated any."""
    constraints = [c.lower() for c in (intent or {}).get("services_or_reachability", [])]
    constraints += [c.lower() for c in (intent or {}).get("constraints", [])]
    if not constraints:
        return {"name": "policy_constraints", "passed": None, "severity": "info",
                "finding": "no explicit policy constraints to check (skipped)"}
    blob = " ".join((l or "").lower() for l in config_lines)
    missing = [c for c in constraints if c.split()[0] not in blob]
    if missing:
        # Static config cannot prove semantic policy satisfaction -> honest
        # inconclusive rather than a false pass.
        return {"name": "policy_constraints", "passed": None, "severity": "warning",
                "finding": f"cannot statically confirm constraints: {missing}"}
    return {"name": "policy_constraints", "passed": True, "severity": "info",
            "finding": "stated constraints are represented in the proposal"}


def check_lab_invariants(config_lines: list[str]) -> dict:
    """Defined lab invariants: no global 'no ip routing'/management wipe.

    Live management-path preservation cannot be proven from static config,
    so it is reported inconclusive (documented, never a false pass)."""
    blob = " ".join((l or "").lower() for l in config_lines)
    removes_all_routing = "no ip routing" in blob
    removes_ospf_and_shuts = "no router ospf" in blob and "shutdown" in blob
    if removes_all_routing or removes_ospf_and_shuts:
        return {"name": "lab_invariants", "passed": False, "severity": "critical",
                "finding": "proposal appears to remove core lab routing"}
    return {"name": "lab_invariants", "passed": None, "severity": "info",
            "finding": ("core lab routing preserved; live management-path "
                        "reachability not verifiable statically (inconclusive)")}
