"""Stage A6b-1 M5 - run an approved M3 role SQL file as a specific role.

Path B of the M5 provisioning plan (``docs/NetPilot_M5_Neon_Disposable_Setup_
Operator_Guide.md``): because the local host has no ``psql`` client, the least-
privilege GRANT/ownership scripts under ``database/sql/roles/`` are executed
through the project's own psycopg2 driver instead. This tool is the *only*
approved way to run those files against a live PostgreSQL/Neon branch.

Security contract (deliberately strict)
---------------------------------------
* **DSN from the environment only.** The connection string is read from a
  process-scoped environment variable whose *name* is passed with ``--dsn-env``.
  The value is never a command-line argument, so it can never appear in the
  process command line, shell history, or an invocation log. There is no
  ``--dsn``/``--password`` option at all.
* **Never prints a secret.** No DSN, host, user, password or SQL parameter is
  ever echoed. Errors pass through :func:`_redact`, which scrubs ``scheme://
  user:pass@`` userinfo and ``password=``/``pass=`` pairs before anything is
  printed.
* **Allowlisted files only.** Refuses anything that is not one of the seven
  approved scripts located directly under ``database/sql/roles/``. The path is
  resolved first, so ``..`` traversal and unrelated files are rejected.
* **TLS required, never downgraded.** A ``postgresql`` connection string whose
  ``sslmode`` is weaker than ``require`` is upgraded to ``require``; a stronger
  operator value is left authoritative. ``verify-ca``/``verify-full`` without a
  CA path fail closed - exactly matching ``database.connection``.
* **Stop on the first error.** Each file runs as a single psycopg2 simple
  query in autocommit; the first statement that errors raises and the process
  exits non-zero. Nothing is silently retried or continued past.

Exit codes: ``0`` success, ``1`` execution error, ``2`` configuration or
validation refusal (missing/invalid DSN, disallowed file, TLS refusal).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote

from sqlalchemy.engine import make_url

_REPO = Path(__file__).resolve().parents[1]
_ROLES_DIR = (_REPO / "database" / "sql" / "roles").resolve()

#: The only files this runner will execute (the M3 provisioning scripts).
APPROVED_SQL_FILES = {
    "10_create_roles.sql",
    "20_ownership.sql",
    "21_schema_privileges.sql",
    "30_grant_collector.sql",
    "31_grant_app.sql",
    "32_grant_retention_rollup.sql",
    "40_default_privileges.sql",
}

#: libpq sslmode strength, weakest -> strongest (mirrors database.connection).
_SSLMODE_RANK = {
    "disable": 0, "allow": 1, "prefer": 2,
    "require": 3, "verify-ca": 4, "verify-full": 5,
}
_VERIFY_SSLMODES = ("verify-ca", "verify-full")

#: The PostgreSQL default port, used whenever a connection string carries no
#: explicit (positive) port. .NET ``[Uri].Port`` and an omitted libpq port both
#: surface a non-positive value, which otherwise yields
#: ``invalid port number: -1`` on connect.
DEFAULT_PG_PORT = 5432

_USERINFO_RE = re.compile(r"://[^/@]*@", re.IGNORECASE)
_PASSWORD_KW_RE = re.compile(r"(?i)\b(password|pass)\b\s*=\s*\S+")


class RoleRunError(RuntimeError):
    """A credential-safe refusal or validation failure.

    The message names only file paths, environment-variable *names*, or a
    setting label - never a connection string, host, user or password.
    """


# ------------------------------------------------------------------- redaction
def _redact(message: str) -> str:
    """Scrub userinfo and password keyword pairs before anything is printed."""
    message = _USERINFO_RE.sub("://***@", message)
    message = _PASSWORD_KW_RE.sub(r"\1=***", message)
    return message


def _safe_error(exc: BaseException) -> str:
    """A one-line, credential-safe description of ``exc``."""
    return f"{type(exc).__name__}: {_redact(str(exc))[:160]}"


# --------------------------------------------------------------- path allowlist
def resolve_approved_sql(sql_file: str) -> Path:
    """Return the resolved path of an approved script or raise RoleRunError."""
    candidate = Path(sql_file)
    if not candidate.is_absolute():
        candidate = _REPO / candidate
    resolved = candidate.resolve()
    if resolved.parent != _ROLES_DIR or resolved.name not in APPROVED_SQL_FILES:
        raise RoleRunError(
            "refusing to run: only the approved M3 scripts located directly "
            "under database/sql/roles/ are allowed. Requested file must be "
            "named exactly one of: " + ", ".join(sorted(APPROVED_SQL_FILES))
            + ".")
    return resolved


# ------------------------------------------------------------------ DSN reading
def read_dsn(dsn_env: str, environ=None) -> str:
    """Read the connection string from the named process env var.

    Only presence is inspected; the value is returned for use, never printed.
    """
    environ = os.environ if environ is None else environ
    dsn = (environ.get(dsn_env) or "").strip()
    if not dsn:
        raise RoleRunError(
            f"the connection string is not configured. Set the process-scoped "
            f"environment variable {dsn_env!r} (its value is never read into "
            "output, logs, or command history).")
    return dsn


# ------------------------------------------------------------------ TLS policy
def build_tls_kwargs(dsn: str) -> dict:
    """Validate the DSN and return psycopg2 kwargs enforcing TLS.

    Parsing uses SQLAlchemy's ``make_url`` (always installed, and it opens no
    connection), so this path runs offline. Never renders the DSN. Fails closed
    on a non-PostgreSQL backend, an invalid ``sslmode``, or a verify-mode with
    no CA path.
    """
    try:
        url = make_url(dsn)
    except Exception:  # noqa: BLE001 - parse failure is surfaced safely
        raise RoleRunError("the connection string could not be parsed.") from None
    if url.get_backend_name() != "postgresql":
        raise RoleRunError(
            "only PostgreSQL connection strings are supported here, and always "
            "over TLS. Pass a postgresql:// (or postgresql+psycopg2://) string.")

    query = dict(url.query or {})

    def _q(name: str) -> str:
        v = query.get(name, "")
        if isinstance(v, (list, tuple)):
            v = v[0] if v else ""
        return (v or "").strip()

    mode = _q("sslmode").lower()
    if mode and mode not in _SSLMODE_RANK:
        raise RoleRunError(
            "the connection string carries an invalid sslmode. TLS is "
            "mandatory: use sslmode=require (or stronger).")

    kwargs: dict = {}
    effective = mode if (mode and _SSLMODE_RANK[mode] >= _SSLMODE_RANK["require"]) \
        else "require"
    if effective != mode:
        # Absent or weaker than require -> enforce TLS by upgrading. A stronger
        # operator value is left authoritative (no sslmode kwarg is emitted).
        kwargs["sslmode"] = "require"
    if effective in _VERIFY_SSLMODES and not _q("sslrootcert"):
        raise RoleRunError(
            f"sslmode '{effective}' requires a CA certificate (sslrootcert); "
            "none is present in the connection string.")
    if not _q("connect_timeout"):
        kwargs["connect_timeout"] = "10"
    return kwargs


def _default_connect(dsn: str, **kwargs):
    """Open a real psycopg2 connection (lazy import - Stage 2 only).

    psycopg2 is a runtime prerequisite for connecting to Neon and is imported
    only here, so the module still imports and its offline tests still run on a
    host without psycopg2 installed. A test injects a fake ``connect``.
    """
    import psycopg2

    return psycopg2.connect(dsn, **kwargs)


# ------------------------------------------------------- DSN target assembly
def default_pg_port(port) -> int:
    """Return ``port`` when it is a positive int, else ``DEFAULT_PG_PORT``.

    Handles the absent (``None``), ``-1`` (``[Uri].Port`` on a non-HTTP scheme)
    and ``0`` cases uniformly, and rejects a non-numeric value.
    """
    try:
        value = int(port)
    except (TypeError, ValueError):
        return DEFAULT_PG_PORT
    return value if value > 0 else DEFAULT_PG_PORT


def parse_pg_target(dsn: str) -> dict:
    """Extract ``{host, port, database}`` from a Postgres DSN (TLS-safe, no
    credential is rendered). The port is normalised via :func:`default_pg_port`.
    """
    try:
        url = make_url(dsn)
    except Exception:  # noqa: BLE001 - parse failure is surfaced safely
        raise RoleRunError("the connection string could not be parsed.") from None
    return {"host": url.host or "", "port": default_pg_port(url.port),
            "database": url.database or ""}


def build_role_dsn(template_dsn: str, user: str, password: str) -> str:
    """Build a TLS-required ``postgresql://`` DSN for ``user`` on the same host,
    port and database as ``template_dsn`` (typically the verified owner string).

    The password is URL-encoded so special characters cannot corrupt the DSN.
    The returned string is a credential and must never be printed or logged.
    """
    target = parse_pg_target(template_dsn)
    if not target["host"] or not target["database"]:
        raise RoleRunError(
            "the connection string lacks a host or database name.")
    return (
        f"postgresql://{quote(user, safe='')}:"
        f"{quote(password, safe='')}@{target['host']}:{target['port']}"
        f"/{target['database']}?sslmode=require")


# --------------------------------------------------------------------- runner
def run(dsn_env: str, sql_file: str, environ=None, connect=None) -> int:
    """Execute one approved SQL file as the role named by the DSN env var."""
    environ = os.environ if environ is None else environ
    connect = connect or _default_connect
    try:
        path = resolve_approved_sql(sql_file)
        dsn = read_dsn(dsn_env, environ)
        kwargs = build_tls_kwargs(dsn)
        sql_text = path.read_text(encoding="utf-8")
        conn = connect(dsn, **kwargs)
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(sql_text)
        finally:
            conn.close()
    except RoleRunError as exc:
        print(str(exc))
        return 2
    except Exception as exc:  # noqa: BLE001 - report safely, stop on first error
        print(_safe_error(exc))
        return 1
    print(f"OK: executed {path.name}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_sql_as_role",
        allow_abbrev=False,
        description="Execute one approved M3 role SQL file through psycopg2, "
                    "reading the connection string only from an environment "
                    "variable. Never pass a DSN or password as an argument.")
    parser.add_argument(
        "--dsn-env", required=True, metavar="VAR",
        help="NAME of the process-scoped environment variable holding the "
             "direct PostgreSQL connection string (the value is never read "
             "into output).")
    parser.add_argument(
        "--file", required=True, metavar="PATH",
        help="An approved SQL file under database/sql/roles/ "
             "(see the tool's allowlist).")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run(args.dsn_env, args.file)


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
