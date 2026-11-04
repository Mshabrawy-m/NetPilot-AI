# NetPilot AI — Neon Persistent Production Database Provisioning Checklist

Scope: provision a **persistent** Neon PostgreSQL branch for the live telemetry
plane, with the five least-privilege roles, TLS enforced, and Demo/Live isolation
preserved. This is a checklist for the **operator to execute**. It creates no
branch and runs nothing here — every command below is gated on operator approval
and is run against the **new production branch only**.

> **Do NOT reuse the M5 disposable branch.** `m5-disposable`
> (`br-icy-breeze-bdxgftqg`, endpoint `ep-aged-sky-bdmdoqzq`) expires
> **2026-11-05** and is validation-only. This checklist targets a separate,
> persistent branch.
>
> **Do NOT run `tools/validate_pg_roles_m5.py` against production.** That harness
> rotates role passwords and performs live writes to prove the matrix; it is
> disposable-only by design. Production verification here is **read-only**.

---

## 0. Preconditions and operator-supplied values (placeholders only)

Fill from your vault / secret manager. Never paste any of these into a commit,
PR, shell history as an argument, or this file.

| Value | Where used | Notes |
|---|---|---|
| Neon project / **persistent** branch name + `region_id` | branch create | not the disposable branch |
| Production DB name | all DSNs | e.g. `netpilot` |
| Branch **owner** role (Neon `neondb_owner`-equivalent) | role/ownership SQL | Neon grants `CREATE ROLE`/ownership to the branch owner |
| 5 role passwords (collector/app/retention/rollup/admin) | `ALTER ROLE … PASSWORD` | CSPRNG-generated; see §3 |
| Endpoint DSNs, one per role | `NETPILOT_DB_URL_*` | always `?sslmode=require` |
| `statement_timeout` per role (ms) | role defaults | admin/rollup/forecast may need `0` |

Reused names (do not rename): roles `netpilot_admin`, `netpilot_collector`,
`netpilot_app`, `netpilot_retention`, `netpilot_rollup`
(`database/roles.py` `ROLE_URL_ENV` → `NETPILOT_DB_URL_{ADMIN,COLLECTOR,APP,RETENTION,ROLLUP}`);
privilege matrix source of truth `database/pg_privileges.py`; approved SQL under
`database/sql/roles/`.

---

## 0b. Linux host preparation (collector plane)

Operate the collector on a **dedicated** Linux host that also runs the
Containerlab/FRR lab (the collector reaches nodes via `docker exec`, so the lab
must be co-located). Keep this consistent with the repository layout.

- [ ] Create the unprivileged service account (nologin, no home on disk):
      `sudo useradd -r -s /usr/sbin/nologin netpilot`
- [ ] Repository checkout at **`/opt/netpilot`**, owned `root:netpilot`, group
      read/execute only (the unit mounts it read-only via `ProtectSystem=strict`):
      `sudo chown -R root:netpilot /opt/netpilot && sudo chmod -R g-w,o-rwx /opt/netpilot`
- [ ] Virtualenv the runner uses, matching the unit's `ExecStart` path:
      `/opt/netpilot/.venv/bin/python` (create with `python3.12 -m venv`, then
      `pip install -r requirements.txt` — psycopg2-binary ships there).
- [ ] System dependencies: `docker.io`, Containerlab (`ghcr.io/srlabs/clab` /
      the clab installer), `iputils-ping` (the collector's ICMP probe + the
      FRR nodes' ping), and Python 3.12. The lab pulls `frrouting/frr:v8.4.4`
      as a container image; nothing here installs FRR on the host.
- [ ] Lab topology referenced by the collector env: `NETPILOT_LAB_TOPOLOGY=lab/topology.clab.yml`
      (deploy it with `containerlab deploy -t lab/topology.clab.yml`).
- [ ] Runtime secrets dir, OUTSIDE the repo: `/etc/netpilot/collector.env` mode
      **0600**, `root:netpilot` (systemd reads it as PID1; the service process
      never needs to open the file itself).
- [ ] Docker group / privilege note: the collector user needs the `docker` group
      to `docker exec` into lab nodes. **`docker` membership is root-equivalent**
      — a member controls the daemon and can mount the host from a privileged
      container, so the systemd sandbox is defence-in-depth only. **Require a
      dedicated host** for the lab + collector and **prohibit co-hosting untrusted
      workloads** or exposing the daemon socket beyond this service.
- [ ] Do NOT put `NETPILOT_DB_URL_ADMIN` in `collector.env` (the timer must never
      run DDL); supply the admin DSN only for the one-shot `init-schema` (§5).

---

## 1. Create the persistent branch (operator, Neon CLI/console)

- [ ] Create a **persistent production branch** (protected; not auto-expiring).
- [ ] Confirm the direct/pooled endpoint and that it is PG 16+ / Neon-supported.
- [ ] Record the endpoint host and DB name for §3–§5 (no credentials recorded).
- [ ] Enable **Neon point-in-time backup / PITR** (retention per policy).

Identity check before any later mutation (mirrors the M5 discipline):
- [ ] Re-verify the target is the new branch, **not** the disposable one, by
      comparing project id + branch id + endpoint against this checklist's record.

---

## 2. Provisioning connection identity on Neon

Neon has **no customer superuser**; the branch owner (a `neondb_owner`-like
role) is the identity that holds `CREATE ROLE` and owns schema `public`.
- [ ] Provision **roles + ownership** as the **branch owner** identity, and
      **schema/table privileges** as **`netpilot_admin`** (post `20_ownership`),
      via `tools/run_sql_as_role.py --dsn-env <ROLE_URL_ENVVAR>` (see §4-§5).
- [ ] Grant object ownership to `netpilot_admin` **before** creating tables, so
      `netpilot_admin` gains `CREATE` on schema `public` (M5 finding: `netpilot_admin`
      has no `CREATE` on `public` until `20_ownership.sql` runs).
- [ ] `20_ownership.sql` cannot run atomically under a single non-superuser
      identity — run it as the **current owner of `public`** (the branch owner).

---

## 3. Create roles and set strong passwords

`tools/run_sql_as_role.py` runs **only** these seven allowlisted files, reads the
DSN from a **process-scoped env var name** (`--dsn-env`, never a value argument),
**never** prints a secret, **upgrades** any `sslmode` weaker than `require`, and
**stops on the first error**.

- [ ] Run `10_create_roles.sql` as the branch owner.
      `python tools/run_sql_as_role.py --dsn-env NP_DSN_OWNER database/sql/roles/10_create_roles.sql`
- [ ] Set each role password **out-of-band** (SQL files carry no secrets). Use the
      bound-parameter path the M5 harness proved, run as the owner, with a
      CSPRNG-generated password per role, then discard the value into the vault:
      `ALTER ROLE <role> PASSWORD <generated>;` (never echoed, never committed).
      - [ ] `netpilot_admin` - [ ] `netpilot_collector` - [ ] `netpilot_app`
      - [ ] `netpilot_retention` - [ ] `netpilot_rollup`
- [ ] Confirm `ROLECT == 5` (the five roles exist) via a read-only count.

---

## 4. Ownership + schema privileges (BEFORE schema creation)

Execution identity matters: `20_ownership.sql` runs as the **current owner of
`public`** (the Neon branch owner); once it has transferred ownership, the
remaining pre-schema scripts run as **`netpilot_admin`** (the new schema owner).
Each `run_sql_as_role.py` call takes that role's DSN via `--dsn-env <NAME>` (env
variable NAME only, never the value; TLS upgraded to `require`; stops on first
error). Scripts `30/31/32` are deferred to §5 because they reference tables that
do not exist until `init-schema` runs.

- [ ] `20_ownership.sql` — **branch owner**: transfer `public` ownership to
      `netpilot_admin`.
      `python tools/run_sql_as_role.py --dsn-env NP_DSN_OWNER database/sql/roles/20_ownership.sql`
- [ ] `21_schema_privileges.sql` — **netpilot_admin**: `USAGE` on `public` for the
      four runtime roles. **USAGE only — deliberately NO `CREATE`** (no runtime
      role may issue DDL; see `21_schema_privileges.sql`).
- [ ] `40_default_privileges.sql` — **netpilot_admin**: default `SELECT` on future
      tables to `netpilot_app`. Runs BEFORE `init-schema` so created tables
      inherit it. Intentionally NO default INSERT/UPDATE/DELETE and NO default
      sequence privilege (a new writable table must be granted explicitly).

TLS/pool/statement-timeout notes:
- [ ] Every DSN uses `?sslmode=require` (stronger operator values are left
      authoritative; `verify-ca`/`verify-full` require a CA path or they fail closed).
- [ ] Set per-role `statement_timeout` to match `NETPILOT_PG_STATEMENT_TIMEOUT_MS`
      (collector writes small; admin DDL and rollup/forecast reads may need `0`).

---

## 5. Create the schema, THEN grant per-table / per-sequence privileges

Scripts `30/31/32` are `GRANT … ON TABLE public.<t>` / `GRANT USAGE ON SEQUENCE
…` and therefore MUST run **after** `init-schema` creates those objects, and as
**`netpilot_admin`** (the table owner). Running them earlier fails on a missing
relation and `run_sql_as_role.py` stops on the first error.

- [ ] Schema DDL **once** as `netpilot_admin`:
      `NETPILOT_DB_URL_ADMIN=<from vault> NETPILOT_MANAGE_SCHEMA=true python tools/run_collector.py init-schema`
      (uses `ROLE_ADMIN`; this is the only step that touches DDL).
- [ ] Confirm tables/sequences exist, incl. `metric_samples`, `metric_daily_rollups`,
      `devices`, `interfaces`, `links`, alert/incident/change tables. `TBLCT` count matches expectation.
- [ ] `30_grant_collector.sql` — **netpilot_admin**: SELECT/INSERT/UPDATE on
      inventory + `metric_samples` (+ sequence USAGE); **no DELETE, no DDL**.
- [ ] `31_grant_app.sql` — **netpilot_admin**: reader SELECT + the limited
      INSERT/UPDATE the UI persists; **no DELETE, no DDL**.
- [ ] `32_grant_retention_rollup.sql` — **netpilot_admin**: retention SELECT+DELETE
      on `metric_samples`; rollup SELECT samples + SELECT/INSERT/UPDATE rollups
      (+ `metric_daily_rollups_id_seq` USAGE).
- [ ] **After** the grants, set `NETPILOT_MANAGE_SCHEMA=false` for every runtime
      plane. The collector/app/retention/rollup DSNs never carry DDL, role
      separation stays active, and **no runtime role holds `CREATE`**.

---

## 6. Read-only verification against production (no mutations)

Use the **dedicated read-only production validator** (`tools/validate_pg_roles_prod.py`,
Phase 2A). It opens every connection with `SET TRANSACTION READ ONLY`, runs only
catalog `SELECT`/`SHOW` reads, performs **no DDL/DML and no password rotation**,
reuses the M3 privilege matrix, and **refuses to target the M5 disposable branch**.
Do **not** use `validate_pg_roles_m5.py validate` here — that harness writes probe
rows and rotates passwords and is disposable-only by design.

- [ ] Offline guarantee check (opens no connection):
      `python tools/validate_pg_roles_prod.py selfcheck` → expect `8/8 checks passed`.
- [ ] Live read-only validation against the persistent branch:
      `NETPILOT_DEMO_MODE=false` with all `NETPILOT_DB_URL_*` bound to the
      **production** endpoint, then
      `python tools/validate_pg_roles_prod.py validate --confirm-production`
      → expect `N/N checks passed` (exit 0). It asserts: five roles present;
      `USAGE` on `public` and **no `CREATE`** for every runtime role; table +
      sequence privileges **match `database/pg_privileges.py`**; every
      table/sequence owned by `netpilot_admin`; default privileges are
      `SELECT`→`netpilot_app` on future tables **only** (no write/sequence
      defaults); every model foreign key exists; all model tables present; and
      per-role TLS + timeouts.
- [ ] Confirm the gate refusals behave (should each exit `2`, no connection):
      missing `--confirm-production`; `NETPILOT_DEMO_MODE=true`; role separation
      unset; or a `NETPILOT_DB_URL_*` host matching a known disposable marker
      (`br-icy-breeze-bdxgftqg` / `ep-aged-sky-bdmdoqzq`).
- [ ] Confirm TLS client-side on the production endpoint: `sslmode=require`
      succeeds **and** `sslmode=disable` is refused (Neon terminates TLS at its
      proxy, so `pg_stat_ssl.ssl` may read false — verify via connect modes, not
      the view). The validator does this per role.
- [ ] Confirm **Demo/Live isolation**: `NETPILOT_DEMO_MODE=true` + an external DB
      raises `demo_isolation_violation()` before any connection.

---

## 7. Retention / rollup policy (safe ordering preserved)

- [ ] Enable **daily rollups** first: schedule `python tools/run_rollup.py`
      (aggregates completed UTC days, `NETPILOT_ROLLUP_LOOKBACK_DAYS`, distinct lock,
      rollup role). It performs no DDL and no deletion.
- [ ] Keep **retention disabled** until rollups are validated: `run_retention.py`
      refuses unless `NETPILOT_RETENTION_RAW_DAYS > 0`; there is no `--force`.
- [ ] After rollups are proven to survive raw shrink, enable retention with a
      positive `NETPILOT_RETENTION_RAW_DAYS`; deletion is hard-scoped to
      `data_source='live' AND collected_at < cutoff`.

---

## 8. Provision → verify → cut over (no live collection started here)

- [ ] Create collector/app/retention/rollup DSNs in the vault, each `sslmode=require`.
- [ ] Populate `/etc/netpilot/collector.env` (0600, root:netpilot) from
      `deploy/env/netpilot-collector.env.example` — collector role **only**, safety
      flags `false`, `NETPILOT_DB_URL_ADMIN` **absent**.
- [ ] Populate Streamlit Cloud Secrets from
      `deploy/env/streamlit-cloud-secrets.example.toml` — app-reader role **only**.
- [ ] `NETPILOT_CHANGE_LOOP_ENABLED=false` and `NETPILOT_LAB_EXECUTION_ENABLED=false`
      on every plane.
- [ ] Only after this checklist is signed off: deploy the lab, run one
      `run_collector.py collect`, and confirm the first `data_source='live'` row.

---

## Sign-off matrix (operator completes after §1–§8)

| Item | Evidence | Result |
|---|---|---|
| Persistent branch created, not disposable | project+branch+endpoint id | ☐ PASS |
| 5 roles exist, strong CSPRNG passwords set, none echoed | `ROLECT=5`, vault entries | ☐ PASS |
| Ownership + privileges + defaults applied in order | run_sql_as_role exit 0 ×7 | ☐ PASS |
| Schema created once as admin | init-schema exit 0, `TBLCT` match | ☐ PASS |
| Zero privilege mismatches (read-only) | matrix compare | ☐ PASS |
| TLS require ok / disable refused (client-side) | connect-mode probe | ☐ PASS |
| Demo/Live isolation enforced | violation raised on demo+external | ☐ PASS |
| Rollups enabled, retention gated | `run_rollup` schedule, retention off | ☐ PASS |
| Safety flags false everywhere | env inspection | ☐ PASS |

**Blockers requiring operator decision:** approval to create the persistent Neon
branch and set role passwords (this checklist does neither); the vault/secret
manager for the DSNs; confirmation of the per-role `statement_timeout` values.
