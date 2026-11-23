"""Shared plain-language UI helpers.

These helpers keep operator-facing wording consistent across pages: source labels,
metric names, timestamps, staleness, empty states, and developer-only details.
They do not access data or change business logic.
"""
from __future__ import annotations

import datetime as dt
import html
from typing import Any

import streamlit as st

from database import source_scope

# Friendly metric labels for operator-facing UI. Raw metric IDs remain available
# in tooltips/captions and are preserved in CSV/data exports.
METRIC_LABELS: dict[str, tuple[str, str]] = {
    "ping.avg_rtt_ms": ("Round-trip time", "ms"),
    "ping.packet_loss_pct": ("Packet loss", "%"),
    "ping.loss_pct": ("Packet loss", "%"),
    "ping.jitter_ms": ("Jitter", "ms"),
    "if.operstate": ("Interface state", ""),
    "if.oper_status": ("Interface state", ""),
    "cpu.usage_pct": ("CPU usage", "%"),
    "mem.usage_pct": ("Memory usage", "%"),
}

STATUS_TONES = {
    "up": "ok",
    "down": "bad",
    "degraded": "warn",
    "unknown": "muted",
    "healthy": "ok",
    "warning": "warn",
    "problem": "bad",
    "watch": "warn",
    "ok": "ok",
}


def source_label(source: str | None, *, demo_mode: bool = False) -> str:
    if demo_mode or source == source_scope.DEMO:
        return "Demo data"
    if source == source_scope.SIMULATED:
        return "Simulated data"
    if source == source_scope.LIVE:
        return "Live data"
    return str(source or "Unknown source")


def source_caption(source: str | None, *, demo_mode: bool = False) -> str:
    if demo_mode or source == source_scope.DEMO:
        return "Demo data (sample data, not real network measurements)"
    if source == source_scope.SIMULATED:
        return "Simulated data (test data, not real network measurements)"
    if source == source_scope.LIVE:
        return "Live data (collected from the network)"
    return str(source or "Unknown source")


def metric_label(metric: str | None) -> str:
    if not metric:
        return "Metric"
    name, unit = METRIC_LABELS.get(metric, (metric.replace(".", " ").replace("_", " ").capitalize(), ""))
    return f"{name} ({unit})" if unit else name


def metric_caption(metric: str | None) -> str:
    """Small caption showing the raw metric ID for transparency."""
    if not metric:
        return ""
    friendly = metric_label(metric)
    if friendly == metric:
        return f"Metric ID: `{metric}`"
    return f"Metric ID: `{metric}`"


def format_utc(value: dt.datetime | None, *, with_seconds: bool = False) -> str:
    if value is None:
        return "Unknown time"
    fmt = "%Y-%m-%d %H:%M:%S UTC" if with_seconds else "%Y-%m-%d %H:%M UTC"
    return value.strftime(fmt)


def humanize_age(seconds: float | None) -> str:
    if seconds is None:
        return "unknown age"
    seconds = max(0, int(seconds))
    minutes = seconds // 60
    hours = minutes // 60
    days = hours // 24
    if seconds < 60:
        return "less than a minute ago"
    if minutes < 60:
        return f"{minutes} min ago"
    if hours < 24:
        rem_m = minutes % 60
        return f"{hours} hr {rem_m} min ago" if rem_m else f"{hours} hr ago"
    rem_h = hours % 24
    return f"{days} day{'s' if days != 1 else ''} {rem_h} hr ago" if rem_h else f"{days} day{'s' if days != 1 else ''} ago"


def freshness_message(latest: dt.datetime | None, *, now: dt.datetime | None = None,
                      warning_after_seconds: int = 15 * 60) -> tuple[str, str]:
    """Return (level, message) for data freshness."""
    if latest is None:
        return "info", "No live sample has been stored yet."
    now = now or dt.datetime.now(dt.UTC).replace(tzinfo=None)
    try:
        age_seconds = max(0.0, (now - latest).total_seconds())
    except TypeError:
        age_seconds = 0.0
    age = humanize_age(age_seconds)
    stamp = format_utc(latest)
    if age_seconds > warning_after_seconds:
        return "warning", (
            f"Live data may be out of date. Last live sample: {age} ({stamp}). "
            "Check that the collector is running and the data source is reachable."
        )
    return "info", f"Last live sample: {age} ({stamp})."


def technical_details(summary: str = "Technical details", **items: Any) -> None:
    """Render developer-facing details in a collapsed expander."""
    lines = []
    for key, value in items.items():
        if value is None or value == "":
            continue
        lines.append(f"- **{html.escape(str(key))}:** `{html.escape(str(value))}`")
    if not lines:
        return
    with st.expander(summary, expanded=False):
        st.markdown("\n".join(lines))


def technical_details_text(summary: str, body: str) -> None:
    if not body:
        return
    with st.expander(summary, expanded=False):
        st.markdown(body)


def empty_state(title: str, why: str, next_step: str, *, icon: str = "ℹ️",
                technical: str | None = None) -> None:
    st.info(f"**{title}**\n\n{why}\n\n**Next step:** {next_step}", icon=icon)
    if technical:
        technical_details_text("Technical details", technical)


def status_tone(status: str | None) -> str:
    return STATUS_TONES.get(str(status or "").lower(), "muted")


def friendly_status(status: str | None) -> str:
    raw = str(status or "unknown").lower()
    return {
        "up": "Up",
        "down": "Down",
        "degraded": "Degraded",
        "unknown": "Unknown",
        "healthy": "Healthy",
        "warning": "Watch",
        "problem": "Problem",
        "ok": "OK",
    }.get(raw, raw.replace("_", " ").capitalize())