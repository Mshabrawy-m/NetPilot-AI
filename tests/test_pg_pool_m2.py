"""Stage A6b-1 M2 tests: PostgreSQL serverless connection-pool tuning.

All offline. No real database is contacted. Pool/statement resolution is
exercised as a pure function, and the engine builder is exercised with
``create_engine`` monkeypatched so the exact kwargs handed to SQLAlchemy are
captured without opening a socket. These tests also re-assert that M1 TLS and
the SQLite path are untouched by M2.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import config
from database import connection

_URL = "postgresql+psycopg2://user@host:5432/db"

_ALL_PG_ENV = (
    "NETPILOT_PG_SSLMODE", "NETPILOT_PG_CONNECT_TIMEOUT",
    "NETPILOT_PG_SSLROOTCERT", "NETPILOT_PG_POOL_SIZE",
    "NETPILOT_PG_MAX_OVERFLOW", "NETPILOT_PG_POOL_RECYCLE",
    "NETPILOT_PG_POOL_TIMEOUT", "NETPILOT_PG_STATEMENT_TIMEOUT_MS",
)


def _s2(pool_size="3", max_overflow="3", pool_recycle="60",
        pool_timeout="30", statement_timeout_ms="15000"):
    """A settings stand-in carrying only the M2 pool/timeout fields."""
    return SimpleNamespace(
        pg_pool_size=pool_size, pg_max_overflow=max_overflow,
        pg_pool_recycle=pool_recycle, pg_pool_timeout=pool_timeout,
        pg_statement_timeout_ms=statement_timeout_ms)


def _clear_pg_env(monkeypatch):
    for var in _ALL_PG_ENV:
        monkeypatch.delenv(var, raising=False)


# --------------------------------------------------------- default pool settings
def test_default_pool_settings():
    pool_kwargs, additions = connection._resolve_pg_pool_args(
        _URL, settings=_s2())
    assert pool_kwargs == {
        "pool_size": 3, "max_overflow": 3, "pool_recycle": 60,
        "pool_timeout": 30}
    assert additions == {"options": "-c statement_timeout=15000"}


# ----------------------------------------------------------- custom env values
def test_custom_pool_values():
    pool_kwargs, _ = connection._resolve_pg_pool_args(
        _URL, settings=_s2(pool_size="8", max_overflow="4", pool_recycle="120",
                           pool_timeout="45", statement_timeout_ms="30000"))
    assert pool_kwargs == {
        "pool_size": 8, "max_overflow": 4, "pool_recycle": 120,
        "pool_timeout": 45}


# ------------------------------------------------- statement timeout application
def test_statement_timeout_option_applied():
    _, additions = connection._resolve_pg_pool_args(
        _URL, settings=_s2(statement_timeout_ms="5000"))
    assert additions["options"] == "-c statement_timeout=5000"


def test_statement_timeout_zero_disables():
    _, additions = connection._resolve_pg_pool_args(
        _URL, settings=_s2(statement_timeout_ms="0"))
    assert additions == {}


def test_dsn_statement_timeout_is_not_overridden():
    # Operator already encodes statement_timeout in the DSN options param.
    url = _URL + "?options=-c%20statement_timeout%3D500"
    _, additions = connection._resolve_pg_pool_args(url, settings=_s2())
    assert "options" not in additions


# ---------------------------------------------- invalid zero/negative/malformed
@pytest.mark.parametrize("kwargs", [
    {"pool_size": "0"}, {"pool_size": "-1"}, {"pool_size": "abc"},
    {"pool_size": ""}, {"pool_size": "2.5"},
    {"max_overflow": "-1"}, {"max_overflow": "junk"},
    {"pool_recycle": "0"}, {"pool_recycle": "-5"},
    {"pool_timeout": "0"}, {"pool_timeout": "x"},
    {"statement_timeout_ms": "-1"}, {"statement_timeout_ms": "many"},
])
def test_invalid_pool_values_rejected(kwargs):
    with pytest.raises(ValueError):
        connection._resolve_pg_pool_args(_URL, settings=_s2(**kwargs))


def test_max_overflow_zero_is_allowed():
    pool_kwargs, _ = connection._resolve_pg_pool_args(
        _URL, settings=_s2(max_overflow="0"))
    assert pool_kwargs["max_overflow"] == 0


# ------------------------------------------------------- credential-safe errors
def test_error_message_never_leaks_credentials():
    secret = "Sup3rS3cretPw"
    url = f"postgresql+psycopg2://admin:{secret}@db.internal:5432/netpilot"
    with pytest.raises(ValueError) as excinfo:
        connection._resolve_pg_pool_args(url, settings=_s2(pool_size="bogus"))
    assert secret not in str(excinfo.value)
    assert "db.internal" not in str(excinfo.value)


# -------------------------------------------- engine applies pool + keeps TLS M1
def test_build_engine_applies_pool_and_keeps_tls(monkeypatch):
    captured: dict = {}

    def _fake_create_engine(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(connection, "create_engine", _fake_create_engine)
    _clear_pg_env(monkeypatch)

    connection._build_engine(_URL)

    kwargs = captured["kwargs"]
    # M2 pool tuning reaches create_engine (engine-level, not connect_args).
    assert kwargs["pool_size"] == 3
    assert kwargs["max_overflow"] == 3
    assert kwargs["pool_recycle"] == 60
    assert kwargs["pool_timeout"] == 30
    # M1 requirement preserved.
    assert kwargs["pool_pre_ping"] is True
    assert kwargs["future"] is True
    # M1 TLS behaviour intact, now merged with the statement_timeout option.
    assert kwargs["connect_args"]["sslmode"] == "require"
    assert kwargs["connect_args"]["connect_timeout"] == 10
    assert kwargs["connect_args"]["options"] == "-c statement_timeout=15000"


def test_build_engine_rejects_bad_pool_config_before_create_engine(monkeypatch):
    called: list[str] = []
    monkeypatch.setattr(connection, "create_engine",
                        lambda *a, **k: called.append("create_engine"))
    _clear_pg_env(monkeypatch)
    monkeypatch.setenv("NETPILOT_PG_POOL_SIZE", "bogus")

    with pytest.raises(ValueError, match="NETPILOT_PG_POOL_SIZE must be an integer"):
        connection._build_engine(_URL)
    assert called == []


# ------------------------------------------------------------ SQLite unchanged
def test_build_engine_sqlite_has_no_pool_or_options(monkeypatch):
    captured: dict = {}
    real_create_engine = connection.create_engine

    def _spy_create_engine(url, **kwargs):
        captured["kwargs"] = kwargs
        return real_create_engine(url, **kwargs)

    monkeypatch.setattr(connection, "create_engine", _spy_create_engine)
    _clear_pg_env(monkeypatch)
    # Even with pool env present, SQLite must ignore it entirely.
    monkeypatch.setenv("NETPILOT_PG_POOL_SIZE", "9")

    connection._build_engine("sqlite:///:memory:")

    kwargs = captured["kwargs"]
    assert kwargs["connect_args"] == {"check_same_thread": False, "timeout": 15}
    for key in ("pool_size", "max_overflow", "pool_recycle", "pool_timeout"):
        assert key not in kwargs
    assert "options" not in kwargs["connect_args"]
    assert "sslmode" not in kwargs["connect_args"]


# ------------------------------------------------------------ M1 TLS still works
def test_m1_tls_resolution_still_intact():
    ssl = SimpleNamespace(pg_sslmode="require", pg_connect_timeout="10",
                          pg_sslrootcert="")
    args = connection._resolve_pg_ssl_args(_URL, settings=ssl)
    assert args == {"sslmode": "require", "connect_timeout": 10}


# ----------------------------------------------------------- config defaults
def test_settings_pool_defaults(monkeypatch):
    _clear_pg_env(monkeypatch)
    settings = config.get_settings()
    assert settings.pg_pool_size == "3"
    assert settings.pg_max_overflow == "3"
    assert settings.pg_pool_recycle == "60"
    assert settings.pg_pool_timeout == "30"
    assert settings.pg_statement_timeout_ms == "15000"


def test_settings_pool_read_from_env(monkeypatch):
    monkeypatch.setenv("NETPILOT_PG_POOL_SIZE", "7")
    monkeypatch.setenv("NETPILOT_PG_MAX_OVERFLOW", "2")
    monkeypatch.setenv("NETPILOT_PG_POOL_RECYCLE", "180")
    monkeypatch.setenv("NETPILOT_PG_POOL_TIMEOUT", "20")
    monkeypatch.setenv("NETPILOT_PG_STATEMENT_TIMEOUT_MS", "60000")
    settings = config.get_settings()
    assert settings.pg_pool_size == "7"
    assert settings.pg_max_overflow == "2"
    assert settings.pg_pool_recycle == "180"
    assert settings.pg_pool_timeout == "20"
    assert settings.pg_statement_timeout_ms == "60000"
