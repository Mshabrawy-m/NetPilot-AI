# NetPilot AI interface guide

The Streamlit navigation is organized around workflows. The app keeps the existing page modules and business services intact; this change groups the pages in the sidebar and gives the overview a shorter path to current network state.

## Navigation map

| Sidebar section | Page | Main capabilities |
|---|---|---|
| Overview | Network overview | Inventory health, current up/down counts, most recent successful collection and its source, device inventory |
| Network operations | Topology | Stored device/link graph, four layouts, demo fabric controls, per-hop structural path and provenance-scoped metric inspection |
| Network operations | Devices & interfaces | Device inventory and device details |
| Network operations | Connectivity tests | Reachability probes and stored results |
| Network operations | Metrics & history | Provenance-filtered time-series exploration, descriptive summaries, recent-vs-earlier median comparison, and a prompt handoff to the Assistant |
| Network operations | Anomalies | Detector runs, event review, batch scans and calibration |
| Network operations | Operations center | Alerts, event and graph insights, Digital Twin structural report, impact analysis, human-reviewed change requests and audit history |
| AI lab | AI Lab · data & forecasting | Simulated, stored, uploaded, allowlisted local, and configured read-only Prometheus sources; data quality, anomaly, forecasting, classification and statistics tasks; history and exports |
| Agentic Operations | Network assistant & tools | Existing evidence-grounded LangGraph investigation and change proposal review, plus a bounded plan that calls dataset inspection, statistics, anomaly detection, forecasting, stored topology/impact and experiment-history tools |
| System status | System status | Database/lab availability, collector state and runs, incidents, and disposable database reset |

The Ops Center owns the structural Digital Twin and change-safety workflow because impact reports and human approval are part of request review. The Digital Twin is structural and read-only; its report must not be read as routing or traffic simulation. Change execution remains disabled by default and guarded by the existing approval and configuration checks.

The older Data Analysis and Forecasting page modules remain in the repository but are no longer duplicated in the sidebar; AI Lab contains the unified cross-source task flow. The Ops Center owns structural Digital Twin and human change review. The Network Assistant exposes the additional bounded Agentic Operations tab; the existing Phase 3 workflow remains separately available. URL slugs follow Streamlit's grouped-page routing; legacy direct links were not separately browser-tested.

There is no separate user settings page in this project. System configuration is environment based and the System Status page reports service availability and collector diagnostics. The UI does not claim optional integrations are connected unless their availability check succeeds.

## Common workflows

### Inspect the network

Start at **Network overview** to see the stored inventory status and the timestamp and source of the most recent successful collection. Open **Topology** for device relationships, **Devices & interfaces** for inventory detail, or **Metrics & history** for measured time series. The mode indicator in the sidebar distinguishes demo mode from live-lab mode; missing live infrastructure is reported as unavailable.

Metrics are queried for exactly one selected provenance, device, metric and interface. Summary statistics exclude unavailable values; gaps remain visible. The recent-versus-earlier comparison is descriptive and does not claim a cause. You can pass the selected series and its observed range into the Network Assistant as an editable, read-only investigation prompt.

### Run an AI Lab experiment

Open **AI lab → AI Lab · data & forecasting**. Select a source, inspect the dataset and quality information, choose a supported task, configure and run it, then review actual evaluation output, stored history, and exports. Simulated data is identified as simulated and may include ground-truth labels. A Prometheus source is only usable when configured and available. Classification requires labels. The experiment workflow is analysis-only and does not execute network changes.

The suggested task is selected from the dataset's actual task-readiness gates and its label provenance; it is a starting point only. Loading another dataset clears the previous task selection so the recommendation is recalculated.

### Review a proposed change

Open **Network operations → Operations center** and inspect the impact evidence and request state before making an approval decision. Proposal, validation, approval, and execution are distinct persisted states. A structural impact report is not proof that a production change is safe. Execution remains subject to the existing disabled-by-default gates.

In the Network Assistant, suggested prompts are based on the selected source's stored inventory, anomaly events and measured metrics. The **Agentic Operations** tab uses the active AI Lab dataset and a strict plan schema to invoke registered read-only services, records actual outputs and provides saved run history. Completed Phase 3 runs expose persisted stage transitions and next read-only checks. Informational requests stop before configuration proposals; explicit change requests remain proposal-only and pass through the deterministic verifier.

## Shared UI organization

- `ui/theme.py` contains the shared dark palette, spacing/surface styling, page header, KPI, status pill, and Plotly defaults.
- `services/app_state.py` applies shared page bootstrap, authentication, sidebar mode/collector status, database reads, and transient feedback.
- `app.py` declares grouped Streamlit navigation and renders the overview from real repository/service state.
- Page modules continue to call existing analytics, service and repository code; the navigation and theme do not replace computation with display fixtures.

## Known limits

- The interface has no user-editable settings screen; runtime options use environment configuration.
- Live telemetry depends on the optional lab/integration being configured and reachable. The UI reports unavailable paths instead of substituting mock observations.
- The Digital Twin is structural, and it is not integrated into the AI graph workflow.
- A successful-looking Streamlit component render does not establish real-browser accessibility or live-lab correctness. Refer to the project acceptance and closure reports for the verified boundaries.
