# LIVE Demo Runbook (Phase 3A)

This runbook drives the **Windows-native live collector**. It runs real ICMP
probes from the operator machine, writes `data_source='live'` measurements to
Neon Production, and lets the existing Live pages analyse them. Docker,
Containerlab, WSL and FRR are **not** required.

Safety properties, unchanged throughout:

- `NETPILOT_CHANGE_LOOP_ENABLED=false` and
  `NETPILOT_LAB_EXECUTION_ENABLED=false`; nothing is configured or executed.
- The collector writes with the least-privilege `netpilot_collector` role only.
- Demo rows and live rows are never mixed. Every collector row is labelled
  `data_source='live'`.
- DSNs and passwords are never printed, logged or committed.

## 1. Prerequisites

1. **Operator host**: Windows with the project `.venv` and OS `ping`.
   `psutil` is optional; local NIC counters are skipped when it is absent.
2. **Collector credential** in Windows Credential Manager, target
   `netpilot/prod/netpilot_collector` (user `netpilot_collector`). The runner
   reads it in-process and never echoes it. Alternatively export
   `NETPILOT_DB_URL_COLLECTOR` in the shell.
3. **Neon schema and grants** already deployed: `netpilot_collector` has INSERT
   on `metric_samples` and INSERT/UPDATE on `devices`, `interfaces`, `links`,
   `collection_runs`, `incidents`.
4. **Live mode** for the process: `NETPILOT_DEMO_MODE=false`. The collector
   refuses to run while demo mode is on, because live and demo data must never
   mix.

No Cloud Secrets change is needed for the collector. The Cloud app only needs a
manual **Reboot** after code changes; new rows in Neon appear without a reboot.

## 2. Prepare the PowerShell session

`config.py` loads `.env` at import, but `python-dotenv` never overrides a
variable that is already set. Export the live-mode flags in the shell so a
local `.env` with `NETPILOT_DEMO_MODE=true` cannot block the run:

```powershell
cd "E:\Graduation Projects\NetPilot AI"

$env:NETPILOT_DEMO_MODE = "false"
$env:NETPILOT_CHANGE_LOOP_ENABLED = "false"
$env:NETPILOT_LAB_EXECUTION_ENABLED = "false"

# Optional: verify the credential is present without printing it.
cmdkey /list | Select-String "netpilot/prod/netpilot_collector"
```

## 3. Run one deterministic cycle

```powershell
.venv\Scripts\python.exe tools\live_collector.py `
    --once --no-interfaces --count 3 --timeout 1
```

Observed output on the operator machine (addresses and timings vary):

```
INFO netpilot.live_collector: collector credential loaded from wcm (value not printed)
INFO netpilot.live_collector: targets: gateway=192.168.1.1, cloudflare-dns=1.1.1.1, google-dns=8.8.8.8
INFO netpilot.live_collector: interval=30.0s count=3 timeout=1.0s interfaces=off
INFO netpilot.live_collector: cycle 1: status=ok inserted=9 skipped=0 up=3/3
Live collector summary
  cycles: 1
  samples inserted: 9 (duplicates skipped: 0)
  targets: gateway=192.168.1.1, cloudflare-dns=1.1.1.1, google-dns=8.8.8.8
  last cycle status: ok
```

Nine rows = three targets × three ping metrics. Omit `--no-interfaces` to add
the local NIC counters.

Per target the collector records `ping.avg_rtt_ms`, `ping.jitter_ms` and
`ping.loss_pct`. A failed probe stores `NULL` RTT/jitter and `100.0` loss; values
are never invented. With NIC counters enabled it also stores
`if.rx_bytes`/`if.tx_bytes`/error/drop counters and `if.operstate` under the
`collector-host` device. Add `--no-interfaces` for the smallest, fastest batch.

Writes are one batched INSERT per cycle. The idempotency key is
`(device_name, interface_name, metric)` at second-resolution `collected_at`, so
a repeated `--once` in the same second is skipped rather than duplicated.
Transient database errors retry with backoff; a failed write is reported and the
next cycle retries.

## 4. Continuous collection

```powershell
# One cycle every 30 seconds for 30 minutes, then a clean summary.
.venv\Scripts\python.exe tools\live_collector.py `
    --interval 30 --duration 1800

# Or run until Ctrl+C; the exit is clean (code 0).
.venv\Scripts\python.exe tools\live_collector.py --interval 30
```

`--duration 0` (the default) means "until Ctrl+C". The summary prints on both
normal completion and Ctrl+C.

Exit codes: `0` success, `1` collection error (all probes failed), `2`
configuration/credential error, `3` database error.

## 5. Targets and identities

Default: `gateway,1.1.1.1,8.8.8.8`. `gateway` is auto-detected from the OS
routing table and skipped if detection fails.

```powershell
# Explicit list (ADDRESS or LABEL=ADDRESS):
.venv\Scripts\python.exe tools\live_collector.py --once `
    --targets "gateway,1.1.1.1,8.8.8.8,9.9.9.9"

# Stable custom identity, e.g. to measure a different address under the same
# device label later:
.venv\Scripts\python.exe tools\live_collector.py --once `
    --targets "gateway,1.1.1.1,demo-target=192.0.2.10"
```

Known addresses map to stable labels: `1.1.1.1` → `cloudflare-dns`, `8.8.8.8` →
`google-dns`, `9.9.9.9` → `quad9-dns`. Other IPv4 addresses map to
`target-<ip-with-dashes>`; hostnames map to `target-<slug>`; an explicit
`LABEL=ADDRESS` always wins. Labels are validated (alphanumeric, `.`, `_`, `-`),
addresses are validated IPv4 literals or DNS hostnames, and `ping` is invoked
with a fixed argv list and `shell=False` — no shell injection is possible.

## 6. Verify the Live pages

Open the Cloud app. Every source selector defaults to `live` when
`NETPILOT_DEMO_MODE=false`; the available-source lists are built from stored
measurements, and demo rows live in the separate demo store, so live pages show
only `data_source='live'`.

| Page | What to check | Honest sparse state |
| --- | --- | --- |
| Overview | last successful run says `[source: live]`, device counts | "No successful data collection yet" |
| Topology | live nodes and link status | "Isolated (no links)" warning, nodes listed |
| Metrics & History | RTT/jitter/loss charts per device | "No measured values exist for device ..." |
| Anomalies | persisted events for `live` only | "No persisted anomaly events match these filters ..." |
| Forecasting | forecastable series and runs | "Not forecastable yet: ... needs >= 30 non-null samples, has N" |
| Operations center (RCA / insights / Digital Twin / approval) | live events, impact report, simulation status | "No alert rules yet", "Need at least 2 open anomalies ..." |
| System status | recent runs show `Source: live`, DB reachable | Containerlab warning only describes the virtual lab |

Analysis floors are explicit: `min_points_analysis=5`,
`min_points_detection=12`, `min_points_forecast=30`. Thinner series are reported
as skipped with the required count, never plotted as if complete.

## 7. Repeatable end-to-end scenario (execution disabled)

The scenario creates a **real** anomaly from real probes without touching the
operator network: keep one stable device label (`demo-target`) and change only
the address it measures. Phase A establishes a baseline; phase B points the same
label at a deliberately unreachable TEST-NET-2 address (`198.51.100.42`). The
measurements are genuine; only the target choice is deliberate.

1. **Baseline** — 12 cycles (the detection floor) at 5-second spacing:
   ```powershell
   .venv\Scripts\python.exe tools\live_collector.py `
       --targets "gateway,1.1.1.1,8.8.8.8,demo-target=1.1.1.1" `
       --interval 5 --duration 60 --count 3 --timeout 1
   ```
2. **Inject the change** — same label, unreachable address:
   ```powershell
   .venv\Scripts\python.exe tools\live_collector.py `
       --targets "gateway,1.1.1.1,8.8.8.8,demo-target=198.51.100.42" `
       --interval 5 --duration 60 --count 3 --timeout 1
   ```
   `demo-target` now has `ping.loss_pct=100` and `NULL` RTT samples after a
   baseline of reachable samples.
3. **Anomalies** — open *Anomalies*, select source `live`, device
   `demo-target`, metric `ping.loss_pct`, and press the detection button. The
   rolling-MAD/z-score result explains the deviation against the trailing
   baseline. A `data quality` result naming `min_points_detection=12` is the
   honest sparse state when fewer samples exist.
4. **RCA and recommendation** — open the event's investigation. The RCA service
   links evidence and produces a recommendation. Read it; nothing is applied.
5. **Agent recommendation** — the assistant/ops insights can explain the
   evidence and propose the next investigation. This is advisory text only.
6. **Digital Twin** — the Operations center renders a read-only simulation
   report (`SIMULATED`, `PARTIAL`, `INSUFFICIENT`, `INVALID` or `NOT
   SIMULATED`). A simulation is not proof that a real change is safe.
7. **Approval card** — the change-loop tab shows the request, impact report and
   approval controls. `NETPILOT_CHANGE_LOOP_ENABLED=false` keeps **apply**
   disabled by design; the card is recorded, not executed. No post-change
   verification or rollback runs.

To restore normal collection, rerun step 1 or drop the `demo-target` entry.

## 8. Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `live collector requires NETPILOT_DEMO_MODE=false` | Export the flag in the shell (section 2); `.env` alone may not override it. |
| `no collector credential found` | Store the DSN in WCM target `netpilot/prod/netpilot_collector`, or set `NETPILOT_DB_URL_COLLECTOR`. |
| `collector requires a PostgreSQL connection ...` | The resolved collector URL is SQLite. Fix the role credential; do not change grants. |
| `default gateway could not be detected; skipping it` | Expected on unusual routing setups; other targets still run. |
| `ping` reported `unsupported` | The OS `ping` binary is missing or not executable; the cycle continues. |
| Repeated `--once` inserts 0 samples | Same-second idempotency key; wait one second and rerun. |

## 9. Validate the shipped change

```powershell
.venv\Scripts\python.exe -m compileall -q tools/live_collector.py database/repository.py app_pages/1_Overview.py app_pages/6_System_Status.py
.venv\Scripts\ruff.exe check tools/live_collector.py tests/test_live_collector.py app_pages/1_Overview.py app_pages/6_System_Status.py
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe tools/secret_scan_check.py

git status --porcelain
git diff --stat
git push origin main
git rev-parse HEAD
git rev-parse origin/main   # must match HEAD
```

`database/repository.py` has pre-existing ruff findings (UP035/RUF100) outside
this change; do not rewrite them here. After pushing code, press **Reboot** on
share.streamlit.io so the app re-clones. Data-only changes in Neon need no
reboot.
