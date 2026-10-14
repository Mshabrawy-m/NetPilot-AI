"""Lightweight in-process observability for the API (Slice 6/8 core).

A tiny, dependency-free, thread-safe counter registry plus an uptime
snapshot, exposed at ``GET /metrics``. It intentionally does NOT pull in
Prometheus or any compiled client - this machine's App Control blocks those -
so it is a pragmatic, honest first step in operational visibility.
"""
from __future__ import annotations

import threading
import time

_lock = threading.Lock()
_counters: dict[str, float] = {}
_started = time.time()


def inc(name: str, by: float = 1.0) -> None:
    with _lock:
        _counters[name] = _counters.get(name, 0.0) + by


def set_value(name: str, value: float) -> None:
    with _lock:
        _counters[name] = value


def snapshot() -> dict:
    with _lock:
        return {
            "counters": dict(_counters),
            "uptime_seconds": round(time.time() - _started, 3),
        }


def render_prometheus() -> str:
    """Prometheus text exposition of the counter registry (Slice 8 polish).

    Every counter is emitted as an UNTYPED metric plus its value; the uptime
    gauge is appended so a scrape target always has at least one sample. No
    compiled Prometheus client is involved - the format is written by hand
    (kept this way deliberately; see module docstring)."""
    snap = snapshot()
    lines = []
    for name in sorted(snap["counters"]):
        safe = name.replace(".", "_")
        lines.append(f"# TYPE netpilot_{safe} untyped")
        lines.append(f"netpilot_{safe} {snap['counters'][name]:g}")
    lines.append("# TYPE netpilot_uptime_seconds gauge")
    lines.append(f"netpilot_uptime_seconds {snap['uptime_seconds']:g}")
    return "\n".join(lines) + "\n"


def reset() -> None:
    """Clear counters and restart the uptime clock (used by tests)."""
    global _started
    with _lock:
        _counters.clear()
        _started = time.time()
