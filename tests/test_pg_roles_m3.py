"""Stage A6b-1 M3 tests: PostgreSQL role/GRANT scripts (offline, no database).

Nothing here opens a connection. The tests validate that:

* every table/sequence named in the privilege matrix really exists in the
  SQLAlchemy model metadata (grants are derived from the schema, never assumed);
* the least-privilege invariants hold (app/collector hold no DELETE, only
  retention deletes, no runtime role gets DDL, sequences track INSERT rights);
* the committed ``database/sql/roles/*.sql`` scripts contain EXACTLY the grant
  statements the single-source-of-truth module generates (SQL and code cannot
  drift apart);
* the SQL carries no credentials and no forbidden broad grants.

Anything that genuinely needs a live server (whether a role can actually
authenticate, whether the GRANTs apply cleanly, whether ownership transfer works
under Neon) is listed as a limitation in the runbook, not asserted here.
"""
from __future__ import annotations

import glob
import os
import re

import pytest

from database import pg_privileges as pgp
from database.models import Base

SQL_DIR = os.path.join(os.path.dirname(pgp.__file__), "sql", "roles")

# A concrete per-object runtime grant, e.g.
#   GRANT SELECT, INSERT ON TABLE public.metric_samples TO netpilot_app;
#   GRANT USAGE ON SEQUENCE public.users_id_seq TO netpilot_app;
_GRANT_RE = re.compile(
    r"^GRANT (?P<privs>[A-Za-z, ]+) ON (?P<kind>TABLE|SEQUENCE) "
    r"public\.(?P<obj>[a-z_]+) TO (?P<role>netpilot_[a-z]+);$")

ALL_MODEL_TABLES = set(Base.metadata.tables)


def _sql_files():
    return sorted(glob.glob(os.path.join(SQL_DIR, "*.sql")))


def _grant_statements_in_files():
    found = []
    for path in _sql_files():
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if (stripped.startswith("GRANT ") and stripped.endswith(";")
                        and _GRANT_RE.match(stripped)):
                    found.append(stripped)
    return found


# --------------------------------------------------------------- schema truth
def test_sql_files_present():
    assert _sql_files(), "no role scripts found under database/sql/roles"


@pytest.mark.parametrize("role", pgp.RUNTIME_ROLES)
def test_every_granted_table_exists_in_models(role):
    for table in pgp.ROLE_GRANTS[role]:
        assert table in ALL_MODEL_TABLES, (
            f"{role} references unknown table {table!r}")


def test_app_can_read_every_model_table():
    # The hosted reader displays data from all tables, so SELECT must cover
    # every model table - this catches a new table added without a grant.
    for table in ALL_MODEL_TABLES:
        assert "SELECT" in pgp.APP_GRANTS.get(table, ()), (
            f"netpilot_app lacks SELECT on {table}")


# ----------------------------------------------------- least-privilege invariants
@pytest.mark.parametrize("role", ["netpilot_app", "netpilot_collector"])
def test_no_delete_for_app_and_collector(role):
    for table, ops in pgp.ROLE_GRANTS[role].items():
        assert "DELETE" not in ops, f"{role} must not hold DELETE on {table}"


def test_only_retention_holds_delete():
    for role in pgp.RUNTIME_ROLES:
        has_delete = any("DELETE" in ops
                         for ops in pgp.ROLE_GRANTS[role].values())
        assert has_delete == (role == "netpilot_retention")


def test_grants_only_dml_no_ddl_tokens():
    # The matrix models only DML; DDL is expressed via ownership, never GRANT.
    for role in pgp.RUNTIME_ROLES:
        for ops in pgp.ROLE_GRANTS[role].values():
            assert set(ops) <= {"SELECT", "INSERT", "UPDATE", "DELETE"}


def test_retention_is_metric_samples_only():
    assert pgp.RETENTION_GRANTS == {"metric_samples": ("SELECT", "DELETE")}


def test_rollup_matches_spec():
    assert pgp.ROLLUP_GRANTS == {
        "metric_samples": ("SELECT",),
        "metric_daily_rollups": ("SELECT", "INSERT", "UPDATE"),
    }


# ------------------------------------------------------------ sequence coverage
@pytest.mark.parametrize("role", pgp.RUNTIME_ROLES)
def test_sequences_track_insert_privileges(role):
    # USAGE on <table>_id_seq is required exactly where the role may INSERT,
    # and never granted where it cannot.
    expected = sorted(t for t, ops in pgp.ROLE_GRANTS[role].items()
                      if "INSERT" in ops)
    assert pgp.sequence_tables(role) == tuple(expected)
    for table in pgp.sequence_tables(role):
        assert pgp.sequence_name(table) == f"public.{table}_id_seq"


def test_retention_gets_no_sequences():
    assert pgp.sequence_tables("netpilot_retention") == ()


# ------------------------------------------- committed SQL == generated module
def test_sql_grant_lines_match_module_exactly():
    in_files = set(_grant_statements_in_files())
    generated = set(pgp.all_runtime_grant_statements())
    assert in_files == generated, (
        "committed SQL and pg_privileges.py drifted; missing="
        f"{generated - in_files} extra={in_files - generated}")


def test_no_duplicate_grant_lines():
    lines = _grant_statements_in_files()
    assert len(lines) == len(set(lines))


def test_generated_statements_are_well_formed():
    for stmt in pgp.all_runtime_grant_statements():
        assert _GRANT_RE.match(stmt), f"malformed grant: {stmt!r}"


# ------------------------------------------------------------- safety scans
def test_no_broad_grant_to_runtime_roles():
    for path in _sql_files():
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                s = line.strip()
                if s.startswith("--"):
                    continue
                if "ALL PRIVILEGES" in s.upper():
                    pytest.fail(f"broad 'ALL PRIVILEGES' grant in {path}: {s}")
                # GRANT ALL is allowed only for the admin role (ownership).
                if s.upper().startswith("GRANT ALL") and not s.endswith(
                        "TO netpilot_admin;") and "netpilot_admin" not in s:
                    pytest.fail(f"'GRANT ALL' to a non-admin role in {path}: {s}")


def test_runtime_roles_are_never_granted_create_or_ddl():
    runtime = "|".join(pgp.RUNTIME_ROLES)
    pattern = re.compile(rf"GRANT[^;]*\bCREATE\b[^;]*TO (?:{runtime});")
    for path in _sql_files():
        with open(path, encoding="utf-8") as handle:
            text = "\n".join(line for line in handle
                             if not line.strip().startswith("--"))
        assert not pattern.search(text), f"DDL (CREATE) granted to runtime role in {path}"


def test_no_credentials_in_scripts():
    # Active (non-comment) statements must never set a password / secret.
    for path in _sql_files():
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                s = line.strip()
                if s.startswith("--") or not s:
                    continue
                upper = s.upper()
                assert "PASSWORD" not in upper, f"credential in {path}: {s}"
                assert "IDENTIFIED BY" not in upper, f"credential in {path}: {s}"


def test_app_and_collector_have_no_delete_grant_in_sql():
    delete_to_runtime = re.compile(
        r"GRANT[^;]*\bDELETE\b[^;]*TO (?:netpilot_app|netpilot_collector);")
    for path in _sql_files():
        with open(path, encoding="utf-8") as handle:
            text = "\n".join(line for line in handle
                             if not line.strip().startswith("--"))
        assert not delete_to_runtime.search(text)


# ----------------------------------------------------------- documentation glue
def test_every_role_has_a_documented_code_path():
    for role in pgp.RUNTIME_ROLES:
        assert role in pgp.ROLE_CODE_PATHS
        assert "repository" in pgp.ROLE_CODE_PATHS[role] or "collect_once" in \
            pgp.ROLE_CODE_PATHS[role] or "services" in pgp.ROLE_CODE_PATHS[role]
