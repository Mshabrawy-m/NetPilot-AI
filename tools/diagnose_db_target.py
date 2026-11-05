"""Diagnose the Neon database target without exposing credentials.

Read-only: opens exactly ONE psycopg2 connection as ``netpilot_app`` (with
``SET TRANSACTION READ ONLY`` issued first) and runs catalog SELECTs only.
Prints a redacted fingerprint and verdict; never outputs a password, DSN,
or connection string fragment that contains userinfo.

Usage
-----
    # Read the APP-role DSN from Windows Credential Manager:
    python tools/diagnose_db_target.py --from-wcm

    # Paste a Cloud Secrets DSN via stdin (never echoed):
    echo "<DSN>" | python tools/diagnose_db_target.py --dsn-stdin

    # Compare both in one invocation:
    python tools/diagnose_db_target.py --from-wcm --dsn-stdin < cloud_dsn.txt
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# Ensure the project root is importable regardless of invocation path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ---------------------------------------------------------------------------
# Redaction and fingerprinting helpers (exposed for testing)
# ---------------------------------------------------------------------------

_POOLER_RE = re.compile(r"-pooler", re.IGNORECASE)


def _fingerprint(host: str, database: str, user: str) -> str:
    """First 8 hex chars of SHA-256(host+database+user)."""
    payload = f"{host}{database}{user}".encode("utf-8", errors="replace")
    return hashlib.sha256(payload).hexdigest()[:8]


@dataclass(frozen=True)
class TargetInfo:
    host: str
    port: int
    database: str
    user: str
    sslmode: str
    pooler: bool
    fingerprint: str

    def redacted(self) -> str:
        return (f"host={self.host} port={self.port} database={self.database} "
                f"user={self.user} sslmode={self.sslmode} "
                f"pooler={'yes' if self.pooler else 'no'} "
                f"fingerprint={self.fingerprint}")


def parse_target(dsn: str) -> TargetInfo:
    """Extract redacted target info from a DSN without leaking the password."""
    from sqlalchemy.engine import make_url

    url = make_url(dsn)
    host = url.host or ""
    port = url.port or 5432
    database = url.database or ""
    user = url.username or ""
    # sslmode can be in query args or in connect_args
    query = url.query or {}
    sslmode = query.get("sslmode", "prefer")
    pooler = bool(_POOLER_RE.search(host))
    fp = _fingerprint(host, database, user)
    return TargetInfo(host=host, port=port, database=database, user=user,
                      sslmode=sslmode, pooler=pooler, fingerprint=fp)


# ---------------------------------------------------------------------------
# Read-only SQL enforcement (reuses the validator's allow-list)
# ---------------------------------------------------------------------------

_SAFE_PREFIXES = ("SELECT", "WITH", "SHOW")
_SAFE_EXACT = ("SET TRANSACTION READ ONLY",)
_FORBIDDEN_TOKENS: tuple[str, ...] = (
    "INSERT", "UPDATE", "DELETE", "TRUNCATE", "ALTER", "CREATE", "DROP",
    "GRANT", "REVOKE", "COPY", "VACUUM", "REFRESH", "REINDEX", "CLUSTER",
    "COMMENT", "SECURITY", "PREPARE", "EXECUTE", "DEALLOCATE",
    "CALL", "MERGE", "DO",
)


def assert_readonly(sql: str) -> None:
    """Refuse any SQL that could mutate the database."""
    stripped = sql.strip().rstrip(";").strip()
    upper = stripped.upper()
    if upper in _SAFE_EXACT:
        return
    if not any(upper.startswith(p) for p in _SAFE_PREFIXES):
        raise RuntimeError(
            f"READ-ONLY guard: refused non-read statement prefix: {upper[:30]}")
    dequoted = re.sub(r"'[^']*'", " ", upper)
    tokens = {t for t in re.split(r"[^A-Z]+", dequoted) if t}
    bad = set(_FORBIDDEN_TOKENS) & tokens
    if bad:
        raise RuntimeError(
            f"READ-ONLY guard: forbidden mutation verb(s) {sorted(bad)}")


# ---------------------------------------------------------------------------
# WCM reader (in-process only, never prints the DSN)
# ---------------------------------------------------------------------------

WCM_TARGET_APP = "netpilot/prod/netpilot_app"


def _wcm_read(target: str) -> str | None:
    """Read a generic WCM credential blob (UTF-16LE). Windows only."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    class _CRED(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    CRED_TYPE_GENERIC = 1
    advapi32.CredReadW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(_CRED))]
    advapi32.CredReadW.restype = wintypes.BOOL
    advapi32.CredFree.argtypes = [ctypes.c_void_p]
    advapi32.CredFree.restype = None

    handle = ctypes.POINTER(_CRED)()
    if not advapi32.CredReadW(target, CRED_TYPE_GENERIC, 0,
                              ctypes.byref(handle)):
        return None
    try:
        cred = handle.contents
        raw = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
    finally:
        advapi32.CredFree(handle)
    return raw.decode("utf-16-le", errors="replace").rstrip("\x00")


# ---------------------------------------------------------------------------
# Catalog queries (all SELECT-only)
# ---------------------------------------------------------------------------

_SQL_CURRENT_DB = "SELECT current_database()"
_SQL_CURRENT_USER = "SELECT current_user"
_SQL_SEARCH_PATH = "SELECT current_setting('search_path')"
_SQL_SCHEMA_TABLES = """\
SELECT nspname AS schema, count(c.relname) AS table_count
FROM pg_catalog.pg_namespace n
LEFT JOIN pg_catalog.pg_class c
  ON c.relnamespace = n.oid AND c.relkind = 'r'
WHERE nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
GROUP BY nspname ORDER BY nspname"""
_SQL_TABLE_LOCATIONS = """\
SELECT c.relname AS table_name, n.nspname AS schema_name
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'r'
  AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
ORDER BY n.nspname, c.relname"""


# ---------------------------------------------------------------------------
# Main diagnostic runner
# ---------------------------------------------------------------------------

def run_diagnostic(dsn: str) -> dict:
    """Open ONE read-only connection, gather facts, return a structured report.

    The returned dict contains only redacted information.
    """
    import psycopg2

    target = parse_target(dsn)

    # Connect with TLS required, then set READ ONLY immediately
    sslmode = target.sslmode if target.sslmode != "prefer" else "require"
    conn = psycopg2.connect(
        host=target.host, port=target.port, dbname=target.database,
        user=target.user,
        password=_extract_password(dsn),
        sslmode=sslmode, connect_timeout=15,
    )
    conn.autocommit = False
    cur = conn.cursor()
    try:
        assert_readonly(_SAFE_EXACT[0])
        cur.execute("SET TRANSACTION READ ONLY")

        cur.execute(_SQL_CURRENT_DB)
        current_database = cur.fetchone()[0]

        cur.execute(_SQL_CURRENT_USER)
        current_user_role = cur.fetchone()[0]

        cur.execute(_SQL_SEARCH_PATH)
        search_path = cur.fetchone()[0]

        cur.execute(_SQL_SCHEMA_TABLES)
        schema_summary = cur.fetchall()  # [(schema, count), ...]

        cur.execute(_SQL_TABLE_LOCATIONS)
        table_locations = cur.fetchall()  # [(table, schema), ...]

    finally:
        conn.rollback()
        cur.close()
        conn.close()

    # Determine which ORM tables are found and where
    from database.models import Base
    expected_tables = set(Base.metadata.tables.keys())
    found_map: dict[str, str] = {}  # table_name -> schema
    for tname, tschema in table_locations:
        if tname in expected_tables:
            found_map[tname] = tschema

    missing = sorted(expected_tables - set(found_map))

    # In public schema specifically (what the app inspects)
    in_public = {t for t, s in found_map.items() if s == "public"}
    missing_from_public = sorted(expected_tables - in_public)

    # Verdict
    verdict = _derive_verdict(target, current_database, search_path,
                              found_map, missing, missing_from_public)

    return {
        "target": target.redacted(),
        "current_database": current_database,
        "current_user": current_user_role,
        "search_path": search_path,
        "schema_summary": [(s, int(c)) for s, c in schema_summary],
        "found_orm_tables": len(found_map),
        "expected_orm_tables": len(expected_tables),
        "missing_from_public": missing_from_public,
        "verdict": verdict,
    }


def _extract_password(dsn: str) -> str:
    """Extract password from DSN in-memory without printing it."""
    from sqlalchemy.engine import make_url
    url = make_url(dsn)
    return url.password or ""


def _derive_verdict(target: TargetInfo, current_database: str,
                    search_path: str, found_map: dict[str, str],
                    missing: list[str],
                    missing_from_public: list[str]) -> str:
    """Map findings to H1-H5 hypotheses."""
    expected_count = len(set(found_map) | set(missing))

    if expected_count == 0 or (not found_map and missing):
        # No ORM tables found anywhere
        if current_database != target.database:
            return (f"H2: connected to database '{current_database}' but DSN "
                    f"names '{target.database}'")
        return ("H5: tables are absent on the connected branch/database. "
                "Schema initialization may not have run on this target.")

    # Tables found but not in public
    if missing_from_public and found_map:
        schemas_with_tables = set(found_map.values())
        if "public" not in schemas_with_tables:
            return (f"H3: tables exist in schema(s) {sorted(schemas_with_tables)} "
                    f"but NOT in public. search_path='{search_path}'.")
        # Some in public, some elsewhere
        return (f"H3 (partial): {len(found_map) - len(missing_from_public)} "
                f"in public, rest in {sorted(schemas_with_tables - {'public'})}.")

    # All tables in public
    if not missing_from_public:
        return "PASS: all ORM tables present in schema public."

    # Fallback
    return f"H5: {len(missing)} table(s) missing from public schema."


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only Neon database target diagnostic.")
    parser.add_argument("--from-wcm", action="store_true",
                        help="Load APP-role DSN from Windows Credential Manager.")
    parser.add_argument("--dsn-stdin", action="store_true",
                        help="Read DSN from stdin (pasted by operator).")
    args = parser.parse_args(argv)

    dsn_sources: list[tuple[str, str]] = []  # (label, dsn)

    if args.from_wcm:
        dsn = _wcm_read(WCM_TARGET_APP)
        if not dsn:
            print("ERROR: could not read WCM target "
                  f"'{WCM_TARGET_APP}'. Ensure you are on Windows and the "
                  "credential exists.", file=sys.stderr)
            return 1
        dsn_sources.append(("WCM (netpilot/prod/netpilot_app)", dsn))

    if args.dsn_stdin:
        dsn = sys.stdin.readline().strip()
        if not dsn:
            print("ERROR: --dsn-stdin specified but stdin was empty.",
                  file=sys.stderr)
            return 1
        dsn_sources.append(("STDIN (Cloud Secrets value)", dsn))

    if not dsn_sources:
        parser.print_help()
        return 1

    for label, dsn in dsn_sources:
        print(f"\n{'='*60}")
        print(f"SOURCE: {label}")
        print(f"{'='*60}")
        try:
            report = run_diagnostic(dsn)
        except Exception as exc:
            # Never include the DSN or password in error output
            print(f"CONNECTION/QUERY FAILED: {type(exc).__name__}: "
                  f"{str(exc)[:200]}", file=sys.stderr)
            return 1

        print(f"Target:      {report['target']}")
        print(f"DB (server): {report['current_database']}")
        print(f"User (server): {report['current_user']}")
        print(f"search_path: {report['search_path']}")
        print("Schemas (non-system):")
        for schema, count in report['schema_summary']:
            print(f"  {schema}: {count} table(s)")
        print(f"ORM tables found: {report['found_orm_tables']} / "
              f"{report['expected_orm_tables']}")
        if report['missing_from_public']:
            print(f"Missing from public: {', '.join(report['missing_from_public'])}")
        print(f"\nVERDICT: {report['verdict']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
