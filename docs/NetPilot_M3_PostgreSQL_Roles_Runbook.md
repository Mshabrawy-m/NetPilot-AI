# NetPilot AI — M3: PostgreSQL Roles & GRANT Runbook (Stage A6b-1)

Least-privilege role and privilege design for the live PostgreSQL backend.
Every grant is **derived from actual code** (the repository write functions and
the service/collector/runner call sites), never assumed. The authoritative data
is [`database/pg_privileges.py`](../database/pg_privileges.py); the SQL under
[`database/sql/roles/`](../database/sql/roles/) is generated from it and is
asserted byte-for-byte equal by [`tests/test_pg_roles_m3.py`](../tests/test_pg_roles_m3.py).

> No database was connected to, provisioned, or modified to produce M3. These
> scripts are authored and statically validated offline only.

## Roles

| Role | Purpose | Authenticates? | DELETE? | DDL? |
|---|---|---|---|---|
| `netpilot_admin` | Schema owner; runs `init_db`/additive migrations | yes (owner) | yes (owner) | yes (owner) |
| `netpilot_collector` | Standalone one-shot collector runner | yes | **no** | **no** |
| `netpilot_app` | Hosted Streamlit UI (reader + analytics writer) | yes | **no** | **no** |
| `netpilot_retention` | Retention runner (expire old live samples) | yes | yes (`metric_samples` only) | **no** |
| `netpilot_rollup` | Daily-rollup runner | yes | **no** | **no** |

Roles are created **without a password**. Attach authentication out-of-band
(Neon console role creation, or `ALTER ROLE … PASSWORD` sourced from the secret
manager). No credential ever appears in this repository.

## Execution order

Run as the designated administrative role, in this order. All scripts are
idempotent and safe to re-run.

| # | Script | Run as | Precondition |
|---|---|---|---|
| 1 | `sql/roles/10_create_roles.sql` | superuser / `neon_superuser` | none |
| 2 | *(create schema)* — run `tools/run_collector.py init-schema` **as `netpilot_admin`**, or apply migrations as admin | `netpilot_admin` | step 1 |
| 3 | `sql/roles/20_ownership.sql` | superuser / `neon_superuser` | schema exists (step 2) |
| 4 | `sql/roles/21_schema_privileges.sql` | `netpilot_admin` (or superuser) | steps 1, 3 |
| 5 | `sql/roles/30_grant_collector.sql` | `netpilot_admin` (or superuser) | steps 3, 4 |
| 6 | `sql/roles/31_grant_app.sql` | `netpilot_admin` (or superuser) | steps 3, 4 |
| 7 | `sql/roles/32_grant_retention_rollup.sql` | `netpilot_admin` (or superuser) | steps 3, 4 |
| 8 | `sql/roles/40_default_privileges.sql` | `netpilot_admin` | step 3 (admin owns objects) |

Ownership must be transferred (step 3) **after** the tables exist and
**before** the runtime GRANTs, so `netpilot_admin` — not a runtime role — is the
grantor/owner. `ALTER DEFAULT PRIVILEGES FOR ROLE netpilot_admin` (step 8) only
affects tables admin creates from that point forward.

## Privilege matrix (role → table → operations → code path)

Sequences: any role with `INSERT` on a table also holds `USAGE` on that table's
`SERIAL` sequence `<table>_id_seq`. This is implicit in every INSERT row below.

### `netpilot_collector` — `tools/run_collector.py` → `network.metrics_collector.collect_once`
| Table | Ops | Code path |
|---|---|---|
| `devices` | SELECT, INSERT, UPDATE | `upsert_device`, `set_device_status` |
| `interfaces` | SELECT, INSERT, UPDATE | `upsert_interface` |
| `links` | SELECT, INSERT, UPDATE | `upsert_link` |
| `metric_samples` | SELECT, INSERT | `add_metric_sample` |
| `collection_runs` | SELECT, INSERT, UPDATE | `start_collection_run`, `finish_collection_run` |
| `incidents` | SELECT, INSERT, UPDATE | `open_incident`, `resolve_open_incidents` |

### `netpilot_app` — Streamlit UI + services
| Table | Ops | Code path |
|---|---|---|
| `devices`, `interfaces`, `links`, `metric_samples`, `collection_runs`, `incidents`, `metric_daily_rollups` | SELECT | topology/metrics/history/rollup reads (read-only in live UI) |
| `forecast_runs` | SELECT, INSERT | `forecast_service` → `add_forecast_run` |
| `forecast_predictions` | SELECT, INSERT, UPDATE | `add_forecast_predictions`; `backfill_actuals` (UPDATE actuals) |
| `anomaly_events` | SELECT, INSERT, UPDATE | `create_anomaly_event`; `update_/resolve_anomaly_event` |
| `ai_runs` | SELECT, INSERT, UPDATE | `create_ai_run`; `update_ai_run`, `set_human_review` |
| `ai_status_transitions`, `ai_evidence` | SELECT, INSERT | `add_status_transition`, `add_ai_evidence` |
| `alert_rules` | SELECT, INSERT, UPDATE | `create_alert_rule`, `set_alert_rule_enabled` |
| `alert_events` | SELECT, INSERT, UPDATE | `record_alert_firing`, `acknowledge_/resolve_alert_event` |
| `change_requests` | SELECT, INSERT, UPDATE | `create_change_request`, `update_change_request` |
| `change_audit_events` | SELECT, INSERT | `add_change_audit_event` |
| `users` | SELECT, INSERT, UPDATE | `create_user`, `set_last_login`, `set_user_active` |
| `lab_experiments` | SELECT, INSERT | `add_lab_experiment` |
| `agentic_analysis_runs` | SELECT, INSERT | `add_agentic_analysis_run` |
| `digital_twin_snapshot_bindings` | SELECT, INSERT | `add_dt_snapshot_binding` |
| `connectivity_tests` | SELECT, INSERT | `add_connectivity_test` (Connectivity page) |

### `netpilot_retention` — `tools/run_retention.py` → `delete_expired_live_samples`
| Table | Ops |
|---|---|
| `metric_samples` | SELECT, DELETE |

### `netpilot_rollup` — `tools/run_rollup.py` → `upsert_daily_rollups`
| Table | Ops |
|---|---|
| `metric_samples` | SELECT |
| `metric_daily_rollups` | SELECT, INSERT, UPDATE (+ `USAGE` on `metric_daily_rollups_id_seq`) |

## Foreign keys & identity

- FK columns: `interfaces.device_id→devices`, `forecast_predictions.run_id→forecast_runs`,
  `ai_status_transitions.run_id`/`ai_evidence.run_id→ai_runs`,
  `alert_events.rule_id→alert_rules`, `change_audit_events.request_id→change_requests`.
  `metric_samples`/`links`/`anomaly_events`/`forecast_runs` key on device **names**
  (no FK). PostgreSQL enforces FK constraints as the **table owner**, so runtime
  INSERT roles need **no `REFERENCES` grant** — only the SELECT/INSERT they hold.
- All 22 tables use an `Integer` autoincrement PK → PostgreSQL `SERIAL`
  (sequence `<table>_id_seq`; an `IDENTITY` column uses the same name). `USAGE`
  on the sequence is granted exactly to the roles that INSERT into its table.

## Why nothing broader is granted

- **No `ALL PRIVILEGES`/`GRANT ALL` to runtime roles.** Only `netpilot_admin`
  gets schema-wide rights, via ownership.
- **No DDL to runtime roles**: no `CREATE` on the schema and no `GRANT … CREATE`
  to any runtime role; schema changes belong to the owner.
- **`DELETE` is confined to `netpilot_retention`** (`metric_samples` only). The
  app and collector never delete (idempotent upserts; row purge is admin/retention).
- **Default privileges** (step 8) grant only `SELECT` on future tables to the
  reader — never INSERT/UPDATE/DELETE/sequence defaults — so a new table can
  never silently become writable or deletable by the wrong role.

## Limitations requiring a LIVE PostgreSQL server to validate

These are **not** verifiable offline and are explicitly deferred:

- **L-M3-1 Authentication model.** Whether the operator logs in *as* these roles
  or uses a `NOLOGIN`-group + membership model is a deployment choice; the
  scripts create the roles but set no password. Confirm the chosen auth path
  against the live server.
- **L-M3-2 App "reset" feature.** The System Status reset / `purge_metric_samples`
  issues `DELETE`/DDL the `netpilot_app` role deliberately lacks. Under the app
  role those admin actions must run as `netpilot_admin` (or be disabled). Verify
  the live behaviour.
- **L-M3-3 Neon specifics.** Neon restricts superuser; ownership transfer and
  `ALTER DEFAULT PRIVILEGES` must be executed by `neon_superuser`/the actual
  owner. The `public` schema in Neon may already have non-default privileges.
  Validate ownership + GRANTs apply cleanly on a real Neon branch.
- **L-M3-4 GRANT acceptance.** The SQL is only structurally validated here. Run
  it against a disposable PostgreSQL/Neon branch to confirm it parses, that all
  22 tables + sequences exist, and that each role can/cannot perform exactly its
  matrix operations (positive and negative authorization tests).
- **L-M3-5 Sequence naming.** `<table>_id_seq` assumes SQLAlchemy's default
  `SERIAL`. Confirm the actual sequence names on the live server
  (`\d <table>` / `information_schema.sequences`) in case a migration produced a
  different name.

## M3 boundaries honored

No Neon provisioning or connection; no SQL executed against any database; no
credentials created or rotated; no passwords/secrets in the scripts; `.env`
unchanged with `NETPILOT_DEMO_MODE=true`, `NETPILOT_CHANGE_LOOP_ENABLED=false`,
`NETPILOT_LAB_EXECUTION_ENABLED=false`; no runner enabled or scheduled;
application behaviour unchanged; no commit/push/tag/deploy.
