# NetPilot AI interface guide

The Streamlit navigation is organized around workflows. The app keeps the existing page modules and business services intact; this change groups the pages in the sidebar and gives the overview a shorter path to current network state.

## Navigation map

| Sidebar section | Page | Main capabilities |
|---|---|---|
| Overview | Network overview | Inventory health, current up/down counts, most recent successful collection and its source, device inventory |
| Network operations | Topology | Stored device/link graph, layout choices, demo fabric controls |
| Network operations | Devices & interfaces | Device inventory and device details |
| Network operations | Connectivity tests | Reachability probes and stored results |
| Network operations | Metrics & history | Provenance-filtered time-series exploration, descriptive summaries, recent-vs-earlier median comparison, and a prompt handoff to the Assistant |
| Network operations | Anomalies | Detector runs, event review, batch scans and calibration |
| Network operations | Operations center | Alerts, event and graph insights, Digital Twin structural report, impact analysis, human-reviewed change requests and audit history |
| AI lab | Data analysis | Exploratory analysis and data quality over one selected stored source |
| AI lab | Forecasting | Forecast comparison and evaluation on stored telemetry |
| AI lab | Experiments | Simulated, stored, uploaded, allowlisted local, and configured read-only Prometheus sources; readiness-based task suggestions; anomaly, forecasting, classification and statistics experiments; stored history and exports |
| AI lab | Network assistant | Evidence-grounded investigation, source-aware prompt suggestions, persisted agent execution trace, proposal review, run history and knowledge queries |
| System status | System status | Database/lab availability, collector state and runs, incidents, and disposable database reset |

The Ops Center owns the structural Digital Twin and change-safety workflow because impact reports and human approval are part of request review. The Digital Twin is structural and read-only; its report must not be read as routing or traffic simulation. Change execution remains disabled by default and guarded by the existing approval and configuration checks.

The source page modules are retained. Sidebar label changes are: **Overview → Network overview**, **Network Topology → Topology**, **AI Network Assistant → Network assistant**, **Ops Center → Operations center**, and **AI Lab → Experiments**. Other labels retain their names with consistent sentence case. URL slugs now follow Streamlit's grouped-page routing; legacy direct links were not separately browser-tested.

There is no separate user settings page in this project. System configuration is environment based and the System Status page reports service availability and collector diagnostics. The UI does not claim optional integrations are connected unless their availability check succeeds.

## Common workflows

### Inspect the network

Start at **Network overview** to see the stored inventory status and the timestamp and source of the most recent successful collection. Open **Topology** for device relationships, **Devices & interfaces** for inventory detail, or **Metrics & history** for measured time series. The mode indicator in the sidebar distinguishes demo mode from live-lab mode; missing live infrastructure is reported as unavailable.

Metrics are queried for exactly one selected provenance, device, metric and interface. Summary statistics exclude unavailable values; gaps remain visible. The recent-versus-earlier comparison is descriptive and does not claim a cause. You can pass the selected series and its observed range into the Network Assistant as an editable, read-only investigation prompt.

### Run an AI Lab experiment

Open **AI lab → Experiments**. Select a source, inspect the dataset and quality information, choose a supported task, configure and run it, then review actual evaluation output, stored history, and exports. Simulated data is identified as simulated and may include ground-truth labels. A Prometheus source is only usable when configured and available. Classification requires labels. The experiment workflow is analysis-only and does not execute network changes.

The suggested task is selected from the dataset's actual task-readiness gates and its label provenance; it is a starting point only. Loading another dataset clears the previous task selection so the recommendation is recalculated.

### Review a proposed change

Open **Network operations → Operations center** and inspect the impact evidence and request state before making an approval decision. Proposal, validation, approval, and execution are distinct persisted states. A structural impact report is not proof that a production change is safe. Execution remains subject to the existing disabled-by-default gates.

In the Network Assistant, suggested prompts are based on the selected source's stored inventory, anomaly events and measured metrics. Completed runs expose their persisted stage transitions and next read-only checks. The assistant retains the existing LangGraph branches: informational requests stop before planning and configuration proposals; explicit change requests remain proposal-only and pass through the deterministic verifier.

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
