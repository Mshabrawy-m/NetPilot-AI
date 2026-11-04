# NetPilot AI — M5 Unblock: Neon Disposable Setup (Operator Guide)

Purpose: give the operator the **exact**, credential-free steps to stand up a
throwaway Neon branch so the M5 live role validation
([`NetPilot_M5_Neon_Validation_Runbook.md`](NetPilot_M5_Neon_Validation_Runbook.md))
can be run. This guide performs **no** Neon action and executes **no** SQL — it
is documentation only. The role/GRANT facts come from
[`database/sql/roles/`](../database/sql/roles/) (generated from
[`database/pg_privileges.py`](../database/pg_privileges.py)) and the
[`M3 runbook`](NetPilot_M3_PostgreSQL_Roles_Runbook.md) execution order.

> Never paste a password or connection string into a file, log, commit, or
> chat. Everything below uses hidden input (`Read-Host -AsSecureString`) and a
> **process-scoped** environment that dies with the shell. `.env` is never
> touched; the normal project environment keeps `NETPILOT_DEMO_MODE=true`,
> `NETPILOT_CHANGE_LOOP_ENABLED=false`, `NETPILOT_LAB_EXECUTION_ENABLED=false`.

## 0. Prerequisites

- A Neon account with permission to create a project. This is a **scratch
  project only** — never an existing production project/branch.
- Local `psql` (or any SQL client that can connect with an explicit DSN), plus
  the repo virtualenv (`.venv\Scripts\python.exe`).
- A secret manager (or the operator's password manager) to hold the five role
  passwords. They are only ever typed/pasted into hidden prompts.

---

## 1. Create a disposable project + branch (Console + CLI)

> **Parent safety (E3):** branch from an **empty** `main` so no production rows
> are copied. If the parent you would fork from already holds data, do **not**
> branch from it for this exercise — investigate a **schema-only branch**
> (structure without copying rows) instead, or provision a fresh empty project.

### 1a. Console path

1. Neon Console → **New project**.
   - Name: `netpilot-m5-validation` (a throwaway, unmistakably not production).
   - Region: nearest. Default Postgres version. Accept the default *main*
     branch — it stays **empty**; you will not use it.
2. Open the **Branches** tab → **Create a branch**.
   - Branch name: `m5-disposable`, forked from the empty `main`. Confirm this
     branch's compute endpoint is the **only** one you connect to in this
     exercise.

### 1b. Optional CLI path (Neon's default tooling; supports auto-expiry)

```bash
neon branches create --name m5-disposable --parent main --expires-at <ISO8601>
```

- **Choosing an expiry:** set `<ISO8601>` a few hours out, just past the
  expected validation window (e.g. `--expires-at 2026-11-04T20:00:00Z`). The
  branch self-destructs then, so a forgotten disposable branch cannot linger and
  accrue storage cost — but do not set it so short that it expires mid-run.
- **Verify the branch before connecting:** `neon branches list`, then fetch its
  string via §1c. Do not connect until the endpoint matches `m5-disposable`.

### 1c. Get the DIRECT connection string (no credentials exposed)

- **Prefer** `neon connection-string m5-disposable` — it returns the **direct
  (non-pooled)** string by default; confirm the hostname has **no `-pooler`**
  suffix.
- **Alternative:** `neon env pull` may be used **only** with an explicit
  throwaway output file **outside** the repository (e.g.
  `neon env pull --file "$env:TEMP\neon-m5.env"`). It writes the direct URL as
  `DATABASE_URL_UNPOOLED`.
- ⛔ **Never run `neon env pull` against the project `.env`** — that writes a
  real DSN into a tracked file and violates the credential-handling rule.

### 1d. Direct vs pooled, database name, and compute behavior

- ⚠️ **Use the DIRECT (non-pooled) endpoint for M5**, not the `-pooler` host.
  M5 probes read `pg_stat_ssl` for the backend PID and rely on a session
  `statement_timeout`; PgBouncer (pooled, transaction) mode does not preserve
  that per-session state.
- **Database name:** copy the **exact** database name from the branch's
  connection string (Neon's default is `neondb`, **not** `db`). The §3 DSN uses
  `<database>` for that real value — never assume it.
- The **host is not secret** (no credentials in it); the role **passwords are**.
- **Scale-to-zero (E4):** Neon suspends an idle compute (~5 min default) and the
  first connection pays a cold start (hundreds of ms). Reconnects resume it.
  **Keep the configured connection timeout as-is** unless live evidence shows it
  is genuinely too short.

---

## 2. Create the five least-privilege roles

Create the roles with the SQL scripts, not the Console UI (least-privilege):

- ✅ **Create all five roles with `10_create_roles.sql`** as plain `LOGIN` roles,
  then attach a password out-of-band. `CREATE ROLE` needs role-creation
  privilege: connect as the project's initial owner (an elevated role) to run
  script 10.
- ⚠️ **Avoid the Console "Roles" tab for the four runtime roles.** *Not
  verified by the installed Neon skills:* Console-created roles are reported to
  carry broader membership/privileges than plain roles, which could weaken the
  negative probes. Treat this as an **assumption to verify live**, not a fact.
  The authoritative check is the M5 runtime probe (§6), not the assumption:
  every runtime role must report `has_schema_privilege(role,'public','USAGE') =
  true` **and** `has_schema_privilege(role,'public','CREATE') = false`.

Passwords: after script 10 creates a `NO PASSWORD` role, set each password from
the secret manager via `ALTER ROLE … PASSWORD` executed **interactively** (never
in a file). In practice you can paste the DSN with the password into the hidden
prompt in §3 rather than typing SQL; either way the secret never lands on disk.

The five roles (from `10_create_roles.sql`): `netpilot_admin`,
`netpilot_collector`, `netpilot_app`, `netpilot_retention`, `netpilot_rollup`.

---

## 3. Map the roles to the five connection URLs

Each DSN comes from the **same direct endpoint host + database** you retrieved
in §1c/§1d, differs only by role user, and must carry `sslmode=require`. Start
from `neon connection-string m5-disposable`, then per role swap in the user and
set the scheme to `postgresql+psycopg2://` (the app uses psycopg2, not psycopg),
copying the **exact** `<database>` from §1d. `<PW>` is supplied only through the
hidden prompt in §3a — never echoed, logged, or written to disk:

| Env var | Role user | Used by |
|---|---|---|
| `NETPILOT_DB_URL_ADMIN` | `netpilot_admin` | `run_collector.py init-schema` (DDL) — **direct** endpoint |
| `NETPILOT_DB_URL_APP` | `netpilot_app` | Streamlit app reader/writer |
| `NETPILOT_DB_URL_COLLECTOR` | `netpilot_collector` | collector runner |
| `NETPILOT_DB_URL_RETENTION` | `netpilot_retention` | retention runner |
| `NETPILOT_DB_URL_ROLLUP` | `netpilot_rollup` | rollup runner |

```
postgresql+psycopg2://netpilot_<role>:<PW>@ep-<...>.neon.tech:5432/<database>?sslmode=require
```

### 3a. Set the URLs in a **temporary, isolated PowerShell process only**

Open a fresh PowerShell window (this window *is* the isolated environment).
Every prompt is hidden; nothing is echoed or written to disk.

```powershell
# Hidden reader: paste one DSN, it is returned as a SecureString (never shown).
function Get-Dsn([string]$label) {
  $s = Read-Host "$label (paste DSN, input hidden)" -AsSecureString
  $b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)
  try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b) }
  finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b) }
}

$env:NETPILOT_DB_URL_ADMIN     = Get-Dsn 'ADMIN DSN'
$env:NETPILOT_DB_URL_APP       = Get-Dsn 'APP DSN'
$env:NETPILOT_DB_URL_COLLECTOR = Get-Dsn 'COLLECTOR DSN'
$env:NETPILOT_DB_URL_RETENTION = Get-Dsn 'RETENTION DSN'
$env:NETPILOT_DB_URL_ROLLUP    = Get-Dsn 'ROLLUP DSN'

# Disable demo mode FOR THIS PROCESS ONLY (the repo .env stays demo=true).
$env:NETPILOT_DEMO_MODE            = 'false'
$env:NETPILOT_CHANGE_LOOP_ENABLED  = 'false'
$env:NETPILOT_LAB_EXECUTION_ENABLED= 'false'
```

These variables exist only in this process. Do **not** add them to `.env`, a
profile script, or CI. When the window closes they are gone.

---

## 4. Verify all five URLs hit the same disposable branch — without printing secrets

Compare only the **non-secret host + database** parsed out of each DSN. `Get-Dsn`
values are already in `$env`; this reads them, prints host/db only, and never
touches the userinfo.

```powershell
$vars = 'NETPILOT_DB_URL_ADMIN','NETPILOT_DB_URL_APP','NETPILOT_DB_URL_COLLECTOR',
        'NETPILOT_DB_URL_RETENTION','NETPILOT_DB_URL_ROLLUP'
$rows = foreach ($v in $vars) {
  $dsn = [Environment]::GetEnvironmentVariable($v)   # read by dynamic name
  if (-not $dsn) { continue }
  $u = [Uri]$dsn                                     # parse; never ToString() the userinfo
  [pscustomobject]@{ Var = $v; Host = $u.Host; Db = $u.AbsolutePath.Trim('/')
    Ssl = ([Uri]::UnescapeDataString($u.Query)).Trim('?') }
}
$rows | Format-Table -AutoSize                        # host / db / sslmode only
$hosts = @($rows.Host)
Write-Output ("All-five-same-host:           {0}" -f (($hosts | Select-Object -Unique).Count -eq 1))
Write-Output ("All-five-direct (no -pooler):  {0}" -f ((@($hosts | Where-Object { $_ -match '-pooler' }).Count) -eq 0))
Write-Output ("All-five-same-database:        {0}" -f ((@($rows.Db) | Select-Object -Unique).Count -eq 1))
Write-Output ("All-five-sslmode=require:      {0}" -f ((@($rows | Where-Object { $_.Ssl -notmatch 'sslmode=require' }).Count) -eq 0))
```

Pass criteria: all five hosts identical, every host is **direct** (no `-pooler`),
every database equals the `<database>` copied in §1d, and every URL carries
`sslmode=require`. **A hostname never proves branch identity** — Neon encodes no
branch name in the endpoint host. Confirm the branch out-of-band by matching the
host against `neon connection-string m5-disposable` (or the Console branch
details). If any row shows a `-pooler` host, a different endpoint, or a host that
does not match the `m5-disposable` endpoint, fix it before connecting.

---

## 5. Initialize roles, schema, and grants (documented order)

Use `psql` with the appropriate DSN so you control the executing identity.
(Neon's built-in SQL Editor always connects as the fixed project-owner role —
use `psql` for the per-role steps below.)

```powershell
# 1) Create the five roles (idempotent) - as the project owner (elevated role).
psql "postgresql+psycopg2://<owner>:<PW>@ep-<...>.neon.tech:5432/<database>?sslmode=require" `
     -f database/sql/roles/10_create_roles.sql

# 2) Create the schema/tables AS netpilot_admin (uses the ADMIN URL you set).
.venv\Scripts\python.exe tools\run_collector.py init-schema

# 3) Ownership + revoke PUBLIC - as the project owner (elevated role).
psql "<owner DSN>" -f database/sql/roles/20_ownership.sql

# 4) Schema USAGE for runtime roles - as netpilot_admin (schema owner).
psql "$env:NETPILOT_DB_URL_ADMIN" -f database/sql/roles/21_schema_privileges.sql

# 5) Per-role table + sequence GRANTs - as netpilot_admin.
psql "$env:NETPILOT_DB_URL_ADMIN" -f database/sql/roles/30_grant_collector.sql
psql "$env:NETPILOT_DB_URL_ADMIN" -f database/sql/roles/31_grant_app.sql
psql "$env:NETPILOT_DB_URL_ADMIN" -f database/sql/roles/32_grant_retention_rollup.sql

# 6) Default privileges for future admin-created tables - as netpilot_admin.
psql "$env:NETPILOT_DB_URL_ADMIN" -f database/sql/roles/40_default_privileges.sql
```

Order rationale (M3 runbook): roles → schema → **ownership (after tables exist,
before runtime GRANTs)** → schema USAGE → grants → default privileges. All
scripts are idempotent. `init-schema` binds `netpilot_admin` and **fails closed**
if `NETPILOT_DB_URL_ADMIN` is missing — it never silently uses another role.

---

## 6. Run the M5 live validation

Still in the isolated window (demo off, five URLs set):

```powershell
# Offline audit must stay green first.
.venv\Scripts\python.exe tools\validate_pg_roles_m5.py selfcheck

# Live probes against the disposable branch.
.venv\Scripts\python.exe tools\validate_pg_roles_m5.py validate --confirm-disposable
```

- Exit `0` = every probe PASS; non-zero prints each failing probe with a
  credential-safe signature (host + SQLSTATE class, never a password).
- **Stop on any unexpected FAIL.** Do **not** widen a GRANT to force a pass;
  a genuine denial means a matrix bug — fix `database/pg_privileges.py` + the
  SQL, re-run the M3 byte-equality test, document it, and get approval.

---

## 7. Cleanup

```powershell
# Clear the secrets from this process, then close the isolated window.
Remove-Item Env:NETPILOT_DB_URL_ADMIN,Env:NETPILOT_DB_URL_APP,
            Env:NETPILOT_DB_URL_COLLECTOR,Env:NETPILOT_DB_URL_RETENTION,
            Env:NETPILOT_DB_URL_ROLLUP -ErrorAction SilentlyContinue
Remove-Item Env:NETPILOT_DEMO_MODE -ErrorAction SilentlyContinue
```

- **Delete the `m5-disposable` branch** (Console, or `neon branches delete`) or
  the whole scratch project. That removes the schema, roles, and the single probe
  row (`devices.name = '__np_m5_probe__'`) in one action. A branch created with
  `--expires-at` (§1b) also self-destructs at its expiry as a backstop.
- Never attempt to purge probe rows from anything non-disposable.
- Rotate/discard the role passwords in the secret manager.
- Confirm the normal project environment is unchanged: `.env` still
  `NETPILOT_DEMO_MODE=true`, both other flags still `false`.

---

## Neon-specific limitations requiring manual confirmation

1. **Role creation model (verify live).** The concern that Console-created
   runtime roles carry broader privileges than plain `LOGIN` roles is **not
   documented by the installed Neon skills** — treat it as an assumption to
   verify, not a fact. Mitigation: create roles via `10_create_roles.sql`, then
   let the M5 probes decide — each runtime role must report
   `has_schema_privilege(role,'public','USAGE') = true` and
   `has_schema_privilege(role,'public','CREATE') = false`.
2. **Pooled vs direct endpoint.** Use the **direct** endpoint for M5 so the
   `pg_stat_ssl`/`statement_timeout` probes reflect the real session; the pooled
   PgBouncer endpoint hides that state.
3. **Ownership / default privileges (verify on the disposable branch).** Neon
   has no customer superuser. `20_ownership.sql` (`REVOKE ALL ON SCHEMA public
   FROM PUBLIC`, `ALTER … OWNER TO`) and `40_default_privileges.sql`
   (`ALTER DEFAULT PRIVILEGES FOR ROLE netpilot_admin`) need an elevated/owner
   execution identity. Whether those statements are permitted, and whether
   ownership of all 22 tables + sequences actually transfers to `netpilot_admin`,
   must be **confirmed on the disposable Neon branch**.
4. **Pre-existing `public` privileges.** Neon may ship non-default privileges on
   `public`. Confirm the M5 matrix probes report *exactly* the intended grants
   and nothing extra (e.g. no lingering `PUBLIC` SELECT).
5. **SQL Editor identity.** The console editor connects only as the owner role,
   so per-role scripts (§5) must be run through `psql`/a client that can use the
   admin DSN — otherwise GRANTs execute as the wrong grantor.
6. **`sslmode`.** Neon always requires TLS. Confirm M1 enforcement
   (`NETPILOT_PG_SSLMODE`, default `require`) accepts the endpoint; if you choose
   `verify-full`, supply `NETPILOT_PG_SSLROOTCERT` (a CA path, contents never
   stored in the repo).

## Boundaries honored by this guide

Documentation only. No Neon connection, project/branch creation, or SQL
execution was performed to author it. No credentials requested or displayed.
`.env` unchanged; Demo Mode and both safety flags preserved; no production
access; no commit, push, tag, or deployment.
