"""Stage A2 tests: standalone collector runner + demo/live isolation.

Fully offline: real collection is mocked (empty inventory or injected
callables), no device is ever probed and no external database is used. The
shared-DB isolation hazard is proven by asserting that a demo seed against an
external URL raises *before* schema init or purge runs.
"""
from __future__ import annotations

import types

import pytest
from sqlalchemy.exc import OperationalError

from config import (
    demo_isolation_violation,
    get_settings,
    is_external_database,
)
from database import repository
from database.models import CollectionRun, MetricSample
from network.device_inventory import LabInventory
from tools import run_collector


# ------------------------------------------------------------------- helpers
def _fake_summary(status: str = "ok") -> types.SimpleNamespace:
    return types.SimpleNamespace(status=status, data_source="live", errors=[])


def _live_env(monkeypatch, *, demo="false",
              url="postgresql+psycopg2://h/db",
              change="false", labexec="false"):
    monkeypatch.setenv("NETPILOT_DEMO_MODE", demo)
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("NETPILOT_CHANGE_LOOP_ENABLED", change)
    monkeypatch.setenv("NETPILOT_LAB_EXECUTION_ENABLED", labexec)
    return get_settings()


# ----------------------------------------------------- external-DB classifier
def test_is_external_database_classification():
    assert is_external_database("postgresql+psycopg2://h/db") is True
    assert is_external_database("sqlite:///data/x.db") is False
    assert is_external_database("") is False


# ------------------------------------------------------------ demo isolation
def test_demo_isolation_none_for_local(monkeypatch):
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "true")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    settings = get_settings()
    assert settings.database_url.startswith("sqlite")
    assert demo_isolation_violation(settings) is None


def test_demo_isolation_rejects_external(monkeypatch):
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "true")
    monkeypatch.setenv("DATABASE_URL",
                       "postgresql+psycopg2://user:sup3rsecret@db.example/x")
    msg = demo_isolation_violation(get_settings())
    assert msg and "ISOLATION" in msg
    # The message is credential-free: no DSN or password may leak.
    assert "sup3rsecret" not in msg and "user:" not in msg


def test_seed_demo_data_rejected_before_any_purge(monkeypatch):
    """A demo seed against an external DB must fail with no DB side effects."""
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "true")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg2://h/live")

    import services.demo_data as dd
    calls: list[str] = []
    monkeypatch.setattr(dd, "init_db", lambda *a, **k: calls.append("init"))
    monkeypatch.setattr(dd.repository, "purge_metric_samples",
                        lambda *a, **k: calls.append("purge"))

    with pytest.raises(RuntimeError, match="ISOLATION"):
        dd.seed_demo_data()
    assert calls == []      # neither schema init nor metric purge executed


def test_seed_demo_data_still_refuses_non_demo(monkeypatch):
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "false")
    import services.demo_data as dd
    with pytest.raises(RuntimeError, match="must only run"):
        dd.seed_demo_data()


# ---------------------------------------------------- configuration validation
def test_check_configuration_accepts_live_postgres(monkeypatch):
    assert run_collector.check_configuration(_live_env(monkeypatch)) == []


def test_check_configuration_rejects_demo_mode(monkeypatch):
    errors = run_collector.check_configuration(
        _live_env(monkeypatch, demo="true"))
    assert any("NETPILOT_DEMO_MODE=false" in e for e in errors)


def test_check_configuration_requires_postgres(monkeypatch):
    errors = run_collector.check_configuration(
        _live_env(monkeypatch, url="sqlite:///data/x.db"))
    assert any("PostgreSQL" in e for e in errors)


def test_check_configuration_refuses_change_loop(monkeypatch):
    errors = run_collector.check_configuration(
        _live_env(monkeypatch, change="true"))
    assert any("CHANGE_LOOP" in e for e in errors)


def test_check_configuration_refuses_lab_execution(monkeypatch):
    errors = run_collector.check_configuration(
        _live_env(monkeypatch, labexec="true"))
    assert any("LAB_EXECUTION" in e for e in errors)


# ------------------------------------------------------------- collect outcomes
def test_perform_collect_success(tmp_path):
    code = run_collector.perform_collect(
        str(tmp_path / "c.lock"), run=lambda: _fake_summary("ok"))
    assert code == run_collector.EXIT_OK


def test_perform_collect_partial_is_success(tmp_path):
    code = run_collector.perform_collect(
        str(tmp_path / "c.lock"), run=lambda: _fake_summary("partial"))
    assert code == run_collector.EXIT_OK


def test_perform_collect_error_status(tmp_path):
    code = run_collector.perform_collect(
        str(tmp_path / "c.lock"), run=lambda: _fake_summary("error"))
    assert code == run_collector.EXIT_COLLECTION_ERROR


def test_perform_collect_db_operational_error(tmp_path):
    def boom():
        raise OperationalError("SELECT 1", {}, Exception("server closed"))
    code = run_collector.perform_collect(str(tmp_path / "c.lock"), run=boom)
    assert code == run_collector.EXIT_DB


def test_perform_collect_generic_failure(tmp_path):
    def boom():
        raise ValueError("unexpected")
    code = run_collector.perform_collect(str(tmp_path / "c.lock"), run=boom)
    assert code == run_collector.EXIT_COLLECTION_ERROR


# --------------------------------------------------------------- locking
def test_instance_lock_is_exclusive_then_releasable(tmp_path):
    path = str(tmp_path / "c.lock")
    a = run_collector.InstanceLock(path)
    assert a.acquire() is True
    b = run_collector.InstanceLock(path)
    assert b.acquire() is False          # held by a
    a.release()
    assert b.acquire() is True           # released -> reacquirable
    b.release()


def test_perform_collect_skips_when_locked_and_never_runs(tmp_path):
    path = str(tmp_path / "c.lock")
    held = run_collector.InstanceLock(path)
    assert held.acquire() is True
    ran: list[int] = []
    code = run_collector.perform_collect(path, run=lambda: ran.append(1))
    assert code == run_collector.EXIT_OK   # benign skip
    assert ran == []                        # collection was not started
    held.release()


def test_perform_collect_releases_lock_on_failure(tmp_path):
    path = str(tmp_path / "c.lock")
    def boom():
        raise OperationalError("SELECT 1", {}, Exception("down"))
    assert run_collector.perform_collect(path, run=boom) == run_collector.EXIT_DB
    fresh = run_collector.InstanceLock(path)
    assert fresh.acquire() is True          # lock was released in finally
    fresh.release()


# -------------------------------------------------------------- main() exit codes
def test_main_collect_config_error_exit_code(monkeypatch):
    _live_env(monkeypatch, demo="true", url="postgresql+psycopg2://h/live")
    assert run_collector.main(["collect"]) == run_collector.EXIT_CONFIG


def test_main_collect_requires_postgres_exit_code(monkeypatch):
    _live_env(monkeypatch, url="sqlite:///data/x.db")
    assert run_collector.main(["collect"]) == run_collector.EXIT_CONFIG


def test_main_init_schema_refuses_external_demo(monkeypatch):
    _live_env(monkeypatch, demo="true", url="postgresql+psycopg2://h/live")
    assert run_collector.main(["init-schema"]) == run_collector.EXIT_CONFIG


def test_main_init_schema_creates_usable_schema(db_url):
    assert run_collector.main(["init-schema"]) == run_collector.EXIT_OK
    from database.connection import get_session
    s = get_session()
    try:
        device = repository.upsert_device(s, name="R1", type_="router")
        s.commit()
        assert device.id is not None
    finally:
        s.close()


# --------------------------------------------------- live provenance (integrated)
def test_collect_once_writes_only_live_source(db_url, monkeypatch):
    """An empty inventory runs no probes, but still proves data_source='live'."""
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "false")
    monkeypatch.setattr(
        "network.metrics_collector.load_inventory",
        lambda *a, **k: LabInventory(nodes=[], links=[]))

    summary = run_collector.collect_once()
    assert summary.data_source == "live"

    from database.connection import get_session
    s = get_session()
    try:
        runs = s.query(CollectionRun).all()
        assert runs and all(r.data_source == "live" for r in runs)
        assert s.query(MetricSample).count() == 0   # empty inventory wrote none
    finally:
        s.close()
