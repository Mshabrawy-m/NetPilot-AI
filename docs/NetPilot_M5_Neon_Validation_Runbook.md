# NetPilot AI — M5: Disposable-Neon Role Validation Runbook (Stage A6b-1)

M5 turns the M3 privilege design and the M4 runtime role wiring into a **live,
executable** validation against a **throwaway** PostgreSQL branch, and closes
the five "requires a live server" limitations the M3 runbook deferred
(`L-M3-1` … `L-M3-5`, see
[`NetPilot_M3_PostgreSQL_Roles_Runbook.md`](NetPilot_M3_PostgreSQL_Roles_Runbook.md)).

> **This runbook is documentation only.** No Neon branch was provisioned, no
> database was connected to, and no SQL was executed to produce it. The live
> `validate` command below is **NOT RUN** until the operator grants explicit
> approval *and* provisions disposable-branch credentials into the environment.

## Scope

Validated, against a real server, all at once:

- **M1** — TLS (`sslmode`) and `connect_timeout` are actually applied.
- **M2** — serverless pool tuning and per-connection `statement_timeout`.
- **M3** — the role/GRANT matrix behaves as authored (positive *and* negative
  authorisation, sequence names, schema `USAGE` without `CREATE`).
- **M4** — each process connects with *its own* role URL and fails closed
  (never an admin fallback) when a role URL is missing.
- **Demo isolation** — `NETPILOT_DEMO_MODE=true` can never resolve to, or
  modify, the external PostgreSQL database.

## The harness

[`tools/validate_pg_roles_m5.py`](../tools/validate_pg_roles_m5.py) is the only
thing that touches a live server. It has two sub-commands:

| Command | Touches a DB? | Purpose |
|---|---|---|
| `selfcheck` | **No** | Offline logic audit: the expected grant matrix is derived from the M3 single source of truth [`database/pg_privileges.py`](../database/pg_privileges.py); denial detection + credential redaction behave; demo resolves to SQLite. Ships green today. |
| `validate --confirm-disposable` | **Yes** | Live probes against the branch the `NETPILOT_DB_URL_*` variables point at. Refuses (exit 2) *before any connection* unless safe. |

`validate` connects **through the real M4 machinery** (`connection.set_database_role`
→ `connection.get_engine`), so every probe exercises M1 TLS and the M2 pool
exactly as a running process would — it does not bypass them with a raw DSN.

### Safety gates (all must hold before a single connection is opened)

1. `--confirm-disposable` is passed — an explicit human assertion that the
   configured URLs are a **throwaway** branch, never production.
2. `NETPILOT_DEMO_MODE` is **not** `true`.
3. Role separation is **configured** (at least one `NETPILOT_DB_URL_*` set).

If any gate fails the tool prints `REFUSED:` and returns exit code 2 without
connecting. It also restores the process role to `None` in a `finally`, and it
never prints a connection string — failures are reported through
`error_signature()`, which strips `user:password@` and keeps only host +
SQLSTATE class.

## Operator pre-conditions (do these outside this repo, never in chat)

1. In Neon, **create a disposable branch** of a scratch project. Never use,
   fork-from, or point at the production branch for M5.
2. Create/attach authentication for the five roles out-of-band (Neon console or
   the secret manager). Roles were created **without** a password in M3
   (`sql/roles/10_create_roles.sql`).
3. Export the five role DSNs **into the process environment only**:
   `NETPILOT_DB_URL_ADMIN`, `NETPILOT_DB_URL_APP`, `NETPILOT_DB_URL_COLLECTOR`,
   `NETPILOT_DB_URL_RETENTION`, `NETPILOT_DB_URL_ROLLUP`.
   Do **not** put them in `.env`, do not commit them, do not paste them into
   chat or a report. Each DSN should already carry an `sslmode` at least as
   strong as `require` (M1 will enforce `NETPILOT_PG_SSLMODE` on top).

## Procedure

Work in an isolated shell (PowerShell shown; `;` not `&&`). Keep the three
safety flags off; the harness refuses to run while demo mode is on anyway.

```powershell
# 0. Offline audit first — must be green before you spend a live branch.
.venv\Scripts\python.exe tools\validate_pg_roles_m5.py selfcheck

# 1. Confirm the env is pointing at the disposable branch and separation is on.
#    (Presence check only — never print the DSN values.)
$env:NETPILOT_DEMO_MODE   # expect: not 'true' for the live step
# NETPILOT_DB_URL_ADMIN / _APP / _COLLECTOR / _RETENTION / _ROLLUP set?

# 2. Create the roles and grant baseline schema rights (M3 scripts 1, then run
#    schema init as admin, then scripts 3-8). See the M3 runbook "Execution
#    order" table for the exact, idempotent sequence:
#      10_create_roles.sql  ->  run_collector.py init-schema (ADMIN)
#      -> 20_ownership.sql -> 21_schema_privileges.sql
#      -> 30_grant_collector.sql -> 31_grant_app.sql
#      -> 32_grant_retention_rollup.sql -> 40_default_privileges.sql

# 3. Live validation against the throwaway branch.
.venv\Scripts\python.exe tools\validate_pg_roles_m5.py validate --confirm-disposable
```

`validate` prints one `[PASS]`/`[FAIL]` line per probe and a final
`<n>/<total> checks passed`; exit code is `0` only when every probe passes.

## M5 test catalogue (each maps to a harness probe)

Use this table as the acceptance record. Mark each row PASS / FAIL / NOT RUN.

| # | M5 requirement | Harness probe(s) | Closes |
|---|---|---|---|
| 1 | Schema init succeeds as `netpilot_admin` | step 2 `init-schema` (ADMIN role) | — |
| 2 | app / collector / retention / rollup each **connect** | `<role>: connects over TLS, SELECT allowed, statement_timeout=…` | L-M3-1 |
| 3 | app: SELECT + documented INSERT/UPDATE only; **DELETE denied** | `netpilot_app: table privileges match M3 matrix`; `netpilot_app: DELETE denial`; `… DDL denial` | L-M3-4 |
| 4 | collector: SELECT/INSERT/UPDATE; **DELETE + DDL denied**; real INSERT works | matrix probe; `netpilot_collector: DELETE denial`; `… DDL denial`; `collector: INSERT device row (+seq USAGE)` | L-M3-4 |
| 5 | retention: **DELETE only** on `metric_samples`; unrelated DELETE denied | `retention: DELETE on metric_samples permitted`; `netpilot_retention: unrelated-table DELETE denial`; matrix probe | L-M3-4 |
| 6 | rollup: `metric_samples` SELECT + `metric_daily_rollups` SELECT/INSERT/UPDATE; other writes denied | matrix probe; `netpilot_rollup: write-denied on metric_samples`; `… DDL denial` | L-M3-4 |
| 7 | Sequence names are `<table>_id_seq` | `SERIAL sequences named <table>_id_seq` | L-M3-5 |
| 8 | Sequence `USAGE` tracks INSERT targets | `<role>: sequence USAGE matches INSERT targets` | L-M3-5 |
| 9 | No runtime role holds schema `CREATE` (no DDL) | `<role>: USAGE on schema, no CREATE (no DDL)` | L-M3-3/4 |
| 10 | TLS active + `statement_timeout` applied on every role connection | TLS/SELECT probes report `ssl` true + `statement_timeout` | — |
| 11 | Representative collector write on disposable data | `collector: INSERT device row (+seq USAGE)` (idempotent `ON CONFLICT DO NOTHING`) | — |
| 12 | Demo Mode cannot connect to / modify external PG | `selfcheck`: `demo resolve ignores role URL -> stays SQLite`, `demo_isolation_violation flags demo->external`; plus `validate` REFUSES while demo mode is on | — |
| 13 | App admin actions (reset/purge) disabled under `netpilot_app` | M4 gating (`admin_actions_available`) — verify in the live UI on the branch | L-M3-2 |

### Introspection vs. enforcement

- **Introspection probes** ask PostgreSQL itself (`has_table_privilege`,
  `has_schema_privilege`, `has_sequence_privilege`) and compare the answer to
  the M3 matrix — a structural "the grants are exactly what we authored" check.
- **Enforcement probes** actually attempt the statement through a real role
  login and require a `42501` *permission denied* for the negative cases — a
  behavioural "the server refuses what it must refuse" check. Both must pass.

## Interpreting a failure

- A `[FAIL]` prints a credential-safe `error_signature` (host + SQLSTATE class,
  never a password). **Stop** and report the exact failing probe name.
- **Do not widen a GRANT to make a negative test pass.** If a legitimate
  operation is being denied, that is a matrix bug: fix
  `database/pg_privileges.py` + the SQL scripts, re-run the M3 byte-equality
  test, document the change, and obtain approval before re-validating.
- A `RoleConfigError` probe ("role configuration complete" FAIL) means a role
  URL is missing while separation is on — that is the M4 fail-closed contract
  working; fix the environment, not the code.

## Cleanup

The only row M5 writes is `devices.name = '__np_m5_probe__'` (idempotent). On a
Neon branch the simplest cleanup is to **delete the branch**. Do not attempt to
purge probe rows from anything that is not disposable.

## M5 boundaries honored (this runbook)

No Neon provisioning or connection performed to author it; no SQL executed; no
credentials created, rotated, or printed; `.env` unchanged; `NETPILOT_DEMO_MODE=true`,
`NETPILOT_CHANGE_LOOP_ENABLED=false`, `NETPILOT_LAB_EXECUTION_ENABLED=false`
preserved; no runner scheduled; production branches untouched; no
commit/push/tag/deploy.
