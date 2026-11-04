"""Phase 2A read-only production validator tests - OFFLINE only.

No database connection is ever opened here: every test either exercises a pure
helper (read-only SQL audit, timeout parsing, disposable-branch guard, default-
ACL interpretation, FK/role expectations derived from the ORM/matrix) or drives
the live ``validate`` command's *gating*, which returns before any engine is
built. The real authorisation/TLS checks run only when an operator points the
tool at a persistent production branch with ``--confirm-production``.
"""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from database import roles
from tools import validate_pg_roles_m5 as m5
from tools import validate_pg_roles_prod as prod


# ------------------------------------------------------------------ reuse SSOT
def test_prod_reuses_the_m5_expected_matrix_functions():
    # "Reuse the existing role matrix... do not invent a different policy."
    # The prod tool imports the *same* callables M5 uses (identity, not a copy).
    assert prod.expected_table_privilege is m5.expected_table_privilege
    assert prod.expected_sequence_privilege is m5.expected_sequence_privilege
    assert prod.error_signature is m5.error_signature
    assert prod.ProbeResult is m5.ProbeResult


# ------------------------------------------------------------- read-only audit
def test_assert_readonly_accepts_pure_reads():
    prod._assert_readonly("SELECT 1")
    prod._assert_readonly("WITH x AS (SELECT 1) SELECT * FROM x")
    prod._assert_readonly("SHOW statement_timeout")
    prod._assert_readonly("SET TRANSACTION READ ONLY")  # enforces read-only


@pytest.mark.parametrize("sql", [
    "INSERT INTO t VALUES (1)",
    "UPDATE t SET x = 1",
    "DELETE FROM t",
    "DROP TABLE t",
    "ALTER ROLE x PASSWORD 'y'",
    "CREATE TABLE t (x int)",
    "GRANT ALL ON t TO x",
    "TRUNCATE t",
    "SELECT 1; DELETE FROM t",       # multi-statement smuggling
])
def test_assert_readonly_rejects_writes(sql):
    with pytest.raises(RuntimeError):
        prod._assert_readonly(sql)


def test_every_registered_sql_constant_is_readonly():
    # The exact statements the live run executes must all pass the audit.
    for stmt in prod.ALL_SQL_CONSTANTS:
        prod._assert_readonly(stmt)


def test_catalog_reads_with_privilege_name_literals_are_allowed():
    # 'INSERT'/'DELETE'/'CREATE' appear only as quoted privilege NAMES passed to
    # has_*_privilege(); the audit strips quoted literals, so these are reads.
    prod._assert_readonly(prod._SQL_TABLE_PRIVS)
    prod._assert_readonly(prod._SQL_SCHEMA_PRIVS)
    assert "INSERT" in prod._SQL_TABLE_PRIVS  # inside a string literal only


# ------------------------------------------------------------- expected values
def test_expected_role_names_are_the_five_m3_roles():
    assert set(prod.expected_role_names()) == set(roles.ALL_ROLES)
    assert roles.ROLE_ADMIN in prod.expected_role_names()


def test_expected_foreign_keys_derive_from_metadata():
    fks = prod.expected_foreign_keys()
    assert len(fks) >= 6
    assert ("interfaces", "devices") in fks
    assert ("alert_events", "alert_rules") in fks
    assert ("change_audit_events", "change_requests") in fks


def test_disposable_markers_are_configured():
    assert prod.KNOWN_DISPOSABLE_MARKERS
    assert "ep-aged-sky-bdmdoqzq" in prod.KNOWN_DISPOSABLE_MARKERS


# ---------------------------------------------------------- disposable guard
def test_points_at_disposable_matches_marker_host():
    env = {roles.ROLE_URL_ENV[roles.ROLE_APP]:
           "postgresql+psycopg2://netpilot_app@ep-aged-sky-bdmdoqzq.neon.tech:5432/netpilot?sslmode=require"}
    hit, detail = prod._points_at_disposable(env)
    assert hit is True
    assert "disposable" in detail and "NETPILOT_DB_URL_APP" in detail
    # never leaks the (absent) password / full userinfo
    assert "://" not in detail


def test_points_at_disposable_ignores_real_production_host():
    env = {roles.ROLE_URL_ENV[roles.ROLE_APP]:
           "postgresql+psycopg2://netpilot_app@ep-prod-branch.neon.tech:5432/netpilot?sslmode=require"}
    hit, _ = prod._points_at_disposable(env)
    assert hit is False


def test_points_at_disposable_empty_env_is_safe():
    hit, _ = prod._points_at_disposable({})
    assert hit is False


# ------------------------------------------------------------- timeout helpers
@pytest.mark.parametrize("raw,expected", [
    ("15s", 15000), ("15000ms", 15000), ("0", 0), ("0ms", 0),
    ("1min", 60000), ("garbage", None), ("2.5s", 2500),
])
def test_parse_statement_timeout_ms(raw, expected):
    assert prod._parse_statement_timeout_ms(raw) == expected


def test_expected_timeouts_read_from_settings_not_hardcoded():
    fake = SimpleNamespace(pg_statement_timeout_ms="15000", pg_connect_timeout="10")
    assert prod._expected_statement_timeout_ms(fake) == 15000
    assert prod._expected_connect_timeout_s(fake) == 10
    other = SimpleNamespace(pg_statement_timeout_ms="3000", pg_connect_timeout="5")
    assert prod._expected_statement_timeout_ms(other) == 3000
    assert prod._expected_connect_timeout_s(other) == 5


# ------------------------------------------------------ default-ACL interpretation
def test_interpret_default_acl_accepts_the_ssot_shape():
    rows = [(roles.ROLE_ADMIN, "r", "{netpilot_app=r/netpilot_admin}")]
    ok, detail = prod._interpret_default_acl(rows)
    assert ok is True and detail == ""


# ---------------------------------------------- PG16 catalog column regression
#
# PostgreSQL 16 renamed ``pg_default_acl.roleid`` to ``defaclrole``. A stale
# ``roleid`` reference in the validator's SQL compiles fine, passes every
# offline unit test, and only fails when the live tool runs against the
# production server ("column 'roleid' does not exist"). These tests pin the
# statement to PG16's real catalog shape so the drift is caught in CI.
_PG_DEFAULT_ACL_COLUMNS_PG16 = {
    "oid", "defaclrole", "defaclnamespace", "defaclobjtype", "defaclacl",
}


def test_sql_default_acl_targets_only_pg16_catalog_columns():
    # Every bare identifier that names a column of pg_default_acl must be one
    # PG16 actually exposes. We exclude SQL keywords, table name, function
    # names, aliases we project, and the ::regnamespace cast target.
    sql = prod._SQL_DEFAULT_ACL
    tokens = {
        t for t in re.split(r"[^A-Za-z_]+", sql)
        if t and not t.isupper()  # drop SELECT/FROM/WHERE/AS etc.
    }
    # Non-column identifiers we legitimately use.
    allowed_non_columns = {
        "public", "regnamespace", "text", "grantor", "acl",
        "pg_get_userbyid", "pg_default_acl",
    }
    column_refs = tokens - allowed_non_columns
    unknown = column_refs - _PG_DEFAULT_ACL_COLUMNS_PG16
    assert not unknown, (
        f"_SQL_DEFAULT_ACL references column(s) PG16 does not expose: "
        f"{sorted(unknown)}. This is exactly the 'roleid'/'defaclrole' "
        "drift that broke the production run.")


def test_sql_default_acl_uses_defaclrole_not_the_removed_roleid_alias():
    # Pin the specific rename: the SQL must name ``defaclrole`` and must NOT
    # name the pre-16 alias ``roleid`` (which no longer exists in PG16).
    sql = prod._SQL_DEFAULT_ACL
    assert "defaclrole" in sql
    # Word-boundary check so ``defaclrole`` alone doesn't count as ``roleid``.
    assert re.search(r"\broleid\b", sql) is None, (
        "_SQL_DEFAULT_ACL uses the removed 'roleid' column; PostgreSQL 16 "
        "renamed it to 'defaclrole'.")


@pytest.mark.parametrize("rows", [
    [],                                        # no defaults
    [(roles.ROLE_ADMIN, "r", "{netpilot_app=arwdDxt/netpilot_admin}")],  # writes
    [(roles.ROLE_ADMIN, "S", "{netpilot_app=r/netpilot_admin}")],        # sequences
    [(roles.ROLE_ADMIN, "r", "{netpilot_app=r/x,netpilot_collector=r/x}")],  # extra
    [(roles.ROLE_APP, "r", "{netpilot_app=r/netpilot_admin}")],          # wrong grantor
])
def test_interpret_default_acl_rejects_widening(rows):
    ok, _ = prod._interpret_default_acl(rows)
    assert ok is False


def test_interpret_default_acl_ignores_neon_cloud_admin_infrastructure():
    # Regression: Neon-managed branches ship two pre-existing pg_default_acl
    # rows whose grantor is ``cloud_admin`` (SELECT/INSERT/UPDATE/DELETE/etc.
    # to ``neon_superuser``, for the platform's own maintenance paths). Those
    # rows are NOT part of NetPilot's policy and cannot be removed by us, so
    # the check must scope to netpilot_admin's own entries and pass on a
    # correctly provisioned production branch - the exact state the tool hits
    # in the field.
    rows = [
        ("cloud_admin", "r", "{neon_superuser=a*r*w*d*D*/cloud_admin}"),
        ("cloud_admin", "S", "{neon_superuser=r*w*U*/cloud_admin}"),
        (roles.ROLE_ADMIN, "r", "{netpilot_app=r/netpilot_admin}"),
    ]
    ok, detail = prod._interpret_default_acl(rows)
    assert ok is True, detail


@pytest.mark.parametrize("rows", [
    # Only cloud_admin infrastructure rows and no netpilot_admin row: our
    # default-privilege policy is missing -> FAIL (check is NOT weakened).
    [("cloud_admin", "r", "{neon_superuser=a*r*w*d*D*/cloud_admin}")],
    # Two netpilot_admin rows (e.g. tables + sequences): policy widened -> FAIL.
    [(roles.ROLE_ADMIN, "r", "{netpilot_app=r/netpilot_admin}"),
     (roles.ROLE_ADMIN, "S", "{netpilot_app=r/netpilot_admin}")],
    # No rows at all -> FAIL.
    [],
])
def test_interpret_default_acl_still_fails_when_admin_policy_is_wrong(rows):
    ok, _ = prod._interpret_default_acl(rows)
    assert ok is False


# ---------------------------------------------------------------- credential safe
def test_error_signature_never_leaks_password():
    sig = prod.error_signature(SimpleNamespace(
        __str__=lambda self: "postgresql+psycopg2://u:Sup3rPw@db:5432/prod"))
    # error_signature scrubs the userinfo via str(exc); ensure the pw is gone.
    assert "Sup3rPw" not in sig


# ------------------------------------------------------------------------- gates
def _clear_separation(monkeypatch):
    for var in roles.ROLE_URL_ENV.values():
        monkeypatch.delenv(var, raising=False)


def test_validate_refuses_without_confirmation(monkeypatch, capsys):
    monkeypatch.setattr(prod, "get_settings",
                        lambda: SimpleNamespace(demo_mode=False))
    assert prod.main(["validate"]) == 2
    assert "--confirm-production" in capsys.readouterr().out.lower()


def test_validate_refuses_in_demo_mode(monkeypatch):
    monkeypatch.setattr(prod, "get_settings",
                        lambda: SimpleNamespace(demo_mode=True))
    assert prod.run_validate(True) == 2


def test_validate_refuses_without_separation(monkeypatch):
    monkeypatch.setattr(prod, "get_settings",
                        lambda: SimpleNamespace(demo_mode=False))
    _clear_separation(monkeypatch)
    assert prod.run_validate(True) == 2


def test_validate_refuses_disposable_target(monkeypatch, capsys):
    monkeypatch.setattr(prod, "get_settings",
                        lambda: SimpleNamespace(demo_mode=False))
    _clear_separation(monkeypatch)
    monkeypatch.setenv(roles.ROLE_URL_ENV[roles.ROLE_APP],
                       "postgresql+psycopg2://netpilot_app@"
                       "ep-aged-sky-bdmdoqzq.neon.tech:5432/netpilot?sslmode=require")
    assert prod.run_validate(True) == 2
    assert "disposable" in capsys.readouterr().out.lower()


# ----------------------------------------------------------------- offline command
def test_selfcheck_command_passes(monkeypatch, capsys):
    monkeypatch.setattr(prod, "get_settings", lambda: SimpleNamespace(
        pg_statement_timeout_ms="15000", pg_connect_timeout="10"))
    assert prod.main(["selfcheck"]) == 0
    out = capsys.readouterr().out
    assert "all SQL constants pass read-only audit" in out
    assert "8/8 checks passed" in out
