# NetPilot AI — 4-Minute Demo Runbook

**Purpose:** record the demo described in the project brief without inventing
data, exposing secrets, or claiming unverified live behaviour.

**Status of this runbook:** every screen, value, and refusal message below was
executed against the current code with a fresh demo database on 2026-10-26.
It is a recording script, not a mockup.

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
- Confirm the red **DEMO MODE** banner is visible on every page.

### 1.3 Verified demo dataset

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

### Scene 1 — Introduction (00:00–00:30)

**Page:** landing page (`app.py`), opened at the top.

**Do:**
1. Start on the landing page. Let the title, subtitle, and demo banner settle.
2. Slowly scroll to the **Safety guarantees** panel, pause, then scroll back.
3. Do not click a page link yet.

**What is actually on screen:**
- “NetPilot AI” title and the Phase 1–3 subtitle.
- Red demo banner: synthetic fixture data, not a real network.
- Three columns: Foundation, Analysis, Intelligence.
- Safety guarantees: lab allowlist, no arbitrary shell, deterministic verifier,
  labelled demo data, no stored credentials.

**Narration:** use the Scene 1 text as written.

---

### Scene 2 — Monitoring and topology (00:30–01:15)

**Page A:** Overview (`pages/1_Overview.py`).

**Do:**
1. Click **Overview** from the landing page.
2. Pause on the KPI row and the DEGRADED pill.
3. Scroll to “Devices at a glance” and pause on the table.
4. Do not press “Collect now” (demo mode disables it).

**Actual output:**
- Total devices **5** · Available (up) **3** · Unavailable (down) **2**.
- Network status **DEGRADED**; caption “3/5 devices up (2 down, 0 unknown)”.
- Last successful data collection line with a UTC timestamp and
  `3/5 devices up [source: demo]`.
- Table: PC1 up, PC2 up, R1 down, R2 up, R3 down; lab IPs shown.

**Page B:** Network Topology (`pages/2_Topology.py`).

**Do:**
1. Return to the landing page (browser back or the sidebar page list).
2. Click **Network Topology**.
3. Pause on the KPI row and the graph.
4. Switch the layout once to **Force-directed**, then back to **Layered tree**
   if time allows. Do not seed the optional large fabric.
5. Scroll to “Legend & structure” and pause on the link table.

**Actual output:**
- Devices **5** · Links **4** · Components **1** · SPOF nodes **3** ·
  Down devices **2**.
- Graph nodes coloured by status; links coloured by health; the legend shows
  router/host shapes and up/down colours.
- “Single points of failure: R1, R2, R3.”
- Link table with interfaces, subnets, and status.

**Narration:** use the Scene 2 text. Emphasise “the information shown depends
on the configured data source and operating mode; this is demo data, not live
production access.”

---

### Scene 3 — Investigation and diagnosis (01:15–02:15)

Use the **offline, deterministic** path. It is repeatable, needs no provider,
and is real application output.

**Page:** AI Network Assistant (`pages/10_AI_Network_Assistant.py`) →
**📚 Knowledge & Ask** tab.

**Do:**
1. Click **AI Network Assistant**.
2. Click the **📚 Knowledge & Ask** tab.
3. Keep **Evidence source = demo**.
4. In **Question**, type exactly: `which devices are down?`
5. Click **🔎 Answer from data**.
6. Pause on the green answer and the table.
7. Replace the question with: `single points of failure`
8. Click **🔎 Answer from data** again. Pause on the answer and table.

**Actual output (verified):**

Question 1:
```
[devices_down] 2 device(s) are down or of unknown state
```
Table: R1 (router, down, 10.1.1.1), R3 (router, down, 10.1.1.3).

Question 2:
```
[spof] 3 device SPOF(s), 4 bridge link(s).
```
Table: device SPOF R1, R2, R3; bridge links PC1–R2, PC2–R3, R1–R2, R1–R3.

**Why this scene is honest:** the answers come from the stored demo topology
and telemetry, not from an LLM. The caption on screen says “no LLM, nothing
invented” and “works even without a Groq key”.

**Optional live variant (only if you have a valid, rotated key and accept
non-determinism):**
- Go to **💬 New request**, evidence source **demo**, ask
  “Which devices are down and what should I investigate?”, click
  **🚀 Run investigation**.
- Show the structured sections: parsed intent, evidence, investigation and
  diagnosis, verifier, Digital Twin `not_run`, final answer.
- Never show the raw provider response, the `.env` file, or the key. If the
  run safe-stops, show that honestly instead of retrying to force a result.

**If no key is configured:** the page shows a yellow “GROQ_API_KEY is not
configured” warning, and a run reports a safe failure. That is the correct
behaviour; do not fake a diagnosis. Use the offline path above.

**Narration:** use the Scene 3 text. Add: “The offline answer is deterministic;
the AI workflow, when enabled, is review-only and reports `not_run` for the
Digital Twin rather than pretending a simulation ran.”

---

### Scene 4 — Digital Twin and impact analysis (02:15–03:15)

**Page:** Ops Center (`pages/11_Ops_Center.py`) → **🔁 Change loop** tab.

**Step 4.1 — create a supported, validated proposal**

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

**Step 4.2 — show the read-only impact report**

1. The **“🧭 Topology impact analysis”** selectbox appears above the approval
   controls; select the new request (`R2 · validated`).
2. Pause on the report. Actual output:
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

**Step 4.3 — show the Digital Twin result**

The **“🧪 Digital Twin simulation (read-only)”** panel renders directly below
the impact report. Pause on it. Actual output:

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

**Narration:** use the Scene 4 text. Add: “This result is structural. The demo
baseline already has R1 and R3 down, so R2–PC1 is the only available link; the
simulation removes it and reports the isolation honestly.”

---

### Scene 5 — Safety and conclusion (03:15–04:00)

**Page:** still Ops Center → Change loop tab, then the landing page.

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
4. Return to the landing page (`app.py`).
5. Scroll to **Safety guarantees** and end the recording there.

**Narration:** use the Scene 5 text. When you mention verification, say the
offline checks were run in this environment: **692 tests passed**, headless UI
smoke passed for all 12 scripts, safety/secret scans clean, offline AI-quality
evaluation 27/27 with all safety gates passing. Then add the boundary: these
are offline results and do not replace live validation of PostgreSQL,
Containerlab, browser E2E, or the provider.

---

## 3. Timing sheet

| Time | Screen | Action |
|---|---|---|
| 00:00–00:30 | Landing | Title, demo banner, safety panel |
| 00:30–00:50 | Overview | KPI row, DEGRADED, device table |
| 00:50–01:15 | Topology | KPI row, graph, link table |
| 01:15–01:40 | AI → Knowledge & Ask | “which devices are down?” |
| 01:40–02:15 | AI → Knowledge & Ask | “single points of failure” + boundary narration |
| 02:15–02:40 | Ops Center → Change loop | Propose R2/eth2 shutdown |
| 02:40–03:00 | Ops Center → Change loop | Impact report (PARTIAL) |
| 03:00–03:15 | Ops Center → Change loop | Digital Twin (SIMULATED, PC1 isolated) |
| 03:15–03:40 | Ops Center → Change loop | OFF banner, open-mode notice, approval refusal |
| 03:40–04:00 | Landing | Safety guarantees + conclusion |

If you run long, shorten the topology layout switch and the assumptions
expander, not the impact/Digital Twin result or the safety refusal.

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
- Do not hide the red demo banner; it is part of the honest presentation.
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
| Proposal status is `rejected` | A prohibited command or bad syntax was entered; use exactly the config in §4.1. |
| No impact/Digital Twin panel | The request must be `validated` (or `approved`); check the status in the table. |
| Digital Twin says INSUFFICIENT EVIDENCE | The stored topology or interface context is missing; use a fresh demo DB and the exact R2/eth2 proposal. |
| AI page warns about `GROQ_API_KEY` | Use the offline Knowledge & Ask path; do not paste a key on camera. |
| Connectivity self-test says “NOT RUN – ping binary not available” | The runtime has no `ping`. Locally install `iputils-ping`; on Streamlit Community Cloud keep the root `packages.txt` (`iputils-ping`) and reboot the app. No packet was sent and nothing was stored, so say exactly that — never present it as a passed or failed test. |
| Approval warning does not appear | The request may have expired or is not `validated`; use a fresh DB. |

---

## 7. Post-recording honesty check

- [ ] Red demo banner visible in every scene that shows data.
- [ ] No credential, `.env`, terminal secret, or raw provider response shown.
- [ ] Impact report labelled PARTIAL, not COMPLETE.
- [ ] Digital Twin labelled SIMULATED/PARTIAL, not “safe”.
- [ ] Approval refusal shown or the fail-closed notice shown.
- [ ] No claim of live lab, PostgreSQL, browser E2E, or successful apply.
- [ ] Final frame is the real application, not a slide or mockup.
