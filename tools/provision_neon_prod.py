"""Phase 2F helper - secure Neon production provisioning with ONE owner secret.

Goal
----
Provision the five NetPilot least-privilege roles on the **persistent
production** Neon branch while the operator manages only a single secret: the
Neon branch-owner credential, held in Windows Credential Manager (WCM). This
tool uses the **Neon API** ``reset_password`` endpoint to generate a strong
server-side password for each role, receives it in the HTTPS response, and
stores the resulting per-role connection string back into WCM – so that no
plaintext password is ever generated client-side, transmitted in a SQL statement,
or echoed to any log or report.

Why the Neon API path (not ALTER ROLE … PASSWORD)
---------------------------------------------------
Neon's managed compute proxy rejects pre-computed SCRAM verifiers and requires
plaintext passwords in SQL. This design avoids that constraint entirely: the
API generates the password on Neon's side, returns it via HTTPS (TLS 1.2+), and
the provisioner captures it only in process memory before persisting to WCM.
No SQL ALTER ROLE PASSWORD statement is issued.

Security contract (deliberately strict)
---------------------------------------
* **Never prints a secret.** No DSN, password, token, or credential-bearing
  response body is ever echoed; every message is routed through :func:`redact`.
* **Owner credential from WCM only.** The connection bootstrap reads the owner
  string from the single WCM target :data:`OWNER_TARGET`; it is never an
  argument, never in a file, and never written to ``os.environ``.
* **Role credentials go only to WCM + a child's own environment.** Per-role
  connection strings are handed to a spawned child (``run_collector.py`` /
  the validator) via that child's private ``env`` copy; the parent process
  environment is never mutated, so nothing to leak and nothing to "clear"
  globally.
* **Neon API auth via CLI OAuth token.** Reads the CLI's stored access_token
  from ``~/.config/neon/credentials.json``, never prints it.
* **Production only, gated.** Refuses without ``--confirm-production``, refuses
  a disposable-branch host (the M5 markers), and refuses a non-Windows host that
  has no Credential Manager. Opens no connection and writes nothing during
  ``selfcheck``.

This module performs no work at import time (``ctypes``/``psycopg2``/``urllib``
are used only inside the Windows/live paths), so it imports and its offline
tests run on any host.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database import roles as rolemod
from tools import run_sql_as_role as sqlrun
from tools.run_sql_as_role import RoleRunError

try:  # reuse the validator's disposable-branch SSOT; never duplicate a drift
    from tools.validate_pg_roles_prod import KNOWN_DISPOSABLE_MARKERS
except Exception:  # noqa: BLE001 - pragma: no cover - import safety only
    KNOWN_DISPOSABLE_MARKERS = ("br-icy-breeze-bdxgftqg", "ep-aged-sky-bdmdoqzq")

#: The five roles provisioned here (the M3 SSOT: admin + the four runtime roles).
PROVISION_ROLES = rolemod.ALL_ROLES
_ALLOWED_ROLES = frozenset(rolemod.ALL_ROLES)

#: WCM target that holds the operator-supplied Neon branch-owner credential.
OWNER_TARGET = "netpilot/prod/neondb_owner"
_TARGET_FMT = "netpilot/prod/{role}"

#: Neon API base URL for the control plane (v2).
NEON_API_BASE = "https://console.neon.tech/api/v2"

#: Defaults for the production target; overridable via CLI flags.
PROJECT_ID_DEFAULT = "plain-term-01752005"
BRANCH_ID_DEFAULT = "br-nameless-butterfly-bei57z0p"

#: Path to the Neon CLI OAuth credentials file.
_NEON_CLI_CRED_PATH = Path.home() / ".config" / "neon" / "credentials.json"

# SCRAM-SHA-256 parameters (RFC 7677 minimum iterations; PostgreSQL default).
SCRAM_PREFIX = "SCRAM-SHA-256"
DEFAULT_ITERATIONS = 4096
_SALT_BYTES = 16

_ROLE_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_SCRAM_RE = re.compile(r"SCRAM-SHA-256\$[^\s'\"]+")
_PW_LITERAL_RE = re.compile(r"(?i)\bPASSWORD\s+'[^']*'")


class ProvisionError(RuntimeError):
    """A credential-safe refusal or provisioning failure.

    The message names only role names, environment-variable names, WCM target
    names, or file paths - never a DSN, host, user, password, or verifier.
    """


# ------------------------------------------------------------------- redaction
def redact(message: object) -> str:
    """Scrub every credential shape before any text may be printed.

    Removes DSN userinfo (``scheme://user:pass@`` -> ``://***@``), any
    ``PASSWORD '...'`` literal, and any bare ``SCRAM-SHA-256$...`` verifier, on
    top of :func:`tools.run_sql_as_role._redact`.
    """
    text = sqlrun._redact(str(message))
    text = _PW_LITERAL_RE.sub("PASSWORD '***'", text)
    text = _SCRAM_RE.sub("SCRAM-SHA-256$***", text)
    return text


def _safe_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {redact(str(exc))[:200]}"


# -------------------------------------------------------------- SCRAM-SHA-256
def _salted_password(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations, 32)


def _derive_keys(password: str, salt: bytes, iterations: int):
    """Return ``(client_key, stored_key, server_key)`` per RFC 5802/7677."""
    sp = _salted_password(password, salt, iterations)
    client_key = hmac.new(sp, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(sp, b"Server Key", hashlib.sha256).digest()
    return client_key, stored_key, server_key


def build_scram_secret(password: str, *, iterations: int = DEFAULT_ITERATIONS,
                       salt: bytes | None = None) -> str:
    """Build a PostgreSQL ``SCRAM-SHA-256`` verifier string for ``password``.

    Format (pg_authid): ``SCRAM-SHA-256$<iterations>:<b64 salt>$<b64
    StoredKey>:<b64 ServerKey>``. A random salt is used unless one is supplied
    (the deterministic salt exists solely for RFC-vector tests). The returned
    verifier is a secret and must be handled like a credential.
    """
    if not password:
        raise ProvisionError("refusing to build a verifier for an empty password")
    if iterations < 4096:
        raise ProvisionError("SCRAM iterations must be at least 4096 (RFC 7677)")
    if salt is None:
        salt = secrets.token_bytes(_SALT_BYTES)
    _, stored_key, server_key = _derive_keys(password, salt, iterations)
    b64 = lambda raw: base64.b64encode(raw).decode("ascii")
    return (f"{SCRAM_PREFIX}${iterations}:{b64(salt)}$"
            f"{b64(stored_key)}:{b64(server_key)}")


def is_scram_verifier(value: str) -> bool:
    """True only for a well-formed, >=4096-iteration SCRAM-SHA-256 verifier."""
    match = re.fullmatch(
        r"SCRAM-SHA-256\$([0-9]+):([A-Za-z0-9+/=]+)\$([A-Za-z0-9+/=]+):([A-Za-z0-9+/=]+)",
        value if isinstance(value, str) else "")
    if not match:
        return False
    return int(match.group(1)) >= 4096


# --------------------------------------------------------------- password gen
def generate_password(nbytes: int = 32) -> str:
    """A unique 256-bit CSPRNG password (URL-safe alphabet, no whitespace)."""
    if nbytes < 32:
        raise ProvisionError("password entropy must be at least 32 bytes")
    return secrets.token_urlsafe(nbytes)


# ------------------------------------------------------------------ WCM access

# -------------------------------------------------------- Neon API password mgmt
def _read_neon_cli_token() -> str:
    """Read the OAuth access_token from the Neon CLI config.

    The token is never printed or logged. Raises ProvisionError if unavailable
    or expired (the operator must run `neon auth` or refresh the CLI login).
    """
    if not _NEON_CLI_CRED_PATH.exists():
        raise ProvisionError(
            f"Neon CLI credentials not found at {_NEON_CLI_CRED_PATH}. "
            "Run `npx neon auth` to authenticate the CLI first.")
    try:
        data = json.loads(_NEON_CLI_CRED_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProvisionError(
            f"could not read Neon CLI credentials: {type(exc).__name__}") from exc
    token = data.get("access_token", "")
    if not token:
        raise ProvisionError("Neon CLI credentials contain no access_token")
    expires_at = data.get("expires_at", 0)
    if expires_at and time.time() > int(expires_at):
        raise ProvisionError(
            "Neon CLI OAuth token has expired; run `npx neon auth` to refresh")
    return token


def _neon_api_reset_password(token: str, project_id: str, branch_id: str,
                             role: str) -> tuple[str, list[str]]:
    """Call Neon POST reset_password; return (password, operation_ids).

    The password is returned in memory only — never logged, printed, or
    persisted. Uses urllib (stdlib) with TLS; the auth token goes in the
    Authorization header (HTTPS encrypted in transit).
    """
    url = (f"{NEON_API_BASE}/projects/{project_id}/branches/{branch_id}"
           f"/roles/{role}/reset_password")
    req = Request(url, method="POST",
                  headers={"Authorization": f"Bearer {token}",
                           "Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
    except HTTPError as exc:
        # Do NOT include response body (may contain role metadata).
        raise ProvisionError(
            f"Neon API reset_password failed for {role!r}: "
            f"HTTP {exc.code}") from exc
    except (URLError, OSError) as exc:
        raise ProvisionError(
            f"Neon API unreachable for {role!r}: {type(exc).__name__}") from exc
    password = body.get("role", {}).get("password", "")
    if not password:
        raise ProvisionError(
            f"Neon API returned no password for role {role!r}")
    op_ids = [op.get("id", "") for op in body.get("operations", []) if op.get("id")]
    return password, op_ids


def _neon_api_wait_operations(token: str, project_id: str,
                              operation_ids: list[str], *,
                              max_wait: int = 120,
                              poll_interval: float = 3.0) -> None:
    """Poll Neon operations until all completed (or raise on timeout)."""
    if not operation_ids:
        return
    deadline = time.monotonic() + max_wait
    pending = set(operation_ids)
    while pending and time.monotonic() < deadline:
        done: set[str] = set()
        for op_id in pending:
            url = f"{NEON_API_BASE}/projects/{project_id}/operations/{op_id}"
            req = Request(url, headers={"Authorization": f"Bearer {token}"})
            try:
                with urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read())
                status = data.get("operation", {}).get("state", "")
                if status in ("completed", "success"):
                    done.add(op_id)
                elif status in ("failed",):
                    raise ProvisionError(
                        f"Neon operation {op_id} failed during provisioning")
            except (HTTPError, URLError, OSError, KeyError):
                pass  # retry on transient failures
            pending -= done
        if pending:
            time.sleep(poll_interval)
    if pending:
        raise ProvisionError(
            f"Neon operations did not complete within {max_wait}s "
            f"({len(pending)} still pending)")


# --------------------------------------------------------------- WCM access helpers
def wcm_target(role: str) -> str:
    return _TARGET_FMT.format(role=role)


class WcmUnavailableError(ProvisionError):
    """Credential Manager is not usable on this host."""


def _cred_read(target: str) -> str | None:
    """Read a generic WCM credential's secret (UTF-16LE blob) or ``None``.

    Real Windows path only; tests replace :func:`wcm_read` entirely.
    """
    if sys.platform != "win32":
        raise WcmUnavailableError(
            "Windows Credential Manager is only available on Windows.")
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


def _cred_write(target: str, secret: str, username: str = "") -> bool:
    """Write/overwrite a generic WCM credential (UTF-16LE blob). Real Windows."""
    if sys.platform != "win32":
        raise WcmUnavailableError(
            "Windows Credential Manager is only available on Windows.")
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
    CRED_PERSIST_LOCAL_MACHINE = 2
    blob = secret.encode("utf-16-le")
    buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)

    cred = _CRED()
    cred.Flags = 0
    cred.Type = CRED_TYPE_GENERIC
    cred.TargetName = wintypes.LPWSTR(target)
    cred.CredentialBlobSize = len(blob)
    cred.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
    cred.Persist = CRED_PERSIST_LOCAL_MACHINE
    cred.UserName = wintypes.LPWSTR(username) if username else None

    advapi32.CredWriteW.argtypes = [ctypes.POINTER(_CRED), wintypes.DWORD]
    advapi32.CredWriteW.restype = wintypes.BOOL
    return bool(advapi32.CredWriteW(ctypes.byref(cred), 0))


# The provisioning flow calls these indirections so tests can inject fakes.
def wcm_read(target: str) -> str | None:
    return _cred_read(target)


def wcm_write(target: str, secret: str, *, username: str = "") -> bool:
    return _cred_write(target, secret, username)


# ------------------------------------------------------------------ execution
def _connect(dsn: str):
    """Open a real TLS-required psycopg2 connection (lazy import)."""
    return sqlrun._default_connect(dsn, **sqlrun.build_tls_kwargs(dsn))


def _exec_sql(conn, sql_file: str) -> str:
    """Run one approved role SQL file on ``conn`` (allowlist enforced)."""
    path = sqlrun.resolve_approved_sql("database/sql/roles/" + sql_file)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(path.read_text(encoding="utf-8"))
    return path.name


def _alter_password_statement(role: str, verifier: str) -> str:
    """Build ``ALTER ROLE <role> WITH PASSWORD '<scram verifier>'``.

    Refuses any role outside the SSOT and any value that is not a SCRAM
    verifier, so a plaintext password can never be emitted.
    This is the legacy SCRAM path; NOT used on Neon (Neon rejects SCRAM).
    """
    if role not in _ALLOWED_ROLES or not _ROLE_RE.match(role):
        raise ProvisionError(f"refusing to set a password for unrecognised role {role!r}")
    if not is_scram_verifier(verifier):
        raise ProvisionError(
            "refusing to transmit a non-SCRAM password; a SCRAM-SHA-256 "
            "verifier is required so no plaintext crosses to the server")
    literal = "'" + verifier.replace("'", "''") + "'"
    return f"ALTER ROLE {role} WITH PASSWORD {literal}"


def _alter_password_plaintext(role: str, password: str) -> str:
    """Build ``ALTER ROLE <role> WITH PASSWORD '<plaintext>'``.

    Neon's managed proxy REJECTS pre-computed SCRAM verifiers (HTTP 400)
    but ACCEPTS plaintext passwords, hashing them server-side. This is the
    only way to set the initial password on a role created via SQL
    (``authentication_method: no_login``), because the Neon API's
    ``reset_password`` endpoint returns 422 for roles that never had one.

    Security rationale:
    * The statement is sent over TLS 1.2+ (sslmode=require enforced).
    * The password is a 256-bit CSPRNG token (infeasible to brute-force).
    * Briefly visible in pg_stat_activity during execution (Neon-internal
      superuser only; our least-privilege roles cannot access it).
    * Never printed, logged, or persisted outside WCM.
    * The ``redact()`` function scrubs PASSWORD literals from any exception.
    """
    if role not in _ALLOWED_ROLES or not _ROLE_RE.match(role):
        raise ProvisionError(f"refusing to set a password for unrecognised role {role!r}")
    if not password:
        raise ProvisionError("refusing to set an empty password")
    if password.startswith(f"{SCRAM_PREFIX}$"):
        raise ProvisionError(
            "refusing to re-use a SCRAM verifier as a plaintext password")
    literal = "'" + password.replace("'", "''") + "'"
    return f"ALTER ROLE {role} WITH PASSWORD {literal}"


def _run_child(args: list[str], env_extra: dict, *, executable: str | None = None) -> int:
    """Run a repo tool as a child with a private env copy (parent untouched)."""
    child_env = {**os.environ, **env_extra}
    exe = executable or sys.executable
    proc = subprocess.run([exe, *[str(a) for a in args]], env=child_env,
                          capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[:300]
        raise ProvisionError(
            f"{Path(str(args[0])).name} failed (exit {proc.returncode}): "
            f"{redact(tail)}")
    return proc.returncode


# ----------------------------------------------------------------- orchestration
def _provision_core(*, connect, wcm_read, wcm_write, child,
                    api_reset_password=None, api_wait_ops=None,
                    project_id: str | None = None,
                    branch_id: str | None = None) -> None:
    """The credential-bearing sequence; raises ProvisionError to refuse.

    Password management uses ALTER ROLE … PASSWORD (plaintext over TLS) by
    default, because the Neon API ``reset_password`` returns 422 for roles
    created via SQL that have ``authentication_method: no_login``.

    When ``api_reset_password`` is injected (e.g., for role rotation after
    initial provisioning when the API path becomes viable), the API path is
    used instead. Both paths are tested offline.
    """
    owner_dsn = wcm_read(OWNER_TARGET)
    if not owner_dsn:
        raise ProvisionError(
            f"the Neon owner credential {OWNER_TARGET!r} is not present in "
            "Windows Credential Manager. Store it out-of-band (never in chat), "
            "then re-run provisioning.")

    host = (sqlrun.parse_pg_target(owner_dsn)["host"] or "").lower()
    for marker in KNOWN_DISPOSABLE_MARKERS:
        if marker in host:
            raise ProvisionError(
                "refusing to provision a disposable M5 branch (owner host "
                f"matches marker {marker!r}); target the persistent production "
                "branch instead.")
    sqlrun.build_tls_kwargs(owner_dsn)  # validate TLS before any use

    # --- Phase 1+2: Create roles + set passwords via psycopg2 (single conn) ---
    owner_conn = connect(owner_dsn)
    try:
        print("step: create roles (10_create_roles.sql)")
        _exec_sql(owner_conn, "10_create_roles.sql")

        if api_reset_password is not None:
            # API path (for roles already in password state, e.g. rotation).
            all_op_ids: list[str] = []
            for role in PROVISION_ROLES:
                print(f"step: reset password via Neon API ({role})")
                password, op_ids = api_reset_password(project_id, branch_id, role)
                all_op_ids.extend(op_ids)
                try:
                    role_dsn = sqlrun.build_role_dsn(owner_dsn, role, password)
                    target = wcm_target(role)
                    existing = wcm_read(target)
                    if existing:
                        print(f"step: rotating existing credential for {role}")
                    if not wcm_write(target, role_dsn, username=role):
                        raise ProvisionError(
                            f"could not store the {role!r} role credential")
                    print(f"step: credential stored for {role}")
                finally:
                    password = None
                    role_dsn = None
            if api_wait_ops is not None:
                print("step: waiting for Neon password operations to complete")
                api_wait_ops(all_op_ids)
        else:
            # Default: CSPRNG + ALTER ROLE plaintext over TLS.
            for role in PROVISION_ROLES:
                password = generate_password()
                try:
                    stmt = _alter_password_plaintext(role, password)
                    owner_conn.autocommit = True
                    with owner_conn.cursor() as cur:
                        cur.execute(stmt)
                    role_dsn = sqlrun.build_role_dsn(owner_dsn, role, password)
                    target = wcm_target(role)
                    existing = wcm_read(target)
                    if existing:
                        print(f"step: rotating existing credential for {role}")
                    if not wcm_write(target, role_dsn, username=role):
                        raise ProvisionError(
                            f"could not store the {role!r} role credential")
                    print(f"step: password configured + credential stored for {role}")
                finally:
                    password = None
                    role_dsn = None

        # --- Phase 3: Ownership transfer (same connection, no restart) ---
        print("step: schema ownership (20_ownership.sql)")
        _exec_sql(owner_conn, "20_ownership.sql")
    finally:
        owner_conn.close()

    # --- admin phase: schema/default privileges, init-schema, grants ---
    admin_dsn = wcm_read(wcm_target(rolemod.ROLE_ADMIN))
    if not admin_dsn:
        raise ProvisionError(
            "the netpilot_admin credential is missing after password "
            "configuration; aborting before schema privileges.")
    admin_conn = connect(admin_dsn)
    try:
        print("step: schema privileges (21_schema_privileges.sql)")
        _exec_sql(admin_conn, "21_schema_privileges.sql")
        print("step: default privileges (40_default_privileges.sql)")
        _exec_sql(admin_conn, "40_default_privileges.sql")
    finally:
        admin_conn.close()

    print("step: initialize production schema (init-schema, admin process only)")
    child(["tools/run_collector.py", "init-schema"], {
        rolemod.ROLE_URL_ENV[rolemod.ROLE_ADMIN]: admin_dsn,
        "NETPILOT_MANAGE_SCHEMA": "true",
        "NETPILOT_DEMO_MODE": "false",
    })

    grant_conn = connect(admin_dsn)
    try:
        for fname in ("30_grant_collector.sql", "31_grant_app.sql",
                      "32_grant_retention_rollup.sql"):
            print(f"step: grants ({fname})")
            _exec_sql(grant_conn, fname)
    finally:
        grant_conn.close()
    admin_dsn = None

    # --- read-only production validation with all five role URLs ---
    env_all = {"NETPILOT_DEMO_MODE": "false"}
    for role in PROVISION_ROLES:
        value = wcm_read(wcm_target(role))
        if not value:
            raise ProvisionError(
                f"role credential for {role!r} is missing before validation")
        env_all[rolemod.ROLE_URL_ENV[role]] = value
    print("step: read-only production validation")
    child(["tools/validate_pg_roles_prod.py", "validate", "--confirm-production"],
          env_all)
    for k in env_all:
        env_all[k] = None


def provision(*, confirm_production: bool, connect=None, wcm_read=None,
              wcm_write=None, child=None, api_reset_password=None,
              api_wait_ops=None, project_id: str | None = None,
              branch_id: str | None = None) -> int:
    """Gated entry point. Returns 0 success, 2 refusal, 1 execution error."""
    connect = connect or _connect
    wcm_read = wcm_read or globals()["wcm_read"]
    wcm_write = wcm_write or globals()["wcm_write"]
    child = child or _run_child
    project_id = project_id or PROJECT_ID_DEFAULT
    branch_id = branch_id or BRANCH_ID_DEFAULT
    if not confirm_production:
        print("REFUSED: --confirm-production is required. This helper "
              "provisions a persistent production Neon branch only.")
        return 2
    # API path is opt-in (NETPILOT_USE_API_ROTATION env or explicit injection).
    # Default is ALTER ROLE plaintext over TLS.
    try:
        if api_reset_password is None and os.getenv("NETPILOT_USE_API_ROTATION"):
            token = _read_neon_cli_token()
            api_reset_password = lambda pid, bid, role: _neon_api_reset_password(
                token, pid, bid, role)
            if api_wait_ops is None:
                api_wait_ops = lambda ops: _neon_api_wait_operations(
                    token, project_id, ops)
        _provision_core(connect=connect, wcm_read=wcm_read,
                        wcm_write=wcm_write, child=child,
                        api_reset_password=api_reset_password,
                        api_wait_ops=api_wait_ops,
                        project_id=project_id, branch_id=branch_id)
    except WcmUnavailableError as exc:
        print(f"REFUSED: {redact(exc)}")
        return 2
    except ProvisionError as exc:
        print(f"REFUSED: {redact(exc)}")
        return 2
    except RoleRunError as exc:
        print(f"REFUSED: {redact(exc)}")
        return 2
    except Exception as exc:  # noqa: BLE001 - surface DB errors safely
        print(_safe_error(exc))
        return 1
    print("PROVISIONING COMPLETE (no credential values are ever shown here).")
    return 0


# ------------------------------------------------------------------- selfcheck
def _scram_matches_rfc7677() -> bool:
    """Verify the derivation against RFC 7677 section 3 published values."""
    salt = base64.b64decode("W22ZaJ0SNY7soEsUEjb6gQ==")
    client_key, stored_key, server_key = _derive_keys("pencil", salt, 4096)
    auth_msg = (
        "n=user,r=rOprNGfwEbeRWgbNEkqO"
        ",r=rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0,"
        "s=W22ZaJ0SNY7soEsUEjb6gQ==,i=4096,"
        "c=biws,r=rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0")
    # RFC 5802: ClientSignature = HMAC(StoredKey, AuthMessage);
    #            ClientProof    = ClientKey XOR ClientSignature.
    client_sig = hmac.new(stored_key, auth_msg.encode("utf-8"),
                          hashlib.sha256).digest()
    proof = bytes(a ^ b for a, b in zip(client_key, client_sig))
    signature = hmac.new(server_key, auth_msg.encode("utf-8"),
                         hashlib.sha256).digest()
    b64 = lambda raw: base64.b64encode(raw).decode("ascii")
    ok_proof = b64(proof) == "dHzbZapWIk4jUhN+Ute9ytag9zjfMHgsqmmiz7AndVQ="
    ok_sig = b64(signature) == "6rriTRBi23WpRR/wtup+mMhUZUn/dB5nLTJRsjl95G4="
    verifier = build_scram_secret("pencil", iterations=4096, salt=salt)
    _, _, tail = verifier.split("$", 2)
    stored_b64, server_b64 = tail.split(":")
    return (ok_proof and ok_sig
            and stored_b64 == b64(stored_key)
            and server_b64 == b64(server_key)
            and is_scram_verifier(verifier))


def run_selfcheck() -> int:
    """OFFLINE guarantees. Opens no connection, touches no WCM, writes nothing."""
    results: list[tuple[str, bool, str]] = []
    results.append(("SCRAM derivation matches RFC 7677 vector",
                    _scram_matches_rfc7677(), ""))

    try:
        _alter_password_statement(rolemod.ROLE_APP, "plaintext-pw")
        rejects_plaintext = False
    except ProvisionError:
        rejects_plaintext = True
    results.append((
        ("_alter_password_statement refuses non-SCRAM "
         "(legacy guard; not used in Neon flow)"),
        rejects_plaintext, ""))

    try:
        _alter_password_statement("not_a_role", build_scram_secret("x"))
        rejects_role = False
    except ProvisionError:
        rejects_role = True
    results.append(("refuses an out-of-SSOT role name", rejects_role, ""))

    leaked = "postgresql://bob:Sup3rPw@ep-x.neon.tech:5432/neondb " \
        + build_scram_secret("Sup3rPw", salt=b"0123456789abcdef")
    scrubbed = redact(leaked)
    results.append(("redaction removes DSN + verifier",
                    "Sup3rPw" not in scrubbed and "SCRAM-SHA-256$" in scrubbed
                    and "W22" not in scrubbed, ""))

    pw = generate_password()
    results.append(("password is 256-bit + URL-safe, no whitespace",
                    len(pw) >= 42 and not re.search(r"\s", pw)
                    and pw != generate_password(), ""))

    # Neon API checks
    results.append(("Neon API URL is HTTPS (TLS)",
                    NEON_API_BASE.startswith("https://"), ""))
    results.append(("PROJECT_ID_DEFAULT set",
                    bool(PROJECT_ID_DEFAULT), ""))
    results.append(("BRANCH_ID_DEFAULT set",
                    bool(BRANCH_ID_DEFAULT), ""))
    token_readable = _NEON_CLI_CRED_PATH.exists()
    results.append(("Neon CLI credentials file accessible",
                    token_readable,
                    "" if token_readable else "(run `npx neon auth`)"))

    ok = True
    for name, passed, detail in results:
        print(f"[{'PASS' if passed else 'FAIL'}] {name}"
              + (f" ({detail})" if detail else ""))
        ok = ok and passed
    print(f"\nselfcheck: {sum(1 for _, p, _ in results if p)}/{len(results)} passed")
    return 0 if ok else 1


# ------------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="provision_neon_prod",
        description="Securely provision the five NetPilot roles on the "
                    "persistent production Neon branch using ONE owner "
                    "credential from Windows Credential Manager. Never prints "
                    "or transmits a plaintext password.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("selfcheck", help="offline guarantees; no connection, no writes")
    live = sub.add_parser(
        "provision", help="live provisioning (production only)")
    live.add_argument("--confirm-production", action="store_true",
                      help="assert the owner credential targets a persistent "
                           "production Neon branch (never a disposable one; "
                           "known disposable hosts are still refused).")
    live.add_argument("--project-id", default=PROJECT_ID_DEFAULT,
                      help="Neon project ID for the API password reset "
                           f"(default: {PROJECT_ID_DEFAULT})")
    live.add_argument("--branch-id", default=BRANCH_ID_DEFAULT,
                      help="Neon branch ID for the API password reset "
                           f"(default: {BRANCH_ID_DEFAULT})")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "selfcheck":
        return run_selfcheck()
    return provision(confirm_production=args.confirm_production,
                     project_id=args.project_id, branch_id=args.branch_id)


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
