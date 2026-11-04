"""Stage A6b-1 M4 tests: runtime database role integration.

All offline. No database connection is ever opened: URL resolution is a pure
function, and the engine builder is exercised with ``create_engine``
monkeypatched so the exact URL/kwargs handed to SQLAlchemy are captured without
a socket. These tests prove the M4 guarantees - each process resolves only its
own role URL, a missing role URL fails closed with no (and never an
administrator) fallback, runtime roles run no DDL and get no admin-only UI
actions, Demo Mode stays on isolated SQLite, and M1 TLS / M2 pool settings are
still applied to a PostgreSQL role connection.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from database import connection, roles

_ROLE_ENV = (
    "NETPILOT_DB_URL_APP", "NETPILOT_DB_URL_COLLECTOR",
    "NETPILOT_DB_URL_RETENTION", "NETPILOT_DB_URL_ROLLUP",
    "NETPILOT_DB_URL_ADMIN", "DATABASE_URL", "NETPILOT_DEMO_MODE",
    "NETPILOT_MANAGE_SCHEMA",
)

_PG_ENV = (
    "NETPILOT_PG_SSLMODE", "NETPILOT_PG_CONNECT_TIMEOUT",
    "NETPILOT_PG_SSLROOTCERT", "NETPILOT_PG_POOL_SIZE",
    "NETPILOT_PG_MAX_OVERFLOW", "NETPILOT_PG_POOL_RECYCLE",
    "NETPILOT_PG_POOL_TIMEOUT", "NETPILOT_PG_STATEMENT_TIMEOUT_MS",
)


def _settings(demo_mode=False, database_url="postgresql+psycopg2://legacy/db"):
    return SimpleNamespace(demo_mode=demo_mode, database_url=database_url)


@pytest.fixture(autouse=True)
def _clean_env_and_role(monkeypatch):
    """Isolate every test: clear role/PG env, reset declared role + engine."""
    for var in _ROLE_ENV + _PG_ENV:
        monkeypatch.delenv(var, raising=False)
    prev_role = connection.current_role()
    prev_engine = connection._engine
    prev_factory = connection._session_factory
    prev_url = connection._engine_url
    yield
    if (connection._engine is not None
            and connection._engine is not prev_engine):
        connection._engine.dispose()
    connection._engine = prev_engine
    connection._session_factory = prev_factory
    connection._engine_url = prev_url
    connection.set_database_role(prev_role)


# ------------------------------------------------------------------ mapping
def test_all_five_roles_are_recognised():
    assert set(roles.ROLE_URL_ENV) == set(roles.ALL_ROLES)
    assert roles.RUNTIME_ROLES == (
        roles.ROLE_COLLECTOR, roles.ROLE_APP,
        roles.ROLE_RETENTION, roles.ROLE_ROLLUP)
    assert roles.ROLE_ADMIN not in roles.RUNTIME_ROLES


def test_each_role_resolves_its_own_url(monkeypatch):
    dsn = {
        roles.ROLE_APP: "postgresql+psycopg2://a:pw@h/appdb",
        roles.ROLE_COLLECTOR: "postgresql+psycopg2://c:pw@h/coldb",
        roles.ROLE_RETENTION: "postgresql+psycopg2://r:pw@h/retdb",
        roles.ROLE_ROLLUP: "postgresql+psycopg2://u:pw@h/roldb",
        roles.ROLE_ADMIN: "postgresql+psycopg2://adm:pw@h/admdb",
    }
    for role, url in dsn.items():
        monkeypatch.setenv(roles.ROLE_URL_ENV[role], url)
    settings = _settings(database_url="postgresql+psycopg2://legacy/db")
    for role, url in dsn.items():
        assert roles.resolve_role_url(role, settings=settings) == url


def test_separation_off_falls_back_to_database_url(monkeypatch):
    # No NETPILOT_DB_URL_* set -> legacy single-connection deployment.
    settings = _settings(database_url="sqlite:///data/netpilot.db")
    assert roles.resolve_role_url(roles.ROLE_APP, settings=settings) \
        == "sqlite:///data/netpilot.db"


def test_unknown_role_is_rejected():
    with pytest.raises(roles.RoleConfigError, match="unknown database role"):
        roles.resolve_role_url("netpilot_ghost", settings=_settings())


# --------------------------------------------------- fail closed / no fallback
def test_missing_runtime_url_fails_closed_when_separation_on(monkeypatch):
    # Only the administrator URL is configured -> separation active, but the
    # collector URL is missing. The collector must NOT use the admin URL.
    admin = "postgresql+psycopg2://adm:Sup3rAdm1nPw@db.internal/admdb"
    monkeypatch.setenv(roles.ROLE_URL_ENV[roles.ROLE_ADMIN], admin)
    with pytest.raises(roles.RoleConfigError) as exc:
        roles.resolve_role_url(roles.ROLE_COLLECTOR, settings=_settings())
    message = str(exc.value)
    # No fallback to the administrator connection, and credential-safe.
    assert "Sup3rAdm1nPw" not in message
    assert "db.internal" not in message
    assert "adm:Sup3rAdm1nPw" not in message
    assert "NETPILOT_DB_URL_COLLECTOR" in message
    assert "netpilot_collector" in message


@pytest.mark.parametrize("role", roles.RUNTIME_ROLES)
def test_every_runtime_role_fails_closed_without_its_url(monkeypatch, role):
    # A non-admin role URL present activates separation for everyone.
    monkeypatch.setenv(roles.ROLE_URL_ENV[roles.ROLE_APP],
                       "postgresql+psycopg2://a@h/appdb")
    settings = _settings()
    if role == roles.ROLE_APP:
        # app's own URL is set, so it resolves; the other three must fail.
        assert roles.resolve_role_url(role, settings=settings).endswith("appdb")
        return
    with pytest.raises(roles.RoleConfigError):
        roles.resolve_role_url(role, settings=settings)


def test_admin_never_falls_back_when_separation_on(monkeypatch):
    monkeypatch.setenv(roles.ROLE_URL_ENV[roles.ROLE_APP],
                       "postgresql+psycopg2://a@h/appdb")
    # Separation on, admin URL missing -> schema init fails closed too.
    with pytest.raises(roles.RoleConfigError, match="NETPILOT_DB_URL_ADMIN"):
        roles.resolve_role_url(roles.ROLE_ADMIN, settings=_settings())


def test_role_error_message_never_contains_dsn(monkeypatch):
    monkeypatch.setenv(roles.ROLE_URL_ENV[roles.ROLE_ADMIN],
                       "postgresql+psycopg2://u:s3cr3t@secret-host/db")
    with pytest.raises(roles.RoleConfigError) as exc:
        roles.resolve_role_url(roles.ROLE_RETENTION, settings=_settings())
    assert "s3cr3t" not in str(exc.value) and "secret-host" not in str(exc.value)


# ---------------------------------------------------------- demo isolation
def test_demo_mode_ignores_role_urls_and_stays_sqlite(monkeypatch):
    for role, var in roles.ROLE_URL_ENV.items():
        monkeypatch.setenv(var, f"postgresql+psycopg2://x@h/{role}.db")
    settings = _settings(demo_mode=True,
                         database_url="sqlite:///data/netpilot_demo.db")
    for role in roles.ALL_ROLES:
        assert roles.resolve_role_url(role, settings=settings) \
            == "sqlite:///data/netpilot_demo.db"


# ---------------------------------------------------- DDL / admin action gates
@pytest.mark.parametrize("role", roles.RUNTIME_ROLES)
def test_runtime_role_may_not_run_ddl_under_separation(monkeypatch, role):
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "false")
    monkeypatch.setenv("NETPILOT_MANAGE_SCHEMA", "true")
    monkeypatch.setenv(roles.ROLE_URL_ENV[role],
                       f"postgresql+psycopg2://x@h/{role}.db")
    connection.set_database_role(role)
    assert connection.schema_management_allowed() is False


def test_legacy_single_url_app_may_still_run_ddl(monkeypatch):
    # Separation OFF (no NETPILOT_DB_URL_*): today's behaviour is preserved.
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "false")
    monkeypatch.setenv("NETPILOT_MANAGE_SCHEMA", "true")
    connection.set_database_role(roles.ROLE_APP)
    assert connection.schema_management_allowed() is True


def test_manage_schema_false_still_disables_ddl(monkeypatch):
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "false")
    monkeypatch.setenv("NETPILOT_MANAGE_SCHEMA", "false")
    connection.set_database_role(roles.ROLE_APP)
    assert connection.schema_management_allowed() is False


def test_demo_mode_may_manage_its_own_schema(monkeypatch):
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "true")
    monkeypatch.setenv("NETPILOT_MANAGE_SCHEMA", "true")
    connection.set_database_role(roles.ROLE_APP)
    assert connection.schema_management_allowed() is True
    assert connection.admin_actions_available() is True


@pytest.mark.parametrize("role", roles.RUNTIME_ROLES)
def test_runtime_role_admin_actions_disabled(monkeypatch, role):
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "false")
    monkeypatch.setenv(roles.ROLE_URL_ENV[role],
                       f"postgresql+psycopg2://x@h/{role}.db")
    connection.set_database_role(role)
    assert connection.admin_actions_available() is False


def test_admin_role_actions_not_locked_by_runtime_gate(monkeypatch):
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "false")
    monkeypatch.setenv(roles.ROLE_URL_ENV[roles.ROLE_ADMIN],
                       "postgresql+psycopg2://adm@h/admdb")
    connection.set_database_role(roles.ROLE_ADMIN)
    # Admin is not a runtime role, so it may run DDL / admin actions.
    assert connection.schema_management_allowed() is True
    assert connection.admin_actions_available() is True


# ------------------------------------------------------------ role defaults
def test_active_role_defaults_to_app_when_unset():
    connection.set_database_role(None)
    assert connection.current_role() is None
    assert connection.active_role() == roles.ROLE_APP


def test_set_database_role_rejects_unknown():
    with pytest.raises(roles.RoleConfigError):
        connection.set_database_role("not_a_role")


# --------------------------------------------- engine uses role url + TLS/pool
def test_get_engine_uses_role_url_and_keeps_tls_pool(monkeypatch):
    captured: dict = {}
    real_create_engine = connection.create_engine

    def _fake_create_engine(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        # Return a real in-memory SQLite engine so downstream glue is valid.
        return real_create_engine("sqlite:///:memory:")

    monkeypatch.setattr(connection, "create_engine", _fake_create_engine)
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "false")
    collector_url = "postgresql+psycopg2://c@h:5432/coldb"
    monkeypatch.setenv(roles.ROLE_URL_ENV[roles.ROLE_COLLECTOR], collector_url)
    connection.set_database_role(roles.ROLE_COLLECTOR)

    connection.get_engine()

    assert captured["url"] == collector_url
    kwargs = captured["kwargs"]
    assert kwargs["pool_pre_ping"] is True
    assert kwargs["pool_size"] == 3 and kwargs["max_overflow"] == 3
    assert kwargs["pool_recycle"] == 60 and kwargs["pool_timeout"] == 30
    assert kwargs["connect_args"]["sslmode"] == "require"
    assert kwargs["connect_args"]["connect_timeout"] == 10
    assert kwargs["connect_args"]["options"] == "-c statement_timeout=15000"


def test_get_engine_fails_closed_before_connecting(monkeypatch):
    called: list[str] = []
    monkeypatch.setattr(connection, "create_engine",
                        lambda *a, **k: called.append("create_engine"))
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "false")
    # Separation on via the app URL, but the collector URL is missing.
    monkeypatch.setenv(roles.ROLE_URL_ENV[roles.ROLE_APP],
                       "postgresql+psycopg2://a@h/appdb")
    connection.set_database_role(roles.ROLE_COLLECTOR)

    with pytest.raises(roles.RoleConfigError):
        connection.get_engine()
    assert called == []      # no engine created, so no socket is ever opened


# ------------------------------------------------- runners use their own role
def _runner_settings(demo_mode=False,
                     database_url="postgresql+psycopg2://legacy/db"):
    return SimpleNamespace(
        demo_mode=demo_mode, database_url=database_url,
        change_loop_enabled=False, lab_execution_enabled=False,
        retention_raw_days=30, retention_batch_size=5000,
        rollup_lookback_days=1)


def test_collector_validates_its_role_url_not_database_url(monkeypatch):
    from tools import run_collector
    # DATABASE_URL is unusable SQLite, but the collector's own URL is Postgres:
    # the runner must accept (it looks at the collector URL, not DATABASE_URL).
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "false")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///local.db")
    monkeypatch.setenv(roles.ROLE_URL_ENV[roles.ROLE_COLLECTOR],
                       "postgresql+psycopg2://c@h/coldb")
    assert run_collector.check_configuration(_runner_settings(
        database_url="sqlite:///local.db")) == []


def test_collector_rejected_when_its_role_url_is_sqlite(monkeypatch):
    from tools import run_collector
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "false")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg2://legacy/db")
    monkeypatch.setenv(roles.ROLE_URL_ENV[roles.ROLE_COLLECTOR],
                       "sqlite:///wrong.db")
    errors = run_collector.check_configuration(_runner_settings())
    assert any("netpilot_collector" in e for e in errors)


def test_collector_fails_closed_when_role_url_missing(monkeypatch):
    from tools import run_collector
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "false")
    monkeypatch.setenv(roles.ROLE_URL_ENV[roles.ROLE_APP],
                       "postgresql+psycopg2://a@h/appdb")  # activates separation
    errors = run_collector.check_configuration(_runner_settings())
    assert any("NETPILOT_DB_URL_COLLECTOR" in e for e in errors)


def test_retention_and_rollup_use_their_role_urls(monkeypatch):
    from tools import run_retention, run_rollup
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "false")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///local.db")
    monkeypatch.setenv(roles.ROLE_URL_ENV[roles.ROLE_RETENTION],
                       "postgresql+psycopg2://r@h/retdb")
    monkeypatch.setenv(roles.ROLE_URL_ENV[roles.ROLE_ROLLUP],
                       "postgresql+psycopg2://u@h/roldb")
    assert run_retention.check_configuration(_runner_settings(
        database_url="sqlite:///local.db")) == []
    assert run_rollup.check_configuration(_runner_settings(
        database_url="sqlite:///local.db")) == []


def test_runner_main_restores_role_after_collect(monkeypatch, tmp_path):
    from tools import run_collector
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "false")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg2://legacy/db")
    connection.set_database_role(None)
    monkeypatch.setattr(run_collector, "perform_collect",
                        lambda *a, **k: run_collector.EXIT_OK)
    monkeypatch.setattr(run_collector, "default_lock_path",
                        lambda: str(tmp_path / "c.lock"))
    assert run_collector.main(["collect"]) == run_collector.EXIT_OK
    # The declared role is restored (not left bound to the collector).
    assert connection.current_role() is None
