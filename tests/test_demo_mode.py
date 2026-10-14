"""Demo-mode tests: fixtures work, are always labelled 'demo', live in a
separate database, and cannot run outside demo mode (spec: demo data must
never masquerade as real measurements)."""
from __future__ import annotations

import pytest


@pytest.fixture()
def demo_env(tmp_path, monkeypatch):
    """Demo mode + dedicated demo SQLite file + no containerlab probing."""
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "true")
    monkeypatch.setenv("DATABASE_URL",
                       f"sqlite:///{(tmp_path / 'demo.db').as_posix()}")
    from network import containerlab_manager
    monkeypatch.setattr(containerlab_manager, "inspect_nodes",
                        lambda *a, **k: [])
    return tmp_path


def test_seed_demo_data_labels_everything_demo(demo_env):
    from database import repository
    from database.connection import get_session, init_db
    from services.demo_data import seed_demo_data

    init_db()
    counts = seed_demo_data(hours=2, interval_minutes=5)
    assert counts["devices"] == 5 and counts["links"] == 4
    assert counts["samples"] > 0

    session = get_session()
    try:
        metrics = repository.get_available_metrics(session)
        sources = {src for _, _, _, src in metrics}
        assert sources == {"demo"}       # nothing labelled as live
        history = repository.get_connectivity_history(session, limit=1000)
        assert history and all(h.data_source == "demo" for h in history)
        run = repository.get_last_successful_run(session)
        assert run.data_source == "demo"
        assert "demo" in (run.error_message or "").lower()
    finally:
        session.close()


def test_demo_series_renders_down_device_with_null_rtt(demo_env):
    from database import repository
    from database.connection import get_session, init_db
    from services.demo_data import seed_demo_data

    init_db()
    seed_demo_data(hours=1, interval_minutes=5)
    session = get_session()
    try:
        down = [d for d in repository.get_devices(session) if d.status == "down"]
        assert down, "demo fixtures must include a down device for UI checks"
        series = repository.get_metric_series(session, down[0].name,
                                              "ping.avg_rtt_ms")
        assert all(s.value is None for s in series)  # nulls, not fake RTTs
    finally:
        session.close()


def test_seed_refused_outside_demo_mode(db_url):
    """The db_url fixture forces NETPILOT_DEMO_MODE=false -> must refuse."""
    from services.demo_data import seed_demo_data
    with pytest.raises(RuntimeError):
        seed_demo_data()


def test_demo_and_live_stores_are_separate(tmp_path, monkeypatch):
    """Simulates running demo once and live once: each env gets its own
    SQLite file; cross-contamination is impossible by configuration."""
    live_url = f"sqlite:///{(tmp_path / 'live.db').as_posix()}"
    demo_url = f"sqlite:///{(tmp_path / 'demo.db').as_posix()}"

    from database import connection
    from database import repository

    monkeypatch.setenv("NETPILOT_DEMO_MODE", "false")
    monkeypatch.setenv("DATABASE_URL", live_url)
    connection.init_db()
    with connection.session_scope() as s:
        repository.add_metric_sample(s, "R1", "ping.avg_rtt_ms", 1.0,
                                     data_source="live")

    from network import containerlab_manager
    monkeypatch.setattr(containerlab_manager, "inspect_nodes",
                        lambda *a, **k: [])
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "true")
    monkeypatch.setenv("DATABASE_URL", demo_url)
    from services.demo_data import seed_demo_data
    seed_demo_data(hours=1, interval_minutes=5)

    # Live store: only the live row exists.
    from database.models import MetricSample
    monkeypatch.setenv("DATABASE_URL", live_url)
    with connection.session_scope() as s:
        sources = {row.data_source for row in s.query(MetricSample).all()}
    assert sources == {"live"}

    # Demo store: only demo rows exist.
    monkeypatch.setenv("DATABASE_URL", demo_url)
    with connection.session_scope() as s:
        sources = {row.data_source for row in s.query(MetricSample).all()}
    assert sources == {"demo"}
