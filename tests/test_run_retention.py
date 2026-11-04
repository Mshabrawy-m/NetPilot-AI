"""Stage A6-1 runner tests: config guards, cutoff math, exit codes, lock (mocked).

All offline: the delete and the session are injected fakes, so no database and
no telemetry are touched. PostgreSQL is only a *string* used to satisfy the
scheme guard. Proves there is no bypass/force flag.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError

from tools import run_retention


def _settings(**kw):
    base = dict(
        demo_mode=False,
        database_url="postgresql+psycopg2://u@host/db",
        change_loop_enabled=False,
        lab_execution_enabled=False,
        retention_raw_days=30,
        retention_batch_size=5000,
    )
    base.update(kw)
    return SimpleNamespace(**base)


class _FakeSession:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


# --------------------------------------------------------------- configuration
def test_check_config_accepts_live():
    assert run_retention.check_configuration(_settings()) == []


def test_check_config_rejects_demo():
    errs = run_retention.check_configuration(_settings(demo_mode=True))
    assert any("demo" in e.lower() for e in errs)


def test_check_config_rejects_sqlite():
    errs = run_retention.check_configuration(
        _settings(database_url="sqlite:///local.db"))
    assert any("postgresql" in e.lower() for e in errs)


def test_check_config_rejects_disabled_retention():
    errs = run_retention.check_configuration(_settings(retention_raw_days=0))
    assert any("disabled" in e.lower() for e in errs)


def test_check_config_rejects_change_loop():
    assert run_retention.check_configuration(_settings(change_loop_enabled=True))


def test_check_config_rejects_lab_execution():
    assert run_retention.check_configuration(_settings(lab_execution_enabled=True))


def test_check_config_rejects_bad_batch_size():
    assert run_retention.check_configuration(_settings(retention_batch_size=0))


# ---------------------------------------------------------------- perform path
def test_perform_computes_cutoff_and_deletes(tmp_path):
    seen = {}

    def fake_delete(session, *, cutoff_utc, batch_size):
        seen["cutoff"] = cutoff_utc
        seen["batch"] = batch_size
        return 42

    sessions = []

    def session_factory():
        s = _FakeSession()
        sessions.append(s)
        return s

    now = dt.datetime(2026, 1, 31, 12, 0, 0)
    rc = run_retention.perform_retention(
        str(tmp_path / "ret.lock"),
        _settings(retention_raw_days=30, retention_batch_size=1234),
        session_factory=session_factory, delete_fn=fake_delete,
        now_fn=lambda: now)

    assert rc == run_retention.EXIT_OK
    assert seen["cutoff"] == now - dt.timedelta(days=30)
    assert seen["batch"] == 1234
    assert sessions[0].closed is True          # session always closed


def test_perform_lock_contention_skips_without_deleting(tmp_path):
    from tools.run_collector import InstanceLock

    lock_path = str(tmp_path / "ret.lock")
    held = InstanceLock(lock_path)
    assert held.acquire()
    touched = []
    try:
        rc = run_retention.perform_retention(
            lock_path, _settings(),
            session_factory=lambda: touched.append(1) or _FakeSession(),
            delete_fn=lambda *a, **k: 0,
            now_fn=lambda: dt.datetime(2026, 1, 1))
    finally:
        held.release()
    assert rc == run_retention.EXIT_OK
    assert touched == []                        # benign skip: no session opened


def test_perform_db_error_maps_to_exit_db(tmp_path):
    def boom():
        raise OperationalError("SELECT", {}, Exception("db down"))

    rc = run_retention.perform_retention(
        str(tmp_path / "r.lock"), _settings(), session_factory=boom,
        delete_fn=lambda *a, **k: 0, now_fn=lambda: dt.datetime(2026, 1, 1))
    assert rc == run_retention.EXIT_DB


def test_perform_generic_error_maps_to_exit_retention(tmp_path):
    def boom_delete(session, **k):
        raise RuntimeError("unexpected")

    rc = run_retention.perform_retention(
        str(tmp_path / "r.lock"), _settings(),
        session_factory=_FakeSession, delete_fn=boom_delete,
        now_fn=lambda: dt.datetime(2026, 1, 1))
    assert rc == run_retention.EXIT_RETENTION_ERROR


# --------------------------------------------------------------------- main
def test_main_rejects_demo_before_any_session(monkeypatch):
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "true")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg2://host/db")
    opened = []
    monkeypatch.setattr(run_retention, "perform_retention",
                        lambda *a, **k: opened.append(1))
    assert run_retention.main([]) == run_retention.EXIT_CONFIG
    assert opened == []                         # isolation rejects before connect


def test_main_rejects_disabled_without_deleting(monkeypatch):
    monkeypatch.delenv("NETPILOT_DEMO_MODE", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg2://host/db")
    monkeypatch.setenv("NETPILOT_RETENTION_RAW_DAYS", "0")
    opened = []
    monkeypatch.setattr(run_retention, "perform_retention",
                        lambda *a, **k: opened.append(1))
    assert run_retention.main([]) == run_retention.EXIT_CONFIG
    assert opened == []


def test_main_happy_path_invokes_perform(monkeypatch, tmp_path):
    monkeypatch.delenv("NETPILOT_DEMO_MODE", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg2://host/db")
    monkeypatch.setenv("NETPILOT_RETENTION_RAW_DAYS", "15")
    holder = {}

    def fake_perform(lock_path, settings, **k):
        holder["settings"] = settings
        return run_retention.EXIT_OK

    monkeypatch.setattr(run_retention, "perform_retention", fake_perform)
    monkeypatch.setattr(run_retention, "default_lock_path",
                        lambda: str(tmp_path / "ret.lock"))

    assert run_retention.main([]) == run_retention.EXIT_OK
    assert holder["settings"].retention_raw_days == 15


def test_default_lock_path_is_retention_specific(monkeypatch):
    monkeypatch.setenv("NETPILOT_RETENTION_LOCK", "/custom/retention.lock")
    assert run_retention.default_lock_path() == "/custom/retention.lock"
    monkeypatch.delenv("NETPILOT_RETENTION_LOCK")
    path = run_retention.default_lock_path()
    assert path.endswith("netpilot-retention.lock")
    assert "collector" not in path              # distinct from the collector lock


def test_cli_has_no_force_or_bypass_flag():
    # There is intentionally no way to override the guards from the CLI.
    with pytest.raises(SystemExit):
        run_retention.main(["--force"])
    with pytest.raises(SystemExit):
        run_retention.main(["--enable-before-rollups"])
