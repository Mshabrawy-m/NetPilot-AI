"""Stage A6b-1 M5 harness tests - OFFLINE only (no database connection).

These prove the validation harness's *logic* is correct and safe: its expected
grant matrix is derived from the M3 single source of truth, its denial detection
and credential redaction behave, and the live ``validate`` command refuses to
run - without opening any connection - unless it is confirmed disposable, not
in demo mode, and role separation is configured. The live authorisation checks
themselves require provisioned disposable Neon credentials and are exercised by
``validate_pg_roles_m5.py validate`` at that time, not here.
"""
from __future__ import annotations

import pytest

from database import roles
from database.models import Base
from tools import validate_pg_roles_m5 as m5


# --------------------------------------------------------- expected-matrix logic
def test_all_model_tables_matches_metadata():
    assert set(m5.all_model_tables()) == set(Base.metadata.tables)


@pytest.mark.parametrize("table,role,priv,expected", [
    # Least-privilege denials that MUST hold.
    ("devices", roles.ROLE_APP, "DELETE", False),
    ("metric_samples", roles.ROLE_APP, "DELETE", False),
    ("devices", roles.ROLE_COLLECTOR, "DELETE", False),
    ("metric_samples", roles.ROLE_RETENTION, "INSERT", False),
    ("devices", roles.ROLE_RETENTION, "DELETE", False),
    ("metric_samples", roles.ROLE_ROLLUP, "UPDATE", False),
    ("users", roles.ROLE_ROLLUP, "SELECT", False),
    # Granted operations.
    ("metric_samples", roles.ROLE_RETENTION, "DELETE", True),
    ("metric_samples", roles.ROLE_RETENTION, "SELECT", True),
    ("metric_daily_rollups", roles.ROLE_ROLLUP, "UPDATE", True),
    ("forecast_predictions", roles.ROLE_APP, "UPDATE", True),
    ("devices", roles.ROLE_APP, "SELECT", True),
    # Admin is not a runtime grantee in the matrix.
    ("devices", roles.ROLE_ADMIN, "DELETE", False),
])
def test_expected_table_privilege_tracks_ssot(table, role, priv, expected):
    assert m5.expected_table_privilege(table, role, priv) is expected


@pytest.mark.parametrize("table,role,expected", [
    ("devices", roles.ROLE_COLLECTOR, True),        # collector INSERTs devices
    ("metric_samples", roles.ROLE_RETENTION, False),  # retention never INSERTs
    ("metric_samples", roles.ROLE_COLLECTOR, True),   # collector INSERTs samples
    ("metric_daily_rollups", roles.ROLE_ROLLUP, True),
    ("metric_samples", roles.ROLE_ROLLUP, False),
    ("users", roles.ROLE_APP, True),
])
def test_expected_sequence_privilege_tracks_insert_targets(table, role, expected):
    assert m5.expected_sequence_privilege(table, role) is expected


# ------------------------------------------------------------ denial detection
class _Orig:
    def __init__(self, code):
        self.sqlstate = code


class _Err(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        if code is not None:
            self.orig = _Orig(code)


def test_is_privilege_denied_by_sqlstate():
    assert m5.is_privilege_denied(_Err("boom", "42501")) is True


def test_is_privilege_denied_by_message():
    assert m5.is_privilege_denied(
        _Err("permission denied for table devices")) is True


def test_is_privilege_denied_rejects_unrelated_error():
    assert m5.is_privilege_denied(_Err("connection refused")) is False


# --------------------------------------------------------------- credential safety
def test_error_signature_redacts_credentials():
    sig = m5.error_signature(
        _Err("postgresql+psycopg2://user:Sup3rPw@db.internal:5432/prod"))
    assert "Sup3rPw" not in sig
    assert "user:Sup3rPw" not in sig
    assert "***" in sig                       # userinfo replaced
    assert "db.internal" in sig               # host (non-secret) retained


def test_error_signature_includes_sqlstate_class():
    sig = m5.error_signature(_Err("denied", "42501"))
    assert "42501" in sig and "_Err" in sig


# ---------------------------------------------------------------- offline command
def test_selfcheck_offline_passes(capsys):
    assert m5.main(["selfcheck"]) == 0
    out = capsys.readouterr().out
    assert "demo resolve ignores role URL" in out
    assert "app has no DELETE" in out


# --------------------------------------------------------------------- live gate
@pytest.fixture()
def _no_separation(monkeypatch):
    for var in roles.ROLE_URL_ENV.values():
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("NETPILOT_DB_URL_ADMIN", raising=False)


def test_validate_refuses_without_confirmation(_no_separation, capsys):
    # No --confirm-disposable -> exit 2, and it must not have tried to connect.
    assert m5.main(["validate"]) == 2
    assert "confirm-disposable" in capsys.readouterr().out.lower()


def test_validate_refuses_in_demo_mode(monkeypatch):
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "true")
    monkeypatch.setenv(roles.ROLE_URL_ENV[roles.ROLE_APP], "postgresql+psycopg2://x@h/db")
    assert m5.run_validate(True) == 2   # demo mode is an absolute refusal


def test_validate_refuses_without_separation(monkeypatch, _no_separation):
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "false")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg2://x@h/db")
    # Separation not configured (no NETPILOT_DB_URL_*) -> refuse, no connection.
    assert m5.run_validate(True) == 2


# ------------------------------------------ collector write-probe completeness
# (offline: assert the probe statement, not a live write)
def test_required_device_columns_are_the_not_null_no_server_default_set():
    # name/type/status/updated_at are NOT NULL without a server default; the
    # serial id is filled by devices_id_seq. This is the set a raw INSERT must
    # supply because SQLAlchemy client-side defaults do not fire in text().
    assert m5.required_device_columns() == {"name", "type", "status", "updated_at"}


def test_probe_insert_sql_supplies_every_required_device_column():
    col_clause = m5.PROBE_INSERT_SQL.split("VALUES")[0]
    cols = {c.strip() for c in
            col_clause.split("(", 1)[1].split(")", 1)[0].split(",")}
    assert m5.required_device_columns() <= cols


def test_probe_status_is_a_documented_device_status():
    # never guess: it must be one of the model's allowed literals
    assert m5.PROBE_STATUS in m5.DEVICE_STATUSES


def test_probe_insert_stays_idempotent():
    assert "ON CONFLICT (name) DO NOTHING" in m5.PROBE_INSERT_SQL


def test_probe_insert_binds_status_and_updated_at_params():
    # the params _representative_operations must actually supply at runtime
    assert ":status" in m5.PROBE_INSERT_SQL
    assert ":updated_at" in m5.PROBE_INSERT_SQL


# ------------------------------------------------------- TLS verification (client)
def test_force_sslmode_rewrites_only_sslmode_and_keeps_endpoint():
    out = m5._force_sslmode(
        "postgresql://u:p@ep-aged-sky.neon.tech:5432/neondb?sslmode=require",
        "disable")
    assert "sslmode=disable" in out
    assert "sslmode=require" not in out
    # host + database preserved -> require and disable target the SAME endpoint
    assert "ep-aged-sky.neon.tech" in out and "/neondb" in out


def test_disable_refused_reports_not_configured_without_url(monkeypatch):
    monkeypatch.delenv(m5.roles.ROLE_URL_ENV[m5.roles.ROLE_APP], raising=False)
    refused, why = m5._disable_refused(m5.roles.ROLE_APP)
    assert refused is False and "not configured" in why


def test_tls_enforcement_block_is_composite_per_role():
    # The single per-role TLS check must name both client-side conditions.
    import ast
    import inspect
    src = inspect.getsource(m5._run_enforcement_probes)
    assert "_tls_probe" in src
    # The unreliable backend signal may only appear in explanatory prose
    # (comments/docstrings). To be QUERIED it would have to sit in a SQL string
    # literal, so assert no string constant - EXCLUDING docstrings - mentions it.
    tree = ast.parse(inspect.getsource(m5))
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = getattr(node, "body", ())
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docs.add(id(body[0].value))
    literals = [n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
                and id(n) not in docs]
    assert not [s for s in literals if "pg_stat_ssl" in s]
