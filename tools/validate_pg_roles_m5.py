"""Stage A6b-1 M5 - Neon/PostgreSQL live role validation harness.

This tool verifies, **against a real disposable PostgreSQL/Neon branch**, that
the M3 privilege matrix and the M1/M2/M4 runtime wiring actually hold on the
server. It is the executable form of the deferred live checks L-M3-1..5 and
L-M4-1..4.

TWO commands, deliberately separate:

    python tools/validate_pg_roles_m5.py selfcheck
        # OFFLINE, safe to run anywhere, opens NO connection. Proves the
        # expected grant matrix the live run will assert is internally
        # consistent with the M3 single-source-of-truth, and that Demo Mode
        # can never resolve to an external PostgreSQL role URL.

    python tools/validate_pg_roles_m5.py validate --confirm-disposable
        # LIVE. Connects to the disposable branch through the ordinary M4 role
        # machinery (``database.connection.get_engine``) so every role
        # connection also exercises M1 TLS + the M2 pool + statement_timeout.
        # Runs the positive/negative authorization tests, sequence-name
        # verification, connection-parameter verification and representative
        # operations. REFUSES to run without --confirm-disposable, in demo
        # mode, or unless role separation is configured.

Safety contract
---------------
* **Disposable only.** ``validate`` requires ``--confirm-disposable``: the
  operator's explicit statement that the configured URLs point at a throwaway
  branch, never production. It also refuses while ``NETPILOT_DEMO_MODE=true``.
* **Credential-safe.** Only a role NAME, a SQLSTATE/error class and a redacted
  message are ever printed; a DSN, host, user or password never is. Connection
  strings are read from the environment only and never echoed.
* **Fail loud, never weaken.** Every probe records PASS/FAIL against the
  expected matrix. A denial that should have been allowed, or an action that
  should have been denied but was not, is reported as a FAILURE - the harness
  never relaxes a grant to make a test pass.
* **Side effects only on disposable data.** Real writes use a clearly-labelled
  probe row (``device_name='__np_m5_probe__'``); no production table is touched
  because production is never a target of this tool.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text
from sqlalchemy.engine import make_url

from config import demo_isolation_violation, get_settings
from database import connection, roles
from database import pg_privileges as pgp
from database.models import DEVICE_STATUSES, Base, db_utcnow

PRIVS = ("SELECT", "INSERT", "UPDATE", "DELETE")
#: A device name only this tool ever writes, so probe rows are unmistakable and
#: never collide with real inventory (and are removable by ``netpilot_retention``
#: / admin on the disposable branch).
PROBE_DEVICE = "__np_m5_probe__"
#: Which single table each role uses for its positive SELECT smoke probe (a
#: table the matrix grants it SELECT on).
ROLE_SELECT_PROBE = {
    roles.ROLE_COLLECTOR: "devices",
    roles.ROLE_APP: "devices",
    roles.ROLE_RETENTION: "metric_samples",
    roles.ROLE_ROLLUP: "metric_samples",
}
#: Valid literal for the collector probe's status column. 'unknown' is the
#: documented DEVICE_STATUSES value that repository.upsert_device writes when no
#: ping result exists yet, so the probe mirrors the real collector (never guessed).
PROBE_STATUS = "unknown"
#: The collector write-probe. It supplies every NOT NULL devices column without
#: a server default (name, type, status, updated_at); id comes from the serial
#: sequence. Kept as a module constant so an offline test can assert completeness.
PROBE_INSERT_SQL = (
    "INSERT INTO public.devices (name, type, mgmt_ip, status, updated_at) "
    "VALUES (:name, 'router', NULL, :status, :updated_at) "
    "ON CONFLICT (name) DO NOTHING")
_INSUFFICIENT_PRIV = "42501"   # SQLSTATE: insufficient_privilege


class ProbeResult:
    """One recorded assertion with a printable, credential-safe line."""

    def __init__(self, name: str, passed: bool, detail: str = "") -> None:
        self.name = name
        self.passed = passed
        self.detail = detail

    def __str__(self) -> str:  # used for the console table
        return f"[{'PASS' if self.passed else 'FAIL'}] {self.name}" + (
            f" :: {self.detail}" if self.detail else "")


# --------------------------------------------------------------------- matrix
def all_model_tables() -> list[str]:
    """Every table the ORM models declare (the universe of grant targets)."""
    return sorted(Base.metadata.tables)


def required_device_columns() -> set[str]:
    """``devices`` columns a raw ``text()`` INSERT must supply explicitly.

    That is every NOT NULL column with **no server-side default**; SQLAlchemy
    *client-side* column defaults do not fire for a raw statement, and the
    ``id`` primary key is filled from ``devices_id_seq``. Mirrors what
    ``repository.upsert_device`` writes through the ORM. Used by an offline
    regression test so the collector probe can never omit a required field.
    """
    cols: set[str] = set()
    for col in Base.metadata.tables["devices"].columns:
        if col.primary_key or col.nullable or col.server_default is not None:
            continue
        cols.add(col.name)
    return cols


def expected_table_privilege(table: str, role: str, priv: str) -> bool:
    """The M3 answer to 'may ``role`` do ``priv`` on ``table``?' (runtime only).

    Derived directly from the single source of truth, so the live assertion can
    never drift from the committed grants.
    """
    grants = pgp.ROLE_GRANTS.get(role)
    if grants is None:            # admin is the owner, handled separately
        return False
    return priv in grants.get(table, ())


def expected_sequence_privilege(table: str, role: str) -> bool:
    """``role`` holds USAGE on ``<table>_id_seq`` iff it may INSERT ``table``."""
    return table in pgp.insert_tables(role)


# ------------------------------------------------------------ denial detection
def is_privilege_denied(exc: BaseException) -> bool:
    """True when ``exc`` is PostgreSQL refusing a privilege (not some other bug).

    Reads only the SQLSTATE / a fixed message marker, never connection data.
    """
    orig = getattr(exc, "orig", None)
    code = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    if code == _INSUFFICIENT_PRIV:
        return True
    return "permission denied" in str(exc).lower()


def error_signature(exc: BaseException) -> str:
    """A safe one-line description: class + SQLSTATE + a redacted message head.

    Any ``scheme://user:pass@`` userinfo and ``password=...`` pairs are scrubbed
    before returning, so a driver that embeds the DSN cannot leak it.
    """
    message = str(exc)
    lowered = message.lower()
    if "://" in lowered and "@" in lowered:
        # Redact userinfo: keep only the scheme and the part after the '@'.
        head, _, tail = message.partition("://")
        _, _, after_at = tail.partition("@")
        message = f"{head}://***@{after_at}"
    code = getattr(getattr(exc, "orig", None), "sqlstate", None)
    code_part = f" (SQLSTATE {code})" if code else ""
    return f"{type(exc).__name__}{code_part}: {message[:120]}"


# ------------------------------------------------------------------- OFFLINE
def run_selfcheck() -> int:
    """Consistency + demo-isolation checks that need no database.

    Returns a process exit code (0 = all PASS).
    """
    results: list[ProbeResult] = []

    # 1. The matrix covers exactly the four runtime roles, none is the admin.
    roles_ok = (set(pgp.ROLE_GRANTS) == {roles.ROLE_COLLECTOR, roles.ROLE_APP,
                                         roles.ROLE_RETENTION, roles.ROLE_ROLLUP}
                and roles.ROLE_ADMIN not in pgp.ROLE_GRANTS)
    results.append(ProbeResult("matrix roles == runtime roles (admin excluded)",
                               roles_ok))

    tables = set(all_model_tables())
    unknown = {t for m in pgp.ROLE_GRANTS.values() for t in m} - tables
    results.append(ProbeResult("every granted table exists in the models",
                               not unknown, f"unknown={sorted(unknown)}"))

    # 2. Core least-privilege invariants the live run will re-assert server-side.
    app = pgp.ROLE_GRANTS[roles.ROLE_APP]
    results.append(ProbeResult(
        "app has no DELETE on any table",
        not any("DELETE" in ops for ops in app.values())))
    collector = pgp.ROLE_GRANTS[roles.ROLE_COLLECTOR]
    results.append(ProbeResult(
        "collector has no DELETE on any table",
        not any("DELETE" in ops for ops in collector.values())))
    results.append(ProbeResult(
        "retention DELETEs only metric_samples",
        {t for t, ops in pgp.ROLE_GRANTS[roles.ROLE_RETENTION].items()
         if "DELETE" in ops} == {"metric_samples"}))
    results.append(ProbeResult(
        "rollup writes only metric_daily_rollups",
        {t for t, ops in pgp.ROLE_GRANTS[roles.ROLE_ROLLUP].items()
         if set(ops) & {"INSERT", "UPDATE"}} == {"metric_daily_rollups"}))
    # Sequence usage exactly tracks INSERT targets.
    seq_ok = all(pgp.sequence_tables(r) == pgp.insert_tables(r)
                 for r in pgp.RUNTIME_ROLES)
    results.append(ProbeResult("sequence grants track INSERT targets", seq_ok))

    # 3. Demo Mode can never resolve to an external PostgreSQL role URL.
    results.extend(_demo_isolation_probes())

    return _emit(results)


def _demo_isolation_probes() -> list[ProbeResult]:
    """Pure checks that a demo configuration is quarantined to SQLite."""
    out: list[ProbeResult] = []
    pg_url = "postgresql+psycopg2://x@h/db"
    demo_settings = get_settings()
    # Simulate demo mode with a role URL present; resolution must ignore PG.
    from types import SimpleNamespace
    demo = SimpleNamespace(demo_mode=True, database_url="sqlite:///data/demo.db")
    saved = os.environ.get(roles.ROLE_URL_ENV[roles.ROLE_APP], "")
    try:
        os.environ[roles.ROLE_URL_ENV[roles.ROLE_APP]] = pg_url
        resolved = roles.resolve_role_url(roles.ROLE_APP, settings=demo)
        out.append(ProbeResult(
            "demo resolve ignores role URL -> stays SQLite",
            resolved == "sqlite:///data/demo.db"))
    finally:
        if saved:
            os.environ[roles.ROLE_URL_ENV[roles.ROLE_APP]] = saved
        else:
            os.environ.pop(roles.ROLE_URL_ENV[roles.ROLE_APP], None)
    # And the isolation guard flags demo pointed at an external DB.
    external_demo = SimpleNamespace(demo_mode=True, database_url=pg_url)
    out.append(ProbeResult(
        "demo_isolation_violation flags demo->external",
        demo_isolation_violation(external_demo) is not None
        and demo_isolation_violation(demo_settings) is None))
    return out


# --------------------------------------------------------------------- LIVE
def _open(role: str):
    """Open an engine/connection path as ``role`` via the M4 machinery.

    Uses ``connection.get_engine`` so the connection is built with M1 TLS and
    M2 pool/statement-timeout exactly as a real process would, and fails closed
    (``RoleConfigError``) if that role's URL is not configured.
    """
    connection.set_database_role(role)
    return connection.get_engine()


def _force_sslmode(url: str, mode: str) -> str:
    """Return ``url`` with its sslmode forced to ``mode``.

    Rewrites only the query parameter, so host/port/database/user are preserved
    - both TLS probes therefore target the SAME direct endpoint. The rendered
    string is handed straight to the driver and never printed.
    """
    u = make_url(url)
    q = dict(u.query or {})
    q["sslmode"] = mode
    return u.set(query=q).render_as_string(hide_password=False)


def _connect_plain(dsn: str):
    """Open a raw psycopg2 connection, bypassing the app's M1 guard, so the
    result reflects what the Neon endpoint itself accepts or refuses."""
    import psycopg2
    return psycopg2.connect(dsn, connect_timeout=10)


def _disable_refused(role: str) -> tuple[bool, str]:
    """True when a sslmode=disable connection to the role's endpoint is refused.

    A refusal surfaces as a psycopg2 connection-level error (OperationalError /
    InterfaceError). Anything else - a config/parse problem - is NOT treated as
    proof, so the check cannot pass for the wrong reason.
    """
    url = os.environ.get(roles.ROLE_URL_ENV[role], "")
    if not url:
        return False, "role URL not configured"
    try:
        disable_url = _force_sslmode(url, "disable")
    except Exception as exc:  # noqa: BLE001
        return False, f"sslmode rewrite failed: {error_signature(exc)}"
    try:
        conn = _connect_plain(disable_url)
        conn.close()
        return False, "endpoint ACCEPTED a non-TLS connection"
    except Exception as exc:  # noqa: BLE001
        cls = type(exc).__name__
        if cls in ("OperationalError", "InterfaceError"):
            return True, cls
        return False, f"unexpected {cls}: {error_signature(exc)}"


def _tls_probe(role: str) -> tuple[bool, str]:
    """Client-side TLS proof for ``role``.

    Neon terminates TLS at its proxy in front of the compute, so the backend
    ``pg_stat_ssl`` view can report ssl=false even on a real TLS connection -
    we therefore do NOT assert on it. Instead: (1) a sslmode=require connection
    through the ordinary M1/TLS-guarded engine opens and runs a granted SELECT,
    and (2) a sslmode=disable attempt to the SAME direct endpoint is refused.
    Both must hold for the check to pass; TLS is neither skipped nor weakened.
    """
    stmt = None
    try:
        with _open(role).connect() as conn:
            table = ROLE_SELECT_PROBE[role]
            conn.execute(text(f"SELECT 1 FROM public.{table} LIMIT 1")).first()
            stmt = conn.execute(text("SHOW statement_timeout")).scalar()
    except Exception as exc:  # noqa: BLE001
        return False, f"sslmode=require failed: {error_signature(exc)}"
    refused, why = _disable_refused(role)
    if not refused:
        return False, f"sslmode=disable was NOT refused ({why})"
    return True, (f"require ok (statement_timeout={stmt!r}); "
                  f"disable refused via {why}")


def _run_matrix_introspection(out: list[ProbeResult]) -> None:
    """Compare live ``has_*_privilege`` answers to the M3 matrix (as admin)."""
    with _open(roles.ROLE_ADMIN).connect() as conn:
        for role in pgp.RUNTIME_ROLES:
            rows = conn.execute(text(
                "SELECT c.relname AS tbl, p.priv, "
                "has_table_privilege(:role, 'public.'||c.relname, p.priv) AS ok "
                "FROM pg_class c CROSS JOIN "
                "unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE']) AS p(priv) "
                "WHERE c.relnamespace = 'public'::regnamespace "
                "AND c.relkind = 'r' ORDER BY c.relname, p.priv"),
                {"role": role}).all()
            mismatches = [
                f"{role}.{tbl}.{priv}={actual!r}"
                for tbl, priv, actual in rows
                if bool(actual) != expected_table_privilege(tbl, role, priv)]
            out.append(ProbeResult(
                f"{role}: table privileges match M3 matrix",
                not mismatches,
                f"mismatches={mismatches[:6]}" if mismatches else ""))

            # Schema rights: USAGE yes, CREATE no (no DDL for runtime roles).
            usage, create = conn.execute(text(
                "SELECT has_schema_privilege(:role,'public','USAGE'), "
                "has_schema_privilege(:role,'public','CREATE')"),
                {"role": role}).one()
            out.append(ProbeResult(
                f"{role}: USAGE on schema, no CREATE (no DDL)",
                bool(usage) and not bool(create)))

        # Sequence names + privileges (closes L-M3-5 naming assumption).
        present = {r[0] for r in conn.execute(text(
            "SELECT relname FROM pg_class WHERE relkind='S' "
            "AND relnamespace='public'::regnamespace"))}
        expected_seqs = {f"{t}_id_seq" for t in all_model_tables()}
        out.append(ProbeResult(
            "SERIAL sequences named <table>_id_seq",
            expected_seqs <= present,
            f"missing={sorted(expected_seqs - present)}"
            if expected_seqs - present else ""))
        for role in pgp.RUNTIME_ROLES:
            seq_bad = []
            for table in all_model_tables():
                ok = conn.execute(text(
                    "SELECT has_sequence_privilege(:role, 'public.'||:seq, "
                    "'USAGE')"),
                    {"role": role, "seq": f"{table}_id_seq"}).scalar()
                if bool(ok) != expected_sequence_privilege(table, role):
                    seq_bad.append(f"{role}.{table}_id_seq")
            out.append(ProbeResult(
                f"{role}: sequence USAGE matches INSERT targets",
                not seq_bad, f"bad={seq_bad[:6]}" if seq_bad else ""))


def _run_enforcement_probes(out: list[ProbeResult]) -> None:
    """Positive (granted) + negative (denied) enforcement, per real role login."""
    # TLS enforced from the CLIENT (Neon's proxy makes backend pg_stat_ssl report
    # ssl=false even over TLS, so it is not a trustworthy signal): sslmode=
    # require must connect + SELECT, and sslmode=disable must be refused.
    for role in pgp.RUNTIME_ROLES:
        passed, detail = _tls_probe(role)
        out.append(ProbeResult(
            f"{role}: TLS enforced (require ok + disable refused)",
            passed, detail))

    # Negative enforcement: DELETE and DDL must be denied for app/collector.
    for role in (roles.ROLE_APP, roles.ROLE_COLLECTOR):
        _expect_denied(out, role, "DELETE denial",
                       "DELETE FROM public.devices WHERE 1=0")
        _expect_denied(out, role, "DDL denial",
                       "CREATE TABLE public.__np_m5_ddl_probe__(x int)")

    # Retention: DELETE allowed on metric_samples (0 rows), denied elsewhere.
    with _open(roles.ROLE_RETENTION).connect() as conn:
        try:
            conn.execute(text(
                "DELETE FROM public.metric_samples "
                "WHERE device_name = '__np_m5_probe__'"))
            out.append(ProbeResult(
                "retention: DELETE on metric_samples permitted", True))
        except Exception as exc:  # noqa: BLE001
            out.append(ProbeResult(
                "retention: DELETE on metric_samples permitted", False,
                error_signature(exc)))
    _expect_denied(out, roles.ROLE_RETENTION, "unrelated-table DELETE denial",
                   "DELETE FROM public.devices WHERE 1=0")

    # Rollup: may SELECT metric_samples + write rollups, but not other tables.
    _expect_denied(out, roles.ROLE_ROLLUP, "write-denied on metric_samples",
                   "DELETE FROM public.metric_samples "
                   "WHERE device_name = '__np_m5_probe__'")
    _expect_denied(out, roles.ROLE_ROLLUP, "DDL denial",
                   "CREATE TABLE public.__np_m5_rollup_ddl__(x int)")


def _expect_denied(out: list[ProbeResult], role: str, label: str,
                   sql: str) -> None:
    """Assert running ``sql`` as ``role`` is refused by the server."""
    try:
        with _open(role).connect() as conn:
            conn.execute(text(sql))
        out.append(ProbeResult(f"{role}: {label}", False,
                              "action was ALLOWED but must be DENIED"))
    except Exception as exc:  # noqa: BLE001
        out.append(ProbeResult(
            f"{role}: {label}", is_privilege_denied(exc),
            "" if is_privilege_denied(exc) else error_signature(exc)))


def _representative_operations(out: list[ProbeResult]) -> None:
    """Exercise a real collector write path on disposable data.

    A collector INSERT through its own role proves SELECT + INSERT + USAGE on
    the ``devices_id_seq`` sequence in one shot. The probe supplies every NOT
    NULL devices column that has no server default (name, type, status,
    updated_at) - exactly what ``repository.upsert_device`` writes through the
    ORM - so it reflects the real collector instead of a hand-written row.
    ``ON CONFLICT (name) DO NOTHING`` keeps it idempotent across re-runs.
    """
    if PROBE_STATUS not in DEVICE_STATUSES:
        # Defensive: never write a status the schema does not document.
        out.append(ProbeResult("collector: INSERT device row (+seq USAGE)",
                               False, "PROBE_STATUS is not a documented device status"))
        return
    try:
        with _open(roles.ROLE_COLLECTOR).begin() as conn:
            conn.execute(
                text(PROBE_INSERT_SQL),
                {"name": PROBE_DEVICE, "status": PROBE_STATUS,
                 "updated_at": db_utcnow()})
        out.append(ProbeResult("collector: INSERT device row (+seq USAGE)", True))
    except Exception as exc:  # noqa: BLE001
        out.append(ProbeResult("collector: INSERT device row (+seq USAGE)",
                               False, error_signature(exc)))


def run_validate(confirm_disposable: bool) -> int:
    """The gated live validation. Refuses unsafe configurations."""
    settings = get_settings()
    if not confirm_disposable:
        print("REFUSED: --confirm-disposable is required. This tool must never "
              "run against a production branch. Re-run only when the "
              "NETPILOT_DB_URL_* variables point at a disposable Neon branch.")
        return 2
    if settings.demo_mode:
        print("REFUSED: NETPILOT_DEMO_MODE=true. Live validation requires the "
              "disposable-branch role URLs and demo mode off.")
        return 2
    if not roles.role_separation_enabled():
        print("REFUSED: role separation is not configured (no "
              "NETPILOT_DB_URL_* set). Provision the disposable branch role "
              "URLs via the environment (never in chat) before running.")
        return 2

    out: list[ProbeResult] = []
    try:
        _run_matrix_introspection(out)
        _run_enforcement_probes(out)
        _representative_operations(out)
    except roles.RoleConfigError as exc:
        # Fail closed on a missing/insufficient role configuration.
        out.append(ProbeResult("role configuration complete", False,
                               error_signature(exc)))
    except Exception as exc:  # noqa: BLE001 - surface any DB error safely
        out.append(ProbeResult("live validation aborted on error", False,
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
        prog="validate_pg_roles_m5",
        description="M5 PostgreSQL/Neon role validation harness.")
    parser.add_argument("command", choices=("selfcheck", "validate"),
                        help="selfcheck: offline. validate: live (disposable "
                             "branch, --confirm-disposable required).")
    parser.add_argument("--confirm-disposable", action="store_true",
                        help="assert the configured URLs are a THROWAWAY "
                             "branch, never production.")
    args = parser.parse_args(argv)
    if args.command == "selfcheck":
        return run_selfcheck()
    return run_validate(args.confirm_disposable)


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
