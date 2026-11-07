"""AppTest coverage for all pages in Live Mode (not demo mode).

Three database states are exercised:

* ``empty`` - schema only, no measurements;
* ``live`` - rows written by the native collector (mocked probes);
* ``simulated`` - rows written by ``tools/seed_simulated.py``.

Every page must render without an uncaught exception and must never show the
old false gate ("Live telemetry is unavailable because Containerlab is not
installed or reachable on this host").  When a page offers a source selector,
the simulated source is selected explicitly so its badge is rendered too.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from analytics import lab_simulator
from database import connection
from network.device_inventory import load_inventory
from tools import live_collector, seed_simulated

ROOT = Path(__file__).resolve().parents[1]

PAGES = ["app.py"] + [
    f"app_pages/{name}" for name in (
        "1_Overview.py", "2_Topology.py", "3_Devices.py",
        "4_Connectivity_Tests.py", "5_Metrics_History.py",
        "6_System_Status.py", "7_Data_Analysis.py", "8_Anomalies.py",
        "9_Forecasting.py", "10_AI_Network_Assistant.py",
        "11_Ops_Center.py", "12_AI_Lab.py",
    )
]

FORBIDDEN = (
    "Live telemetry is unavailable because Containerlab is not installed",
    "Lab inventory unavailable",
    "Containerlab is required",
)


def _seed_live() -> None:
    def ok_ping(address, count, timeout):
        return live_collector.PingSample(
            target=address, success=True, packets_sent=count,
            packets_received=count, loss_pct=0.0, avg_rtt_ms=12.0)

    result = live_collector.run_cycle(
        [live_collector.TargetSpec("192.168.1.1", "gateway"),
         live_collector.TargetSpec("1.1.1.1", "cloudflare-dns"),
         live_collector.TargetSpec("8.8.8.8", "google-dns")],
        ping_fn=ok_ping, interface_fn=list,
        now_fn=lambda: dt.datetime(2026, 3, 1, 0, 0, 0))
    assert live_collector.persist_cycle(result).ok is True


def _seed_simulated() -> None:
    dataset = lab_simulator.simulate(
        duration_hours=0.1, interval_seconds=60, seed=11, events={},
        start=dt.datetime(2026, 3, 1))
    with connection.session_scope() as session:
        seed_simulated.seed_simulated(
            session, dataset=dataset, inventory=load_inventory())


def _texts(at) -> list[str]:
    values: list[str] = []
    for name in ("markdown", "warning", "info", "error", "success",
                 "caption", "title", "header", "subheader", "text"):
        for element in getattr(at, name, []):
            value = getattr(element, "value", None)
            if value is not None:
                values.append(str(value))
    return values


def _run_page(page: str, state: str):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(ROOT / page), default_timeout=180)
    at.run()
    if state == "simulated":
        for box in at.selectbox:
            label = str(getattr(box, "label", "") or "").lower()
            if "source" not in label and "provenance" not in label:
                continue
            options = list(box.options or [])
            match = next((opt for opt in options
                          if str(opt).lower().startswith("simulated")), None)
            if match is not None:
                box.select(match)
                at.run()
                break
    return at


@pytest.mark.parametrize("state", ["empty", "live", "simulated"])
def test_all_pages_render_in_live_mode(state, db_url, monkeypatch):
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "false")
    monkeypatch.setenv("NETPILOT_CHANGE_LOOP_ENABLED", "false")
    monkeypatch.setenv("NETPILOT_LAB_EXECUTION_ENABLED", "false")

    if state == "live":
        _seed_live()
    elif state == "simulated":
        _seed_simulated()

    failures: list[str] = []
    for page in PAGES:
        at = _run_page(page, state)
        exceptions = [str(e.value) for e in at.exception]
        if exceptions:
            failures.append(f"{page}: exception {exceptions[0][:200]}")
            continue
        joined = "\n".join(_texts(at))
        for forbidden in FORBIDDEN:
            if forbidden in joined:
                failures.append(f"{page}: false gate {forbidden!r}")

    assert not failures, "\n".join(failures)
