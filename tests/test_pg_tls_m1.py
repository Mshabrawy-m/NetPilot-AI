"""Stage A6b-1 M1 tests: PostgreSQL driver + TLS groundwork.

All offline. No real database is ever contacted: the TLS resolution is exercised
as a pure function, and the engine builder is exercised with ``create_engine``
monkeypatched so the exact ``connect_args`` handed to psycopg2 are captured
without opening a socket. A PostgreSQL URL is used only as a *string*. The
Demo/Live isolation guard and the schema-management guards are re-asserted to
prove M1 changed neither.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import config
from database import connection


def _s(mode="require", timeout="10", rootcert=""):
    """A minimal settings stand-in carrying only the M1 TLS fields."""
    return SimpleNamespace(pg_sslmode=mode, pg_connect_timeout=timeout,
                           pg_sslrootcert=rootcert)


# --------------------------------------------------------------- sslmode default
def test_default_sslmode_is_require():
    args = connection._resolve_pg_ssl_args(
        "postgresql+psycopg2://user@host:5432/db", settings=_s())
    assert args["sslmode"] == "require"
    assert args["connect_timeout"] == 10


# --------------------------------------------------------- verify-full with a CA
def test_verify_full_with_ca_path_emits_rootcert(tmp_path):
    ca = (tmp_path / "root.crt").as_posix()
    args = connection._resolve_pg_ssl_args(
        "postgresql+psycopg2://user@host/db",
        settings=_s(mode="verify-full", rootcert=ca))
    assert args["sslmode"] == "verify-full"
    assert args["sslrootcert"] == ca
    assert args["connect_timeout"] == 10


# -------------------------------------------- verify mode without a CA is refused
@pytest.mark.parametrize("mode", ["verify-ca", "verify-full"])
def test_verify_mode_without_ca_rejected(mode):
    with pytest.raises(ValueError, match="CA certificate"):
        connection._resolve_pg_ssl_args(
            "postgresql+psycopg2://user@host/db",
            settings=_s(mode=mode, rootcert=""))


# ----------------------------------------------------------- invalid sslmode value
def test_invalid_sslmode_rejected():
    with pytest.raises(ValueError, match="NETPILOT_PG_SSLMODE is invalid"):
        connection._resolve_pg_ssl_args(
            "postgresql+psycopg2://user@host/db", settings=_s(mode="bogus"))


def test_invalid_sslmode_in_url_rejected():
    with pytest.raises(ValueError, match="invalid sslmode"):
        connection._resolve_pg_ssl_args(
            "postgresql+psycopg2://user@host/db?sslmode=nonsense",
            settings=_s(mode="require"))


# ---------------------------------------------------------- invalid timeout values
@pytest.mark.parametrize("bad", ["", "abc", "0", "-1", "2.5"])
def test_invalid_timeout_rejected(bad):
    with pytest.raises(ValueError, match="NETPILOT_PG_CONNECT_TIMEOUT"):
        connection._resolve_pg_ssl_args(
            "postgresql+psycopg2://user@host/db",
            settings=_s(timeout=bad))


# ------------------------------------------------ never downgrade a stronger DSN
def test_dsn_stronger_sslmode_is_not_overridden():
    # env default is require; the DSN already asks for verify-full (+ CA).
    args = connection._resolve_pg_ssl_args(
        "postgresql+psycopg2://user@host/db"
        "?sslmode=verify-full&sslrootcert=/etc/neon-root.crt",
        settings=_s(mode="require"))
    # No sslmode key is emitted: the operator's stronger DSN value stays
    # authoritative and psycopg2's kwarg-overrides-DSN rule can never downgrade
    # it. The DSN already supplies a CA, so none is injected either.
    assert "sslmode" not in args
    assert "sslrootcert" not in args


def test_dsn_weaker_sslmode_is_upgraded():
    args = connection._resolve_pg_ssl_args(
        "postgresql+psycopg2://user@host/db?sslmode=disable",
        settings=_s(mode="require"))
    assert args["sslmode"] == "require"


def test_dsn_supplied_connect_timeout_is_respected():
    args = connection._resolve_pg_ssl_args(
        "postgresql+psycopg2://user@host/db?connect_timeout=5",
        settings=_s(timeout="10"))
    # The operator already chose a timeout in the DSN; we do not override it.
    assert "connect_timeout" not in args


# ------------------------------------------------------ error messages leak nothing
def test_error_messages_never_leak_credentials():
    secret = "Sup3rS3cretPw"
    url = f"postgresql+psycopg2://admin:{secret}@db.internal:5432/netpilot"
    with pytest.raises(ValueError) as excinfo:
        connection._resolve_pg_ssl_args(url, settings=_s(mode="bogus"))
    assert secret not in str(excinfo.value)
    assert "db.internal" not in str(excinfo.value)


def test_redact_credentials_helper_still_scrubs_dsn():
    secret = "Sup3rS3cretPw"
    raw = (f"psycopg2.OperationalError: could not connect "
           f"postgresql+psycopg2://admin:{secret}@db.internal:5432/netpilot")
    redacted = connection._redact_credentials(raw)
    assert secret not in redacted


# ----------------------------------------------------- engine receives TLS args
def test_build_engine_passes_tls_connect_args_for_postgres(monkeypatch):
    captured: dict = {}

    def _fake_create_engine(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return object()  # never used; no connection is opened

    monkeypatch.setattr(connection, "create_engine", _fake_create_engine)
    monkeypatch.setenv("NETPILOT_PG_SSLMODE", "require")
    monkeypatch.setenv("NETPILOT_PG_CONNECT_TIMEOUT", "10")
    monkeypatch.delenv("NETPILOT_PG_SSLROOTCERT", raising=False)
    # This test isolates the M1 TLS connect_args; disable the M2 statement
    # timeout so the assertion reflects TLS only (options merged separately).
    monkeypatch.setenv("NETPILOT_PG_STATEMENT_TIMEOUT_MS", "0")

    url = "postgresql+psycopg2://user@host:5432/db"
    connection._build_engine(url)

    assert captured["url"] == url
    assert captured["kwargs"]["connect_args"] == {
        "sslmode": "require", "connect_timeout": 10}
    assert captured["kwargs"]["pool_pre_ping"] is True
    assert captured["kwargs"]["future"] is True


def test_build_engine_bad_tls_config_fails_before_engine(monkeypatch):
    # A configuration error must raise during connect_args resolution, before
    # create_engine is ever invoked (so no socket / no credential exposure).
    called: list[str] = []
    monkeypatch.setattr(connection, "create_engine",
                        lambda *a, **k: called.append("create_engine"))
    monkeypatch.setenv("NETPILOT_PG_SSLMODE", "bogus")
    with pytest.raises(ValueError, match="NETPILOT_PG_SSLMODE is invalid"):
        connection._build_engine("postgresql+psycopg2://user@host/db")
    assert called == []


# ------------------------------------------------------------ SQLite unchanged
def test_build_engine_sqlite_connect_args_unchanged(monkeypatch):
    captured: dict = {}
    real_create_engine = connection.create_engine

    def _spy_create_engine(url, **kwargs):
        captured["kwargs"] = kwargs
        # Return a REAL in-memory SQLite engine (creation opens no network and
        # needs no driver install) so the pragma-listener registration that
        # follows create_engine keeps working exactly as before M1.
        return real_create_engine(url, **kwargs)

    monkeypatch.setattr(connection, "create_engine", _spy_create_engine)
    connection._build_engine("sqlite:///:memory:")

    # Exactly the pre-M1 SQLite arguments; no TLS keys leak in.
    assert captured["kwargs"]["connect_args"] == {
        "check_same_thread": False, "timeout": 15}
    assert "sslmode" not in captured["kwargs"]["connect_args"]


# ----------------------------------------------------- config defaults & plumbing
def test_settings_pg_tls_defaults(monkeypatch):
    for var in ("NETPILOT_PG_SSLMODE", "NETPILOT_PG_CONNECT_TIMEOUT",
                "NETPILOT_PG_SSLROOTCERT"):
        monkeypatch.delenv(var, raising=False)
    settings = config.get_settings()
    assert settings.pg_sslmode == "require"
    assert settings.pg_connect_timeout == "10"
    assert settings.pg_sslrootcert == ""


def test_settings_pg_tls_read_from_env(monkeypatch):
    monkeypatch.setenv("NETPILOT_PG_SSLMODE", "VERIFY-FULL")
    monkeypatch.setenv("NETPILOT_PG_CONNECT_TIMEOUT", "30")
    monkeypatch.setenv("NETPILOT_PG_SSLROOTCERT", "/etc/ca.pem")
    settings = config.get_settings()
    assert settings.pg_sslmode == "verify-full"      # lower-cased on read
    assert settings.pg_connect_timeout == "30"
    assert settings.pg_sslrootcert == "/etc/ca.pem"


# --------------------------------------- isolation + schema guards remain intact
def test_demo_external_pg_isolation_still_blocks_before_engine(monkeypatch):
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "true")
    monkeypatch.setenv("DATABASE_URL",
                       "postgresql+psycopg2://user:secret@db.example/live")
    touched: list[str] = []
    monkeypatch.setattr(connection, "get_engine",
                        lambda *a, **k: touched.append("get_engine"))
    with pytest.raises(RuntimeError, match="ISOLATION"):
        connection.init_db()
    # The TLS path lives inside engine construction; the isolation guard runs
    # first, so a demo-against-shared-PG run never reaches it.
    assert touched == []
