"""Offline tests for the pure configuration model (network.lab_config_ops).

The rollback plan is the part of the change loop that must never be
theatrical: it has to actually *undo* the proposal (remove what was added,
restore what was changed), not merely merge the base config on top of it.
These tests pin that behaviour without any device.
"""
from __future__ import annotations

from network.lab_config_ops import (build_apply_ops, build_rollback_ops,
                                    command_class, config_head,
                                    normalize_config, normalize_line,
                                    parse_blocks, removal_form,
                                    rollback_plan_lines,
                                    unrollbackable_lines)


BASE = [
    "hostname R2",
    "interface eth1",
    "ip address 10.0.12.2/24",
    "router ospf 100",
    "network 10.0.12.0/24 area 0",
]


def test_normalize_line_collapses_whitespace():
    assert normalize_line("  ip   address   10.0.12.2/24 ") == \
        "ip address 10.0.12.2/24"
    assert normalize_line("") == ""
    assert normalize_line(None) == ""


def test_normalize_config_drops_comments_and_duplicates():
    lines = ["! a comment", "hostname R2", "hostname R2", "", "  ip address x"]
    assert normalize_config(lines) == ["hostname R2", "ip address x"]


def test_config_head():
    assert config_head("interface eth1") == "interface"
    assert config_head("  no ip routing") == "no"
    assert config_head("") == ""


def test_parse_blocks_groups_sub_lines_under_their_context():
    top, blocks = parse_blocks(BASE)
    assert top == ["hostname R2"]
    assert [b.context for b in blocks] == ["interface eth1", "router ospf 100"]
    assert blocks[0].lines == ["ip address 10.0.12.2/24"]
    assert blocks[1].lines == ["network 10.0.12.0/24 area 0"]


def test_apply_ops_keep_the_context_structure():
    ops = build_apply_ops(BASE)
    assert ops[0].context is None
    assert ops[0].lines == ["hostname R2"]
    assert ops[1].context == "interface eth1"


def test_rollback_removes_a_block_the_change_added():
    proposed = BASE + ["interface eth3", "ip address 10.0.23.2/24"]
    ops = build_rollback_ops(proposed, BASE)
    flat = [(op.context, tuple(op.lines)) for op in ops]
    assert (None, ("no interface eth3",)) in flat


def test_rollback_undoes_an_address_change_inside_an_interface():
    proposed = [
        "hostname R2",
        "interface eth1",
        "ip address 10.0.99.2/24",
        "router ospf 100",
        "network 10.0.12.0/24 area 0",
    ]
    ops = build_rollback_ops(proposed, BASE)
    flat = [(op.context, tuple(op.lines)) for op in ops]
    assert ("interface eth1",
            ("no ip address 10.0.99.2/24", "ip address 10.0.12.2/24")) in flat


def test_rollback_restores_a_changed_top_level_value():
    proposed = ["hostname R2-renamed", "interface eth1",
                "ip address 10.0.12.2/24", "router ospf 100",
                "network 10.0.12.0/24 area 0"]
    ops = build_rollback_ops(proposed, BASE)
    flat = [(op.context, tuple(op.lines)) for op in ops]
    assert (None, ("hostname R2",)) in flat


def test_rollback_uses_the_head_only_form_for_description():
    # FRR accepts `no description` but not `no description <text>`, and a
    # description always lives inside an interface block.
    base = ["hostname R2", "interface eth1", "ip address 10.0.12.2/24"]
    proposed = base + ["description to_R3"]
    ops = build_rollback_ops(proposed, base)
    flat = [(op.context, tuple(op.lines)) for op in ops]
    assert ("interface eth1", ("no description",)) in flat


def test_rollback_of_an_unchanged_config_is_empty():
    assert build_rollback_ops(BASE, BASE) == []


def test_rollback_plan_lines_is_a_flat_audit_view():
    proposed = BASE + ["interface eth3", "ip address 10.0.23.2/24"]
    flat = rollback_plan_lines(proposed, BASE)
    assert "no interface eth3" in flat


# ---------------------------------------------------------------------------
# The FRR ``no`` forms
# ---------------------------------------------------------------------------
def test_removal_form_uses_the_frr_no_forms():
    """FRR's ``no`` forms are not uniformly ``no <line>``.

    Verified against the FRR 8.4 command reference and sources:
    ``lib/if.c`` defines ``no description`` (no argument), ``lib/command.c``
    defines ``no hostname`` / ``no banner motd``, and ``zebra/interface.c``
    defines ``no ip address A.B.C.D/M`` (with the argument).
    """
    assert removal_form("description to_R1_eth1") == "no description"
    assert removal_form("hostname R2") == "no hostname"
    assert removal_form("banner motd default") == "no banner motd"
    assert removal_form("ospf router-id 10.1.1.2") == "no ospf router-id"
    assert removal_form("log file /var/log/frr/frr.log informational") == \
        "no log file"
    assert removal_form("log stdout") == "no log stdout"
    assert removal_form("ip address 10.0.21.2/24") == \
        "no ip address 10.0.21.2/24"
    assert removal_form("network 10.0.12.0/24 area 0") == \
        "no network 10.0.12.0/24 area 0"
    assert removal_form("interface eth3") == "no interface eth3"
    assert removal_form("router ospf 100") == "no router ospf 100"


def test_removal_form_is_unknown_for_a_command_frr_cannot_remove():
    """An unknown removal form is reported, never guessed."""
    assert removal_form("service password-encryption") is None
    assert removal_form("banner login something") is None


def test_command_class_distinguishes_replace_and_accumulate():
    assert command_class("hostname R2") == "exclusive"
    assert command_class("description to_R1") == "exclusive"
    assert command_class("ospf router-id 10.1.1.2") == "exclusive"
    assert command_class("ip address 10.0.12.2/24") == "slot"
    assert command_class("network 10.0.12.0/24 area 0") == "accumulating"
    assert command_class("neighbor 10.0.12.1") == "accumulating"


# ---------------------------------------------------------------------------
# The rollback plan must be a real undo
# ---------------------------------------------------------------------------
def test_a_removal_in_the_proposal_is_restored_and_never_negated():
    """A proposal line that is itself a removal must not become ``no no X``."""
    proposed = [
        "hostname R2",
        "interface eth1",
        "ip address 10.0.12.2/24",
        "interface eth2",
        "no ip address 10.0.21.2/24",
        "router ospf 100",
        "network 10.0.12.0/24 area 0",
    ]
    base = BASE + ["interface eth2", "ip address 10.0.21.2/24"]
    flat = rollback_plan_lines(proposed, base)
    assert not [ln for ln in flat if "no no" in ln]
    assert "ip address 10.0.21.2/24" in flat


def test_an_added_top_level_line_is_removed_by_the_rollback():
    """A line the proposal adds must be removed - not merged over."""
    base = ["hostname R2", "interface eth1", "ip address 10.0.12.2/24"]
    proposed = base + ["banner motd default"]
    flat = rollback_plan_lines(proposed, base)
    assert "no banner motd" in flat


def test_an_added_log_destination_is_removed_by_the_rollback():
    base = ["hostname R2", "interface eth1", "ip address 10.0.12.2/24"]
    proposed = base + ["log file /tmp/evil.log informational"]
    flat = rollback_plan_lines(proposed, base)
    assert "no log file" in flat
    assert not [ln for ln in flat if ln.startswith("no log file /tmp")]


def test_a_changed_top_level_value_is_restored():
    base = ["hostname R2", "interface eth1", "ip address 10.0.12.2/24"]
    proposed = ["hostname R2-renamed", "interface eth1",
                "ip address 10.0.12.2/24"]
    flat = rollback_plan_lines(proposed, base)
    assert "hostname R2" in flat


def test_a_block_the_proposal_removes_is_restored():
    base = BASE + ["interface eth2", "description to_PC1_eth1",
                   "ip address 10.0.21.2/24"]
    proposed = [
        "hostname R2",
        "interface eth1",
        "ip address 10.0.12.2/24",
        "no interface eth2",
        "router ospf 100",
        "network 10.0.12.0/24 area 0",
    ]
    ops = build_rollback_ops(proposed, base)
    flat = [(op.context, tuple(op.lines)) for op in ops]
    assert ("interface eth2",
            ("description to_PC1_eth1", "ip address 10.0.21.2/24")) in flat


def test_a_block_without_sub_lines_is_restored_by_re_entering_the_context():
    """A block the proposal removes is restored - even when it has no
    sub-lines.

    ``no interface eth3`` deletes the whole interface, so the rollback has to
    re-enter the (empty) context to bring it back. A plan that skipped such a
    block would be *incomplete*: the "verified rollback plan" gate would pass
    while the device could not be restored.
    """
    base = BASE + ["interface eth3"]
    proposed = [ln for ln in BASE if ln != "interface eth3"] + \
        ["no interface eth3"]
    ops = build_rollback_ops(proposed, base)
    flat = [(op.context, tuple(op.lines)) for op in ops]
    assert ("interface eth3", ()) in flat
    # the plan is not empty: there is something to undo
    assert ops


def test_the_plan_of_a_bare_block_removal_is_not_empty():
    """The plan of a change that only removes a bare block is not empty.

    Otherwise the "empty rollback plan" refusal would reject a change that is
    perfectly reversible - with the wrong reason.
    """
    base = BASE + ["interface eth3"]
    proposed = ["no interface eth3"]
    ops = build_rollback_ops(proposed, base)
    assert [(op.context, tuple(op.lines)) for op in ops] == \
        [("interface eth3", ())]


def test_the_plan_of_a_no_op_proposal_is_empty():
    """A proposal that changes nothing has nothing to undo.

    This is what makes a vacuous change visible: the rollback plan is empty,
    so the \"verified rollback plan\" gate refuses to apply it.
    """
    proposed = ["hostname R2", "interface eth1", "ip address 10.0.12.2/24"]
    assert build_rollback_ops(proposed, BASE) == []


def test_a_change_frr_cannot_undo_is_reported_as_unrollbackable():
    """Fail closed: an unknown removal form must not be guessed."""
    base = ["hostname R2", "interface eth1", "ip address 10.0.12.2/24"]
    proposed = base + ["service password-encryption"]
    assert unrollbackable_lines(proposed, base) == \
        ["service password-encryption"]
    # ... and no plan is built for it
    assert build_rollback_ops(proposed, base) == []


def test_the_plan_removes_a_block_the_proposal_added_wholesale():
    proposed = BASE + ["interface eth3", "ip address 10.0.23.2/24"]
    ops = build_rollback_ops(proposed, BASE)
    flat = [(op.context, tuple(op.lines)) for op in ops]
    assert (None, ("no interface eth3",)) in flat
    # the block's sub-lines are not removed one by one
    assert not [op for op in ops if op.context == "interface eth3"]


def test_a_proposal_inside_an_existing_block_is_undone_in_that_block():
    """The removal of an OSPF statement happens inside the OSPF block."""
    base = BASE + ["ospf router-id 10.1.1.2"]
    proposed = ["router ospf 100", "network 10.0.99.0/24 area 0"]
    ops = build_rollback_ops(proposed, base)
    flat = [(op.context, tuple(op.lines)) for op in ops]
    assert ("router ospf 100", ("no network 10.0.99.0/24 area 0",)) in flat
