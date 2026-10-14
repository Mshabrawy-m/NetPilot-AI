"""The independent verifier entry point (deterministic, no LLM)."""
from __future__ import annotations

from ai.schemas import Check, VerifierReport
from ai.verification import policy_checks

VERIFIER_VERSION = "phase3-1.0"

#: Checks that MUST be conclusively passed for a 'passed' verdict.
_REQUIRED_CHECKS = {"target_exists", "config_structured", "targets_in_scope",
                    "policy_constraints"}


def verify(proposal: dict | None, intent: dict | None,
           known_devices: set[str]) -> VerifierReport:
    """Run every deterministic check and aggregate an honest verdict.

    * 'failed'        - a check conclusively failed (unknown target, prohibited
                        command, malformed config, out-of-scope device...).
    * 'inconclusive'  - a required check could not be executed (blocked
                        proposal, unconfirmable policy) - never a false pass.
    * 'passed'        - all required checks executed and passed. A static pass
                        does NOT imply live behaviour or a Digital-Twin result.
    """
    proposal = proposal or {}
    intent = intent or {}

    if proposal.get("status") != "ok":
        reason = proposal.get("blocked_reason") or \
            f"proposal status is {proposal.get('status')!r}"
        return VerifierReport(
            status="inconclusive",
            checks=[Check(name="proposal_available", passed=None,
                          severity="info",
                          finding=f"no valid proposal to verify: {reason}")],
            skipped_checks=["all static checks (no proposal)"],
            version=VERIFIER_VERSION)

    lines = list(proposal.get("config_lines") or [])
    device = proposal.get("device")
    stated_policy = bool(intent.get("services_or_reachability")
                         or intent.get("constraints"))

    raw_checks = [
        policy_checks.check_target_exists(device, known_devices),
        policy_checks.check_syntax_and_prohibited(lines),
        policy_checks.check_out_of_scope(lines, device, known_devices),
        policy_checks.check_policy_constraints(intent, lines),
        policy_checks.check_lab_invariants(lines),
    ]

    checks = [Check(**c) for c in raw_checks]
    statuses = [c.passed for c in checks]

    if False in statuses:
        status = "failed"
    else:
        required_inconclusive = any(
            c.passed is None and c.name in _REQUIRED_CHECKS
            # policy_constraints is only required when the user stated any
            and not (c.name == "policy_constraints" and not stated_policy)
            for c in checks)
        status = "inconclusive" if required_inconclusive else "passed"

    skipped = [
        f"{c.name}: {c.finding}" for c in checks
        if c.passed is None and c.name not in _REQUIRED_CHECKS
    ]
    evidence_refs = [f"config:{device}"] if device else []
    return VerifierReport(status=status, checks=checks,
                          skipped_checks=skipped,
                          evidence_refs=evidence_refs,
                          version=VERIFIER_VERSION)
