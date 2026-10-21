"""Pure model of FRR configuration as a flat list of lines.

Everything in this module is a pure function over strings - no device, no
database, no network. It exists so that the two hard parts of the change
loop can be reasoned about (and unit-tested) without a lab:

* turning a flat proposal into the *context-aware* transaction that FRR
  actually understands (``interface eth2`` then its sub-lines), and
* turning "proposal + base config" into an explicit **rollback plan** that
  really undoes the proposal instead of merely merging the base on top of
  it.

Why the block model matters: ``vtysh`` merges configuration, it does not
replace it. Re-applying the base config after a change that *added* a line
would leave that line in place - a rollback that only looks like one. The
rollback plan therefore emits an explicit ``no <line>`` for everything the
proposal added and re-applies the base value for everything it changed.

**FRR's ``no`` forms are not uniform.** ``no <line>`` is right for most
commands, but wrong for others - ``no description <text>`` is a syntax error
(only ``no description`` exists), ``no hostname`` and ``no banner motd``
drop their arguments, and ``no ip address A.B.C.D/M`` repeats them. The
tables below encode that, verified against the FRR 8.4 command reference
and the FRR 8.4 sources (``lib/command.c``, ``lib/if.c``,
``zebra/interface.c``, ``ospfd/ospf_vty.c``):

===========================  ===========================================
command                      FRR removal command
===========================  ===========================================
``hostname WORD``            ``no hostname``
``description LINE...``      ``no description``
``ospf router-id A.B.C.D``   ``no ospf router-id``
``banner motd ...``          ``no banner motd``
``log file [FILE [LEVEL]]``  ``no log file``
``log stdout [LEVEL]``       ``no log stdout``
``log trap LEVEL``           ``no log trap``
``ip address A.B.C.D/M``     ``no ip address A.B.C.D/M``
``network A.B.C.D/M area X`` ``no network A.B.C.D/M area X``
``interface IFNAME``         ``no interface IFNAME``
``router ospf [ID]``         ``no router ospf [ID]``
===========================  ===========================================

A command whose removal form is **not known** is never guessed: the plan
reports it as *unrollbackable* and the caller refuses the change.

The line grouping is positional, exactly like a real FRR config file: a
line whose head opens a context (``interface``, ``router``, ...) starts a
block, and the lines that follow belong to it until the next context opens.
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: Config-command heads that open a context block in an FRR config file.
CONTEXT_HEADS = frozenset({
    "interface", "router", "vrf", "line", "access-list", "route-map",
})

#: Commands that hold exactly ONE value: setting them again *replaces* the
#: previous value, so re-applying the base value is a complete undo.
_EXCLUSIVE_FAMILIES = ("hostname", "description", "ospf router-id",
                        "banner motd")

#: Commands that hold one *primary* value which a new value replaces, but
#: which still have an explicit removal form: the rollback removes the new
#: value AND re-applies the old one (an interface can carry a primary and
#: secondary address, so both are addressed explicitly).
_SLOT_FAMILIES = ("ip address",)

#: Commands whose FRR ``no`` form does NOT repeat the command's arguments.
#: Longest prefix wins. See the module docstring for the verification.
_NO_ARGUMENT_REMOVALS = {
    "description": "no description",
    "hostname": "no hostname",
    "banner motd": "no banner motd",
    "ospf router-id": "no ospf router-id",
    "log file": "no log file",
    "log stdout": "no log stdout",
    "log syslog": "no log syslog",
    "log trap": "no log trap",
    "log monitor": "no log monitor",
    "log commands": "no log commands",
    "log record-priority": "no log record-priority",
    "log immediate-mode": "no log immediate-mode",
    "log unique-id": "no log unique-id",
    "log timestamp precision": "no log timestamp precision",
    "log timestamp": "no log timestamp precision",
}

#: Heads whose FRR ``no`` form repeats the command's arguments, so the
#: generic ``no <line>`` is the correct removal command.
_GENERIC_REMOVAL_HEADS = frozenset({
    "interface", "vrf", "line", "access-list", "route-map", "network",
    "neighbor", "ip", "router",
})

#: Lines that carry no configuration and therefore nothing to undo.
_NO_OP_LINES = frozenset({"exit", "end", "configure terminal"})


def normalize_line(line: str | None) -> str:
    """Canonical form of one config line for comparison purposes.

    Strips surrounding whitespace and collapses internal runs of whitespace so
    that ``"  ip   address  10.0.21.3/24"`` and ``"ip address 10.0.21.3/24"``
    compare equal. Case is preserved.
    """
    if not line:
        return ""
    return " ".join(str(line).split())


def is_noise(line: str | None) -> bool:
    """True for FRR comment/filler lines that carry no configuration.

    ``!``/``#`` comments and the bare ``end`` that ``vtysh`` prints after a
    running configuration carry no configuration, so they never count as
    "unexpected" content when a device is read back.
    """
    stripped = (line or "").strip()
    if not stripped:
        return True
    if stripped.startswith(("!", "#")):
        return True
    return stripped.lower() == "end"


def normalize_config(lines: list[str] | None) -> list[str]:
    """Normalized, de-duplicated, order-preserving list of real config lines."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in lines or []:
        if is_noise(raw):
            continue
        norm = normalize_line(raw)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def config_head(line: str | None) -> str:
    """The first token of a config line (its command head), lowercased."""
    stripped = (line or "").strip()
    if not stripped:
        return ""
    return stripped.split()[0].lower()


@dataclass
class ConfigOp:
    """One context-aware group of configuration commands.

    ``context`` is the block header (``"interface eth2"``) the lines must be
    applied in, or ``None`` for top-level commands.
    """

    context: str | None
    lines: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"context": self.context, "lines": list(self.lines)}


def parse_blocks(lines: list[str] | None) -> tuple[list[str], list[ConfigOp]]:
    """Split a flat config list into (top-level lines, context blocks).

    Positional grouping, mirroring a real FRR config file: a context head
    starts a block and the following lines belong to it.
    """
    top: list[str] = []
    blocks: list[ConfigOp] = []
    current: ConfigOp | None = None
    for raw in lines or []:
        if is_noise(raw):
            continue
        norm = normalize_line(raw)
        if not norm:
            continue
        if config_head(norm) in CONTEXT_HEADS:
            current = ConfigOp(context=norm, lines=[])
            blocks.append(current)
        elif current is not None:
            current.lines.append(norm)
        else:
            top.append(norm)
    return top, blocks


def build_apply_ops(lines: list[str] | None) -> list[ConfigOp]:
    """Turn a flat proposal into the context-aware ops that apply it."""
    top, blocks = parse_blocks(lines)
    ops: list[ConfigOp] = []
    if top:
        ops.append(ConfigOp(context=None, lines=list(top)))
    for block in blocks:
        ops.append(ConfigOp(context=block.context, lines=list(block.lines)))
    return ops


# --------------------------------------------------------------------------
# FRR command semantics
# --------------------------------------------------------------------------
def slot_key(line: str | None) -> str:
    """The key under which a configuration line occupies a slot.

    Two lines with the same key compete for the same slot on the device:
    setting the second one replaces the first one. ``"ip address
    10.0.1.1/24"`` and ``"ip address 10.0.2.2/24"`` share the key
    ``"ip address"``; two ``network`` statements do not - each one is its
    own entry.
    """
    norm = normalize_line(line)
    if not norm:
        return ""
    low = norm.lower()
    for family in _EXCLUSIVE_FAMILIES + _SLOT_FAMILIES:
        if low == family or low.startswith(family + " "):
            return family
    return low


def command_class(line: str | None) -> str:
    """``"exclusive"``, ``"slot"`` or ``"accumulating"`` for one config line.

    * ``exclusive``   - one value only; a new value replaces it
      (``hostname``, ``description``, ``ospf router-id``, ``banner motd``).
    * ``slot``       - one primary value that a new value replaces, but that
      also has an explicit removal form (``ip address``).
    * ``accumulating`` - every instance coexists (``network``, ``neighbor``,
      ``access-list``, ``route-map``, ``ip route``, ``log file``).
    """
    key = slot_key(line)
    if key in _EXCLUSIVE_FAMILIES:
        return "exclusive"
    if key in _SLOT_FAMILIES:
        return "slot"
    return "accumulating"


def removal_form(line: str | None) -> str | None:
    """The FRR command that removes ``line`` - or ``None`` when unknown.

    ``None`` means "FRR has no known removal command for this line". The
    caller must then treat the change as unrollbackable and refuse it; the
    generic ``no <line>`` is never used as a guess.
    """
    norm = normalize_line(line)
    if not norm:
        return None
    low = norm.lower()
    for prefix in sorted(_NO_ARGUMENT_REMOVALS, key=len, reverse=True):
        if low == prefix or low.startswith(prefix + " "):
            return _NO_ARGUMENT_REMOVALS[prefix]
    if config_head(norm) in _GENERIC_REMOVAL_HEADS:
        return f"no {norm}"
    return None


def removal_target(line: str | None) -> str | None:
    """The target of a removal command (``"no ip route X"`` -> ``"ip route X"``).

    ``None`` when the line is not a removal command.
    """
    norm = normalize_line(line)
    parts = norm.split()
    if len(parts) >= 2 and parts[0].lower() == "no":
        return normalize_line(" ".join(parts[1:]))
    return None


def split_proposal_lines(lines: list[str] | None
                         ) -> tuple[list[str], list[str]]:
    """Split proposal lines into the lines that must be **present** and the
    targets that must be **absent** after the change.

    A proposal is a *delta*: a normal line adds configuration, a ``no ...``
    line removes it. A removal can never show up in the read-back - its
    success is proven by the **absence** of its target. A verification that
    only looks for the proposal's own lines therefore reports every removal
    as a partial application, even when it applied perfectly.
    """
    present: list[str] = []
    absent: list[str] = []
    for raw in lines or []:
        norm = normalize_line(raw)
        if not norm or is_noise(norm):
            continue
        target = removal_target(norm)
        if target:
            absent.append(normalize_line(target))
        else:
            present.append(norm)
    return present, absent


def negation_for(line: str) -> str:
    """The FRR command that removes ``line``.

    Kept for callers that only need the command and cannot cope with
    ``None``; prefer :func:`removal_form`, which reports an unknown
    removal form instead of guessing one.

    ``"ip address 10.0.21.3/24"`` -> ``"no ip address 10.0.21.3/24"``
    ``"description to_R2_eth1"``    -> ``"no description"``
    """
    form = removal_form(line)
    return form if form is not None else f"no {normalize_line(line)}"


# --------------------------------------------------------------------------
# The rollback plan
# --------------------------------------------------------------------------
def _matching_base(target: str, base_lines: list[str]) -> list[str]:
    """The base lines a removal command refers to.

    A removal with arguments (``no ip address 10.0.12.2/24``) refers to one
    exact line; a removal without arguments (``no description``) refers to
    the whole slot.
    """
    if not target:
        return []
    parts = target.split()
    if len(parts) > 1:
        return [b for b in base_lines if b == target]
    key = slot_key(target)
    return [b for b in base_lines if slot_key(b) == key]


def _diff_scope(context: str | None, prop_lines: list[str],
                base_lines: list[str]) -> tuple[list[ConfigOp], list[str]]:
    """The ops that undo the proposal inside one scope (a block or the top).

    Returns ``(ops, unrollbackable)``: the context-aware ops that undo the
    proposal, and the proposal lines FRR gives us no removal command for.
    """
    removals: list[str] = []
    restores: list[str] = []
    unrollbackable: list[str] = []

    base_norm = [normalize_line(b) for b in base_lines if normalize_line(b)]
    base_set = set(base_norm)

    for raw in prop_lines:
        p = normalize_line(raw)
        if not p or p.lower() in _NO_OP_LINES:
            continue

        target = removal_target(p)
        if target is not None:
            # The change REMOVED something -> the rollback puts it back.
            # Context removals are handled by the block level.
            if config_head(target) in CONTEXT_HEADS:
                continue
            for b in _matching_base(target, base_norm):
                if b not in restores:
                    restores.append(b)
            continue

        if p in base_set:
            continue                      # already present: nothing to undo

        cls = command_class(p)
        same_slot = [b for b in base_norm if slot_key(b) == slot_key(p)]
        form = removal_form(p)

        if cls == "exclusive":
            if same_slot:
                # Re-applying the base value replaces the proposal's value;
                # an explicit removal is neither needed nor wanted.
                for b in same_slot:
                    if b not in restores:
                        restores.append(b)
            elif form:
                removals.append(form)
            else:
                unrollbackable.append(p)
        elif cls == "slot":
            if form:
                removals.append(form)
            for b in same_slot:
                if b not in restores:
                    restores.append(b)
            if not form and not same_slot:
                unrollbackable.append(p)
        else:                            # accumulating
            if form:
                removals.append(form)
            else:
                unrollbackable.append(p)

    lines = removals + restores
    ops = [ConfigOp(context=context, lines=lines)] if lines else []
    return ops, unrollbackable


def _plan(proposed: list[str] | None,
          base: list[str] | None) -> tuple[list[ConfigOp], list[str]]:
    """The rollback plan for ``proposed`` over ``base`` and the lines that
    cannot be undone."""
    prop_top, prop_blocks = parse_blocks(proposed)
    base_top, base_blocks = parse_blocks(base)

    prop_by_ctx = {b.context: b for b in prop_blocks}
    base_by_ctx = {b.context: b for b in base_blocks}

    ops: list[ConfigOp] = []
    unrollbackable: list[str] = []

    # 1. A block the proposal added -> remove the whole block.
    for ctx in prop_by_ctx:
        if ctx in base_by_ctx:
            continue
        form = removal_form(ctx)
        if form:
            ops.append(ConfigOp(context=None, lines=[form]))
        else:
            unrollbackable.append(ctx)

    # 2. A block the proposal removed explicitly ("no interface eth2") ->
    #    restore it from the base. A block the proposal merely does not
    #    mention is NOT removed by the change (the proposal is applied *on
    #    top of* the base), so there is nothing to undo for it.
    removed_contexts = set()
    for raw in list(prop_top) + [ln for b in prop_blocks for ln in b.lines]:
        target = removal_target(raw)
        if target and config_head(target) in CONTEXT_HEADS:
            removed_contexts.add(target)
    for ctx, block in base_by_ctx.items():
        if ctx in removed_contexts:
            # A block with no sub-lines still has to be recreated: FRR deletes
            # the whole interface with "no interface eth3", so the rollback has
            # to re-enter the (empty) context to bring it back. Skipping it
            # would leave an *incomplete* plan - the "verified rollback plan"
            # gate would pass while the device could not be restored.
            ops.append(ConfigOp(context=ctx, lines=list(block.lines)))

    # 3. A block in both -> undo the difference inside it.
    for ctx, block in prop_by_ctx.items():
        base_block = base_by_ctx.get(ctx)
        if base_block is None:
            continue
        block_ops, bad = _diff_scope(ctx, block.lines, base_block.lines)
        ops.extend(block_ops)
        unrollbackable.extend(bad)

    # 4. The top level.
    top_ops, bad = _diff_scope(None, prop_top, base_top)
    ops.extend(top_ops)
    unrollbackable.extend(bad)

    return ops, unrollbackable


def build_rollback_ops(proposed: list[str] | None,
                       base: list[str] | None) -> list[ConfigOp]:
    """Build the rollback plan that undoes ``proposed`` and restores ``base``.

    The plan is a list of context-aware ops:

    * a block that exists only in the proposal is removed wholesale
      (``no interface eth2``);
    * a block the proposal removes explicitly (``no interface eth2``) is
      restored from the base (``interface eth2`` plus its base sub-lines) -
      a block without sub-lines is restored as well, by re-entering the
      empty context;
    * a block that exists in both gets the proposal's extra sub-lines
      removed and the base's sub-lines re-applied, inside that context;
    * a top-level line the proposal added is removed with the FRR removal
      command for that command - which is not always ``no <line>``;
    * a top-level line the proposal changed is re-applied from the base.

    The proposal is a *delta* that is applied on top of the base (that is
    what the executor does), so a base line the proposal does not mention
    is still on the device and needs no undo.

    The result is never empty for a real change: if the proposal changed
    nothing, the plan is empty and the caller can skip the rollback.
    """
    ops, _unrollbackable = _plan(proposed, base)
    return ops


def unrollbackable_lines(proposed: list[str] | None,
                         base: list[str] | None) -> list[str]:
    """The proposal lines that cannot be undone deterministically.

    A line belongs here when FRR has no known removal command for it. Such a
    change must never be applied: the caller refuses it (fail closed)
    instead of pretending a rollback exists.
    """
    _ops, bad = _plan(proposed, base)
    return bad


def rollback_plan_lines(proposed: list[str] | None,
                        base: list[str] | None) -> list[str]:
    """Flatten a rollback plan into plain lines (for display/audit only).

    The *executed* plan keeps its context structure - this flattened form is
    only for the audit trail and the diff shown to the operator.
    """
    flat: list[str] = []
    for op in build_rollback_ops(proposed, base):
        if op.context:
            flat.append(op.context)
        flat.extend(op.lines)
    return flat
