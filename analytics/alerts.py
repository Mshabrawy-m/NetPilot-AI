"""Alert rule evaluation (v2 Slice 1).

Pure matching logic - **no SQL, no side effects**. Given enabled
``AlertRule`` rows and the latest telemetry (``MetricSample``-like objects
with ``device_name``/``interface_name``/``metric``/``value``/``data_source``)
it returns the (rule, sample) pairs that fire. Persistence and de-duplication
live in ``database.repository`` so this stays trivially testable.

A rule fires for a sample when:
* the rule is enabled and the sample value is not None;
* the metric matches the rule's metric;
* the sample is within the rule's device/interface scope (NULL = all);
* the value satisfies the condition ('above' -> >, 'below' -> <).
"""
from __future__ import annotations

from typing import Sequence


def matches(rule, value: float | None) -> bool:
    """Return True if ``value`` violates ``rule``'s threshold."""
    if value is None:
        return False
    if rule.condition == "above":
        return value > rule.threshold
    if rule.condition == "below":
        return value < rule.threshold
    return False


def _in_scope(rule, sample) -> bool:
    if rule.device_name and sample.device_name != rule.device_name:
        return False
    if rule.interface_name and sample.interface_name != rule.interface_name:
        return False
    return True


def evaluate(rules: Sequence, samples: Sequence) -> list[dict]:
    """Return ``[{"rule": rule, "sample": sample}, ...]`` for every firing.

    Disabled rules and NULL values are skipped; provenance is preserved by
    handing the original sample object straight back to the caller.
    """
    firings: list[dict] = []
    for rule in rules:
        if not getattr(rule, "enabled", True):
            continue
        for sample in samples:
            if sample.value is None:
                continue
            if sample.metric != rule.metric:
                continue
            if not _in_scope(rule, sample):
                continue
            if matches(rule, sample.value):
                firings.append({"rule": rule, "sample": sample})
    return firings


def summarize(rule, sample) -> str:
    """Short, size-bounded human summary for a fired alert."""
    iface = f" [{sample.interface_name}]" if sample.interface_name else ""
    arrow = ">" if rule.condition == "above" else "<"
    return (f"{rule.name}: {sample.device_name}{iface} {sample.metric}="
            f"{sample.value} {arrow} {rule.threshold}")[:300]
