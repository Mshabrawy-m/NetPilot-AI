"""Post-change and restoration verification for the isolated lab.

This is the *runtime* half of the change loop. ``ai/verification/verifier.py``
judges a proposal **statically** (is it well-formed, in scope, free of
prohibited commands?) - it never touches a device and therefore can never
prove that a change actually took effect. This module closes that gap:

* :func:`compare_config` is a pure function: it compares an *expected* set of
  configuration lines against the lines actually read back from a device.
* :func:`verify_applied_config` reads the device's running configuration and
  asserts that every expected line is present.
* :func:`verify_restored_config` does the same against the pre-change config
  to prove a rollback really restored the device.

Honesty rules that are load-bearing here:

* A line that cannot be found is reported as **missing**, never as "close
  enough" - the verdict is ``passed`` only when the read-back contains every
  expected line.
* If the running configuration cannot be read at all, the verdict is a
  failure with ``unreadable=True`` - a verification that could not run is
  never reported as a pass.
* The comparison ignores ordering and indentation (FRR re-renders a config
  in its own canonical form) but does **not** ignore the content of a line.

Everything in this module is read-only: it never writes to a device.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from network.lab_config_ops import (normalize_config, normalize_line,
                                    split_proposal_lines)

@dataclass
class ConfigComparison:
    """The deterministic verdict of one expected-vs-actual comparison."""

    passed: bool
    missing: list[str] = field(default_factory=list)
    unexpected: list[str] = field(default_factory=list)
    detail: str = ""
    #: True when the actual configuration could not be read at all.
    unreadable: bool = False

    def as_dict(self) -> dict:
        return {
            "passed": self.passed,
            "missing": list(self.missing),
            "unexpected": list(self.unexpected),
            "detail": self.detail,
            "unreadable": self.unreadable,
        }


def compare_config(expected: list[str] | None,
                   actual: list[str] | None) -> ConfigComparison:
    """Compare expected config lines against the lines read back from a device.

    ``passed`` is True only when **every** expected line is present in the
    actual configuration. Extra lines in ``actual`` are reported under
    ``unexpected`` but do not by themselves fail the comparison - a device may
    legitimately hold configuration the proposal did not mention. The caller
    decides what to do with them; the verdict itself stays honest.
    """
    exp = normalize_config(expected)
    act = normalize_config(actual)
    if not exp:
        return ConfigComparison(
            passed=False, detail="no expected configuration to verify")
    act_set = set(act)
    missing = [ln for ln in exp if ln not in act_set]
    exp_set = set(exp)
    unexpected = [ln for ln in act if ln not in exp_set]
    if missing:
        detail = (f"{len(missing)}/{len(exp)} expected line(s) missing from "
                  f"the running configuration: {missing[:5]}")
        return ConfigComparison(passed=False, missing=missing,
                                unexpected=unexpected, detail=detail)
    detail = (f"all {len(exp)} expected line(s) present in the running "
              f"configuration")
    return ConfigComparison(passed=True, unexpected=unexpected, detail=detail)


def verify_applied_config(device_name: str, expected_lines: list[str],
                          read_config) -> ConfigComparison:
    """Post-change verification: read the device back and compare.

    ``read_config`` is a callable ``(device_name) -> list[str]`` that returns
    the device's running configuration. It is injected so this module stays
    testable without a lab, and so the executor stays the only thing that
    knows how to talk to a device.

    A proposal is a delta, so it is verified in **two** directions: the lines
    it adds must be present in the read-back, and the targets it removes
    must be **absent** from it. A removal can never be present - looking for
    it there would call every removal a partial application.
    """
    try:
        actual = read_config(device_name)
    except Exception as exc:  # noqa: BLE001 - a failed read is not a pass
        return ConfigComparison(
            passed=False, unreadable=True,
            detail=(f"could not read the running configuration of "
                    f"{device_name!r}: {type(exc).__name__}: {exc}"))
    if actual is None:
        return ConfigComparison(
            passed=False, unreadable=True,
            detail=(f"could not read the running configuration of "
                    f"{device_name!r}"))
    present, absent = split_proposal_lines(expected_lines)
    if present:
        result = compare_config(present, actual)
        if not result.passed:
            return result
    else:
        result = ConfigComparison(
            passed=True,
            detail="the proposal only removes configuration - nothing to add")
    actual_set = set(normalize_config(actual))
    still_there = [t for t in absent if normalize_line(t) in actual_set]
    if still_there:
        result.passed = False
        result.missing = list(still_there)
        result.detail = (f"{len(still_there)}/{len(absent)} removal(s) did not "
                         f"take effect - the line(s) are still on the device: "
                         f"{still_there[:5]}")
    return result


def verify_restored_config(device_name: str, base_lines: list[str],
                            read_config, removed_lines: list[str] | None = None
                            ) -> ConfigComparison:
    """Restoration verification: the device must hold the pre-change config.

    Restoration is checked with **two** conditions, because "the base is
    still there" alone is not enough:

    1. every base line is present again, and
    2. none of the lines the change *added* (``removed_lines``) is still
       there.

    A device that merely kept the old configuration while the added
    configuration is still active has NOT been restored, and this function
    says so. FRR's own output noise (comments, the trailing ``end``) is
    ignored, so the check is not defeated by the device's rendering.

    Same mechanics as :func:`verify_applied_config`; separate name because the
    acceptance criterion it satisfies is different (rollback happened) and
    keeping them apart makes the audit trail unambiguous.
    """
    try:
        actual = read_config(device_name)
    except Exception as exc:  # noqa: BLE001 - a failed read is not a pass
        return ConfigComparison(
            passed=False, unreadable=True,
            detail=(f"could not read the running configuration of "
                    f"{device_name!r}: {type(exc).__name__}: {exc}"))
    if actual is None:
        return ConfigComparison(
            passed=False, unreadable=True,
            detail=(f"could not read the running configuration of "
                    f"{device_name!r}"))

    base = compare_config(base_lines, actual)
    if not base.passed:
        base.detail = "restoration not verified - " + base.detail
        return base

    if removed_lines:
        actual_set = set(normalize_config(actual))
        base_set = set(normalize_config(base_lines))
        still_there = [ln for ln in removed_lines
                       if normalize_line(ln) in actual_set
                       and normalize_line(ln) not in base_set]
        if still_there:
            base.passed = False
            base.missing = list(still_there)
            base.detail = ("restoration not verified - the change is still "
                           f"active on the device: {still_there[:5]}")
    return base


def config_diff(expected: list[str] | None,
                actual: list[str] | None) -> str:
    """A short, deterministic, human-readable difference summary."""
    comparison = compare_config(expected, actual)
    parts: list[str] = []
    if comparison.missing:
        parts.append("missing: " + ", ".join(comparison.missing[:10]))
    if comparison.unexpected:
        parts.append("not expected: " + ", ".join(comparison.unexpected[:10]))
    if not parts:
        parts.append("no difference")
    return "; ".join(parts)


__all__ = [
    "ConfigComparison", "compare_config", "config_diff", "normalize_line",
    "normalize_config", "verify_applied_config", "verify_restored_config",
]
