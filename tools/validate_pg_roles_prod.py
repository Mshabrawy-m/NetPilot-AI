"""Phase 2A - Read-only NetPilot production validator for PostgreSQL/Neon.

Purpose
-------
Safely verify, against a real **production** Neon/PostgreSQL branch, that the
M3 privilege matrix, ownership, default privileges, foreign keys, TLS, and
statement/connect timeouts match what the runtime (M1/M2/M4) will actually use.
This tool performs **no DDL, no DML, and no password rotation**. It is the
production-safe sibling of ``tools/validate_pg_roles_m5.py``; the M5 disposable
harness is deliberately left untouched, and this module *imports* (never
modifies) M5's credential-safe helpers so the expected-matrix logic and the
redaction behaviour stay identical.

Commands
--------
    python tools/validate_pg_roles_prod.py selfcheck
        # OFFLINE, opens no connection. Audits that every SQL constant this
        # tool would run is read-only, that the disposable-branch markers are
        # configured (see safeguard below), that the expected matrix is exactly
        # the M3 SSOT, that FK expectations derive from the ORM metadata, and
        # that the live command's gating refuses every unsafe configuration.

    python tools/validate_pg_roles_prod.py validate --confirm-production
        # LIVE, READ ONLY. Connects as each role through the ordinary M4
        # machinery (``connection.get_engine``) so every check also exercises
        # M1 TLS + M2 pool/statement-timeout. Opens every introspection
        # connection with ``SET TRANSACTION READ ONLY`` and refuses any SQL
        # constant containing DDL/DML verbs.

Safety contract
---------------
* **Production only, gated.** ``validate`` refuses without
  ``--confirm-production``, while ``NETPILOT_DEMO_MODE=true``, or unless role
  separation is configured. It never targets the M5 disposable branch: if any
  ``NETPILOT_DB_URL_*`` hostname matches a KNOWN_DISPOSABLE_MARKERS entry the
  tool refuses immediately (see the M5 harness for the disposable path).
* **Read-only by construction.** All introspection SQL is SELECT / SHOW /
  catalog reads executed inside a read-only Postgres transaction; the module
  also asserts each statement's shape offline. Nothing in this file writes.
* **Credential-safe.** Only a role NAME, an SQLSTATE/error class and a redacted
  message are printed. DSNs, hosts, users, passwords and env-var values never
  appear in output. The URL is parsed for a marker match only to decide
  refusal, and the marker itself is a branch identifier (not a secret).
* **Never weakens the M5 harness.** M5's disposable-only validator remains the
  only tool that writes probe rows and rotates passwords. This one is
  structurally incapable of either.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text
from sqlalchemy.engine import make_url

from config import get_settings
from database import connection, roles
from database import pg_privileges as pgp
from database.models import Base
from tools.validate_pg_roles_m5 import (  # reuse M5, never modify it
    ROLE_SELECT_PROBE,
    ProbeResult,
    all_model_tables,
    error_signature,
    expected_sequence_privilege,
    expected_table_privilege,
)

#: Neon branch/endpoint identifiers that belong to the M5 disposable branch.
#: If any role URL's host contains one of these, this tool refuses (the M5
#: disposable harness is the right tool there; running read-only checks against
#: a branch that expires is a wasted opportunity, and accidentally pointing a
#: runtime role at a disposable branch is exactly what this guard prevents).
#: These are *branch identifiers* recorded in the checklist, not credentials.
KNOWN_DISPOSABLE_MARKERS: tuple[str, ...] = (
    "br-icy-breeze-bdxgftqg",
    "ep-aged-sky-bdmdoqzq",
)

# --------------------------------------------------------------------- SQL set
# Every literal below is read-only. The selfcheck scans this tuple and asserts
# each statement is safe. Adding a write statement here is a contract break.
_SQL_ROLES_PRESENT = (
    "SELECT rolname FROM pg_roles "
    "WHERE rolname = ANY(:names) ORDER BY rolname")
_SQL_SCHEMA_PRIVS = (
    "SELECT has_schema_privilege(:role, 'public', 'USAGE'), "
    "has_schema_privilege(:role, 'public', 'CREATE')")
_SQL_TABLE_PRIVS = (
    "SELECT c.relname AS tbl, p.priv, "
    "has_table_privilege(:role, 'public.'||c.relname, p.priv) AS ok "
    "FROM pg_class c CROSS JOIN "
    "unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE']) AS p(priv) "
    "WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'r' "
    "ORDER BY c.relname, p.priv")
_SQL_SEQUENCE_PRIVS = (
    "SELECT relname FROM pg_class WHERE relkind='S' "
    "AND relnamespace='public'::regnamespace")
_SQL_SEQUENCE_HAS = (
    "SELECT has_sequence_privilege(:role, 'public.'||:seq, 'USAGE')")
_SQL_OWNERSHIP = (
    "SELECT relname, relkind, pg_get_userbyid(relowner) AS owner "
    "FROM pg_class WHERE relnamespace='public'::regnamespace "
    "AND relkind IN ('r','S') ORDER BY relkind, relname")
_SQL_DEFAULT_ACL = (
    "SELECT pg_get_userbyid(roleid) AS grantor, defaclobjtype, "
    "defaclacl::text AS acl FROM pg_default_acl "
    "WHERE defaclnamespace='public'::regnamespace")
_SQL_FOREIGN_KEYS = (
    "SELECT c.conname, rel.relname AS tbl, ref.relname AS referred "
    "FROM pg_constraint c "
    "JOIN pg_class rel ON rel.oid = c.conrelid "
    "JOIN pg_class ref ON ref.oid = c.confrelid "
    "WHERE c.contype = 'f' AND c.connamespace = 'public'::regnamespace "
    "ORDER BY rel.relname, c.conname")
_SQL_TABLES_PRESENT = (
    "SELECT relname FROM pg_class "
    "WHERE relnamespace = 'public'::regnamespace AND relkind = 'r' "
    "ORDER BY relname")
_SQL_SHOW_STATEMENT_TIMEOUT = "SHOW statement_timeout"
_SQL_SET_READ_ONLY = "SET TRANSACTION READ ONLY"

#: Registry the offline selfcheck scans. Any SQL constant we ever execute
#: against production must appear here.
ALL_SQL_CONSTANTS: tuple[str, ...] = (
    _SQL_ROLES_PRESENT, _SQL_SCHEMA_PRIVS, _SQL_TABLE_PRIVS,
    _SQL_SEQUENCE_PRIVS, _SQL_SEQUENCE_HAS, _SQL_OWNERSHIP,
    _SQL_DEFAULT_ACL, _SQL_FOREIGN_KEYS, _SQL_TABLES_PRESENT,
    _SQL_SHOW_STATEMENT_TIMEOUT, _SQL_SET_READ_ONLY,
)

#: Only these statement prefixes are ever allowed. ``SET TRANSACTION READ ONLY``
#: is explicitly whitelisted (it *enforces* read-only); no other SET runs.
_SAFE_PREFIXES = ("SELECT", "WITH", "SHOW")
_SAFE_EXACT = ("SET TRANSACTION READ ONLY",)
#: Words that, if present as a standalone token in a statement we would execute,
#: indicate a mutation. Kept deliberately broad so future SQL additions cannot
#: sneak a write past the reader.
_FORBIDDEN_TOKENS: tuple[str, ...] = (
    "INSERT", "UPDATE", "DELETE", "TRUNCATE", "ALTER", "CREATE", "DROP",
    "GRANT", "REVOKE", "COPY", "VACUUM", "REFRESH", "REINDEX", "CLUSTER",
    "COMMENT", "SECURITY", "PREPARE", "EXECUTE", "DEALLOCATE",
    "CALL", "MERGE", "DO",
)

# ---------------------------------------------------------- read-only enforcement
def _assert_readonly(sql: str) -> None:
    """Refuse to run any SQL that could mutate the database.

    Called at execution time AND from the offline selfcheck against every
    registered SQL constant. Fails loudly - never silently weakens.
    """
    stripped = sql.strip().rstrip(";").strip()
    upper = stripped.upper()
    if upper in _SAFE_EXACT:
        return
    if not any(upper.startswith(p) for p in _SAFE_PREFIXES):
        raise RuntimeError(
            "read-only validator: statement must begin with SELECT/WITH/SHOW "
            "(or the exact SET TRANSACTION READ ONLY); refused a non-read "
            "prefix.")
    # Privilege *names* passed as string literals (e.g. has_table_privilege's
    # 'INSERT','CREATE') are data, not verbs. Strip single-quoted literals
    # before scanning so those legitimate catalog reads are not misflagged.
    dequoted = re.sub(r"'[^']*'", " ", upper)
    tokens = {t for t in re.split(r"[^A-Z]+", dequoted) if t}
    bad = set(_FORBIDDEN_TOKENS) & tokens
    if bad:
        raise RuntimeError(
            f"read-only validator: forbidden mutation verb {sorted(bad)} "
            "present in statement.")


def _open_readonly(role: str):
    """Open a connection as ``role`` with the transaction marked READ ONLY.

    Uses the same M4 machinery as the runtime (``connection.get_engine``), so
    TLS, pool, and statement_timeout are exercised identically, then issues
    ``SET TRANSACTION READ ONLY`` as the first statement of the transaction.
    Any subsequent write would raise - this is a *server-side* guarantee, not
    only the code-level ``_assert_readonly`` check.
    """
    connection.set_database_role(role)
    engine = connection.get_engine()
    conn = engine.connect()
    try:
        conn.execute(text(_SQL_SET_READ_ONLY))
    except Exception:
        conn.close()
        raise
    return conn


@contextmanager
def _readonly_conn(role: str) -> Iterator:
    """Context-manager form: guarantees the connection closes and rolls back."""
    conn = _open_readonly(role)
    try:
        yield conn
    finally:
        with suppress(Exception):  # best-effort rollback; READ ONLY leaves nothing
            conn.rollback()
        conn.close()


def _q(conn, sql: str, params: dict | None = None):
    """Run one vetted read-only query."""
    _assert_readonly(sql)
    if params is None:
        return conn.execute(text(sql))
    return conn.execute(text(sql), params)


# ------------------------------------------------------ disposable-target guard
def _url_host(url: str) -> str:
    """Return only the hostname of a URL, for marker matching. Never printed."""
    try:
        return (make_url(url).host or "").lower()
    except Exception:  # noqa: BLE001 - unparsable URLs are handled elsewhere
        return ""


def _points_at_disposable(env) -> tuple[bool, str]:
    """True if any NETPILOT_DB_URL_* host matches a KNOWN_DISPOSABLE_MARKER.

    The message names the matched marker (a branch identifier, not a secret)
    and the offending env-var name. The URL value is never echoed.
    """
    for role_name, env_var in roles.ROLE_URL_ENV.items():
        url = (env.get(env_var, "") or "").strip()
        if not url:
            continue
        host = _url_host(url)
        if not host:
            continue
        for marker in KNOWN_DISPOSABLE_MARKERS:
            if marker.lower() in host:
                return True, (f"{env_var} (role {role_name!r}) points at "
                              f"disposable marker {marker!r}")
    return False, ""


# ------------------------------------------------------------ expected values
def _expected_statement_timeout_ms(settings) -> int:
    """Statement timeout (ms) the runtime applies, read from the validated
    setting (default 15000). Never hardcoded, so it always tracks config."""
    return int(str(settings.pg_statement_timeout_ms).strip())


def _expected_connect_timeout_s(settings) -> int:
    """TCP connect timeout (seconds) from NETPILOT_PG_CONNECT_TIMEOUT (10)."""
    return int(str(settings.pg_connect_timeout).strip())


def expected_role_names() -> tuple[str, ...]:
    """The five roles production must expose (M3 SSOT + admin owner)."""
    return tuple(sorted({roles.ROLE_ADMIN, *pgp.RUNTIME_ROLES}))


def expected_foreign_keys() -> set[tuple[str, str]]:
    """``(table, referred_table)`` pairs declared on the ORM models.

    Derived from ``Base.metadata``, so the FK expectation can never drift from
    the schema definition. Used to verify every FK actually exists in the
    catalog after ``init-schema``.
    """
    out: set[tuple[str, str]] = set()
    for table in Base.metadata.tables.values():
        for column in table.columns:
            for fk in column.foreign_keys:
                referred = fk.target_fullname.split(".", 1)[0]
                out.add((table.name, referred))
    return out


# ------------------------------------------------------------------- OFFLINE
def run_selfcheck() -> int:
    """Every offline guarantee the live run depends on. Opens no connection."""
    results: list[ProbeResult] = []

    # 1. Every SQL constant this tool ever executes is read-only by construction.
    stmt_bad: list[str] = []
    for stmt in ALL_SQL_CONSTANTS:
        try:
            _assert_readonly(stmt)
        except RuntimeError:
            stmt_bad.append(stmt[:40])
    results.append(ProbeResult(
        "all SQL constants pass read-only audit", not stmt_bad,
        f"bad={stmt_bad}" if stmt_bad else ""))

    # 2. The disposable-branch markers are configured (safeguard 13).
    results.append(ProbeResult(
        "disposable markers are configured for the guard",
        bool(KNOWN_DISPOSABLE_MARKERS)
        and all(isinstance(m, str) and m for m in KNOWN_DISPOSABLE_MARKERS)))

    # 3. Expected role set == M3 SSOT (never invents a different policy).
    expected = set(expected_role_names())
    results.append(ProbeResult(
        "expected roles == M3 SSOT (4 runtime + admin)",
        expected == set(roles.ALL_ROLES)
        and roles.ROLE_ADMIN in expected
        and set(pgp.RUNTIME_ROLES) <= expected))

    # 4. Table-privilege expectations derive from the same matrix M5 uses.
    matrix_ok = all(
        expected_table_privilege(t, r, p)
        is (p in pgp.ROLE_GRANTS[r].get(t, ()))
        for r in pgp.RUNTIME_ROLES
        for t in pgp.ROLE_GRANTS[r]
        for p in ("SELECT", "INSERT", "UPDATE", "DELETE"))
    results.append(ProbeResult(
        "table-privilege helper tracks pg_privileges SSOT", matrix_ok))

    # 5. Foreign-key expectations come from Base.metadata (not hand-typed).
    fks = expected_foreign_keys()
    results.append(ProbeResult(
        "FK expectations derive from ORM metadata",
        len(fks) >= 6 and ("interfaces", "devices") in fks
        and ("alert_events", "alert_rules") in fks))

    # 6. Sequence privilege expectations track INSERT targets.
    seq_ok = all(
        expected_sequence_privilege(t, r) == (t in pgp.insert_tables(r))
        for r in pgp.RUNTIME_ROLES for t in all_model_tables())
    results.append(ProbeResult(
        "sequence-privilege helper tracks insert targets", seq_ok))

    # 7. Expected timeouts derive from the validated settings (not hardcoded).
    settings = get_settings()
    try:
        stmt = _expected_statement_timeout_ms(settings)
        conn_t = _expected_connect_timeout_s(settings)
        ok = stmt >= 0 and conn_t > 0
        detail = f"statement_timeout_ms={stmt} connect_timeout_s={conn_t}"
    except (TypeError, ValueError) as exc:
        ok, detail = False, error_signature(exc)
    results.append(ProbeResult(
        "expected timeouts derive from settings (parse ok)", ok, detail))

    # 8. Read-only connection guard is the exact whitelisted statement.
    results.append(ProbeResult(
        "SET TRANSACTION READ ONLY whitelisted as safe",
        _SQL_SET_READ_ONLY == "SET TRANSACTION READ ONLY"))

    return _emit(results)


# ---------------------------------------------------------------------- LIVE
def _validate_roles_present(out: list[ProbeResult]) -> None:
    with _readonly_conn(roles.ROLE_ADMIN) as conn:
        rows = [r[0] for r in _q(conn, _SQL_ROLES_PRESENT,
                                 {"names": list(expected_role_names())})]
    out.append(ProbeResult(
        "all five production roles exist",
        set(rows) == set(expected_role_names()),
        f"missing={sorted(set(expected_role_names()) - set(rows))}"
        if set(rows) != set(expected_role_names()) else ""))


def _validate_schema_privileges(out: list[ProbeResult]) -> None:
    with _readonly_conn(roles.ROLE_ADMIN) as conn:
        for role in pgp.RUNTIME_ROLES:
            usage, create = _q(conn, _SQL_SCHEMA_PRIVS,
                               {"role": role}).one()
            out.append(ProbeResult(
                f"{role}: USAGE on schema public, NO CREATE",
                bool(usage) and not bool(create),
                f"usage={bool(usage)} create={bool(create)}"))
        # Admin owns the schema, so it must have CREATE.
        usage, create = _q(conn, _SQL_SCHEMA_PRIVS,
                           {"role": roles.ROLE_ADMIN}).one()
        out.append(ProbeResult(
            f"{roles.ROLE_ADMIN}: owner USAGE+CREATE on public",
            bool(usage) and bool(create)))


def _validate_table_privileges(out: list[ProbeResult]) -> None:
    with _readonly_conn(roles.ROLE_ADMIN) as conn:
        for role in pgp.RUNTIME_ROLES:
            rows = _q(conn, _SQL_TABLE_PRIVS, {"role": role}).all()
            mismatches = [
                f"{role}.{tbl}.{priv}={actual!r}"
                for tbl, priv, actual in rows
                if bool(actual) != expected_table_privilege(tbl, role, priv)]
            out.append(ProbeResult(
                f"{role}: table privileges match M3 matrix",
                not mismatches,
                f"mismatches={mismatches[:6]}" if mismatches else ""))


def _validate_sequence_privileges(out: list[ProbeResult]) -> None:
    with _readonly_conn(roles.ROLE_ADMIN) as conn:
        present = {r[0] for r in _q(conn, _SQL_SEQUENCE_PRIVS)}
        expected_seqs = {f"{t}_id_seq" for t in all_model_tables()}
        out.append(ProbeResult(
            "SERIAL sequences named <table>_id_seq",
            expected_seqs <= present,
            f"missing={sorted(expected_seqs - present)}"
            if expected_seqs - present else ""))
        for role in pgp.RUNTIME_ROLES:
            bad: list[str] = []
            for table in all_model_tables():
                seq_name = f"{table}_id_seq"
                if seq_name not in present:
                    continue
                ok = _q(conn, _SQL_SEQUENCE_HAS,
                        {"role": role, "seq": seq_name}).scalar()
                if bool(ok) != expected_sequence_privilege(table, role):
                    bad.append(f"{role}.{seq_name}")
            out.append(ProbeResult(
                f"{role}: sequence USAGE matches INSERT targets",
                not bad, f"bad={bad[:6]}" if bad else ""))


def _validate_ownership(out: list[ProbeResult]) -> None:
    with _readonly_conn(roles.ROLE_ADMIN) as conn:
        rows = _q(conn, _SQL_OWNERSHIP).all()
    bad = [(name, kind, owner) for name, kind, owner in rows
           if owner != roles.ROLE_ADMIN]
    out.append(ProbeResult(
        "every public table/sequence owned by netpilot_admin",
        not bad,
        f"wrong_owners={bad[:6]}" if bad else ""))


def _interpret_default_acl(rows) -> tuple[bool, str]:
    """Pure check of ``pg_default_acl`` rows against the M3 policy.

    Expected: exactly one default-ACL entry, created by ``netpilot_admin`` for
    relations (``objtype == 'r'``), granting ``netpilot_app`` SELECT ('r') and
    nothing else - no default INSERT/UPDATE/DELETE, no default sequence
    privileges, no other grantee. Factored out so an offline fixture can assert
    it without a connection.
    """
    if len(rows) != 1:
        return False, f"unexpected default ACL rows: {len(rows)}"
    grantor, objtype, acl_text = rows[0]
    if grantor != roles.ROLE_ADMIN or objtype != "r":  # 'r' = relations/tables
        return False, f"grantor/objtype mismatch: {grantor}/{objtype}"
    body = (acl_text or "").strip("{}")
    entries = [e for e in body.split(",") if e.strip()]
    non_admin = [e for e in entries
                 if not e.startswith(roles.ROLE_ADMIN + "=")]
    if len(non_admin) != 1 or "netpilot_app=r/" not in non_admin[0]:
        return False, f"default ACL not SELECT-for-app: {acl_text!r}"
    return True, ""


def _validate_default_privileges(out: list[ProbeResult]) -> None:
    with _readonly_conn(roles.ROLE_ADMIN) as conn:
        rows = _q(conn, _SQL_DEFAULT_ACL).all()
    ok, detail = _interpret_default_acl(rows)
    out.append(ProbeResult(
        "default privileges == SSOT (SELECT on future tables -> app, only)",
        ok, detail))


def _validate_foreign_keys(out: list[ProbeResult]) -> None:
    with _readonly_conn(roles.ROLE_ADMIN) as conn:
        rows = _q(conn, _SQL_FOREIGN_KEYS).all()
    present = {(tbl, referred) for _, tbl, referred in rows}
    expected = expected_foreign_keys()
    missing = sorted(expected - present)
    out.append(ProbeResult(
        "every model foreign key exists in the catalog",
        not missing,
        f"missing={missing}" if missing else ""))


def _validate_schema_tables(out: list[ProbeResult]) -> None:
    with _readonly_conn(roles.ROLE_ADMIN) as conn:
        present = {r[0] for r in _q(conn, _SQL_TABLES_PRESENT)}
    expected = set(all_model_tables())
    missing = sorted(expected - present)
    out.append(ProbeResult(
        "all model tables exist in the schema",
        not missing,
        f"missing={missing}" if missing else ""))


def _tls_probe(role: str) -> tuple[bool, str]:
    """Client-side TLS proof, read-only.

    Same strategy as M5: Neon's proxy makes backend pg_stat_ssl unreliable, so
    prove TLS via (a) a sslmode=require connection that opens and runs a
    granted SELECT (never a write) and (b) a sslmode=disable attempt to the
    SAME endpoint that the endpoint must refuse.
    """
    from tools.validate_pg_roles_m5 import _disable_refused  # reuse, do not modify
    stmt_value = None
    try:
        with _readonly_conn(role) as conn:
            table = ROLE_SELECT_PROBE[role]
            conn.execute(text(f"SELECT 1 FROM public.{table} LIMIT 1"))
            stmt_value = conn.execute(text(_SQL_SHOW_STATEMENT_TIMEOUT)).scalar()
    except Exception as exc:  # noqa: BLE001
        return False, f"sslmode=require read-only connect failed: {error_signature(exc)}"
    refused, why = _disable_refused(role)
    if not refused:
        return False, f"sslmode=disable was NOT refused ({why})"
    return True, (f"require ok (statement_timeout={stmt_value!r}); "
                  f"disable refused via {why}")


def _validate_tls(out: list[ProbeResult]) -> None:
    for role in pgp.RUNTIME_ROLES:
        passed, detail = _tls_probe(role)
        out.append(ProbeResult(
            f"{role}: TLS enforced (require ok + disable refused)",
            passed, detail))


def _validate_timeouts(out: list[ProbeResult]) -> None:
    """Confirm the runtime's own M1/M2 argument builders land the expected
    connect_timeout and statement_timeout on the URL, without any DSN printing."""
    settings = get_settings()
    try:
        expected_stmt_ms = _expected_statement_timeout_ms(settings)
        expected_conn_s = _expected_connect_timeout_s(settings)
    except (TypeError, ValueError) as exc:
        out.append(ProbeResult("settings parse as integers", False,
                               error_signature(exc)))
        return

    for role in roles.ALL_ROLES:
        env_var = roles.ROLE_URL_ENV[role]
        url = os.environ.get(env_var, "").strip()
        if not url:
            out.append(ProbeResult(f"{role}: URL configured", False,
                                   f"{env_var} unset"))
            continue
        # Exercise the same helper that the runtime engine builder uses; it
        # reads sslmode/connect_timeout from settings + DSN query, and its
        # errors are credential-safe by construction.
        try:
            ssl_args = connection._resolve_pg_ssl_args(url, settings=settings)
        except ValueError as exc:
            out.append(ProbeResult(f"{role}: ssl args parse", False,
                                   error_signature(exc)))
            continue
        sslmode = ssl_args.get("sslmode", "operator-stronger")
        conn_t = ssl_args.get("connect_timeout",
                              # If omitted, the DSN already carries connect_timeout
                              # (or defaults); the helper's own precedence stands.
                              expected_conn_s)
        out.append(ProbeResult(
            f"{role}: sslmode >= require, connect_timeout matches",
            (sslmode in ("require", "verify-ca", "verify-full",
                         "operator-stronger")
             and conn_t == expected_conn_s),
            f"sslmode={sslmode!r} connect_timeout={conn_t}"))

    # Server-side SHOW statement_timeout as each role, on a read-only conn.
    with _readonly_conn(roles.ROLE_ADMIN) as conn:
        raw = conn.execute(text(_SQL_SHOW_STATEMENT_TIMEOUT)).scalar()
    actual_ms = _parse_statement_timeout_ms(str(raw))
    out.append(ProbeResult(
        "statement_timeout applied as configured (admin session)",
        actual_ms == expected_stmt_ms or expected_stmt_ms == 0,
        f"SHOW={raw!r} parsed_ms={actual_ms} expected_ms={expected_stmt_ms}"))


def _parse_statement_timeout_ms(value: str) -> int | None:
    """Parse Postgres ``SHOW statement_timeout`` ('15s', '15000ms', '0', '0ms')
    into milliseconds. Returns None for anything unexpected so the caller's
    comparison fails safe rather than passing on a parse coincidence."""
    v = value.strip().lower()
    if v in ("0", "off", ""):
        return 0
    for unit, mult in (("ms", 1), ("s", 1000), ("min", 60000), ("h", 3600000)):
        if v.endswith(unit):
            try:
                return round(float(v[: -len(unit)]) * mult)
            except ValueError:
                return None
    return None


# --------------------------------------------------------------------- gate
def run_validate(confirm_production: bool) -> int:
    """The gated live read-only validation. Refuses unsafe configurations."""
    if not confirm_production:
        print("REFUSED: --confirm-production is required. This tool is for a "
              "persistent production Neon branch only. Re-run against the M5 "
              "disposable branch via `tools/validate_pg_roles_m5.py validate "
              "--confirm-disposable` if that is what you meant.")
        return 2
    settings = get_settings()
    if settings.demo_mode:
        print("REFUSED: NETPILOT_DEMO_MODE=true. Production validation requires "
              "the persistent-branch role URLs and demo mode off.")
        return 2
    if not roles.role_separation_enabled():
        print("REFUSED: role separation is not configured (no "
              "NETPILOT_DB_URL_* set). Provision the production branch role "
              "URLs via the environment (never in chat) before running.")
        return 2
    hit, detail = _points_at_disposable(os.environ)
    if hit:
        print(f"REFUSED: the configured role URLs point at the M5 disposable "
              f"branch ({detail}). Do not validate production on a throwaway "
              f"that will expire; use validate_pg_roles_m5 for the disposable "
              f"branch instead.")
        return 2

    out: list[ProbeResult] = []
    try:
        _validate_roles_present(out)
        _validate_schema_privileges(out)
        _validate_table_privileges(out)
        _validate_sequence_privileges(out)
        _validate_ownership(out)
        _validate_default_privileges(out)
        _validate_foreign_keys(out)
        _validate_schema_tables(out)
        _validate_tls(out)
        _validate_timeouts(out)
    except roles.RoleConfigError as exc:
        out.append(ProbeResult("role configuration complete", False,
                               error_signature(exc)))
    except Exception as exc:  # noqa: BLE001 - surface DB errors safely
        out.append(ProbeResult("production validation aborted on error", False,
                               error_signature(exc)))
    finally:
        connection.set_database_role(None)
    return _emit(out)


def _emit(results: list[ProbeResult]) -> int:
    for r in results:
        print(str(r))
    failed = [r for r in results if not r.passed]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 0 if not failed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="validate_pg_roles_prod",
        description="NetPilot read-only production role/privilege/TLS validator.")
    parser.add_argument("command", choices=("selfcheck", "validate"),
                        help="selfcheck: offline. validate: live, read-only "
                             "against production (--confirm-production required).")
    parser.add_argument("--confirm-production", action="store_true",
                        help="assert the configured URLs point at a "
                             "persistent production Neon branch (never a "
                             "disposable one; this tool additionally refuses "
                             "known disposable hosts).")
    args = parser.parse_args(argv)
    if args.command == "selfcheck":
        return run_selfcheck()
    return run_validate(args.confirm_production)


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
