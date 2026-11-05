# NetPilot AI — Live Data Demo Runbook (Windows, no Docker)

This document describes how to flow **real network measurements** into the
Neon-backed Streamlit Cloud app using the Windows-native live collector.
Containerlab, Docker, and FRR are NOT required.

---

## Prerequisites

| Requirement | Status |
|---|---|
| Windows host with `ping.exe` | built-in |
| Internet access (8.8.8.8, 1.1.1.1) | required |
| WCM credential `netpilot/prod/netpilot_collector` | provisioned by Phase 2 |
| Neon schema ready (22 tables) | provisioned by Phase 2 |
| Streamlit Cloud app running (Live Mode) | `NETPILOT_DEMO_MODE=false` in Secrets |
| Safety flags OFF | `CHANGE_LOOP_ENABLED=false`, `LAB_EXECUTION_ENABLED=false` |

---

## 1. Collect real telemetry

Run from the project root in PowerShell:

```powershell
# One-shot (quick test):
$env:NETPILOT_DEMO_MODE = "false"
.venv\Scripts\python.exe tools/live_collector.py --once --no-interfaces

# Sustained (5 s interval, 10 minutes):
.venv\Scripts\python.exe tools/live_collector.py --interval 5 --duration 600

# Custom targets:
.venv\Scripts\python.exe tools/live_collector.py --targets gateway,1.1.1.1,8.8.8.8,9.9.9.9 --interval 10 --duration 300
```

### Expected output

```
Live collector summary
  cycles: 12
  samples inserted: 108 (duplicates skipped: 0)
  targets: gateway=192.168.1.1, cloudflare-dns=1.1.1.1, google-dns=8.8.8.8
  last cycle status: ok
```

### What it writes

Per cycle, per target:
- `ping.avg_rtt_ms` — average round-trip time (ms)
- `ping.jitter_ms` — statistical jitter across replies (ms)
- `ping.loss_pct` — packet loss percentage (%)

All rows tagged `data_source='live'`.

### Minimum data thresholds

| Feature | Non-null points required | Achieved after ~cycles |
|---|---|---|
| Data Analysis | 5 | 2 |
| Anomaly Detection | 12 | 4 (at 5 s interval) |
| Forecasting | 30 | 10 |

---

## 2. Verify data in Streamlit Cloud

1. Open https://netpilot-ai.streamlit.app/
2. Navigate to **Data Analysis** → select a device (e.g. `google-dns`), metric
   (`ping.avg_rtt_ms`), time range → a real time-series chart appears.
3. Navigate to **Anomalies** → click **Run Detection** → if enough points exist
   (≥12), the detector returns any statistical outliers.

---

## 3. Trigger a real anomaly (safe, documented)

**Method**: Ping a host that responds slowly or intermittently.
No disruption to your machine or the production targets.

```powershell
# Add a slow/unreliable target (TEST-NET-1, RFC 5737, unreachable by design):
.venv\Scripts\python.exe tools/live_collector.py --targets "unstable=192.0.2.1,8.8.8.8" --interval 5 --duration 120
```

`192.0.2.1` (TEST-NET-1) will show 100% loss → after 12+ cycles, the anomaly
detector flags `ping.loss_pct` spike. The Anomalies page shows:
- The detected event with severity, source detector, and timestamp.
- The underlying context series.

---

## 4. Full pipeline walkthrough

| Step | Page | Expected result |
|---|---|---|
| Collect | (terminal) | `samples inserted: N` |
| View data | Data Analysis | Real RTT/loss/jitter charts |
| Detect anomaly | Anomalies → Run Detection | Detects the `unstable` target spike |
| Forecast | Forecasting → Run | Needs ≥30 points; shows band + prediction if enough |
| RCA | Anomalies → event detail | Root-cause context (if configured) |
| AI recommendation | AI Network Assistant | Ask "What's wrong with unstable?" |
| Digital Twin | (disabled) | `data_source=live` binding exists, verification pending |
| Approval card | (disabled) | `CHANGE_LOOP_ENABLED=false` → no execution path |

**Safety boundaries**: Execution (change loop, lab commands) is permanently
disabled on the hosted plane. No device configuration happens.

---

## 5. Troubleshooting

| Symptom | Fix |
|---|---|
| `ERROR: no collector credential found` | Run `python tools/provision_neon_prod.py` once to store WCM credentials |
| `destination unreachable (100% loss)` for all targets | Check internet connectivity; firewall may block outbound ICMP |
| `ping binary not available` | Verify `C:\Windows\System32\PING.EXE` exists |
| `NETPILOT_DEMO_MODE=true` refusal | Set `$env:NETPILOT_DEMO_MODE = "false"` before running |
| Cloud shows no data | Verify collector DSN points to the same Neon branch as Cloud (`fingerprint=f5ee5ef2`) |

---

## 6. Stop conditions (do NOT proceed if)

- You see the collector attempting to write `data_source='demo'` (it shouldn't).
- Safety flags are enabled.
- The Cloud app is in Demo Mode.

---

## Quick reference (all commands)

```powershell
# Activate the virtual environment
cd "e:\Graduation Projects\NetPilot AI"

# Single cycle
$env:NETPILOT_DEMO_MODE = "false"
.venv\Scripts\python.exe tools/live_collector.py --once

# 10 minutes at 5s interval
.venv\Scripts\python.exe tools/live_collector.py --interval 5 --duration 600

# 5 minutes with a high-loss target for anomaly generation
.venv\Scripts\python.exe tools/live_collector.py --targets "unstable=192.0.2.1,gateway,8.8.8.8" --interval 5 --duration 300

# Run tests only (offline, no DB)
.venv\Scripts\python.exe -m pytest tests/test_live_collector.py -v
```
