"""Database connection management (replaceable storage layer).

The project uses SQLAlchemy 2.0 so the same code runs against:
  * SQLite     - default, zero-config local development
  * PostgreSQL - set DATABASE_URL=postgresql+psycopg2://user:pass@host/db

The connection URL comes only from configuration/environment variables.
"""
from __future__ import annotations

import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.schema import CreateColumn

from config import get_settings, demo_isolation_violation
from database import roles
from database.models import Base

_lock = threading.Lock()
# Separate from ``_lock`` (which guards engine creation) so that holding it
# while running DDL can never deadlock against ``get_engine()``.
_init_lock = threading.Lock()
_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None
_engine_url: str | None = None

# ---- Stage A6b-1 M4: runtime database role integration --------------------
#: The logical database role this process connects as (see ``database.roles``).
#: ``None`` means no role was declared, in which case the process defaults to
#: the least-privilege application reader - never the administrator. Set once at
#: startup by each entry point (Streamlit bootstrap, ``tools/run_*.py``).
_db_role: str | None = None


def set_database_role(role: str | None) -> None:
    """Declare which logical role this process uses for all DB access.

    Passing ``None`` clears the declaration (restores the least-privilege
    application default). The value is validated so a typo fails fast and
    credential-safe. Runners set/restore it around their ``main`` so a role
    never leaks into another process sharing the interpreter in tests.
    """
    global _db_role
    if role is not None:
        roles.validate_role(role)
    _db_role = role


def current_role() -> str | None:
    """The explicitly declared role, or ``None`` when none was set."""
    return _db_role


def active_role() -> str:
    """The role connections are resolved for (declared role or app default)."""
    return _db_role if _db_role is not None else roles.ROLE_APP


def _resolve_engine_url() -> str:
    """Resolve the connection URL for this process's role (M4).

    Demo mode always yields its isolated SQLite store; a live process yields
    its own ``NETPILOT_DB_URL_*`` when role separation is adopted, and only
    falls back to ``DATABASE_URL`` when separation is inactive (legacy/local).
    It never falls back to another (or the administrator) role's URL. A missing
    required role URL raises a credential-safe :class:`roles.RoleConfigError`
    before any socket is opened.
    """
    return roles.resolve_role_url(active_role(), settings=get_settings())


def _runtime_role_locked() -> bool:
    """True when separation is on and this process runs a runtime role.

    A runtime role holds only DML (retention additionally DELETEs just
    ``metric_samples``); it has no schema DDL and must not attempt admin-only
    destructive actions. Demo mode is exempt (its SQLite sandbox is not
    governed by these grants)."""
    settings = get_settings()
    if settings.demo_mode:
        return False
    return (roles.role_separation_enabled()
            and active_role() in roles.RUNTIME_ROLES)


def schema_management_allowed() -> bool:
    """Whether this process may run DDL (``init_db``) at all.

    ``NETPILOT_MANAGE_SCHEMA=false`` disables it (preserved exactly). In
    addition, once role separation is adopted a runtime role is never allowed to
    run DDL even if ``manage_schema`` were left true, because it holds no CREATE
    grant and attempting it would only produce a confusing permission error.
    Demo mode may always manage its own isolated schema.
    """
    settings = get_settings()
    if not settings.manage_schema:
        return False
    if settings.demo_mode:
        return True
    return not _runtime_role_locked()


def admin_actions_available() -> bool:
    """Whether admin-only destructive UI actions (reset/purge, DDL) are allowed.

    Disabled for a runtime role under separation (notably ``netpilot_app``,
    which has no DELETE or schema privileges). Demo mode keeps them available
    because the demo store is a disposable sandbox.
    """
    if get_settings().demo_mode:
        return True
    return not _runtime_role_locked()


def app_collection_allowed() -> bool:
    """Whether the in-app collector may write collection rows right now.

    The hosted Streamlit app runs as the SELECT-only ``netpilot_app`` role once
    role separation is enabled (see :mod:`database.roles`). Starting the in-app
    collector there can only fail with ``InsufficientPrivilege`` on
    ``collection_runs``, so the UI must not offer it. Demo mode keeps its
    disposable SQLite sandbox writable; a legacy single-connection deployment
    (role separation inactive) preserves the previous behaviour.
    """
    if get_settings().demo_mode:
        return True
    return not (roles.role_separation_enabled()
                and active_role() == roles.ROLE_APP)


# ---- Stage A6b-1 M1: PostgreSQL TLS groundwork ----------------------------
#: libpq sslmode values accepted by psycopg2, ordered weakest -> strongest.
#: The rank lets the engine builder refuse to *downgrade* an operator-supplied
#: sslmode that is already stronger than the enforced default.
_SSLMODE_RANK = {
    "disable": 0,
    "allow": 1,
    "prefer": 2,
    "require": 3,
    "verify-ca": 4,
    "verify-full": 5,
}
#: Modes that only make sense with an explicit CA certificate.
_VERIFY_SSLMODES = ("verify-ca", "verify-full")


def _resolve_pg_ssl_args(url: str, settings=None) -> dict:
    """Build the psycopg2 TLS/connect ``connect_args`` for a PostgreSQL URL.

    Configuration comes from ``NETPILOT_PG_SSLMODE`` (default ``require``),
    ``NETPILOT_PG_CONNECT_TIMEOUT`` (default ``10`` seconds) and the optional
    ``NETPILOT_PG_SSLROOTCERT`` CA path for ``verify-ca`` / ``verify-full``.

    Non-negotiable properties:

    * **Never downgrade the operator.** If ``DATABASE_URL`` already carries an
      ``sslmode`` at least as strong as the configured one, it stays authoritative
      and no ``sslmode`` key is emitted (psycopg2 would otherwise let the kwarg
      override the DSN). Equal strength also leaves the DSN untouched.
    * **Fail closed, credential-safe.** An unknown sslmode, a non-positive or
      non-numeric timeout, or a verify-mode without a CA path raise ``ValueError``
      whose message quotes only the offending setting name/value - never the URL,
      host or any credential material.
    * **SQLite untouched.** This runs only for ``postgresql*`` URLs (see
      ``_build_engine``).
    """
    settings = settings or get_settings()

    mode = (settings.pg_sslmode or "require").strip().lower()
    if mode not in _SSLMODE_RANK:
        raise ValueError(
            "NETPILOT_PG_SSLMODE is invalid: " + repr(mode) + ". Use one of: "
            + ", ".join(sorted(_SSLMODE_RANK)) + ".")

    raw_timeout = str(settings.pg_connect_timeout or "").strip()
    try:
        connect_timeout = int(raw_timeout)
    except ValueError:
        raise ValueError(
            "NETPILOT_PG_CONNECT_TIMEOUT must be a positive integer of seconds; "
            "got " + repr(raw_timeout) + ".") from None
    if connect_timeout <= 0:
        raise ValueError(
            "NETPILOT_PG_CONNECT_TIMEOUT must be a positive integer of seconds; "
            "got " + repr(connect_timeout) + ".")

    rootcert = (settings.pg_sslrootcert or "").strip()

    # Read only the DSN's *query parameters* so an operator who already encoded
    # sslmode / sslrootcert / connect_timeout in DATABASE_URL is respected. The
    # URL object is never rendered into a message, so no credential leaks here.
    try:
        query = {str(k).lower(): str(v) for k, v in make_url(url).query.items()}
    except Exception:  # noqa: BLE001 - deliberate fail-open; a URL that cannot
        # be parsed here is surfaced (safely, redacted) by create_engine below.
        query = {}

    args: dict = {}

    dsn_mode = (query.get("sslmode") or "").strip().lower()
    if dsn_mode and dsn_mode not in _SSLMODE_RANK:
        raise ValueError(
            "DATABASE_URL carries an invalid sslmode. Use one of: "
            + ", ".join(sorted(_SSLMODE_RANK)) + ".")
    if dsn_mode and _SSLMODE_RANK[dsn_mode] >= _SSLMODE_RANK[mode]:
        effective = dsn_mode            # operator's value stands; emit nothing
    else:
        effective = mode
        args["sslmode"] = mode          # enforce default or upgrade a weaker one

    dsn_rootcert = (query.get("sslrootcert") or "").strip()
    if effective in _VERIFY_SSLMODES and not (dsn_rootcert or rootcert):
        raise ValueError(
            "sslmode '" + effective + "' requires a CA certificate: set "
            "NETPILOT_PG_SSLROOTCERT to a valid CA file path (or add "
            "?sslrootcert=/path/to/root.crt to DATABASE_URL).")
    if rootcert and not dsn_rootcert and effective in _VERIFY_SSLMODES:
        args["sslrootcert"] = rootcert

    if not (query.get("connect_timeout") or "").strip():
        args["connect_timeout"] = connect_timeout

    return args


def _pg_int(name: str, raw, *, minimum: int) -> int:
    """Parse one PostgreSQL pool/timeout setting as a validated integer.

    ``name`` is the environment variable (never a value that could carry a
    credential). A non-integer or an out-of-range value raises ``ValueError``
    quoting only the setting name and the offending raw text. ``minimum`` is the
    inclusive lower bound (``0`` where disabling the knob is legitimate).
    """
    text = str(raw if raw is not None else "").strip()
    try:
        value = int(text)
    except ValueError:
        raise ValueError(
            name + " must be an integer; got " + repr(text) + ".") from None
    if value < minimum:
        raise ValueError(
            name + " must be an integer >= " + str(minimum) + "; got "
            + repr(value) + ".")
    return value


def _resolve_pg_pool_args(url: str, settings=None) -> tuple[dict, dict]:
    """Build the PostgreSQL-only pool kwargs and statement-timeout connect args.

    Returns ``(engine_kwargs, connect_args_additions)``. Engine-level pool
    tuning (``pool_size``/``max_overflow``/``pool_recycle``/``pool_timeout``)
    cannot go in ``connect_args`` - they configure SQLAlchemy's ``QueuePool`` -
    so they are returned separately for ``_build_engine`` to merge into the
    ``create_engine`` kwargs. ``pool_pre_ping`` stays ``True`` (already set in
    ``_build_engine``); these settings complement it for a serverless backend.

    ``statement_timeout`` is applied per connection through psycopg2's
    ``options`` (``-c statement_timeout=<ms>``), the documented mechanism that
    needs no server-side role/session setup. A value of ``0`` disables it, so a
    legitimate long forecast/rollup write is never cut off unless the operator
    asks. It is deliberately NOT assumed to require PgBouncer or change
    prepared-statement behaviour - those are out of scope for M2.

    Like M1, this respects the operator: if ``DATABASE_URL`` already carries an
    ``options`` value mentioning ``statement_timeout``, nothing is injected (a
    psycopg2 kwarg would otherwise override the DSN). Every validation error is
    credential-safe and raised before any connection is opened.
    """
    settings = settings or get_settings()

    pool_kwargs = {
        "pool_size": _pg_int("NETPILOT_PG_POOL_SIZE", settings.pg_pool_size,
                             minimum=1),
        "max_overflow": _pg_int("NETPILOT_PG_MAX_OVERFLOW",
                                settings.pg_max_overflow, minimum=0),
        "pool_recycle": _pg_int("NETPILOT_PG_POOL_RECYCLE",
                                settings.pg_pool_recycle, minimum=1),
        "pool_timeout": _pg_int("NETPILOT_PG_POOL_TIMEOUT",
                                settings.pg_pool_timeout, minimum=1),
    }
    statement_timeout_ms = _pg_int("NETPILOT_PG_STATEMENT_TIMEOUT_MS",
                                   settings.pg_statement_timeout_ms, minimum=0)

    additions: dict = {}
    if statement_timeout_ms > 0:
        try:
            dsn_options = str(make_url(url).query.get("options") or "")
        except Exception:  # noqa: BLE001 - unparsable URL fails in create_engine
            dsn_options = ""
        if "statement_timeout" not in dsn_options:
            additions["options"] = f"-c statement_timeout={statement_timeout_ms}"

    return pool_kwargs, additions


def _build_engine(url: str) -> Engine:
    kwargs: dict = {"future": True, "pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 15}
        # Make sure the parent directory of a file-based SQLite DB exists.
        path_part = url.split("sqlite:///")[-1]
        if path_part and not path_part.startswith(":memory:"):
            Path(path_part).parent.mkdir(parents=True, exist_ok=True)
    elif url.startswith("postgres"):
        # Normalise PostgreSQL scheme → explicit ``postgresql+psycopg2://`` so
        # SQLAlchemy always loads the psycopg2 dialect (our only installed
        # driver on Streamlit Cloud). Handles:
        #   - bare ``postgresql://`` (Neon CLI output)
        #   - ``postgresql+psycopg://`` (wrong psycopg3 suffix from paste)
        # Leaves ``postgresql+psycopg2://`` and other intentional drivers
        # (e.g. ``+asyncpg``) untouched.
        if url.startswith("postgresql://"):
            url = "postgresql+psycopg2://" + url[len("postgresql://"):]
        elif url.startswith("postgresql+psycopg://"):
            url = "postgresql+psycopg2://" + url[len("postgresql+psycopg://"):]
        # Stage A6b-1 M1: TLS is mandatory for managed/Neon PostgreSQL. The
        # helper validates the configuration and refuses to downgrade an
        # operator-supplied stronger sslmode; bad config raises a credential-
        # safe ValueError before any connection attempt.
        connect_args = _resolve_pg_ssl_args(url)
        # Stage A6b-1 M2: serverless pool tuning + statement timeout, PostgreSQL
        # only. Both resolvers run before create_engine, so an invalid value
        # fails with a clear message and no socket is ever opened.
        pool_kwargs, stmt_additions = _resolve_pg_pool_args(url)
        connect_args.update(stmt_additions)
        kwargs.update(pool_kwargs)
        kwargs["connect_args"] = connect_args

    engine = create_engine(url, **kwargs)

    if url.startswith("sqlite"):
        # SQLite uses a single writer; WAL improves concurrent read/write
        # behaviour for the background collector + Streamlit sessions.
        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, _record):  # pragma: no cover - glue
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    return engine


def get_engine() -> Engine:
    global _engine, _session_factory, _engine_url
    url = _resolve_engine_url()  # role-aware (M4); demo stays on isolated SQLite
    with _lock:
        if _engine is None or _engine_url != url:
            if _engine is not None:
                _engine.dispose()
            _engine = _build_engine(url)
            _session_factory = sessionmaker(bind=_engine, future=True)
            _engine_url = url
        assert _engine is not None
        return _engine


def get_session() -> Session:
    """Return a new ORM session (caller is responsible for closing)."""
    get_engine()  # ensures the factory matches the currently configured URL
    factory = _session_factory
    assert factory is not None
    return factory()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commits on success, rolls back on error."""
    session = get_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _ensure_additive_columns(engine: Engine) -> None:
    """Add nullable model columns that an older database does not have yet.

    ``create_all`` only creates missing *tables*; it never alters an existing
    table. NetPilot's migration strategy is strictly additive, so this helper
    adds only missing **nullable** columns with ``ALTER TABLE ... ADD COLUMN``.
    It never drops or rewrites a column and refuses to guess at a NOT NULL
    column, which would need an explicit data migration.
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    preparer = engine.dialect.identifier_preparer
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            existing_columns = {
                col["name"] for col in inspector.get_columns(table.name)
            }
            for column in table.columns:
                if column.name in existing_columns:
                    continue
                if not column.nullable:
                    raise RuntimeError(
                        f"additive migration refused: {table.name}."
                        f"{column.name} is NOT NULL and needs a data migration")
                ddl = str(CreateColumn(column).compile(dialect=engine.dialect))
                conn.execute(text(
                    f"ALTER TABLE {preparer.quote(table.name)} "
                    f"ADD COLUMN {ddl}"))


def init_db() -> None:
    """Create all tables if they do not exist (safe to call repeatedly).

    Before touching the engine, the demo/live isolation rule is enforced so a
    misconfigured demo run can never open a connection to - or create schema in
    - a shared external database. This is the deepest guard: every entry point
    that initializes the database (app bootstrap, seeding, the standalone
    ``init-schema`` command) passes through here.

    ``MetaData.create_all`` performs a *check-then-create*: it asks the dialect
    whether each table exists and only then issues ``CREATE TABLE``. That is
    not atomic, and Streamlit Community Cloud starts several script runs (page
    loads, reruns and the prerun) in the same process at boot, so two runs can
    both observe a missing ``devices`` table and both emit ``CREATE TABLE
    devices``. The loser raises ``OperationalError: table devices already
    exists`` and the page renders a red error box.

    Two guards make this safe on every backend:

    * a process lock, so concurrent callers in this process are serialised;
    * a short bounded retry, so a writer in *another* process - or one that
      committed between our check and our ``CREATE`` - is tolerated. Once the
      schema exists, ``checkfirst`` turns the call into a cheap no-op, so the
      retry is idempotent.

    Genuine failures (bad credentials, unwritable path, ...) still propagate.
    """
    violation = demo_isolation_violation(get_settings())
    if violation:
        raise RuntimeError(violation)
    engine = get_engine()  # acquires ``_lock``; never nested with the below
    with _init_lock:
        for attempt in range(5):
            try:
                Base.metadata.create_all(engine)
                _ensure_additive_columns(engine)
                return
            except OperationalError as exc:
                message = str(exc).lower()
                raced = ("already exists" in message
                         or "duplicate column" in message)
                if attempt == 4 or not raced:
                    raise
                time.sleep(0.1 * (attempt + 1))


def check_connection() -> tuple[bool, str]:
    """Lightweight connectivity probe used by the System Status page."""
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, get_engine().url.render_as_string(hide_password=True)
    except Exception as exc:  # pragma: no cover - depends on environment
        # The failure text reaches the token-less /health response and the
        # UI. Several DBAPIs echo the full DSN (``user:password@host``,
        # ``password=...``) inside their exception strings, so scrub any
        # credential-shaped substring before returning the detail.
        return False, _redact_credentials(f"{type(exc).__name__}: {exc}")


_URL_USERINFO = re.compile(r"://[^/@\s]*@")
_SECRET_KV = re.compile(
    r"(?i)\b(password|passwd|pwd|token|api[_-]?key)(\s*[=:]\s*)[^\s,;]+")


def _redact_credentials(message: str) -> str:
    """Strip credential-shaped fragments from a driver error message.

    ``scheme://user:pass@host`` userinfo runs and ``password=value``-style
    pairs are replaced with ``***``; everything else (error class, host,
    diagnostic text) is kept so the status pages stay useful.
    """
    message = _URL_USERINFO.sub("://***@", message)
    return _SECRET_KV.sub(r"\1\2***", message)


#: Tables that are configuration, identity or audit evidence rather than
#: disposable telemetry. A "reset lab data" action must never erase user
#: accounts, alert rules, change approvals/audit history, or immutable digital
#: twin bindings - those are not measurements and cannot be regenerated.
_RESET_PRESERVED_TABLES = frozenset({
    "users",
    "alert_rules",
    "change_requests",
    "change_audit_events",
    "digital_twin_snapshot_bindings",
})


def reset_lab_data() -> int:
    """Delete disposable lab/measurement rows, preserving identity and audit.

    Returns the number of tables cleared. This only ever touches NetPilot's own
    database (SQLite file or the configured PostgreSQL schema) - never a
    production system - and it deliberately leaves users, alert rules, change
    requests/audit events and digital-twin bindings intact.
    """
    engine = get_engine()
    cleared = 0
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            if table.name in _RESET_PRESERVED_TABLES:
                continue
            conn.execute(table.delete())
            cleared += 1
    return cleared


def missing_schema_objects() -> list[str]:
    """Read-only check for tables the models expect but the DB does not have.

    Uses catalog reflection only (``SELECT`` on the system schema), so a
    SELECT-only reader role can run it. It never issues DDL. Returns a list of
    human-readable descriptions; empty means the schema is present.
    """
    engine = get_engine()
    present = set(inspect(engine).get_table_names())
    missing = sorted(set(Base.metadata.tables) - present)
    return [f"table {name}" for name in missing]


def _redacted_target_block() -> str:
    """Build a credential-safe diagnostic block for schema failures.

    Issues read-only SELECTs on the already-open engine to surface server-side
    identity (database, user, search_path) and table counts per schema. Never
    exposes a password, connection string, or userinfo fragment.
    """
    from sqlalchemy import text as _txt
    from tools.diagnose_db_target import parse_target

    try:
        engine = get_engine()
        url_str = str(engine.url)  # includes password; only parse, never print
        target = parse_target(url_str)
    except Exception:
        return "[target info unavailable]"

    lines: list[str] = []
    lines.append(f"  Target: {target.redacted()}")

    try:
        with engine.connect() as conn:
            conn.execute(_txt("SET TRANSACTION READ ONLY"))
            db_name = conn.execute(_txt("SELECT current_database()")).scalar()
            user_name = conn.execute(_txt("SELECT current_user")).scalar()
            sp = conn.execute(
                _txt("SELECT current_setting('search_path')")).scalar()
            rows = conn.execute(_txt(
                "SELECT nspname, count(c.relname) "
                "FROM pg_namespace n "
                "LEFT JOIN pg_class c "
                "ON c.relnamespace = n.oid AND c.relkind = 'r' "
                "WHERE nspname NOT IN ('pg_catalog','information_schema') "
                "GROUP BY nspname ORDER BY nspname"
            )).fetchall()
            conn.rollback()
    except Exception as exc:
        lines.append(f"  Server query failed (permissions?): {type(exc).__name__}")
        return "\n".join(lines)

    lines.append(f"  current_database(): {db_name}")
    lines.append(f"  current_user: {user_name}")
    lines.append(f"  search_path: {sp}")
    schemas = ", ".join(f"{r[0]}({r[1]} tbl)" for r in rows) or "(none)"
    lines.append(f"  Schemas: {schemas}")
    return "\n".join(lines)


def require_schema() -> None:
    """Verify the schema is ready without creating or migrating anything.

    This is the ``NETPILOT_MANAGE_SCHEMA=false`` path for a hosted read-only
    app: it connects to reflect the catalog (SELECT only) and refuses to start
    with an actionable operator instruction if tables are missing, instead of
    silently attempting DDL the reader role is not granted. The demo/live
    isolation guard runs first so even this read path never connects to a shared
    database from a demo configuration.

    On failure, appends a credential-safe diagnostic block so the operator can
    compare the target fingerprint with the known-good local WCM value.
    """
    violation = demo_isolation_violation(get_settings())
    if violation:
        raise RuntimeError(violation)
    try:
        missing = missing_schema_objects()
    except Exception as exc:
        # Connection-level failure (auth, DNS, timeout): still show target
        # info so the operator can verify the endpoint matches.
        target_info = _redacted_target_block()
        raise RuntimeError(
            f"require_schema() could not reach the database: "
            f"{type(exc).__name__}\n\n"
            "--- DB Target Diagnostics (credential-safe) ---\n"
            f"{target_info}\n"
            "Check the NETPILOT_DB_URL_APP value in Cloud Secrets. The "
            "fingerprint must match: `python tools/diagnose_db_target.py "
            "--from-wcm`\non the operator workstation.") from exc
    if missing:
        detail = _redacted_target_block()
        raise RuntimeError(
            "NETPILOT_MANAGE_SCHEMA=false but the database schema is not ready "
            "(missing: " + ", ".join(missing) + "). A read-only application "
            "role cannot create it. Run the schema initialization command as "
            "the schema administrator - `python tools/run_collector.py "
            "init-schema` (needs DDL privileges) - then restart the reader.\n\n"
            "--- DB Target Diagnostics (credential-safe) ---\n"
            f"{detail}\n"
            "Compare the fingerprint above with the output of:\n"
            "  python tools/diagnose_db_target.py --from-wcm\n"
            "on the operator workstation.")
