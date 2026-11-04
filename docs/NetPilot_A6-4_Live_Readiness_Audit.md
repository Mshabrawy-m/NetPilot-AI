# NetPilot AI — Stage A6-4 Live Readiness Audit

**Scope.** A factual, code-grounded assessment of what it takes to move NetPilot
from the current labelled-Demo posture (`NETPILOT_DEMO_MODE=true`, local SQLite)
to a Live deployment on a managed PostgreSQL/Neon backend.

**Method.** Derived entirely from inspecting this repository
(`config.py`, `database/connection.py`, `database/models.py`,
`database/repository.py`, `tools/run_collector.py`, `tools/run_retention.py`,
`tools/run_rollup.py`, `Dockerfile`, `docker-compose.yml`, `requirements.txt`,
`packages.txt`, `.streamlit/config.toml`, `.env.example`, `services/app_state.py`).
**No external service was connected to and no credential was requested, printed,
or stored.** Statements about an external provider (Neon, PostgreSQL) are the
provider's documented requirements applied to what this codebase does and does
not do; they are not the result of a live probe.

Legend: **[OK]** already handled in code · **[GAP]** must be added/configured
before Live · **[OP]** an operator task, not a code task.

---

## 1. PostgreSQL compatibility

- **[OK] Dual-backend ORM.** SQLAlchemy 2.0 across the whole stack; the same
  code runs on SQLite (default) and PostgreSQL. Only the connection URL changes
  (`database/connection.py`).
- **[OK] Naive-UTC timestamp convention.** Every timestamp is naive UTC
  (`config.utc_now()` / `db_utcnow()`), columns are `TIMESTAMP` without a time
  zone. No `timestamptz`/`AT TIME ZONE` is relied upon, so SQLite and PostgreSQL
  agree on stored values.
- **[OK] Dialect-aware date bucketing.** Daily-rollup bucketing uses a
  dialect-specific expression (`repository._rollup_bucket_expr`) rather than a
  generic `CAST(... AS DATE)`, because a portable cast produced wrong buckets on
  SQLite. PostgreSQL takes the `DATE(...)` branch; SQLite is exercised in the
  offline suite.
- **[OK] Version-independent uniqueness.** Rollup uniqueness uses a
  non-null surrogate `interface_key` (empty string for device-level rows) rather
  than `NULLS NOT DISTINCT`, so it behaves identically on PostgreSQL 13–16 with
  nullable interface names.
- **[OK] Additive-only migrations.** `init_db()` runs `create_all` plus
  `_ensure_additive_columns`, which adds only **nullable** columns and refuses
  on a `NOT NULL` column (an explicit data migration would be needed). There is
  no Alembic; the strategy is "create tables / add nullable columns," nothing
  destructive. **[GAP]** No down-migrations and no migration versioning — a
  schema rollback is manual.
- **[GAP] Real-database validation has never run here.** The entire offline test
  suite (including A6-1/A6-2/A6-3/A6-4) executes on SQLite. The PostgreSQL
  branches of the dialect-aware SQL are code-reviewed but not end-to-end tested
  against a live engine in this environment.

## 2. Required database driver and TLS configuration

- **Driver.** The PostgreSQL URL scheme used throughout is
  `postgresql+psycopg2://` (`.env.example`, `docker-compose.yml`, tests), i.e.
  **psycopg2**, not psycopg3.
  - **[OP]** For local/dev on SQLite no driver is needed — it is deliberately
    commented out in `requirements.txt`.
  - **[OK]** The Docker image installs `psycopg2-binary>=2.9,<3` in the
    `Dockerfile` (not in `requirements.txt`, precisely so the SQLite dev machine
    stays driver-free).
  - **[GAP]** A **Streamlit Community Cloud** deploy that points at
    PostgreSQL/Neon must add a psycopg driver to `requirements.txt` (Cloud does
    not build the Docker image), and it must resolve a wheel for the Cloud
    interpreter.
- **TLS.** **[GAP]** No TLS is configured anywhere in the codebase.
  - `_build_engine()` adds no `connect_args` for PostgreSQL (TLS would have to
    come from `?sslmode=` DSN query parameters, or be added to `connect_args`).
  - The compose URL (`postgresql+psycopg2://netpilot:netpilot@db:5432/netpilot`)
    is plaintext over a private Docker network — acceptable for the local lab,
    **not** for a hosted provider.
  - Neon and most managed Postgres tiers **require** TLS. Enabling Live means
    appending `?sslmode=require` (encryption) or, for full verification,
    `sslmode=verify-full` with a configured CA (e.g. `sslrootcert`). The repo has
    no CA-handling today. **[OP]** Provide the CA and set the DSN accordingly.

## 3. Neon connection prerequisites (no account was provisioned)

Neon appears **nowhere** in the codebase — there is no Neon-specific
integration, driver, or DSN. Applying a managed-serverless provider's
documented requirements to this code:

- **[OP]** Provision a Neon project/endpoint and a role; put the resulting
  `DATABASE_URL` **only** in the environment / Streamlit Secrets (never in a
  committed file). `config.py` reads `DATABASE_URL` from the environment and
  hard-codes no secret.
- **[GAP] TLS** must be enabled (see §2) — Neon requires it; the current default
  DSN does not request it.
- **[OP] Pooling / idle timeout.** Neon offers a connection pooler and
  autosuspends idle compute. `get_engine()` sets `pool_pre_ping=True` (a dead
  serverless connection is detected before use) but **not** `pool_recycle`,
  `pool_size`, or `max_overflow`. The one-shot runners (`collect`, `rollup`,
  `retention`) open a connection, do one cycle, and exit, so they are naturally
  autosuspend-friendly. **[GAP]** The long-lived Streamlit/API reader should set
  a bounded `pool_recycle` (below Neon's idle timeout) and a small pool to avoid
  holding suspended connections.
- **[OK] Demo guard still applies.** Pointing `DATABASE_URL` at a shared external
  engine while `NETPILOT_DEMO_MODE=true` raises
  `demo_isolation_violation()` *before* any connection/DDL, so a misconfigured
  demo run can never purge real Neon telemetry. A Neon deploy must therefore run
  with `NETPILOT_DEMO_MODE=false`.

## 4. Database role permissions (least privilege)

These roles are the project's **documented convention** —
**[GAP] the repository ships no SQL `GRANT`/`CREATE ROLE` artifacts**, so
provisioning them is a manual schema-administrator task. Mapping the privilege
each code path actually needs:

| Role | Granted on | Used by | Evidence |
|---|---|---|---|
| **Admin / schema owner** | DDL (`CREATE`/`ALTER`), owner of the schema | `run_collector.py init-schema` → `init_db()` | `run_collector.py:232-246`, `connection.init_db` |
| **Collector** | `SELECT, INSERT, UPDATE` on `metric_samples` (+ inventory tables), sequence `USAGE`; **no DDL** | `run_collector.py collect` → `collect_once()` | `run_collector.py:26-38`, `check_configuration` |
| **Reader (hosted app)** | `SELECT` across read paths **plus `INSERT` on the forecast/alert/change tables** | Streamlit/API reads; `run_forecast_comparison(persist=True)` writes `forecast_runs`/`forecast_predictions` | `connection.require_schema`, `forecast_service` persist |
| **Retention** | `SELECT` + `DELETE` on `metric_samples` (scoped, batched) | `run_retention.py` (never scheduled here) | A6-1, `run_retention.py` |
| **Rollup** | `SELECT` on `metric_samples`; `SELECT, INSERT, UPDATE` on `metric_daily_rollups`; **no DDL** | `run_rollup.py` → `upsert_daily_rollups()` | A6-2, `run_rollup.py` |

- **[GAP] The "reader" is not purely read-only.** Forecasting **persists**
  results, so a strictly `SELECT`-only role cannot serve the Forecasting page
  with `persist=True`. Either grant the app role `INSERT` on the forecast tables
  (and on alert-acknowledgement / change-request tables if those features are
  used), or run the reader against a replica with persistence disabled. The
  `NETPILOT_MANAGE_SCHEMA=false` path guarantees the app **never runs DDL**, but
  it does not make its writes disappear.

## 5. Schema initialization vs runtime read/write responsibilities

- **Initialization (DDL owner only).**
  `python tools/run_collector.py init-schema` → `init_db()` creates every table
  (including `metric_daily_rollups`) and adds missing nullable columns. The
  rollup and retention runners **perform no DDL** — they refuse and instruct the
  operator to run `init-schema` first if the schema is missing.
- **Runtime responsibilities.**
  - Collector: writes `metric_samples` (and device/interface inventory upserts).
  - Rollup runner: reads raw, upserts completed UTC days into
    `metric_daily_rollups`.
  - Retention runner: batched `DELETE` of expired **live** raw samples (never
    scheduled by the app; operational only).
  - Streamlit/API app: **reads** samples + rollups + inventory; **writes**
    forecast runs/predictions (and alert/change tables when used). With
    `NETPILOT_MANAGE_SCHEMA=true` (dev/demo default) it may run `init_db()`;
    with it **false** (hosted reader) it only reflects the catalog
    (`missing_schema_objects`, a `SELECT`-only reflection) and raises an
    actionable message if the schema is not already present.
- **[OK] Isolation-before-connection is the deepest guard.** Both `init_db()`
  and `require_schema()` call `demo_isolation_violation()` first, so no
  connection is opened for a demo config aimed at a shared database.

## 6. Collector deployment prerequisites

- **[OP] Host.** A Linux host with Docker + Containerlab and LAN reachability to
  the monitored devices. `tools/run_collector.py` imports **no** Streamlit/FastAPI
  (the entire write path is Streamlit-free) so it is safe to drive from cron or a
  systemd timer. Streamlit Community Cloud never runs the collector — it only
  reads.
- **[OP] Explicit live config.** `collect` refuses unless `NETPILOT_DEMO_MODE=false`
  **and** a `postgresql` `DATABASE_URL` are set, and refuses when
  `NETPILOT_CHANGE_LOOP_ENABLED` or `NETPILOT_LAB_EXECUTION_ENABLED` is true. It
  is read-only probing; it never changes device configuration.
- **[OK] `ping` binary.** `packages.txt` (Cloud) and the `Dockerfile`
  `apt-get install iputils-ping` both ship `iputils-ping`, which the connectivity
  probes need (slim images otherwise lack it).
- **[OP] Single-instance lock is per-host only** (`fcntl`/`msvcrt`, lock file
  `netpilot-collector.lock`). It prevents a double-fire on one machine; it does
  **not** coordinate two collectors on different hosts — run exactly one
  collector per lab across the fleet.
- **[OP] Scheduling.** The rollup job (`run_rollup.py`) must run on the collector
  host's own timer **before** retention is enabled, so completed days are
  aggregated before raw rows age out. Nothing here schedules retention.

## 7. Streamlit Community Cloud environment requirements

- **[OK] Interpreter / wheels.** `requirements.txt` is intentionally **unpinned**
  so Cloud can resolve a wheel for whatever CPython it defaults to (recently
  3.14). `statsmodels` is deliberately not used (compiled Kalman/CFA extensions
  are blocked by Windows App Control on the dev box); damped Holt is pure NumPy
  in `analytics/forecasting.py`.
- **[GAP] No PostgreSQL driver by default.** Cloud installs `requirements.txt`,
  which does **not** include psycopg2 — a live Cloud deploy must add it (see §2).
- **[OK] Secrets-as-env.** `python-dotenv` is optional; Cloud promotes Secrets to
  environment variables before the script runs, so no `.env` is needed. All
  config comes from env (`config.get_settings()`).
- **[OK] Read-only posture.** Cloud deploy should set
  `NETPILOT_MANAGE_SCHEMA=false` (no DDL from the reader) and
  `NETPILOT_DEMO_MODE=false` when pointing at shared storage; the isolation guard
  enforces the pairing.
- **[OK] File watcher off.** `.streamlit/config.toml` sets
  `server.fileWatcherType = "none"`, which avoids the `sys.modules`-eviction
  crash that broke ORM/dataclass rebuilds on deploy. Theme keys stay within the
  set the pinned Streamlit accepts (no `warningColor`/`errorColor`/`successColor`,
  which newer 1.6x rejects).
- **[OK] `packages.txt`.** Provides the apt dependency the probes need.
- **[OP] Change-loop / lab execution stay off** on Cloud
  (`NETPILOT_CHANGE_LOOP_ENABLED=false`, `NETPILOT_LAB_EXECUTION_ENABLED=false`)
  — Cloud has no Docker socket and must remain inert.

## 8. Remaining Demo-to-Live blockers (consolidated)

1. **TLS is not wired.** Managed Postgres/Neon require `sslmode` (and CA for
   verification); the engine adds none. **[GAP §2]**
2. **No PostgreSQL driver in `requirements.txt`** for a Cloud deploy (only the
   Docker image has it). **[GAP §2]**
3. **No serverless pool tuning** (`pool_recycle`/`pool_size`) for the long-lived
   reader against Neon idle timeouts. **[GAP §3]**
4. **Roles/GRANTs exist only as convention** — no SQL artifacts; and the app
   "reader" needs `INSERT` on the forecast tables (it is not purely read-only).
   **[GAP §4]**
5. **Never validated against a real PostgreSQL/Neon engine** — all offline tests
   run on SQLite; the PG SQL branches are code-reviewed, not executed here.
   **[GAP §1]**
6. **Retention not enabled/scheduled** and must be gated behind a working rollup
   job so raw history is not lost before it is aggregated. **[OP §6]**
7. **Docker/Containerlab unavailable on this host**, so live collection and the
   rollup/retention runners have not been exercised end-to-end here. **[OP]**
8. **UI inventory coverage is forecasting-only** (A6-4): the Metrics/History
   page still lists raw series only, so a rollup-only series is discoverable for
   forecasting but not yet everywhere.

---

**Bottom line.** The *code* paths for a PostgreSQL Live deployment (dual backend,
naive-UTC, dialect-aware buckets, surrogate keys, additive migrations,
isolation-before-connect, schema-vs-runtime separation, one-shot runners) are in
place and Demo-safe. The *remaining* work is operational and small but real:
enable TLS, ship the driver, tune the serverless pool, provision the five
least-privilege roles (with forecast `INSERT` for the reader), and validate
end-to-end against a real managed-Postgres instance. None of that was performed
here, by design.
