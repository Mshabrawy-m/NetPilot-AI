"""Configuration structure/syntax validation for FRR-style proposals.

Pure functions used by the verifier. They check that every proposed line is a
plausible FRR configuration token sequence and that it contains **no**
deployment / execution / shell-injection content (Phase 3 must only ever
propose, never apply).
"""
from __future__ import annotations

import re

#: Characters that never appear in a legitimate FRR config line - their
#: presence means the "config" is really a shell/CLI injection attempt.
_SHELL_METACHARS = set(";|&<>`$(){}\n\r\x00")

#: Whole-line or substring commands that would apply/persist/execute state
#: changes. Their presence is prohibited in a Phase-3 *proposal*.
PROHIBITED_COMMANDS = (
    "write memory", "wr mem", "copy running-config", "copy start",
    "configure terminal", "configure replace", "config terminal",
    "commit", "reload", "reload now", "do write", "do copy", "do clear",
    "systemctl", "iptables", "nft", "ip link set", "ifdown", "ifup",
   "shutdown -h", "reboot", "service ", "bash", "sh -c", "python",
    "docker exec", "containerlab deploy", "%",
)

_MAX_LINE_LEN = 300
_VALID_START = re.compile(r"^[A-Za-z0-9!][A-Za-z0-9 .:/_\-,'\"()]*$")


def syntax_ok(line: str) -> tuple[bool, str]:
    """Return (ok, reason) for one configuration line."""
    if line is None:
        return False, "empty line object"
    stripped = line.strip()
    if not stripped:
        return False, "blank line"
    if len(stripped) > _MAX_LINE_LEN:
        return False, "line too long"
    bad = [c for c in stripped if c in _SHELL_METACHARS]
    if bad:
        return False, f"shell metacharacters present: {''.join(sorted(set(bad)))}"
    # '!' is a valid FRR filler/comment; everything else must be a plain
    # keyword/argument sequence (no quotes-with-semantics, no braces).
    if not _VALID_START.match(stripped) and stripped != "!":
        return False, "non-config token"
    return True, ""


def prohibited_present(line: str) -> tuple[bool, str]:
    low = f" {(line or '').strip().lower()} "
    for cmd in PROHIBITED_COMMANDS:
        token = cmd.strip().lower()
        if not token:
            continue
        if f" {token}" in low or low.strip().startswith(token):
            return True, cmd
    return False, ""


def validate_lines(lines: list[str]) -> list[dict]:
    """Per-line findings: {line, syntax_ok, prohibited, reason}."""
    findings = []
    for raw in lines or []:
        ok, reason = syntax_ok(raw)
        prohibited, matched = prohibited_present(raw)
        findings.append({
            "line": (raw or "")[:_MAX_LINE_LEN],
            "syntax_ok": ok,
            "prohibited": prohibited,
            "reason": reason or (f"prohibited command '{matched}'"
                                 if prohibited else ""),
        })
    return findings
