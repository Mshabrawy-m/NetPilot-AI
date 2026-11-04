"""Stage A3.1 tests: P1 demo/live reject-before-connect, P2 schema control.

All offline. PostgreSQL is only ever used as a *string* that triggers the
isolation guard before any connection; real connections/DDL are intercepted by
monkeypatching ``get_engine`` / ``init_db`` / ``create_all`` so the tests can
prove that nothing touches the database or issues DDL when it must not.
"""
from __future__ import annotations

import pytest
from sqlalchemy import inspect

import config
from database import connection
from database.models import Base
from services import app_state


# ------------------------------------------------- P1: reject before any connect
def test_init_db_rejects_demo_external_before_connection(monkeypatch):
    """init_db() must raise before it opens a connection to a shared DB."""
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "true")
    monkeypatch.setenv("DATABASE_URL",
                       "postgresql+psycopg2://user:secret@db.example/live")
    touched: list[str] = []
    monkeypatch.setattr(connection, "get_engine",
                        lambda *a, **k: touched.append("get_engine"))

    with pytest.raises(RuntimeError, match="ISOLATION"):
        connection.init_db()
    assert touched == []          # no engine, therefore no connection


def test_ensure_ready_rejects_before_any_db_call(monkeypatch):
    """The app entry point raises before init_db/require_schema is reached."""
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "true")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg2://db.example/live")
    touched: list[str] = []
    monkeypatch.setattr(app_state, "init_db",
                        lambda *a, **k: touched.append("init_db"))
    monkeypatch.setattr(app_state, "require_schema",
                        lambda *a, **k: touched.append("require_schema"))

    with pytest.raises(RuntimeError, match="ISOLATION"):
        app_state.ensure_ready_for_mode()
    assert touched == []


# -------------------------------------------------- demo continues on local SQLite
def test_demo_no_url_initializes_local_sqlite(monkeypatch, tmp_path):
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "true")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)   # redirect data dir

    app_state.ensure_ready_for_mode()          # manage_schema defaults to True

    assert "devices" in inspect(connection.get_engine()).get_table_names()


def test_demo_sqlite_url_supported(db_url, monkeypatch):
    # db_url fixture created a SQLite schema; switching demo on must still work.
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "true")
    app_state.ensure_ready_for_mode()          # local SQLite URL: allowed


# -------------------------------------------------------- P2: MANAGE_SCHEMA=false
def test_manage_schema_false_prevents_all_ddl(db_url, monkeypatch):
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "false")
    monkeypatch.setenv("NETPILOT_MANAGE_SCHEMA", "false")
    calls: list[str] = []
    monkeypatch.setattr(app_state, "init_db",
                        lambda *a, **k: calls.append("init_db"))
    monkeypatch.setattr(Base.metadata, "create_all",
                        lambda *a, **k: calls.append("create_all"))

    app_state.ensure_ready_for_mode()          # schema already present via db_url
    assert calls == []                          # reader ran no DDL at all


def test_reader_starts_when_schema_present(db_url, monkeypatch):
    monkeypatch.setenv("NETPILOT_MANAGE_SCHEMA", "false")
    app_state.ensure_ready_for_mode()          # no raise


def test_reader_fails_clearly_when_schema_missing(monkeypatch, tmp_path):
    url = f"sqlite:///{(tmp_path / 'empty.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "false")
    monkeypatch.setenv("NETPILOT_MANAGE_SCHEMA", "false")
    # Prove the reader NEVER attempts DDL, even to self-heal a missing schema.
    monkeypatch.setattr(Base.metadata, "create_all",
                        lambda *a, **k: pytest.fail("reader attempted DDL"))

    with pytest.raises(RuntimeError, match="init-schema"):
        app_state.ensure_ready_for_mode()


def test_require_schema_reports_missing_tables(monkeypatch, tmp_path):
    url = f"sqlite:///{(tmp_path / 'empty.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "false")
    missing = connection.missing_schema_objects()
    assert any("table devices" in m for m in missing)


def test_rollup_table_is_created_by_the_existing_init_schema(db_url, monkeypatch):
    """Stage A6-2 adds no migration step: the rollup table comes from ``init_db``.

    ``create_all`` covers every declared model, so the operator only ever runs the
    existing ``run_collector.py init-schema``; the rollup runner issues no DDL of
    its own and a missing table surfaces there as a database error (exit 3).
    """
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "false")
    assert "metric_daily_rollups" in inspect(
        connection.get_engine()).get_table_names()
    assert connection.missing_schema_objects() == []


# --------------------------------------------- P2: MANAGE_SCHEMA=true preserves dev
def test_manage_schema_true_calls_init_db(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL",
                       f"sqlite:///{(tmp_path / 'x.db').as_posix()}")
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "false")
    monkeypatch.setenv("NETPILOT_MANAGE_SCHEMA", "true")
    calls: list[str] = []
    monkeypatch.setattr(app_state, "init_db",
                        lambda *a, **k: calls.append("init"))

    app_state.ensure_ready_for_mode()
    assert calls == ["init"]


def test_config_manage_schema_default_is_true(monkeypatch):
    monkeypatch.delenv("NETPILOT_MANAGE_SCHEMA", raising=False)
    assert config.get_settings().manage_schema is True


def test_config_manage_schema_reads_env(monkeypatch):
    monkeypatch.setenv("NETPILOT_MANAGE_SCHEMA", "false")
    assert config.get_settings().manage_schema is False
