"""Stage A6-2 rollup runner tests: config guards, day-window math, exit codes,
lock contention, and the two structural guarantees that matter most - the runner
performs **no DDL** and **never invokes retention**.

All offline: the upsert and the session are injected fakes, so no database and no
telemetry are touched. PostgreSQL is only a *string* that satisfies the scheme
guard.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError

from tools import run_rollup


def _settings(**kw):
    base = dict(
        demo_mode=False,
        database_url="postgresql+psycopg2://u@host/db",
        change_loop_enabled=False,
        lab_execution_enabled=False,
        rollup_lookback_days=1,
    )
    base.update(kw)
    return SimpleNamespace(**base)


class _FakeSession:
    """Session stand-in with a real-looking dialect for the version probe."""

    def __init__(self):
        self.closed = False

    def get_bind(self):
        return SimpleNamespace(
            dialect=SimpleNamespace(name="sqlite", server_version_info=(3, 45, 0)))

    def close(self):
        self.closed = True


# --------------------------------------------------------------- configuration
def test_check_config_accepts_live():
    assert run_rollup.check_configuration(_settings()) == []


def test_check_config_rejects_demo():
    errs = run_rollup.check_configuration(_settings(demo_mode=True))
    assert any("demo" in e.lower() for e in errs)


def test_check_config_rejects_sqlite():
    errs = run_rollup.check_configuration(
        _settings(database_url="sqlite:///local.db"))
    assert any("postgresql" in e.lower() for e in errs)


def test_check_config_rejects_change_loop():
    assert run_rollup.check_configuration(_settings(change_loop_enabled=True))


def test_check_config_rejects_lab_execution():
    assert run_rollup.check_configuration(_settings(lab_execution_enabled=True))


# ------------------------------------------------------------- day-window math
def test_completed_windows_exclude_current_day():
    today = dt.date(2026, 10, 6)
    windows = run_rollup.completed_day_windows(today, 1)
    assert windows == [(dt.datetime(2026, 10, 5), dt.datetime(2026, 10, 6))]
    # The current (incomplete) day 10/6 is only ever an *upper* bound, never a
    # day that gets aggregated.
    assert all(end <= dt.datetime.combine(today, dt.time(0, 0))
               for _, end in windows)


def test_completed_windows_lookback_newest_first():
    today = dt.date(2026, 10, 6)
    windows = run_rollup.completed_day_windows(today, 3)
    assert len(windows) == 3
    assert windows[0] == (dt.datetime(2026, 10, 5), dt.datetime(2026, 10, 6))
    assert windows[1] == (dt.datetime(2026, 10, 4), dt.datetime(2026, 10, 5))
    assert windows[2] == (dt.datetime(2026, 10, 3), dt.datetime(2026, 10, 4))


def test_completed_windows_zero_lookback_is_empty():
    assert run_rollup.completed_day_windows(dt.date(2026, 10, 6), 0) == []


# ---------------------------------------------------------------- perform path
def test_perform_rebuilds_each_completed_day(tmp_path):
    calls = []

    def fake_upsert(session, *, day_start, day_end):
        calls.append((day_start, day_end))
        return 7

    sessions = []

    def session_factory():
        s = _FakeSession()
        sessions.append(s)
        return s

    now = dt.datetime(2026, 10, 6, 13, 30)   # mid-day: today (10/6) incomplete
    rc = run_rollup.perform_rollup(
        str(tmp_path / "rollup.lock"), _settings(rollup_lookback_days=2),
        session_factory=session_factory, upsert_fn=fake_upsert,
        now_fn=lambda: now)

    assert rc == run_rollup.EXIT_OK
    # Exactly the two most-recent *completed* days, newest first, today excluded.
    assert calls == [
        (dt.datetime(2026, 10, 5), dt.datetime(2026, 10, 6)),
        (dt.datetime(2026, 10, 4), dt.datetime(2026, 10, 5)),
    ]
    assert all(s.closed for s in sessions)     # every session closed


def test_perform_zero_lookback_does_nothing(tmp_path):
    def never(*a, **k):
        raise AssertionError("no upsert expected")

    rc = run_rollup.perform_rollup(
        str(tmp_path / "rollup.lock"), _settings(rollup_lookback_days=0),
        session_factory=lambda: (_ for _ in ()).throw(
            AssertionError("no session expected")),
        upsert_fn=never,
        now_fn=lambda: dt.datetime(2026, 10, 6, 13, 30))
    assert rc == run_rollup.EXIT_OK


def test_perform_lock_contention_skips_without_opening_session(tmp_path):
    from tools.run_collector import InstanceLock

    lock_path = str(tmp_path / "rollup.lock")
    held = InstanceLock(lock_path)
    assert held.acquire()
    touched = []
    try:
        rc = run_rollup.perform_rollup(
            lock_path, _settings(rollup_lookback_days=3),
            session_factory=lambda: touched.append(1) or _FakeSession(),
            upsert_fn=lambda *a, **k: 0,
            now_fn=lambda: dt.datetime(2026, 10, 6))
    finally:
        held.release()
    assert rc == run_rollup.EXIT_OK
    assert touched == []                        # benign skip: no session opened


def test_perform_db_error_maps_to_exit_db(tmp_path):
    def boom():
        raise OperationalError("SELECT", {}, Exception("db down"))

    rc = run_rollup.perform_rollup(
        str(tmp_path / "r.lock"), _settings(), session_factory=boom,
        upsert_fn=lambda *a, **k: 0,
        now_fn=lambda: dt.datetime(2026, 10, 6))
    assert rc == run_rollup.EXIT_DB


def test_perform_generic_error_maps_to_exit_rollup(tmp_path):
    def boom_upsert(session, **k):
        raise RuntimeError("unexpected")

    rc = run_rollup.perform_rollup(
        str(tmp_path / "r.lock"), _settings(),
        session_factory=_FakeSession, upsert_fn=boom_upsert,
        now_fn=lambda: dt.datetime(2026, 10, 6))
    assert rc == run_rollup.EXIT_ROLLUP_ERROR


# -------------------------------------------- version inspection is pure diagnostics
def test_version_inspection_never_blocks_the_rollup(tmp_path, monkeypatch):
    """The PG-version provenance probe may never prevent the aggregation.

    The rollup runner reads the configured server major only to *explain* why the
    version-independent surrogate key is used. If that read is unavailable the
    run must still rebuild every completed day and exit 0.
    """
    def explode(session):
        raise RuntimeError("cannot inspect dialect")

    monkeypatch.setattr(run_rollup, "rollup_uniqueness_strategy_info", explode)
    calls = []
    rc = run_rollup.perform_rollup(
        str(tmp_path / "r.lock"), _settings(rollup_lookback_days=2),
        session_factory=_FakeSession,
        upsert_fn=lambda s, *, day_start, day_end: calls.append(day_start) or 0,
        now_fn=lambda: dt.datetime(2026, 10, 6))
    assert rc == run_rollup.EXIT_OK
    assert calls == [dt.datetime(2026, 10, 5), dt.datetime(2026, 10, 4)]


def test_version_inspection_is_reported_and_uses_no_ddl(tmp_path, monkeypatch, caplog):
    """The probe reads a cached dialect attribute only - it is not a schema check."""
    import database.connection as connection

    def boom(*a, **k):
        raise AssertionError("version inspection must not perform DDL")

    monkeypatch.setattr(connection, "init_db", boom)
    monkeypatch.setattr(connection.Base.metadata, "create_all", boom)
    caplog.set_level("INFO")
    rc = run_rollup.perform_rollup(
        str(tmp_path / "r.lock"), _settings(),
        session_factory=_FakeSession, upsert_fn=lambda *a, **k: 1,
        now_fn=lambda: dt.datetime(2026, 10, 6))
    assert rc == run_rollup.EXIT_OK
    assert any("uniqueness strategy" in r.getMessage()
               for r in caplog.records)
    # The runner still binds no DDL entry point at all.
    assert not hasattr(run_rollup, "init_db")


# ------------------------------------------------- structural safety guarantees
def test_runner_performs_no_ddl(tmp_path, monkeypatch):
    """The rollup path must never create/alter schema - that is init-schema's job."""
    import database.connection as connection

    def boom(*a, **k):
        raise AssertionError("rollup runner must not perform DDL")

    monkeypatch.setattr(connection, "init_db", boom)
    monkeypatch.setattr(connection.Base.metadata, "create_all", boom)
    # The runner module must not even bind the DDL entry points.
    assert not hasattr(run_rollup, "init_db")
    assert not hasattr(run_rollup, "create_all")

    rc = run_rollup.perform_rollup(
        str(tmp_path / "r.lock"), _settings(rollup_lookback_days=2),
        session_factory=_FakeSession, upsert_fn=lambda *a, **k: 1,
        now_fn=lambda: dt.datetime(2026, 10, 6))
    assert rc == run_rollup.EXIT_OK


def test_runner_never_invokes_retention():
    # Retention stays a separate, still-disabled command; rollups must not reach
    # for it (behaviorally inert and not even imported).
    assert not hasattr(run_rollup, "delete_expired_live_samples")
    assert not hasattr(run_rollup, "perform_retention")
    assert not hasattr(run_rollup, "run_retention")


# --------------------------------------------------------------------- main
def test_main_rejects_demo_before_any_session(monkeypatch):
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "true")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg2://host/db")
    opened = []
    monkeypatch.setattr(run_rollup, "perform_rollup",
                        lambda *a, **k: opened.append(1))
    assert run_rollup.main([]) == run_rollup.EXIT_CONFIG
    assert opened == []                         # isolation rejects before connect


def test_main_rejects_non_postgres(monkeypatch):
    monkeypatch.delenv("NETPILOT_DEMO_MODE", raising=False)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///local.db")
    opened = []
    monkeypatch.setattr(run_rollup, "perform_rollup",
                        lambda *a, **k: opened.append(1))
    assert run_rollup.main([]) == run_rollup.EXIT_CONFIG
    assert opened == []


def test_main_rejects_unsafe_change_loop(monkeypatch):
    monkeypatch.delenv("NETPILOT_DEMO_MODE", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg2://host/db")
    monkeypatch.setenv("NETPILOT_CHANGE_LOOP_ENABLED", "true")
    opened = []
    monkeypatch.setattr(run_rollup, "perform_rollup",
                        lambda *a, **k: opened.append(1))
    assert run_rollup.main([]) == run_rollup.EXIT_CONFIG
    assert opened == []


def test_main_happy_path_invokes_perform(monkeypatch, tmp_path):
    monkeypatch.delenv("NETPILOT_DEMO_MODE", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg2://host/db")
    monkeypatch.setenv("NETPILOT_ROLLUP_LOOKBACK_DAYS", "4")
    holder = {}

    def fake_perform(lock_path, settings, **k):
        holder["settings"] = settings
        return run_rollup.EXIT_OK

    monkeypatch.setattr(run_rollup, "perform_rollup", fake_perform)
    monkeypatch.setattr(run_rollup, "default_lock_path",
                        lambda: str(tmp_path / "rollup.lock"))

    assert run_rollup.main([]) == run_rollup.EXIT_OK
    assert holder["settings"].rollup_lookback_days == 4


def test_default_lock_path_is_rollup_specific(monkeypatch):
    monkeypatch.setenv("NETPILOT_ROLLUP_LOCK", "/custom/rollup.lock")
    assert run_rollup.default_lock_path() == "/custom/rollup.lock"
    monkeypatch.delenv("NETPILOT_ROLLUP_LOCK")
    path = run_rollup.default_lock_path()
    assert path.endswith("netpilot-rollup.lock")
    assert "collector" not in path              # distinct from the collector lock
    assert "retention" not in path              # distinct from the retention lock


def test_cli_has_no_force_or_bypass_flag():
    # There is intentionally no way to override the guards from the CLI.
    with pytest.raises(SystemExit):
        run_rollup.main(["--force"])
    with pytest.raises(SystemExit):
        run_rollup.main(["--run-retention"])


# ------------------------------------------------------- configurable settings
def test_rollup_settings_defaults_and_env(monkeypatch):
    import config

    monkeypatch.delenv("NETPILOT_ROLLUP_LOOKBACK_DAYS", raising=False)
    assert config.get_settings().rollup_lookback_days == 1

    monkeypatch.setenv("NETPILOT_ROLLUP_LOOKBACK_DAYS", "7")
    assert config.get_settings().rollup_lookback_days == 7
    # Conservative clamp: a negative lookback becomes the 0 no-op, never a
    # window that reaches into the future or aggregates the current day.
    monkeypatch.setenv("NETPILOT_ROLLUP_LOOKBACK_DAYS", "-5")
    assert config.get_settings().rollup_lookback_days == 0
    # Unparseable input falls back to the default instead of a wild value.
    monkeypatch.setenv("NETPILOT_ROLLUP_LOOKBACK_DAYS", "weekly")
    assert config.get_settings().rollup_lookback_days == 1
