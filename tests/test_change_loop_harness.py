"""The Gate D harness itself must be safe to run - and safe to *not* run.

``tools/phase4_change_loop_validation.py`` is the only place in the project
that writes configuration to a device. Two properties of it are therefore
pinned here, completely offline (no Docker, no Containerlab, no device):

* the change it proposes is a **real** change against the lab's own R2
  configuration - the previous proposal repeated three lines that were
  already on the device, which made the whole run vacuous - and it has a
  deterministic undo;
* the explicit, per-change human approval gate fails closed: without the
  flag, or without a named approver, nothing is applied.

The lab configuration is read from the repository, so a change to
``lab/configs/R2/frr.conf`` that invalidates the proposal is caught here
rather than in the middle of a live run.
"""
from __future__ import annotations

import pathlib

from ai.verification.config_validation import validate_lines
from network.lab_config_ops import build_rollback_ops
from tools.phase4_change_loop_validation import CHANGE_LINES, approval_gate

ROOT = pathlib.Path(__file__).resolve().parents[1]
R2_CONFIG = ROOT / "lab" / "configs" / "R2" / "frr.conf"


def _config_lines(path: pathlib.Path) -> list[str]:
    """The configuration lines of an FRR config file (comments dropped)."""
    out: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("!"):
            continue
        out.append(line)
    return out


def _base() -> list[str]:
    return _config_lines(R2_CONFIG)


# ---------------------------------------------------------------------------
# The proposed change
# ---------------------------------------------------------------------------
def test_the_proposed_change_is_not_already_on_the_router():
    """A proposal that repeats the current configuration changes nothing.

    That was the defect in the previous harness: all three of its lines
    were already on R2, so the run proved nothing.
    """
    base = set(_base())
    added = [ln for ln in CHANGE_LINES if ln not in base]
    assert added, ("the proposed change is already present on the router - "
                   "the change-loop run would be vacuous")


def test_the_proposed_change_passes_the_deterministic_validation():
    findings = validate_lines(CHANGE_LINES)
    assert findings, "the proposal must be validated, not skipped"
    for finding in findings:
        assert finding["syntax_ok"], finding
        assert not finding["prohibited"], finding


def test_the_proposed_change_has_a_deterministic_undo():
    ops = build_rollback_ops(CHANGE_LINES, _base())
    assert ops, "the change must have a rollback plan"
    flat = [(op.context, tuple(op.lines)) for op in ops]
    assert ("router ospf 100",
            ("no network 10.0.99.0/24 area 0",)) in flat


def test_the_proposed_change_never_touches_the_management_path():
    """No addressing, no interface, no hostname - only the OSPF statement."""
    forbidden = ("interface ", "ip address", "hostname", "router-id")
    for line in CHANGE_LINES:
        assert not line.startswith(forbidden), line


# ---------------------------------------------------------------------------
# The explicit, per-change human approval
# ---------------------------------------------------------------------------
def test_the_approval_gate_refuses_without_the_explicit_flag():
    allowed, reason = approval_gate(False, "gate4-operator")
    assert allowed is False
    assert "--approve-change" in reason
    assert "NOT applied" in reason


def test_the_approval_gate_refuses_without_a_named_approver():
    allowed, reason = approval_gate(True, "   ")
    assert allowed is False
    assert "approver" in reason
    assert "NOT applied" in reason


def test_the_approval_gate_allows_only_with_both():
    allowed, reason = approval_gate(True, "gate4-operator")
    assert allowed is True
    assert "gate4-operator" in reason


def test_the_approval_is_not_remembered_between_runs():
    """Approval is per change: a second call without the flag refuses again."""
    assert approval_gate(True, "gate4-operator")[0] is True
    assert approval_gate(False, "gate4-operator")[0] is False
