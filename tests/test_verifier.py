"""Independent deterministic verifier + config/policy checks (spec §8/§9).

The verifier is pure code - it never calls the LLM - and it never reports a
static pass unless every required check actually passed.
"""
from __future__ import annotations

from ai.verification import config_validation, policy_checks, verifier


KNOWN = {"R1", "R2", "PC1"}


def _proposal(lines, device="R1", status="ok"):
    return {"status": status, "device": device, "config_lines": lines,
            "intent": "test", "assumptions": [], "diff": None,
            "blocked_reason": None}


# -- config_validation ------------------------------------------------------
def test_clean_frr_line_is_accepted():
    ok, reason = config_validation.syntax_ok(" ip access-group 100 in")
    assert ok and reason == ""


def test_shell_injection_is_rejected():
    ok, _ = config_validation.syntax_ok("ip address 1.1.1.1; rm -rf /")
    assert ok is False


def test_prohibited_apply_commands_detected():
    for bad in ("write memory", "iptables -F", "configure terminal",
                "docker exec R2 reload"):
        prohibited, matched = config_validation.prohibited_present(bad)
        assert prohibited, f"{bad!r} should be prohibited ({matched})"


def test_validate_lines_flags_prohibited_and_metachars():
    findings = config_validation.validate_lines(
        ["interface eth1", "reload now", "ip address 1.1.1.1 | sh"])
    assert findings[0]["syntax_ok"] and not findings[0]["prohibited"]
    assert findings[1]["prohibited"]                 # 'reload'
    assert not findings[2]["syntax_ok"]              # '|' shell metachar


# -- policy_checks ----------------------------------------------------------
def test_target_exists_requires_inventory_membership():
    assert policy_checks.check_target_exists("R1", KNOWN)["passed"] is True
    assert policy_checks.check_target_exists("GHOST", KNOWN)["passed"] is False
    assert policy_checks.check_target_exists(None, KNOWN)["passed"] is False


def test_out_of_scope_device_detected():
    res = policy_checks.check_out_of_scope(
        ["ip route 10.0.0.0/24 via R2"], "R1", KNOWN)
    assert res["passed"] is False       # references R2 while targeting R1
    res_ok = policy_checks.check_out_of_scope(["interface eth1"], "R1", KNOWN)
    assert res_ok["passed"] is True


def test_lab_invariant_blocks_removing_routing():
    res = policy_checks.check_lab_invariants(["no ip routing"])
    assert res["passed"] is False


def test_policy_constraints_inconclusive_when_unconfirmable():
    intent = {"services_or_reachability": ["allow https to web tier"],
              "constraints": []}
    res = policy_checks.check_policy_constraints(intent, ["interface eth1"])
    assert res["passed"] is None        # static config cannot prove semantics


# -- verifier aggregation ---------------------------------------------------
def test_blocked_proposal_is_inconclusive_not_passed():
    report = verifier.verify(_proposal([], status="blocked"), {}, KNOWN)
    assert report.status == "inconclusive"
    assert report.checks[0].name == "proposal_available"


def test_missing_proposal_is_inconclusive():
    report = verifier.verify(None, None, KNOWN)
    assert report.status == "inconclusive"


def test_valid_proposal_passes_all_required_checks():
    report = verifier.verify(_proposal(["interface eth1",
                                         " ip access-group 100 in"]), {}, KNOWN)
    assert report.status == "passed"
    # lab_invariants is advisory (inconclusive) and does not block the pass.
    names = {c.name for c in report.checks}
    assert {"target_exists", "config_structured", "targets_in_scope"} <= names


def test_unknown_device_fails():
    report = verifier.verify(_proposal(["interface eth1"], device="GHOST"),
                             {}, KNOWN)
    assert report.status == "failed"


def test_prohibited_command_fails():
    report = verifier.verify(_proposal(["write memory"]), {}, KNOWN)
    assert report.status == "failed"


def test_stated_policy_keeps_it_inconclusive():
    # A stated reachability constraint can't be proven statically -> never a
    # false 'passed'.
    intent = {"services_or_reachability": ["preserve management access"],
              "constraints": []}
    report = verifier.verify(_proposal(["interface eth1"]), intent, KNOWN)
    assert report.status == "inconclusive"


def test_verifier_is_deterministic_and_versioned():
    p = _proposal(["interface eth1", " ip access-group 100 in"])
    r1 = verifier.verify(p, {}, KNOWN)
    r2 = verifier.verify(p, {}, KNOWN)
    assert r1.status == r2.status == "passed"
    assert r1.version == verifier.VERIFIER_VERSION
