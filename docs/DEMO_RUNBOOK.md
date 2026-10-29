# NetPilot AI — Graduation Demo Runbook

**Purpose:** record the demo described in the project brief without inventing
data, exposing secrets, or claiming unverified live behaviour.

**Status of this runbook:** the steps match the current source and grouped
sidebar navigation. The expected demo seed values below were observed on
2026-10-26; verify them on the recording machine. The latest headless
Streamlit/AppTest attempt stalled without a page result, so this document does
not claim that the current UI was visually re-verified.

**What this is not:** a claim of live Containerlab, PostgreSQL, browser E2E, or
live-provider validation. The demo runs in clearly labelled **demo mode**.

---

## 1. Pre-flight (do this before recording)

### 1.1 Use a fresh, isolated demo database

The demo seed is deterministic, but an existing `data/netpilot_demo.db` may
contain data from earlier runs (including the optional large fabric). For a
repeatable take, point the app at a new file:

```powershell
$env:NETPILOT_DEMO_MODE = "true"
$env:DATABASE_URL = "sqlite:///C:/Users/USER/AppData/Local/Temp/netpilot_demo_recording.db"
.\.venv\Scripts\python.exe -m streamlit run app.py
```

The app creates the schema and seeds the demo fixtures automatically on first
page load. To re-record from a clean state, stop Streamlit, delete that file,
and start again. Do not delete `data/netpilot.db` or any live-lab data.

If you prefer the default demo database, run with only
`NETPILOT_DEMO_MODE=true` and verify the numbers in §1.3 before recording.

### 1.2 Screen hygiene

- Clean browser profile or Incognito; close all other tabs.
- 1920×1080, browser zoom 100–125%; hide the bookmarks bar.
- Close DevTools, terminal windows containing environment values, and
  notification pop-ups.
- Never open `.env`, `docker-compose.yml` secrets, Streamlit Cloud settings, or
  any credential page on camera.
- The app never displays the Groq key, but do not hover over terminal history.
- Confirm the **DEMO MODE** warning is visible on each page shown.

### 1.3 Previously observed demo seed

Fresh demo seed (`hours=2` in the verification run; the app default is 6 hours,
which produces the same device/link statuses):

| Item | Value |
|---|---|
| Devices | 5 (PC1, PC2, R1, R2, R3) |
| Links | 4 |
| Device status | PC1 up · PC2 up · **R1 down** · R2 up · **R3 down** |
| Link status | R2–PC1 **up** · R1–R2 down · R1–R3 down · R3–PC2 down |
| Overview | 5 devices · 3 up · 2 down · **DEGRADED** |
| Topology | 5 devices · 4 links · 1 component · 3 SPOF nodes · 2 down |

The two down routers are part of the deterministic fixture, not a live fault.
Say so if you mention them. They are what makes the R2→PC1 simulation
meaningful: R2–PC1 is the only available documented link in the baseline.

### 1.4 Safety state to confirm on camera

- Sidebar: **Demo mode** and “collector disabled – no live probes”.
- Ops Center → Change loop tab: **“Change loop is OFF – apply will refuse by
  design.”**
- Ops Center → Change loop tab: open-mode notice explaining that approval is
  fail-closed without a signed-in identity.
- Do **not** enable `NETPILOT_CHANGE_LOOP_ENABLED` or
  `NETPILOT_LAB_EXECUTION_ENABLED` for the recording.

---

## 2. Scene-by-scene script

### Scene 1 — Overview (00:00–00:25)

**Page:** **Overview → Network overview** (`app.py`). This page opens at
startup; there is no separate landing page.

**Do:**
1. Start the app in demo mode and leave its demo warning visible.
2. Pause on network status and device counts.
3. Point out the latest successful collection timestamp and `demo` source.

The overview has no separate “Safety guarantees” panel. Explain the safety
boundary in narration and show the disabled change-loop status later.

**Narration:** “NetPilot AI brings network monitoring and analysis into one
workflow. This session uses synthetic demo data, not measurements from a live
network.”

---

### Scene 2 — Monitoring and topology (00:25–00:55)

**Do:**
1. Stay on **Network overview** to show inventory and collection source.
2. In the sidebar open **Network operations → Topology**.
3. Pause on the graph and link table; optionally change the graph layout.

**Expected demo seed (confirm before narration):**
- Devices **5** · Links **4** · Components **1** · SPOF nodes **3** ·
  Down devices **2**.
- Graph nodes coloured by status; links coloured by health; the legend shows
  router/host shapes and up/down colours.
- “Single points of failure: R1, R2, R3.”
- Link table with interfaces, subnets, and status.

**Narration:** “Topology is based on the stored links and statuses. Reachability
here is structural evidence, not a packet probe. The displayed data is the
clearly labelled demo source.”

---

### Scene 3 — AI Lab analytics (00:55–02:00)

**Page:** **AI lab → AI Lab · data & forecasting** (`pages/12_AI_Lab.py`).

**Do:**
1. On **① Data source**, keep **Simulated telemetry** and click
   **Build simulated dataset**. The defaults make a bounded dataset with
   injected, labelled fault windows.
2. On **② Inspect**, show the `simulated` source, synthetic-data warning,
   timestamps, series, quality information and task gates.
3. On **③ Configure & run**, keep the recommended supported task and selected
   series, then click **Run experiment**.
4. Review the computed result under **④ Results**, then show **⑤ History**.

The dataset is synthetic method-validation data, not a live measurement.
Describe only metrics shown by the completed run. Before recording, confirm
the experiment finishes and its result renders on screen.

### Scene 4 — Agentic Operations (conditional; allow extra time)

**Page:** **Agentic Operations → Network assistant & tools** →
**🧭 Agentic Operations** tab. Keep the AI Lab dataset loaded in the same
Streamlit session.

Only include this live segment after a provider preflight succeeds. Ask the
agent to summarize statistics, detect anomalies, and forecast a relevant
series. Show the validated plan, registered-tool results, experiment IDs and
saved run. The tools are bounded and read-only. A Groq timeout is a failed
run, not a diagnosis; the last provider check timed out, so this is not
currently a reliable demo step.

If the provider is unavailable, switch to **📚 Knowledge & Ask** and query
`which devices are down?` with the `demo` source. This deterministic query
shows a separate offline evidence lookup; it is not an agent run. Explain that
live provider-driven tool selection needs a reachable provider and must not be
faked with mocked output.

---

### Scene 5 — Structural impact and human-review boundary (02:00–03:30)

**Page:** **Network operations → Operations center** (`pages/11_Ops_Center.py`)
→ **🔁 Change loop** tab.

**Step 5.1 — create a supported, validated proposal**

1. Set **Data source = demo** at the top.
2. Click the **🔁 Change loop** tab.
3. Scroll to **“Propose a configuration change (validated only)”**.
4. Fill the form exactly:
   - **Device:** `R2`
   - **Description:** `Demo: administrative shutdown of R2 eth2 (to PC1)`
   - **Current/base config:** `hostname R2`
   - **Proposed config:**
     ```
     hostname R2
     interface eth2
      shutdown
     ```
     (The single leading space before `shutdown` is intentional; the parser
     accepts it inside the interface block.)
   - **Risk level:** `high`
5. Click **Validate & record proposal**.
6. Expected confirmation: **“Proposal recorded as validated.”**
7. The page reruns; the **Change requests** table now lists the request with
   status `validated`.

**Step 5.2 — show the read-only impact report**

1. The **“🧭 Topology impact analysis”** selectbox appears above the approval
   controls; select the new request (`R2 · validated`).
2. Pause on the report. The previously observed demo run showed the following;
   recheck the values in your current run:
   - Warning: “Topology source: demo (synthetic fixtures in the separate demo
     database; NOT live production topology)”.
   - **“Impact analysis: PARTIAL.”** Notes: 3 documented path(s) blocked;
     1 unknown-impact finding; 4 documented reachable devices analysed from
     R2; classification counts confirmed=2, potential=1, unknown=5.
   - **Directly targeted (confirmed):** R2 (device), eth2 (interface).
   - **Findings:** confirmed=2 · potential=1 · unknown=5. The caption explains
     that these are evidence classes, not risk scores.
   - **Unknown impact / insufficient evidence:** R2 (device-level uncertainty)
     and routes/services.
   - Expand **“Missing information, assumptions & limitations”**: the stored
     topology has devices, links, and subnets only; no route table, service
     inventory, or traffic flow is represented; the source is demo, not live.
3. Do not approve anything yet.

**Step 5.3 — show the Digital Twin result**

The **“🧪 Digital Twin simulation (read-only)”** panel renders directly below
the impact report. Pause on it. Previously observed demo output (recheck in
your run):

- **“Digital Twin: SIMULATED (modeled topology only; not a real-network
  success). 1 device(s) lose documented reachability from the target.”**
- Topology snapshot and fingerprints (demo source, structural reachability
  only).
- **Before:** 5 nodes · 1 available link · 1 reachable from the target.
- **After (simulated):** 5 nodes · 0 available links · 0 reachable.
- Red line: **“Predicted loss of documented reachability from the target:
  PC1.”**
- Warning: **“Newly isolated in the simulated topology: PC1, R2.”**
- Caption: “Lost reachable pair(s): 1.”
- Expand **“Simulated operations”**: `interface eth2` opens an interface block;
  `shutdown` is an `interface_shutdown` with effect “administratively disables
  the interface; documented links on it become unavailable in the simulation”.
- Expand **“Assumptions, limitations & conflicts”** and read one or two:
  structural only; no routing/traffic; a simulation is not proof of safety;
  no device contacted, no packet sent, no config applied.
- Bottom caption: “A simulated result is not proof that a real change is safe.
  It never authorizes, approves, applies, verifies or rolls back a change.”

**Narration:** “This result is structural. The demo baseline already has R1 and
R3 down, so R2–PC1 is the only available link; the what-if removes it and
reports the resulting isolation.”

---

### Scene 6 — Safety and conclusion (03:30–04:00)

**Page:** remain in **Network operations → Operations center** →
**🔁 Change loop** tab.

**Do (in order):**

1. Scroll up slightly and show the two safety notices:
   - **“Change loop is OFF – apply will refuse by design. Set
     NETPILOT_CHANGE_LOOP_ENABLED on a lab host to enable application.”**
   - **“Open mode: no signed-in identity. … approval is fail-closed because
     the app cannot prove the approver is a different person from the
     requester.”**
2. Optionally click **✅ Approve** once on the validated request. Expected
   durable warning:
   ```
   approval requires an authenticated principal; open mode cannot prove a
   distinct approver
   ```
   Pause on the refusal. Do not retry, and do not claim the change was applied.
3. Scroll to the approval/apply controls and show:
   - “Nothing approved and ready to apply.” (no approved request exists)
   - The audit trail is available in the request inspector, if time allows.
4. End on the disabled-apply notice and state that this demo does not apply
   changes.

For test and deployment status, refer to the current
[`PROJECT_STATUS_AND_HANDOVER.md`](PROJECT_STATUS_AND_HANDOVER.md). Do not
repeat old test counts or say the UI smoke passed: the latest attempt stalled
without a page result. Live provider, lab, PostgreSQL, browser, and full API
integration checks are not verified in that handover.

---

## 3. Timing sheet

| Time | Screen | Action |
|---|---|---|
| 00:00–00:25 | Overview → Network overview | Source and inventory |
| 00:25–00:55 | Network operations → Topology | Graph, links and dependencies |
| 00:55–02:00 | AI lab → AI Lab | Build a simulated dataset and run one experiment |
| Optional | Agentic Operations → Network assistant & tools | Inspect planned and executed tools after live provider preflight |
| 02:00–03:30 | Network operations → Operations center | Structural impact report and what-if result |
| 03:30–04:00 | Operations center → Change loop | Disabled-apply and approval boundary |

Use the **Knowledge & Ask** deterministic query if the provider is unavailable;
do not call it an agent run. If a live agent demo is required, arrange a
successful provider preflight before the presentation.

---

## 4. Do-not-say / do-not-show list

- Do not say “live network”, “production”, “real traffic”, or “verified on
  PostgreSQL” for this demo.
- Do not say the change was applied, approved, or rolled back. It was not.
- Do not call the Digital Twin a traffic or routing simulator; it is
  structural reachability only.
- Do not show `.env`, API keys, Streamlit Cloud secrets, GitHub settings, or
  raw provider responses.
- Do not fabricate an anomaly, a metric, a test result, or a successful apply.
- Keep the **DEMO MODE** warning visible; it is part of the honest
  presentation.
- If a page is empty or a feature is unavailable, say so and skip it.

---

## 5. Reset between takes

1. Stop Streamlit (`Ctrl+C`).
2. Delete the dedicated recording DB (not the live `data/netpilot.db`):
   ```powershell
   Remove-Item "C:\Users\USER\AppData\Local\Temp\netpilot_demo_recording.db*" -Force
   ```
3. Start Streamlit again with the same environment variables.
4. Reload the browser; the seed runs once per process.
5. Re-check §1.3 values before recording.

If you reuse a DB, the Change requests table will contain earlier proposals.
Use a new DB filename for each clean take.

---

## 6. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Pages show live mode | `NETPILOT_DEMO_MODE` not set in the Streamlit process; restart with it set. |
| No demo banner | Same as above; the banner is mandatory for this demo. |
| “No change requests for this source yet” | The proposal form is at the bottom of the Change loop tab; submit it, then the table appears. |
| Proposal status is `rejected` | A prohibited command or bad syntax was entered; use exactly the config in Scene 5, step 5.1. |
| No impact/Digital Twin panel | The request must be `validated` (or `approved`); check the status in the table. |
| Digital Twin says INSUFFICIENT EVIDENCE | The stored topology or interface context is missing; use a fresh demo DB and the exact R2/eth2 proposal. |
| AI page warns about `GROQ_API_KEY` | Use the offline Knowledge & Ask path; do not paste a key on camera. |
| Connectivity self-test says “NOT RUN – ping binary not available” | The runtime has no `ping`. Locally install `iputils-ping`; on Streamlit Community Cloud keep the root `packages.txt` (`iputils-ping`) and reboot the app. No packet was sent and nothing was stored, so say exactly that — never present it as a passed or failed test. |
| Approval warning does not appear | The request may have expired or is not `validated`; use a fresh DB. |

---

## 7. Post-recording honesty check

- [ ] The **DEMO MODE** warning is visible in every scene that shows data.
- [ ] No credential, `.env`, terminal secret, or raw provider response shown.
- [ ] Impact report labelled PARTIAL, not COMPLETE.
- [ ] Digital Twin labelled SIMULATED/PARTIAL, not “safe”.
- [ ] Approval refusal shown or the fail-closed notice shown.
- [ ] No claim of live lab, PostgreSQL, browser E2E, or successful apply.
- [ ] Final frame is the real application, not a slide or mockup.
