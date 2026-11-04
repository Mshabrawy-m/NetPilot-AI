"""OFFLINE tests for tools/run_sql_as_role.py - never opens a connection.

They prove the runner's security contract holds without touching Neon: the SQL
path allowlist, DSN-from-environment-only, mandatory TLS (never downgraded),
credential redaction on every output path, and stop-on-first-error behaviour.
A fake ``connect`` is injected everywhere a real connection would be made.
"""
from __future__ import annotations

import pytest

from tools import run_sql_as_role as runner


# ------------------------------------------------------------- fake connection
class _FakeCursor:
    def __init__(self, recorder, error):
        self._recorder = recorder
        self._error = error

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql):
        self._recorder.append(sql)
        if self._error is not None:
            raise self._error


class _FakeConn:
    def __init__(self, recorder, error=None):
        self.autocommit = False
        self.closed = False
        self._recorder = recorder
        self._error = error

    def cursor(self):
        return _FakeCursor(self._recorder, self._error)

    def close(self):
        self.closed = True


def _make_connect(recorder, error=None, captured=None):
    def _connect(dsn, **kwargs):
        if captured is not None:
            captured["dsn"] = dsn
            captured["kwargs"] = kwargs
        return _FakeConn(recorder, error)
    return _connect


SAFE_DSN = "postgresql://neondb_owner:Secr3tPw@ep-test.neon.tech:5432/neondb"
APPROVED = "database/sql/roles/10_create_roles.sql"


# ---------------------------------------------------------------- path allowlist
@pytest.mark.parametrize(
    "rel", [f"database/sql/roles/{n}" for n in sorted(runner.APPROVED_SQL_FILES)])
def test_resolve_accepts_every_approved_script(rel):
    p = runner.resolve_approved_sql(rel)
    assert p.is_absolute() and p.name == rel.split("/")[-1]


def test_resolve_rejects_traversal_outside_roles_dir():
    with pytest.raises(runner.RoleRunError):
        runner.resolve_approved_sql("database/sql/roles/../../../app.py")


def test_resolve_rejects_unapproved_filename():
    with pytest.raises(runner.RoleRunError):
        runner.resolve_approved_sql("database/sql/roles/99_evil.sql")


def test_resolve_rejects_other_directory():
    with pytest.raises(runner.RoleRunError):
        runner.resolve_approved_sql("app.py")


# ------------------------------------------------------------------ DSN reading
def test_read_dsn_returns_value_from_named_env():
    env = {"MY_URL": SAFE_DSN}
    assert runner.read_dsn("MY_URL", env) == SAFE_DSN


def test_read_dsn_missing_is_credential_safe():
    env = {"MY_URL": "   "}
    with pytest.raises(runner.RoleRunError) as exc:
        runner.read_dsn("MY_URL", env)
    # Names the variable, never a value.
    assert "MY_URL" in str(exc.value)


# ------------------------------------------------------------------- TLS policy
def test_tls_upgrades_when_sslmode_absent():
    kwargs = runner.build_tls_kwargs(
        "postgresql://u:p@h:5432/db")
    assert kwargs["sslmode"] == "require"
    assert kwargs["connect_timeout"] == "10"


def test_tls_upgrades_when_weaker_than_require():
    kwargs = runner.build_tls_kwargs(
        "postgresql://u:p@h:5432/db?sslmode=disable")
    assert kwargs["sslmode"] == "require"


def test_tls_leaves_strong_operator_value_authoritative():
    kwargs = runner.build_tls_kwargs(
        "postgresql://u:p@h:5432/db?sslmode=verify-full&sslrootcert=/ca.crt")
    # No sslmode kwarg is emitted -> the stronger DSN value stands.
    assert "sslmode" not in kwargs


def test_tls_verify_without_ca_fails_closed():
    with pytest.raises(runner.RoleRunError):
        runner.build_tls_kwargs(
            "postgresql://u:p@h:5432/db?sslmode=verify-full")


def test_tls_rejects_non_postgres_scheme():
    with pytest.raises(runner.RoleRunError):
        runner.build_tls_kwargs("mysql://u:p@h:3306/db")


def test_tls_accepts_sqlalchemy_style_postgres_url():
    # postgresql+psycopg2:// is a valid Postgres DSN (psycopg2 is the DBAPI).
    kwargs = runner.build_tls_kwargs("postgresql+psycopg2://u:p@h:5432/db")
    assert kwargs["sslmode"] == "require"


# ------------------------------------------------------------------- redaction
def test_redact_scrubs_uri_userinfo_and_password_keyword():
    out = runner._redact(
        "fail postgresql://bob:Sup3rPw@host:5432/db password=TopSecret")
    assert "Sup3rPw" not in out and "TopSecret" not in out
    assert "://***@" in out and "bob:" not in out
    assert "host" in out                     # non-secret host retained


# ------------------------------------------------------------------------ run()
def test_run_success_executes_file_and_enforces_tls(capsys):
    recorder, captured = [], {}
    env = {"NP_URL": "postgresql://neondb_owner:Secr3tPw@ep.neon.tech:5432/neondb"}
    code = runner.run("NP_URL", APPROVED, environ=env,
                      connect=_make_connect(recorder, captured=captured))
    assert code == 0
    # The approved file's SQL was handed to the cursor, TLS was enforced.
    assert len(recorder) == 1 and "CREATE ROLE" in recorder[0]
    assert captured["kwargs"].get("sslmode") == "require"
    printed = capsys.readouterr().out
    assert "OK: executed 10_create_roles.sql" in printed
    assert "Secr3tPw" not in printed          # never leaks the password


def test_run_stops_on_execution_error_and_redacts(capsys):
    boom = RuntimeError(
        'connection failed: postgresql://neondb_owner:Secr3tPw@ep:5432/db')
    env = {"NP_URL": SAFE_DSN}
    code = runner.run("NP_URL", APPROVED, environ=env,
                      connect=_make_connect([], error=boom))
    printed = capsys.readouterr().out
    assert code == 1
    assert "OK" not in printed
    assert "Secr3tPw" not in printed          # error path is scrubbed too


def test_run_missing_dsn_refuses_without_connecting(capsys):
    calls: list = []

    def _no_connect(*a, **k):
        calls.append(a)
        raise AssertionError("must not connect when the DSN is missing")

    code = runner.run("NP_ABSENT", APPROVED, environ={}, connect=_no_connect)
    assert code == 2
    assert calls == []
    assert "NP_ABSENT" in capsys.readouterr().out


def test_run_disallowed_file_refuses_without_connecting(capsys):
    env = {"NP_URL": SAFE_DSN}
    code = runner.run("NP_URL", "app.py", environ=env,
                      connect=_make_connect([]))
    assert code == 2
    assert "approved" in capsys.readouterr().out.lower()


# --------------------------------------------------------- no DSN via arguments
def test_cli_has_no_dsn_or_password_argument():
    with pytest.raises(SystemExit):
        runner.build_parser().parse_args(
            ["--dsn", SAFE_DSN, "--file", APPROVED])
    with pytest.raises(SystemExit):
        runner.build_parser().parse_args(
            ["--password", "x", "--file", APPROVED])


def test_cli_requires_named_env_and_file():
    args = runner.build_parser().parse_args(
        ["--dsn-env", "NP_URL", "--file", APPROVED])
    assert args.dsn_env == "NP_URL" and args.file == APPROVED


# --------------------------------------------------------- port normalisation
@pytest.mark.parametrize("raw,expected", [
    (None, 5432),          # absent -> default
    (-1, 5432),            # [Uri].Port on a non-HTTP scheme -> default
    (0, 5432),             # zero -> default
    ("", 5432),            # empty -> default
    ("notaport", 5432),    # non-numeric -> default
    (5432, 5432),          # explicit default retained
    (5433, 5433),          # explicit non-default retained
    ("6432", 6432),        # numeric string coerced
])
def test_default_pg_port(raw, expected):
    assert runner.default_pg_port(raw) == expected


def test_parse_pg_target_defaults_missing_port():
    t = runner.parse_pg_target(
        "postgresql://neondb_owner:pw@ep-x.neon.tech/neondb")
    assert t == {"host": "ep-x.neon.tech", "port": 5432, "database": "neondb"}


def test_parse_pg_target_keeps_explicit_port():
    t = runner.parse_pg_target("postgresql://u:p@h:5433/db")
    assert t["port"] == 5433


def test_parse_pg_target_rejects_unparsable():
    with pytest.raises(runner.RoleRunError):
        runner.parse_pg_target("not a dsn at all")


# ------------------------------------------------------------- role DSN build
def test_build_role_dsn_defaults_port_and_requires_tls():
    built = runner.build_role_dsn(
        "postgresql://neondb_owner:OldPw@ep-x.neon.tech/neondb",
        "netpilot_admin", "NewPw123")
    assert built.startswith("postgresql://netpilot_admin:NewPw123@ep-x.neon.tech:5432/neondb")
    assert "sslmode=require" in built
    # Round-trips to a correct target (the very bug that broke Stage 3).
    t = runner.parse_pg_target(built)
    assert t["port"] == 5432 and t["host"] == "ep-x.neon.tech"


def test_build_role_dsn_urlencodes_special_password():
    built = runner.build_role_dsn(
        "postgresql://o:p@h:5432/db", "netpilot_admin", "a@b:c/d")
    # reserved userinfo characters are percent-encoded, so the DSN still parses
    assert "a%40b%3Ac%2Fd" in built
    assert runner.parse_pg_target(built)["host"] == "h"


def test_build_role_dsn_missing_host_or_db_refuses():
    with pytest.raises(runner.RoleRunError):
        runner.build_role_dsn("postgresql:///db", "netpilot_admin", "pw")
