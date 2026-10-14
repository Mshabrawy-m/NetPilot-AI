# NetPilot AI --- Phase 1: Network Foundation & Data Collection

## 1. Phase Objective

Build the practical foundation of **NetPilot AI**: a working virtual
network, periodic network data collection, data storage, and a Streamlit
interface for viewing network status.

By the end of this phase, a user should be able to open the dashboard,
inspect the network topology and device/connectivity status, run basic
connectivity tests, and review historical network measurements.

> **Scope boundary:** This phase focuses on the network lab, data
> collection, storage, and dashboard only. Do not implement Multi-Agent
> AI, AI-generated configurations, forecasting, advanced anomaly
> detection, or automated configuration deployment yet. These will be
> implemented in later phases.

## 2. Implementation Rules

-   Use free and open-source tools wherever possible.
-   Primary UI: **Streamlit**.
-   Primary language: **Python**.
-   Virtual network lab: **Containerlab + FRRouting (FRR)**, provided
    the execution environment supports Docker and Containerlab.
-   Data storage: use **PostgreSQL** if available. Otherwise, use a
    replaceable storage layer and SQLite for local development,
    documenting the decision. Do not depend on a paid external database.
-   Do not require paid APIs or API keys in this phase.
-   Do not connect to or modify real production networks. All network
    operations must stay inside the project lab.
-   Never present fabricated metrics as real measurements. If a demo
    mode is needed, label it clearly and keep it separate from live lab
    data.
-   Provide clear setup and run instructions.
-   Never commit passwords, tokens, or other secrets to source control.

## 3. Scope of Work

### 3.1 Set Up the Virtual Network Lab

Create a small, functional virtual network that can be extended later.
For example:

``` text
              R1
             /  \
           R2    R3
           |      |
          PC1    PC2
```

Requirements: - Create the Containerlab topology file and the FRR
configuration files needed to run the lab. - Use clear device names,
link definitions, and documented IP addresses. - Configure basic routing
so connectivity can be tested according to the lab design. - Document
commands to deploy, inspect, stop, and redeploy the lab. - Provide clear
checks to confirm that the lab started successfully.

Start with the smallest topology that proves the required functionality.
Do not expand the topology until the basic setup is stable.

### 3.2 Collect Network Data

Create a Python data-collection layer that reads measurements from the
running lab using available, real data sources. Collect as many of the
following as can be measured reliably:

-   Device availability and connectivity status.
-   Interface and link status.
-   `ping` results, including success/failure and average round-trip
    latency when available.
-   `traceroute` results when supported.
-   Available routing information from FRR.
-   Traffic counters, interface errors, and dropped-packet counters when
    they can be collected reliably.
-   Timestamp, device name, and interface associated with each
    measurement.

Do not assume every metric is available on every device. Store
unavailable values as `null` or show them as unsupported instead of
inventing values.

### 3.3 Data Storage

Design a simple, extensible schema. At minimum, store:

-   **devices:** device ID, name, type, management address if
    applicable, and status.
-   **links:** connected endpoints, interfaces, and link status.
-   **metric_samples:** metric name, value, unit, device/interface, and
    timestamp.
-   **connectivity_tests:** source, destination, test type, result,
    latency, and test timestamp.
-   **incidents (optional in this phase):** basic records of observed
    failures, without AI-based root-cause analysis.

Requirements: - Use consistent timestamps and document the timezone
convention. - Add appropriate indexes for time-series queries. - Keep
data-access logic separate from the UI. - Configure database connections
through environment variables or configuration files. - Provide a safe
way to initialize tables and reset disposable lab data.

### 3.4 Build the Streamlit UI

Create a clear, usable application named **NetPilot AI** with the
following pages or sections:

1.  **Overview**
    -   Total number of devices.
    -   Number of available and unavailable devices.
    -   Overall network status based on explicit, documented rules.
    -   Last successful data-collection time.
    -   A clear warning if the application is in demo mode or cannot
        connect to the lab.
2.  **Network Topology**
    -   Visually display devices and links.
    -   Indicate device/link status with colors and readable labels.
    -   Use a free visualization library compatible with Streamlit, or
        provide a clear fallback if interactive rendering is
        unavailable.
    -   Generate the topology from lab data rather than relying only on
        a static image.
3.  **Devices & Interfaces**
    -   Show a table of devices, status, addresses, and available
        interfaces.
    -   Allow the user to select a device and inspect its available
        details.
4.  **Connectivity Tests**
    -   Allow selection of a source and destination from the lab
        inventory.
    -   Run `ping` and display its result.
    -   Run `traceroute` if supported.
    -   Apply timeouts and handle failures clearly.
5.  **Metrics & History**
    -   Plot time-series charts for available measurements such as
        latency or traffic/interface counters.
    -   Allow filtering by device, metric, and time range.
    -   Display units and data sources clearly.
    -   Do not plot metrics for which no actual data exists.
6.  **System Status**
    -   Show database connectivity.
    -   Show virtual lab connectivity.
    -   Show the last data-collection run and recent errors.

### 3.5 Periodic Data Collection

-   Implement periodic collection or a manual refresh button.
-   Make the collection interval configurable.
-   Prevent overlapping collection jobs.
-   Avoid unstable infinite loops or timers inside Streamlit. Use a
    simple background scheduling mechanism suitable for the environment.
-   Log errors without exposing secrets or sensitive information.

## 4. Suggested Project Structure

Keep modules separated by responsibility. Names can be adjusted when
justified and documented.

``` text
netpilot-ai/
├── app.py
├── pages/
│   ├── 1_Overview.py
│   ├── 2_Topology.py
│   ├── 3_Devices.py
│   ├── 4_Connectivity_Tests.py
│   ├── 5_Metrics_History.py
│   └── 6_System_Status.py
├── network/
│   ├── containerlab_manager.py
│   ├── device_inventory.py
│   ├── connectivity.py
│   └── metrics_collector.py
├── database/
│   ├── connection.py
│   ├── models.py
│   └── repository.py
├── services/
│   ├── topology_service.py
│   └── collection_service.py
├── lab/
│   ├── topology.clab.yml
│   └── configs/
├── tests/
├── .env.example
├── requirements.txt
├── README.md
└── .gitignore
```

FastAPI may be introduced later if a separate API layer becomes
necessary. Do not add unnecessary layers in this phase. If FastAPI is
added now, document the reason and avoid duplicating business logic
between the API and Streamlit.

## 5. Reliability and Safety Requirements

-   Run network commands only against the NetPilot lab.
-   Do not accept arbitrary user-provided shell commands for direct
    execution.
-   Use an allowlist of supported operations and validate device names
    and destinations.
-   Apply timeouts and limit command output size.
-   Handle unreachable devices and failed commands without crashing the
    application.
-   Never store credentials or API keys in source files.
-   Clearly separate demo mode from live lab mode.
-   Do not implement automated configuration deployment in this phase.

## 6. Required Tests

Add appropriate automated tests for at least:

-   Creating and reading device/link records.
-   Storing and retrieving time-series metric samples.
-   Handling connection failures and timeouts.
-   Ensuring selected devices/destinations belong to the approved lab
    inventory.
-   Computing device/network status using documented rules.
-   Testing services with demo fixtures separately from real lab data.

Document manual checks in the README for deploying the lab, viewing
topology, running ping, saving metrics, and viewing historical charts.

## 7. Definition of Done / Acceptance Criteria

This phase is complete only when:

-   [ ] The project can be configured using the README on a supported
    environment.
-   [ ] The Containerlab/FRR lab can be deployed and stopped using
    documented commands.
-   [ ] Devices and links are displayed from lab data, not only from a
    hard-coded diagram.
-   [ ] Device and available interface status can be viewed.
-   [ ] The app can run `ping` to an allowed destination inside the lab.
-   [ ] Failures and timeouts are reported clearly.
-   [ ] Actual measurements are collected and stored with timestamps.
-   [ ] Historical charts appear when enough measurements are available.
-   [ ] Missing metrics, unavailable lab services, and database failures
    are handled clearly.
-   [ ] Basic automated tests pass.
-   [ ] No secrets are committed to the repository.
-   [ ] No automated configuration changes are executed in this phase.

## 8. Explicitly Out of Scope

Do not implement these features yet:

-   Groq API or any LLM integration.
-   LangGraph agent workflows.
-   AI-generated network configurations.
-   Batfish configuration verification.
-   Digital Twin testing of proposed changes.
-   Autonomous deployment or rollback.
-   Forecasting or advanced anomaly detection.
-   RAG or embeddings.
-   MCP servers.

These belong to later phases, after network setup and data collection
are stable.

## 9. Required Deliverables

Deliver all of the following:

1.  Complete, organized source code.
2.  Virtual network lab and device configuration files.
3.  `requirements.txt` or an equivalent environment definition.
4.  `.env.example` without real secrets.
5.  A README covering installation, setup, deployment, shutdown, and
    troubleshooting.
6.  Automated tests and their results.
7.  Screenshots of the main pages or a short demo recording.
8.  A list of metrics actually collected, including each metric's source
    and any known limitations.
9.  A short implementation report stating what is complete, what is
    incomplete, and any technical decisions or trade-offs.

## 10. Execution Instructions for the Vibe Coder

Implement this phase in small, verifiable steps:

1.  Inspect the execution environment and confirm whether Docker and
    Containerlab are supported.
2.  Create the virtual lab and verify connectivity manually.
3.  Implement data collection and persistence.
4.  Build the Streamlit pages and connect them to real data.
5.  Add tests and documentation.
6.  Run the acceptance tests and fix issues before declaring completion.

**Do not claim a feature works until it has been run and tested.** If
the server environment does not support Containerlab or the required
network privileges, document the blocker and recommend a suitable free
alternative rather than presenting a static or fabricated demo as a real
network.
