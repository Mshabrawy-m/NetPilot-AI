"""Offline tests for the lab verifier (network.lab_verifier).

The verifier is the part that turns a read-back into a verdict. It has to be
honest in both directions:

* a line that is not there is **missing** - never "close enough";
* a removal that is proven by the **absence** of its target - a removal can
  never appear in the read-back, so a verification that only looked for the
  proposal's own lines would call every removal a partial application.

Nothing here touches a device: the read-back is injected.
"""
from __future__ import annotations

from network.lab_verifier import (compare_config, verify_applied_config,
                                   verify_restored_config)


BASE = [
    "hostname R2",
    "interface eth2",
    "ip address 10.0.21.2/24",
]


# -- compare_config ---------------------------------------------------------
def test_compare_config_passes_when_every_expected_line_is_present():
    result = compare_config(BASE, list(BASE))
    assert result.passed is True
    assert result.missing == []


def test_compare_config_reports_a_missing_line():
    result = compare_config(BASE, BASE[:2])
    assert result.passed is False
    assert result.missing == ["ip address 10.0.21.2/24"]


def test_compare_config_never_passes_without_an_expectation():
    result = compare_config([], BASE)
    assert result.passed is False


# -- verify_applied_config --------------------------------------------------
def test_a_removal_is_verified_by_the_absence_of_its_target():
    """The proposal removes the interface: the read-back must not have it."""
    actual = ["hostname R2"]
    result = verify_applied_config("R2", ["no interface eth2"],
                                   lambda _device: actual)
    assert result.passed is True, result.detail
    assert result.missing == []


def test_a_removal_that_did_not_land_is_not_a_pass():
    """The interface is still there: the removal did not take effect."""
    result = verify_applied_config("R2", ["no interface eth2"],
                                   lambda _device: list(BASE))
    assert result.passed is False
    assert result.missing == ["interface eth2"]
    assert "removal" in result.detail


def test_a_proposal_that_only_adds_is_verified_by_presence():
    actual = BASE + ["interface eth3"]
    result = verify_applied_config("R2", ["interface eth3"],
                                   lambda _device: actual)
    assert result.passed is True, result.detail


def test_an_unreadable_device_is_never_a_pass():
    def boom(_device):
        raise RuntimeError("vtysh failed")

    result = verify_applied_config("R2", ["hostname R3"], boom)
    assert result.passed is False
    assert result.unreadable is True


# -- verify_restored_config -------------------------------------------------
def test_the_restoration_is_verified_against_the_base():
    result = verify_restored_config("R2", BASE, lambda _device: list(BASE),
                                     removed_lines=["interface eth3"])
    assert result.passed is True, result.detail


def test_a_device_that_kept_the_change_is_not_restored():
    """The base is there AND the change is still active -> not restored."""
    actual = BASE + ["interface eth3"]
    result = verify_restored_config("R2", BASE, lambda _device: actual,
                                     removed_lines=["interface eth3"])
    assert result.passed is False
    assert "still active" in result.detail
