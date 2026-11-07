# NetPilot AI - Phase 3C Feature Parity Audit

## Overview

This document audits the feature-control parity across the three data sources:
- **Demo** - Synthetic fixtures in isolated database (NOT real telemetry)
- **Simulated** - AI Lab simulator output, badged "Simulated lab data - not real telemetry"
- **Live** - Real probe measurements from the native Windows collector

## Feature Audit Table

| Page | Feature/Control | Demo | Simulated | Live | Notes |
|------|------------------|------|-----------|------|-------|
| **1_Overview** | Data source selector | ✅ | ✅ | ✅ | All sources selectable |
| | Device table display | ✅ | ✅ | ✅ | Shows devices for selected source |
| | Network status KPIs | ✅ | ✅ | ✅ | Computed from selected source |
| | Last collection timestamp | ✅ | ✅ | ✅ | From collection_runs table |
| **2_Topology** | Data source selector | ✅ | ✅ | ✅ | All sources selectable |
| | Multi-layout views (layered/force/circular/grid) | ✅ | ✅ | ✅ | Same for all sources |
| | Path troubleshooting | ✅ | ✅ | ✅ | Inspect path between devices |
| | **Fabric size selector** | ✅ | ✅ | ❌ | Compact/Standard/Large |
| | **Seed/refresh fabric button** | ✅ | ✅ | ❌ | Generates synthetic topology |
| | Device/link visualization | ✅ | ✅ | ✅ | Same rendering for all |
| | Connectivity summary | ✅ | ✅ | ✅ | Components, SPOF, etc. |
| **3_Devices** | Data source selector | ✅ | ✅ | ✅ | All sources selectable |
| | Device table | ✅ | ✅ | ✅ | Filtered by source |
| | Device detail view | ✅ | ✅ | ✅ | Interfaces, metrics, status |
| **4_Connectivity_Tests** | Data source selector | ✅ | ✅ | ✅ | All sources selectable |
| | Test results table | ✅ | ✅ | ✅ | Filtered by source |
| | Test execution history | ✅ | ✅ | ✅ | From connectivity_tests table |
| **5_Metrics_History** | Data source selector | ❌ | ❌ | ❌ | **Missing** - No source selection |
| | Metrics visualization | ✅ | ✅ | ✅ | Works but can't select source |
| **6_System_Status** | Data source selector | ✅ | ✅ | ✅ | All sources selectable |
| | System health KPIs | ✅ | ✅ | ✅ | Filtered by source |
| | Incident timeline | ✅ | ✅ | ✅ | From anomaly_events table |
| **7_Data_Analysis** | Data source selector | ✅ | ✅ | ✅ | All sources selectable |
| | Device selector | ✅ | ✅ | ✅ | Filtered by source |
| | Metric selector | ✅ | ✅ | ✅ | Available metrics for device |
| | Time range selector | ✅ | ✅ | ✅ | Same options for all |
| | Statistical analysis | ✅ | ✅ | ✅ | Same computations |
| **8_Anomalies** | Data source selector | ✅ | ✅ | ✅ | All sources selectable |
| | Anomaly detection | ✅ | ✅ | ✅ | Same algorithms |
| | Anomaly timeline | ✅ | ✅ | ✅ | Filtered by source |
| | Anomaly detail view | ✅ | ✅ | ✅ | Same for all sources |
| **9_Forecasting** | Data source selector | ✅ | ✅ | ✅ | All sources selectable |
| | Forecast model selection | ✅ | ✅ | ✅ | Same models available |
| | Training/validation split | ✅ | ✅ | ✅ | Same controls |
| | Forecast visualization | ✅ | ✅ | ✅ | Same rendering |
| **10_AI_Network_Assistant** | Data source selector | ✅ | ✅ | ✅ | All sources selectable |
| | AI-powered insights | ✅ | ✅ | ✅ | Same for all sources |
| | Natural language queries | ✅ | ✅ | ✅ | Same capability |
| **11_Ops_Center** | Data source selector | ✅ | ✅ | ✅ | All sources selectable |
| | Operations dashboard | ✅ | ✅ | ✅ | Same layout |
| | Alert management | ✅ | ✅ | ✅ | Same controls |
| **12_AI_Lab** | Data source selector | ✅ | ✅ | ✅ | All sources selectable |
| | **Device multiselect** | ❌ | ✅ | ❌ | Simulated only |
| | **Duration slider** | ❌ | ✅ | ❌ | Simulated only |
| | **Sampling interval slider** | ❌ | ✅ | ❌ | Simulated only |
| | **Random seed input** | ❌ | ✅ | ❌ | For reproducibility |
| | **Metrics multiselect** | ❌ | ✅ | ❌ | CPU/memory simulated only |
| | **Fault event injection** | ❌ | ✅ | ❌ | Latency spike, packet loss, etc. |
| | **Build simulated dataset button** | ❌ | ✅ | ❌ | Generates synthetic data |
| | Stored measurements view | ✅ | ✅ | ✅ | For all sources |

## Summary Statistics

### Pages with Source Selectors: 11/12
- **Missing**: Page 5 (Metrics History)
- **Present**: Pages 1, 2, 3, 4, 6, 7, 8, 9, 10, 11, 12

### Demo-Specific Features (not in Live)
1. **Topology Page**: Fabric size selector + seed button (synthetic topology generation)
2. **AI Lab Page**: Complete simulated dataset builder with:
   - Device selection (multiselect)
   - Duration configuration (slider)
   - Sampling interval (slider)
   - Random seed (number input)
   - Metrics selection (multiselect)
   - Fault event injection (number inputs for each fault type)
   - Build button

### Features Missing from Simulated (compared to Demo)
- None identified - Simulated has all Demo features plus AI Lab controls

### Features Impossible on Real Data
1. **Fabric size selector** - Cannot arbitrarily create real devices/links
2. **Fault injection** - Cannot inject faults into real network (safety requirement)
3. **CPU/Memory metrics** - Cannot measure on real devices via ping/traceroute
4. **Deterministic reproducibility** - Real data is inherently non-deterministic

## Restore Demo-Level Features on Simulated (Task 3)

### ✅ Already Implemented
- **Topology Page**: Fabric size selector + seed button for Simulated source
  - Location: `app_pages/2_Topology.py` lines 44-66
  - Uses `demo_data.seed_simulated_fabric()` with `data_source='simulated'`
  - Devices prefixed with `sim:` for provenance tracking
  - Same controls as Demo mode

### ❌ Still Missing
- **AI Lab Page**: Simulated dataset builder already exists and works for Simulated source
  - All controls are already present and functional
  - No additional work needed

## Real Topology Discovery for Live (Task 4)

### ✅ Implemented
- **Network module**: `network/traceroute.py` with:
  - Safe OS traceroute/tracert execution (no shell, allowlisted argv)
  - Windows and Unix parsing
  - Timeout handling
  - Per-hop RTT extraction
  - `*` (unresponsive) hop handling
  
- **Collector integration**: `tools/live_collector.py` with:
  - `--traceroute` CLI flag to enable topology discovery
  - `--traceroute-targets` CLI option for destination list
  - Traceroute-based device/link discovery
  - Per-hop RTT metrics stored with `data_source='live'`

- **Tests**: `tests/test_traceroute.py` with 28 tests covering:
  - Platform detection
  - Output parsing (Windows and Unix)
  - Execution with mocked subprocess
  - Topology discovery
  - Malformed input handling
  - Injection attempt safety

## Recommendations

1. **Fix Metrics History Page**: Add source selector for consistency with other pages
2. **Verify Traceroute Integration**: Test on real Windows machine with actual tracert
3. **Update Documentation**: Document traceroute destinations and usage
4. **Consider Live-Specific Features**: 
   - Traceroute destination configuration in UI
   - Real-time topology refresh
   - Hop-by-hop latency visualization

## Test Results

All 28 traceroute tests pass:
```
tests/test_traceroute.py ............................                    [100%]
============================= 28 passed in 0.53s ===============================
```

## Files Changed

1. `network/traceroute.py` - NEW (441 lines)
2. `tools/live_collector.py` - MODIFIED (added traceroute integration)
3. `tests/test_traceroute.py` - NEW (28 tests, 453 lines)
4. `services/demo_data.py` - MODIFIED (added `seed_simulated_fabric`)
5. `app_pages/2_Topology.py` - MODIFIED (added Simulated fabric controls)

## Next Steps

1. Run full test suite (1548 + 28 = 1576 tests expected)
2. Run compileall, ruff, secret_scan
3. Commit and push to GitHub
4. Reboot Cloud app
5. Verify Pages 1-12 work with Live/Simulated/Demo sources
