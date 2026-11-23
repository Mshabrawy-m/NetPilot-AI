"""UX-1 regression tests: plain language, no raw technical strings in visible UI."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from analytics import lab_simulator
from database import connection, repository
from database.connection import init_db
from network.device_inventory import load_inventory
from tools import live_collector, seed_simulated

ROOT = Path(__file__).resolve().parents[1]
PAGES = ["app.py"] + sorted(f"app_pages/{p.name}" for p in (ROOT / "app_pages").glob("*.py"))

FORBIDDEN_VISIBLE = ["NETPILOT_", "data_source=", "docker=", "containerlab=", "python tools/"]
SQLITE_FORBIDDEN_OUTSIDE_EXPANDER = "sqlite"


def _seed(mode: str):
    init_db()
    if mode == "empty":
        return
    if mode == "demo":
        from services.demo_data import seed_demo_data, seed_demo_fabric
        try:
            seed_demo_fabric()
        except Exception:  # noqa: BLE001, S110 - optional demo fabric seed in test fixture
            pass
        seed_demo_data()
        return
    if mode == "simulated":
        dataset = lab_simulator.simulate(duration_hours=0.1, interval_seconds=60, seed=11, events={}, start=dt.datetime(2026, 3, 1))
        with connection.session_scope() as session:
            seed_simulated.seed_simulated(session, dataset=dataset, inventory=load_inventory())
        return
    if mode == "live":
        def ok_ping(address, count, timeout):
            return live_collector.PingSample(target=address, success=True, packets_sent=count, packets_received=count, loss_pct=0.0, avg_rtt_ms=12.0)
        result = live_collector.run_cycle(
            [live_collector.TargetSpec("192.168.1.1", "gateway"),
             live_collector.TargetSpec("1.1.1.1", "cloudflare-dns"),
             live_collector.TargetSpec("8.8.8.8", "google-dns")],
            ping_fn=ok_ping, interface_fn=list, now_fn=lambda: dt.datetime(2026, 3, 1, 0, 0, 0))
        ok = live_collector.persist_cycle(result)
        assert getattr(ok, "ok", ok) is True
        return
    raise AssertionError(mode)


def _visible_texts(at):
    texts = []
    def add(kind, value):
        if value is None:
            return
        s = str(value)
        if s:
            texts.append((kind, s))

    # exclude expander internals by collecting expander-owned text
    expander_text = set()
    for ex in getattr(at, "expander", []):
        for attr in ("markdown", "caption", "code", "text", "info", "warning", "success", "error"):
            for e in getattr(ex, attr, []):
                v = getattr(e, "value", None) or getattr(e, "body", None)
                if v:
                    expander_text.add(str(v))

    for attr in ("title", "header", "subheader", "caption", "markdown", "text", "info", "warning", "error", "success", "toast"):
        for e in getattr(at, attr, []):
            v = getattr(e, "value", None) or getattr(e, "body", None)
            if v and str(v) not in expander_text:
                add(attr, v)
    for e in getattr(at, "metric", []):
        add("metric.label", getattr(e, "label", ""))
        add("metric.value", getattr(e, "value", ""))
    for e in getattr(at, "button", []):
        add("button", getattr(e, "label", ""))
    for attr in ("selectbox", "multiselect", "radio"):
        for e in getattr(at, attr, []):
            add(attr + ".label", getattr(e, "label", ""))
            try:
                for o in list(getattr(e, "options", []) or []):
                    add(attr + ".opt", o)
            except Exception:  # noqa: BLE001, S110 - AppTest option shapes vary
                pass
    return texts


def _run_page(page: str, mode: str):
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(ROOT / page), default_timeout=180)
    at.run()
    return at


@pytest.fixture()
def ux_db(tmp_path, monkeypatch, request):
    mode = request.param
    db_path = tmp_path / f"ux_{mode}.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "true" if mode == "demo" else "false")
    monkeypatch.setenv("NETPILOT_CHANGE_LOOP_ENABLED", "false")
    monkeypatch.setenv("NETPILOT_LAB_EXECUTION_ENABLED", "false")
    import config
    config._settings = None
    import database.connection as conn
    conn._engine = None
    conn._SessionLocal = None
    init_db()
    _seed(mode)
    return mode


@pytest.mark.parametrize("ux_db", ["demo", "simulated", "live", "empty"], indirect=True)
@pytest.mark.parametrize("page", PAGES)
def test_no_forbidden_visible_strings(ux_db, page):
    at = _run_page(page, ux_db)
    assert not at.exception
    texts = _visible_texts(at)
    for kind, value in texts:
        for bad in FORBIDDEN_VISIBLE:
            assert bad not in value, f"{ux_db}/{page} [{kind}] contains {bad}: {value[:200]}"
        if SQLITE_FORBIDDEN_OUTSIDE_EXPANDER in value.lower():
            # allow "sqlite" only in technical expander - already excluded
            assert "sqlite" not in value.lower(), f"{ux_db}/{page} [{kind}] contains sqlite outside expander: {value[:200]}"


@pytest.mark.parametrize("ux_db", ["demo", "simulated", "live", "empty"], indirect=True)
def test_overview_attention_panel_renders(ux_db):
    at = _run_page("app.py", ux_db)
    assert not at.exception
    body = "\n".join(str(getattr(e, "value", "")) for e in at.markdown)
    body += "\n" + "\n".join(str(getattr(e, "value", "")) for e in at.subheader)
    assert "What needs attention now" in body


def _overview_body(at):
    parts = []
    for group in (at.markdown, at.subheader, at.info, at.warning, at.success):
        for e in group:
            parts.append(str(getattr(e, "value", "")))
    return "\n".join(parts)


def test_overview_panel_severity_states(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    import config
    import database.connection as conn
    # OK state
    db = tmp_path / "ok.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "false")
    monkeypatch.setenv("NETPILOT_CHANGE_LOOP_ENABLED", "false")
    config._settings = None; conn._engine = None; conn._SessionLocal = None
    init_db()
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=180).run()
    assert "No issues detected right now" in _overview_body(at)

    # Watch state (stale live data)
    db2 = tmp_path / "watch.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db2.as_posix()}")
    config._settings = None; conn._engine = None; conn._SessionLocal = None
    init_db()
    def ok_ping(address, count, timeout):
        return live_collector.PingSample(target=address, success=True, packets_sent=count, packets_received=count, loss_pct=0.0, avg_rtt_ms=12.0)
    result = live_collector.run_cycle(
        [live_collector.TargetSpec("192.168.1.1", "gateway")],
        ping_fn=ok_ping, interface_fn=list, now_fn=lambda: dt.datetime(2026, 3, 1, 0, 0, 0))
    live_collector.persist_cycle(result)
    at2 = AppTest.from_file(str(ROOT / "app.py"), default_timeout=180).run()
    body2 = _overview_body(at2)
    assert "Watch" in body2 or "getting old" in body2

    # Problem state (down device)
    db3 = tmp_path / "problem.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db3.as_posix()}")
    config._settings = None; conn._engine = None; conn._SessionLocal = None
    init_db()
    with connection.session_scope() as session:
        repository.upsert_device(session, name="R1", type_="router", status="down")
    at3 = AppTest.from_file(str(ROOT / "app.py"), default_timeout=180).run()
    body3 = _overview_body(at3)
    assert "Problem" in body3 or "down" in body3.lower()


@pytest.mark.parametrize("page", PAGES)
def test_link_targets_exist(page):
    # static sanity: referenced app pages exist
    targets = [
        "app_pages/1_Overview.py", "app_pages/2_Topology.py", "app_pages/3_Devices.py",
        "app_pages/4_Connectivity_Tests.py", "app_pages/5_Metrics_History.py", "app_pages/6_System_Status.py",
        "app_pages/7_Data_Analysis.py", "app_pages/8_Anomalies.py", "app_pages/10_AI_Network_Assistant.py",
        "app_pages/11_Ops_Center.py", "app_pages/13_Evaluation.py",
    ]
    for t in targets:
        assert (ROOT / t).exists(), t
    assert (ROOT / page).exists()