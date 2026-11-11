"""AppTest coverage for the read-only Evaluation page (page 13).

The page reads only three committed files under ``docs/``. These tests run it
under the three runtime modes (Demo, Simulated and Live) and assert that it
renders the A/B/C comparison, the per-category chart, the failure cases and the
forecast backtest summary without an uncaught exception. No network, provider
or write path is exercised.
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
PAGE = "app_pages/13_Evaluation.py"


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


def _texts(at) -> str:
    values: list[str] = []
    for name in ("markdown", "warning", "info", "error", "success",
                 "caption", "title", "header", "subheader", "text"):
        for element in getattr(at, name, []):
            value = getattr(element, "value", None)
            if value is not None:
                values.append(str(value))
    return "\n".join(values)


@pytest.mark.parametrize("mode", ["demo", "simulated", "live"])
def test_evaluation_page_renders_in_every_mode(mode, tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'eval.db').as_posix()}")
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "true" if mode == "demo" else "false")
    monkeypatch.setenv("NETPILOT_CHANGE_LOOP_ENABLED", "false")
    monkeypatch.setenv("NETPILOT_LAB_EXECUTION_ENABLED", "false")

    if mode != "demo":
        connection.init_db()
        if mode == "simulated":
            _seed_simulated()
        else:
            _seed_live()

    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(ROOT / PAGE), default_timeout=180)
    at.run()

    exceptions = [str(exc.value) for exc in at.exception]
    assert not exceptions, exceptions[0][:800]

    joined = _texts(at)
    lowered = joined.lower()
    assert "a: llm only" in lowered
    assert "b: llm + verifier" in lowered
    assert "c: llm + verifier + digital twin" in lowered
    assert "wilson" in lowered
    assert "per-category false accepts" in lowered
    assert "network_isolation_or_loop" in joined
    assert "preserve_reachability" in joined
    assert "forecast backtest summary" in lowered
    assert "mean_ensemble" in lowered
    assert "no database, network or llm access" in lowered

    # Four tables: A/B/C comparison, per-category rates, forecast models and
    # forecast sources, plus the per-category false-accept chart.
    assert len(at.dataframe) >= 4
    assert len(at.get("plotly_chart")) == 1

    # The committed evidence must not be replaced by a live LLM run: the
    # comparison table records cached replay calls only.
    comparison = at.dataframe[0].value
    assert "LLM calls" in comparison.columns
    assert all("0 network / 80 cached" == value
               for value in comparison["LLM calls"])
