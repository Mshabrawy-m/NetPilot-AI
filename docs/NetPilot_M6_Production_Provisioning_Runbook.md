# NetPilot AI — M6: Production Neon Provisioning Runbook (Stage A6b-1)

**Preparation only.** M6 documents *how* to bring a production Neon PostgreSQL
backend online for the Demo→Live programme. Nothing here was executed: no
production project/branch was created, no collection was started, no
retention/rollup job was scheduled, and the deployed app was **not** switched
to Live Mode. Do those only after M5 passes on a disposable branch and a human
approves each step.

Gating order: **M5 green on a throwaway branch → M6 production provisioning →
operator flips Live Mode manually.** M6 never automates the flip.

## 1. Neon project / database / branch settings

| Item | Value | Note |
|---|---|---|
| Project | one dedicated `netpilot-prod` project | isolated from any scratch/M5 project |
| Database | `netpilot` | single logical DB |
| Branch model | `production` branch is the **only** Live Mode target | M5 used a disposable branch; it is not reused |
| Auth model | five roles from M3, each with its **own** Neon role/secret | see §2 |
| TLS | mandatory | `sslmode` enforced by M1 (`NETPILOT_PG_SSLMODE`) |
| Compute | autoscaling, start small | pool is tuned for serverless (M2) |

Neon does not expose a plain superuser. Role creation, `ALTER … OWNER TO`
(script `20`), and `ALTER DEFAULT PRIVILEGES` (script `40`) must run as
`neon_superuser` / the actual owner. Use the **pooled** connection string
(`-pooler` host) for the runtime roles; use the direct (non-pooled) string for
`netpilot_admin` schema init so DDL is not routed through a pooler.

## 2. Role → URL → secret mapping (exact requirements)

M4 activates role separation the moment **any** `NETPILOT_DB_URL_*` is set; from
then on every process must have its **own** URL or it **fails closed** (never an
admin fallback). Production must therefore set **all five**.

| Process | Role | Env var | Privileges (M3) |
|---|---|---|---|
| Streamlit app | `netpilot_app` | `NETPILOT_DB_URL_APP` | SELECT broadly + limited INSERT/UPDATE; **no DELETE/DDL** |
| Collector runner | `netpilot_collector` | `NETPILOT_DB_URL_COLLECTOR` | SELECT/INSERT/UPDATE on inventory + samples; **no DELETE/DDL** |
| Retention runner | `netpilot_retention` | `NETPILOT_DB_URL_RETENTION` | SELECT + DELETE on `metric_samples` only |
| Rollup runner | `netpilot_rollup` | `NETPILOT_DB_URL_ROLLUP` | SELECT `metric_samples`; SELECT/INSERT/UPDATE `metric_daily_rollups` |
| Schema init (manual, admin) | `netpilot_admin` | `NETPILOT_DB_URL_ADMIN` | owner; DDL; **only** `run_collector.py init-schema` |

DSN shape (placeholder password shown as `CHANGE_ME` — never a real value):

```
postgresql+psycopg2://netpilot_app:CHANGE_ME@<pooled-host>:5432/netpilot?sslmode=require
```

### Secure secret configuration rules

- Store every DSN in the platform **secret manager** (or Neon's own role
  secrets) and inject it into the process environment at runtime.
- **Never** commit a DSN, never put a real value in `.env`, and never print one
  in a report, log, or chat. M4 fail-closed errors and the M5
  `error_signature()` are already credential-safe (name the variable + host +
  SQLSTATE class only).
- One secret per role; rotate independently; the admin DSN is used only for the
  manual schema step and is not attached to any always-on process.
- Keep `NETPILOT_MANAGE_SCHEMA=false` for every runtime process — schema is the
  admin's exclusive job (M4 gates DDL and the UI reset/purge accordingly).

## 3. Connection tuning to apply in production (from M1/M2)

Defaults already validated by M1/M2/M4 tests; set explicitly per host:

| Variable | Default | Production guidance |
|---|---|---|
| `NETPILOT_PG_SSLMODE` | `require` | `require` at minimum; `verify-full` + `NETPILOT_PG_SSLROOTCERT` for full CA pinning |
| `NETPILOT_PG_CONNECT_TIMEOUT` | `10` | keep low so a stalled branch fails fast |
| `NETPILOT_PG_POOL_SIZE` | `3` | per-process ceiling |
| `NETPILOT_PG_MAX_OVERFLOW` | `3` | burst headroom |
| `NETPILOT_PG_POOL_RECYCLE` | `60` | keep **below** Neon's idle timeout so dead sockets do not accumulate |
| `NETPILOT_PG_POOL_TIMEOUT` | `30` | wait for a free pooled connection |
| `NETPILOT_PG_STATEMENT_TIMEOUT_MS` | `15000` | **raise or set `0` on the rollup host** — a full-day re-aggregation can legitimately exceed 15 s; the forecasting host likewise |

## 4. Storage & retention estimate for the selected telemetry interval

This is a **capacity-planning estimate**, not a measurement, so an operator can
size the branch before turning collection on. Numbers are derived from the real
collector loop ([`network/metrics_collector.py`](../network/metrics_collector.py),
`METRIC_UNITS` + the per-interface loop) and the config defaults
(`NETPILOT_COLLECT_INTERVAL=60`, `NETPILOT_RETENTION_RAW_DAYS=30`).

### Samples written per collection cycle

Per device per cycle:

```
2      (ping.avg_rtt_ms, ping.loss_pct)
+ 2    per in-lab device (inlab_ping.*)            [optional]
+ 1    per routing device (frr.routes_total)
+ 7 × I  (if.rx_bytes, tx_bytes, rx_errors, tx_errors,
          rx_dropped, tx_dropped, operstate)        [I = interfaces on the device]
```

### Worked example — reference lab (8 devices, 2 interfaces each, 4 in-lab)

```
per cycle  = 2·8  + 2·4  + 1·8  + 7·2·8  = 16 + 8 + 8 + 112 ≈ 144 samples/cycle
cycles/day = 86400 / 60                    = 1440
rows/day   ≈ 144 × 1440                    ≈ 2.07e5  (~207,000 metric_samples/day)
```

### Retention-window storage

```
raw row cost (incl. PK + 1-2 secondary indexes, ~150 B/row estimate)
  bytes/day ≈ 207,000 × 150 B ≈ 31 MB/day
steady-state raw at 30-day retention ≈ 31 MB × 30 ≈ 0.9 GB
```

Scale linearly and re-run for the real topology:

```
rows/day ≈ samples_per_cycle × (86400 / NETPILOT_COLLECT_INTERVAL)
GB@Rdays ≈ rows/day × bytes_per_row × NETPILOT_RETENTION_RAW_DAYS / 1e9
```

Halving the interval doubles the ingest rate. `metric_daily_rollups` adds a
roughly **constant, tiny** amount (~ one row per distinct series per day —
hundreds of rows/day, < 20 MB/year), and is precisely what lets you later
shorten the raw `metric_samples` retention once the rollups are trusted.

### Retention / rollup operating rule (NOT scheduled by M6)

- Both jobs remain **manual**. Nothing in M6 wires them into a scheduler.
- Rollup first, then retention, so `metric_daily_rollups` covers a day before
  its raw `metric_samples` are expired (`retention_raw_days=30` bounds what a
  *manual* retention run may delete; `0`/negative disables it).
- Use the **`netpilot_retention`** URL for retention and **`netpilot_rollup`**
  URL for rollup — each runner binds and restores only its own role (M4).

## 5. go-live checklist (execute manually, in order, only after approval)

1. M5 reported **all PASS** on a disposable branch.
2. Provision `netpilot-prod` project + `production` branch + the five roles
   (§1–§2); store DSNs in the secret manager.
3. Run the M3 scripts in order against the production branch; schema init as
   `netpilot_admin` via `run_collector.py init-schema` (direct, non-pooled URL).
4. Re-run `validate_pg_roles_m5.py` **on a fresh disposable branch** (never the
   production branch) to confirm the same scripts behave; production only
   receives reviewed, M5-proven scripts.
5. Set the four runtime `NETPILOT_DB_URL_*`, `NETPILOT_MANAGE_SCHEMA=false`, and
   §3 tuning on each host; confirm each process starts with its own role and
   fails closed if a URL is missing.
6. **Only then**, and only by explicit operator action, flip Live Mode
   (`NETPILOT_DEMO_MODE=false`) and start the collector. Keep
   `NETPILOT_CHANGE_LOOP_ENABLED=false` and `NETPILOT_LAB_EXECUTION_ENABLED=false`
   until the change-loop / lab gates are separately approved — M6 does not
   enable network configuration changes.

## 6. Deliberately NOT done by M6

- No collection started; no retention/rollup scheduled or run.
- Deployed app not switched to Live Mode.
- No production branch created, altered, or connected to while writing this.
- No credentials requested, generated, printed, or committed.
- No network-configuration changes enabled; change-loop and lab execution stay off.

## M6 boundaries honored

Preparation/documentation only. `.env` unchanged; `NETPILOT_DEMO_MODE=true`,
`NETPILOT_CHANGE_LOOP_ENABLED=false`, `NETPILOT_LAB_EXECUTION_ENABLED=false`
preserved; no Neon provisioning or connection; no commit/push/tag/deploy;
production branches untouched.
