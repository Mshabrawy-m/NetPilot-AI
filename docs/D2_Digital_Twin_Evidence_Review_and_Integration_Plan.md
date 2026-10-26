# Phase D2 — Digital Twin Evidence Review and Safe Integration Plan

**Date:** 2026-10-26
**Branch / HEAD:** `main` / `f5dcb5f5518a7d69af1bb36242536c60d2170526`
**Upstream:** `origin/main` = `f5dcb5f`
**Working tree:** uncommitted by instruction (see §10).
**Scope:** independent review of the Phase D1 Digital Twin, narrowly scoped
read-only hardening, and a proposed future integration contract. No workflow
routing change, no execution, no deployment, no infrastructure, no Gate 3/Gate D.

---

## 1. Verdict

The D1 design is sound: it is a deterministic, isolated, structural
reachability simulator with explicit snapshot/change references and honest
failure states. It is not wired into `ai/graph.py`, and that remains true after
D2.

The audit nevertheless found concrete honesty and parser defects in the D1
implementation. All were fixed narrowly without changing workflow behavior:

1. An unsupported routing invariant (`preserve_lab_routing`) was treated as a
   structural pass/fail instead of unsupported.
2. An empty topology returned `invalid` instead of `insufficient_evidence`.
3. A target device stored as `down` was not reported as a conflict and could
   look like a normal simulation.
4. `end` was skipped as noise before it could close an interface block; an
   `ip ...` line wrongly closed the interface block; malformed supported
   commands (`shutdown now`, `interface eth1 extra`, bare `hostname`, ...) were
   silently accepted.
5. Conflicting duplicate links/devices trusted the first row, making the
   result depend on row order; duplicate-link metadata conflicts were described
   as “identical”.
6. `available_parallel_links` counted links that were not available.
7. Unknown device status was treated as available in the “available” graph.
8. The ABC adapter could raise instead of returning `error`, ignored a
   caller/snapshot `topology_ref` mismatch, and did not expose the full report.
9. The UI used green “success” styling for a simulation.

D2 fixes these in `services/digital_twin.py`,
`ai/simulation/__init__.py`, `tests/test_digital_twin.py`, and the read-only
panel in `pages/11_Ops_Center.py`. `ai/graph.py` is unchanged and still uses
`UnavailableDigitalTwin`.

**Current D2 verdict: PASS for the engine’s documented structural contract;
NOT VERIFIED for live/stale topology detection, real-network behavior, and
workflow integration, which are explicitly deferred.**

---

## 2. Canonical requirements reviewed

| Source | Requirement used in this review |
|---|---|
| `docs/NetPilot_AI_Advanced_Roadmap.md` v2.0 | Safety chain: propose → deterministic verify → Digital-Twin simulate → human approval → isolated lab apply → post-change verify → auto-rollback. “Real” Digital Twin; evidence over vibes; honesty. |
| `docs/FEATURE_ACCEPTANCE_MATRIX.md` | E4 Digital Twin acceptance, E3/E5 safety/UI requirements, B1–B4 preservation, Phase C evidence rules. |
| `docs/FEATURE_FREEZE_REPORT.md` | E4 was the confirmed Phase 4 gap; conditional freeze; live Gate 3/Gate D and PostgreSQL remain external evidence. |
| `docs/NetPilot_Phase_3_Multi_Agent_AI_Safe_Operations_EN.md` §7 | Digital-Twin boundary; `not_run` is the honest default until a real simulator exists. |
| `docs/D1_Digital_Twin_Implementation_Report.md` | D1 claims, architecture, status vocabulary, and 35-test baseline. |

D1 claims that are corrected or clarified by D2:

| D1 claim | D2 correction |
|---|---|
| `preserve_lab_routing` and `preserve_reachability` are computed structurally | `preserve_lab_routing` is **unsupported**; routing semantics are not modeled. Only `preserve_reachability` is evaluated. |
| `insufficient_evidence` covers no usable topology | D1 code returned `invalid` for an empty topology; D2 returns `insufficient_evidence`. |
| Duplicate rows are collapsed and parallel links preserved | Conflicting duplicate metadata is now collapsed conservatively to `unknown` rather than trusting the first row; parallel availability is counted separately. |
| `not_simulated` covers “only context/metadata lines” | Context-only changes are `not_simulated`; metadata-only changes are `simulated` with `topology_effect: none` and an explicit reason. |
| Adapter returns the extended result | D2 adds fail-closed `error`, `topology_ref` mismatch rejection, and the additive `details` report. |

---

## 3. Findings (PASS / FAIL / NOT VERIFIED)

Statuses below describe the **D2 final state** after the narrow fixes. The
“D1 issue” column records what the audit found before the fix.

| # | Finding | D1 issue | D2 verdict | Evidence |
|---|---|---|---|---|
| F1 | Simulation uses an explicit, identifiable topology snapshot and proposed change | None | **PASS** | `simulate_change` takes `devices`, `links`, `device_name`, `proposed_lines`, `base_lines`, `topology_ref`, `scenario` (`services/digital_twin.py:594`). Reports include `topology_ref`, `topology_fingerprint`, `change_ref.fingerprint`. Tests: `test_valid_shutdown_disconnects_a_leaf`, `test_repeatability_for_identical_inputs`, `test_fingerprints_are_stable_and_input_sensitive`. |
| F2 | Topology/change fingerprints are deterministic and change when relevant inputs change | None | **PASS** | `_fingerprint` uses `json.dumps(..., sort_keys=True)` + SHA-256 (`:129`); `_topology_fingerprint` (`:357`); normalized rows are sorted. Test reorders devices/links, changes link status/interface, and changes proposed lines. |
| F3 | Duplicate links and genuine parallel links are handled consistently | First row won on conflicting duplicates; parallel count included unavailable links | **PASS** | `_normalise_links` collapses canonical endpoint/interface keys, reports `duplicate_link` / `duplicate_link_metadata_conflict`, and collapses conflicting status/subnet to `unknown`/`None` (`:250–355`). `available_parallel_links` is separate (`:857`). Tests: `test_duplicate_links_are_collapsed_and_parallel_links_kept`, `test_conflicting_duplicate_links_are_order_independent`, `test_parallel_link_availability_is_counted_separately`, `test_parallel_links_are_not_reported_as_bridges`. |
| F4 | `shutdown` / `no shutdown` semantics match documented stored-link state | `end` skipped as noise; `ip` reset context; malformed commands accepted | **PASS** | `_parse_operations` handles exact `end` before `is_noise`, tracks interface context, and only supported operations become topology ops (`:432–570`). `shutdown` removes only links stored `up`; `no shutdown` never invents recovery. Tests: `test_valid_shutdown_disconnects_a_leaf`, `test_no_shutdown_does_not_invent_recovery`, `test_last_interface_state_wins`, `test_end_closes_interface_context`. |
| F5 | Unsupported operations always produce honest unsupported/partial/not-simulated results | Malformed supported commands were silently modeled | **PASS** | Strict token counts for `interface`, `no interface`, `shutdown`, `no shutdown`, `exit`, `end`, `hostname`, `description`; malformed lines are `supported: false`. Tests: `test_unsupported_operation_is_not_simulated`, `test_only_unsupported_operations_are_not_simulated`, `test_malformed_supported_lines_are_unsupported`, `test_malformed_context_end_does_not_leave_interface_open`. |
| F6 | Missing/contradictory evidence prevents unjustified success claims | Empty topology `invalid`; down target not flagged; unsupported invariant still `simulated` | **PASS** | Status order now checks no usable devices/links before target absence (`:899–955`); `target_device_down` conflict (`:699`); unsupported invariants downgrade to `partial` (`:949`). Tests: `test_empty_topology_is_insufficient_evidence`, `test_target_device_down_is_partial_not_success`, `test_routing_invariant_is_reported_unsupported`, `test_unknown_status_is_uncertainty_not_availability`. |
| F7 | Before/after reachability, isolation, component splits, bridge links, articulation points, alternate paths are consistent | None | **PASS (covered cases)** | `_simple_graph`, `_summary`, split-component loop, `nx.bridges`, `nx.articulation_points`, alternate-path loop (`:382–870`). Tests: `test_disconnects_a_previously_reachable_component`, `test_cycle_preserves_reachability_via_alternate_path`, `test_parallel_links_are_not_reported_as_bridges`. Exhaustive property-based graph correctness is **NOT VERIFIED** (no independent oracle). |
| F8 | UI distinguishes simulation from approval, execution, verification, rollback, and real success | Green success styling for simulation | **PASS** | Panel is read-only, rendered after impact analysis and before approval controls; caption explicitly says “NOT approval, execution, post-change verification, rollback, or a real-network success” (`pages/11_Ops_Center.py:182–217`). D2 changed `simulated` and “no loss” from `st.success` to `st.info` (`:196–199`, `:238–241`). UI smoke guard passes. |
| F9 | Engine isolated from network I/O, shell execution, config mutation, execution services | None | **PASS** | AST import test forbids `socket`, `subprocess`, `os`, `shutil`, `requests`, `urllib`, `http`, `paramiko`, `docker`, `network.lab_executor`, `services.change_execution`, `services.change_service`, `services.ops_center_actions`; monkeypatch tests explode on socket/subprocess/executor/apply/probe calls. `tools/safety_scan.py` exit 0. |
| F10 | Read-only session wrapper fails closed and never writes | None | **PASS** | `simulate_request` only calls `repository.get_devices/get_links`; malformed JSON becomes `input_error`; exceptions become `STATUS_ERROR` (`:1111–1185`). Tests: `test_simulate_request_is_read_only`, `test_simulate_request_reports_invalid_stored_json`, `test_simulate_request_fails_closed_on_repository_error`, `test_failure_does_not_enable_or_bypass_approval_or_apply`. |
| F11 | ABC adapter is honest and fail-closed | Could raise; ignored `topology_ref` mismatch; no full detail | **PASS** | Adapter wraps `simulate_change` in `try/except` returning `STATUS_ERROR`, rejects mismatched `topology_ref` with `topology_ref_mismatch`, and sets `details=report` (`:1190–1285`). Tests: `test_adapter_returns_structured_simulation_result`, `test_adapter_fails_closed_on_engine_exception`, `test_adapter_refuses_mismatched_topology_ref`, `test_adapter_accepts_pydantic_like_proposal`, `test_adapter_refuses_non_ok_proposal`. |
| F12 | AI workflow remains intentionally unwired | None | **PASS** | `ai/graph.py:43` still constructs `UnavailableDigitalTwin`; `ai/graph.py:168–176` still calls `self.twin.simulate` with the Phase 3 arguments; `tests/test_workflow_routing.py:86` still asserts `digital_twin_status == "not_run"`; B4 `digital_twin_not_run` still passes. No D2 edit to `ai/graph.py`. |
| F13 | Stale topology / fingerprint mismatch detection | Not implemented in D1 | **NOT VERIFIED / NOT IMPLEMENTED** | No persistence of snapshot fingerprint with the change request/approval, and no TTL/mismatch check before simulation. The adapter only rejects an explicit `topology_ref` mismatch. This is a future integration-contract requirement (§5), not a D2 workflow change. |
| F14 | Live Gate 3/Gate D, PostgreSQL, real browser E2E, live provider, Docker/Containerlab/WSL, real device I/O | Not available/authorized | **NOT VERIFIED** | Not executed by design. Headless UI smoke and offline B4 evaluation are not live evidence. |
| F15 | Unknown device status is uncertainty, not availability | Unknown devices counted as available | **PASS** | `_simple_graph` requires known-`up` endpoints for the available graph; the possible graph admits unknown but never `down` (`:382–415`). Test: `test_unknown_device_status_is_not_available`. |
| F16 | Input ordering does not unexpectedly change results | Conflicting duplicate rows trusted first row | **PASS** | Normalized devices/links are sorted; conflicting duplicate status/type/subnet are collapsed to unknown/None. Tests: `test_fingerprints_are_stable_and_input_sensitive`, `test_conflicting_duplicate_devices_are_order_independent`, `test_conflicting_duplicate_links_are_order_independent`. |
| F17 | Metadata-only changes are not reported as topology success | None | **PASS** | Metadata-only change is `simulated` with `topology_effect: none` and “no topology-affecting operation” reason. Test: `test_metadata_only_change_is_not_a_topology_success`. |
| F18 | Unknown device/interface references are honest | None | **PASS** | Missing target → `invalid`; target with no documented links → `insufficient_evidence`; undocumented interface → `undocumented_interface` + `partial`; unknown link status → `partial`. Tests: `test_missing_device_is_invalid`, `test_disconnected_device_is_insufficient_evidence`, `test_undocumented_interface_is_partial_not_success`. |

---

## 4. Defects found and narrow fixes

All fixes are in the simulation engine, its adapter/result boundary, its tests,
and the read-only UI panel. No approval, execution, rollback, workflow routing,
or runtime flag was changed.

### 4.1 Invariant honesty

- **Defect:** `_SUPPORTED_INVARIANTS` included `preserve_lab_routing`, so the
  engine could report `passed`/`failed` for routing semantics it does not model.
- **Fix:** only `preserve_reachability` is supported. `preserve_lab_routing`
  returns `status: "unsupported"` with a routing-specific detail. Any
  unsupported requested invariant downgrades an otherwise clean result to
  `partial` and adds an explicit reason.
- **Evidence:** `services/digital_twin.py:78–84`, `:870–884`, `:949–955`,
  `:979–985`; `tests/test_digital_twin.py::test_routing_invariant_is_reported_unsupported`.

### 4.2 Empty topology and target-down evidence

- **Defect:** an empty topology reached the target-absent branch and returned
  `invalid`; a target stored `down` could produce a normal `simulated` result
  with no conflict.
- **Fix:** status order now checks “no usable devices/links” before target
  absence, yielding `insufficient_evidence`; a `target_device_down` conflict is
  added, and the result is `partial`. Invariants are `unknown` for a down
  target, never `passed`.
- **Evidence:** `services/digital_twin.py:699–706`, `:899–955`, `:877–884`;
  tests `test_empty_topology_is_insufficient_evidence`,
  `test_target_device_down_is_partial_not_success`.

### 4.3 Parser/context correctness

- **Defect:** `is_noise("end")` is true, so `end` was skipped before it could
  close an interface block. An `ip ...` line reset the interface context, so a
  following `shutdown` was treated as outside the block. Malformed supported
  commands were accepted as valid.
- **Fix:** exact `end` is handled before the noise check; only real context
  heads reset the interface context; malformed `interface`, `no interface`,
  `shutdown`, `no shutdown`, `exit`/`end`, `hostname`, and `description` lines
  are unsupported; a malformed `exit`/`end` still closes the context so a later
  command cannot be misattributed.
- **Evidence:** `services/digital_twin.py:432–570`; tests
  `test_end_closes_interface_context`,
  `test_unsupported_non_context_line_keeps_interface_context`,
  `test_malformed_supported_lines_are_unsupported`,
  `test_malformed_context_end_does_not_leave_interface_open`.

### 4.4 Duplicate and parallel link/device handling

- **Defect:** duplicate rows with conflicting status/subnet trusted the first
  row, so row order could change availability; duplicate metadata conflicts
  were described as “identical”; `parallel_links` counted unavailable links.
- **Fix:** conflicting duplicate status/type/subnet are collapsed to
  `unknown`/`None`, making the result order-independent; the conflict detail
  states what was collapsed. `available_parallel_links` counts only links that
  are stored `up` with known-up endpoints.
- **Evidence:** `services/digital_twin.py:184–355`, `:850–860`; tests
  `test_conflicting_duplicate_links_are_order_independent`,
  `test_conflicting_duplicate_devices_are_order_independent`,
  `test_duplicate_link_metadata_conflict_is_reported`,
  `test_parallel_link_availability_is_counted_separately`.

### 4.5 Unknown device status

- **Defect:** the available graph excluded only `down` devices, so an unknown
  device could still contribute available edges, contradicting “unknown is
  uncertainty, not availability”.
- **Fix:** the available graph requires known-`up` endpoints; the possible
  graph admits unknown links/devices but never a device stored `down`.
- **Evidence:** `services/digital_twin.py:382–415`; test
  `test_unknown_device_status_is_not_available`.

### 4.6 Adapter and result boundary

- **Defect:** the adapter could raise out of `simulate`, ignored a mismatched
  `topology_ref`, and did not expose the full report.
- **Fix:** adapter catches engine exceptions and returns `STATUS_ERROR` with a
  `simulation_error` conflict; rejects a caller/snapshot `topology_ref`
  mismatch as `invalid`; sets the additive `details` field to the full report.
  `SimulationResult.details` is added with an empty default, so
  `UnavailableDigitalTwin` and Phase 3 behavior are unchanged.
- **Evidence:** `ai/simulation/__init__.py:15–57`;
  `services/digital_twin.py:1190–1285`; tests
  `test_adapter_returns_structured_simulation_result`,
  `test_adapter_fails_closed_on_engine_exception`,
  `test_adapter_refuses_mismatched_topology_ref`.

### 4.7 UI wording

- **Defect:** a `simulated` result and “no documented reachability loss” used
  green `st.success`, which can read as real-network success.
- **Fix:** both are now neutral `st.info` with explicit “modeled topology only”
  / “not a safety guarantee” wording. The panel remains read-only and before
  approval controls.
- **Evidence:** `pages/11_Ops_Center.py:182–217`, `:238–241`; UI smoke guard
  `pages/11_Ops_Center.py: impact report visible before approval and
  demo-labelled` passes.

---

## 5. Proposed future integration contract (not implemented)

This section is a contract for a later, separately reviewed phase. Nothing in
this section is wired into `ai/graph.py` in D2.

### 5.1 Required topology snapshot inputs

A future caller must pass an explicit, immutable snapshot, not just a data
source label:

- **Devices:** `name`, `type`, `status` ∈ `{up, down, unknown}`.
- **Links:** `device_a`, `if_a`, `device_b`, `if_b`, `subnet`, `status` ∈
  `{up, down, unknown}`.
- **Snapshot identity:** `topology_ref` (stable identifier, e.g.
  `stored:<snapshot_id>` or `demo:<version>`) and
  `topology_fingerprint` (SHA-256 from the normalized snapshot).
- **Capture metadata:** capture time/source/version, so staleness can be
  evaluated.
- **Proposed change:** target device, normalized proposed lines, optional base
  lines, request UUID, scenario, and a `change_fingerprint`.
- **Invariants:** `preserve_reachability` only. `preserve_lab_routing` must be
  treated as unsupported, not passed.

### 5.2 Associating the change with the exact snapshot

1. Capture the topology snapshot at proposal/verification time.
2. Store `topology_ref` and `topology_fingerprint` with the change request and
   with the human approval record.
3. Store the normalized proposed lines and `change_fingerprint` with the
   approval record.
4. Before simulation, reload the snapshot and recompute both fingerprints.
5. Pass the exact snapshot rows and proposal to `simulate_change` (or construct
   `TopologyDigitalTwin` with that snapshot).
6. Persist the simulation report, including both fingerprints and
   `not_proof_of_safety: true`.

### 5.3 Stale topology and mismatch detection

Not implemented in D1/D2. A future integration must:

- **Topology fingerprint mismatch:** return `insufficient_evidence` (or
  `error`) with a `topology_fingerprint_mismatch` conflict; do not run a
  normal simulation and do not report success.
- **Topology reference mismatch:** reject as `invalid` (the adapter already
  does this when both references are supplied).
- **Stale snapshot:** if the capture time exceeds a configured TTL, return
  `insufficient_evidence` with a `stale_topology` conflict.
- **Change fingerprint mismatch:** if the proposal changed after approval,
  return `invalid`/`error` and require re-approval.
- **No snapshot:** preserve `not_run`; never fabricate a topology.

### 5.4 Status propagation to the AI workflow

| Engine status | Workflow meaning | Must not be treated as |
|---|---|---|
| `not_run` | No simulation was performed | safe / approved / verified |
| `simulated` | Structural model ran on the stated snapshot | real-network success, approval, execution, verification |
| `partial` | Missing/unknown evidence, unsupported operation/invariant, or conflict | a pass |
| `insufficient_evidence` | The snapshot/target cannot support the simulation | a pass |
| `invalid` | Bad input, target absence, or provenance mismatch | a pass |
| `error` | Simulation failed closed | a pass |
| `not_simulated` | No supported state change was proposed | a pass |

A future workflow must surface the status, reasons, conflicts, and fingerprints
in the run detail and final answer. It must never collapse these states into a
boolean “safe” flag. A failed invariant is a finding for human review, not an
automatic execution authorization.

### 5.5 B4 expectations and tests that need deliberate revision

These are **not** changed in D2; they must be revised only when integration is
authorized:

- `evaluation/runner.py:674–676` asserts `digital_twin_status == "not_run"`.
- `evaluation/runner.py:697–698` allows only
  `("not_run", "passed", "failed", "inconclusive")` in structured-output
  validity; the new vocabulary is `simulated` / `partial` /
  `insufficient_evidence` / `invalid` / `not_simulated` / `error`.
- `tests/test_workflow_routing.py:86` asserts `digital_twin_status == "not_run"`.
- `tools/phase3_lab_validation.py:196` expects `digital_twin_status == "not_run"`.
- `ai/graph.py:168–176` still passes `topology_ref=self.data_source`,
  `invariants=["preserve_lab_routing"]`, and logs the action
  `simulation_not_run` regardless of the returned status.
- B4 dataset/fakes and UI smoke expectations may need new status cases; they
  must remain offline/synthetic unless a live-provider run is separately
  authorized.

### 5.6 Why a simulation result cannot authorize execution

- It is structural reachability over documented rows only; it models no
  routing protocol, route table, ACL, NAT, firewall, service, QoS, traffic
  flow, convergence, timing, or packet loss.
- The stored topology may be stale; D2 does not implement stale detection.
- It contacts no device, sends no packet, and applies no configuration.
- `not_proof_of_safety` is always true; a `simulated` status is not a safety
  proof.
- Human approval, the disabled change-loop/lab-execution flags, isolated-lab
  application, post-change verification, and rollback remain mandatory.
- A simulation may inform or veto a human decision; it may never approve,
  execute, verify, or roll back.

### 5.7 UI contract

- Render the panel read-only, after impact analysis and before approval
  controls.
- Show status, snapshot/change refs and fingerprints, before/after structural
  counts, reachability loss, newly isolated devices, unsupported operations,
  conflicts, and assumptions/limitations.
- Use neutral information styling for `simulated`; never green “success”.
- Do not enable or imply approval/apply/verify/rollback because of a
  simulation.
- Keep the explicit “not approval / not execution / not verification / not a
  real-network success” disclaimer.

---

## 6. Edge-case matrix (executed)

| Case | Observed D2 behavior | Test |
|---|---|---|
| Empty topology | `insufficient_evidence`; no loss invented | `test_empty_topology_is_insufficient_evidence` |
| Target absent from non-empty snapshot | `invalid`; target-absent reason | `test_missing_device_is_invalid` |
| Target with no documented links | `insufficient_evidence` | `test_disconnected_device_is_insufficient_evidence` |
| Target device stored `down` | `partial`; `target_device_down`; invariant `unknown` | `test_target_device_down_is_partial_not_success` |
| Unknown link status | `partial`; available graph excludes it; possible graph admits it | `test_unknown_status_is_uncertainty_not_availability` |
| Unknown device status | `partial`; available graph excludes it; possible graph admits it | `test_unknown_device_status_is_not_available` |
| Duplicate identical link | Collapsed; `duplicate_link`; `partial` | `test_duplicate_links_are_collapsed_and_parallel_links_kept` |
| Duplicate link with conflicting status/subnet | Collapsed to `unknown`/`None`; order-independent | `test_conflicting_duplicate_links_are_order_independent` |
| Duplicate device with conflicting status/type | Collapsed to `unknown`/`None`; order-independent | `test_conflicting_duplicate_devices_are_order_independent` |
| Genuine parallel links | Kept; available count separate; not a bridge | `test_parallel_link_availability_is_counted_separately`, `test_parallel_links_are_not_reported_as_bridges` |
| Parallel link through unknown device | Not counted as available redundancy; no false alternate path | `test_parallel_link_through_unknown_device_is_not_available` |
| `interface eth1` → `ip address ...` → `shutdown` | `ip` unsupported; `shutdown` still modeled inside interface; `partial` | `test_unsupported_non_context_line_keeps_interface_context` |
| `interface eth1` → `end` → `shutdown` | `end` closes context; `shutdown` unsupported; no false outage | `test_end_closes_interface_context` |
| `interface eth1` → `end extra` → `shutdown` | Malformed end closes context; `shutdown` unsupported | `test_malformed_context_end_does_not_leave_interface_open` |
| `shutdown now`, `interface eth1 extra`, bare `hostname`, `no interface` | Unsupported; never modeled | `test_malformed_supported_lines_are_unsupported` |
| `no shutdown` on stored-down link | `cannot_infer_recovery`; no recovery invented | `test_no_shutdown_does_not_invent_recovery` |
| `shutdown` then `no shutdown` / reverse | Last interface state wins | `test_last_interface_state_wins` |
| Metadata-only change | `simulated`, `topology_effect: none`, explicit reason | `test_metadata_only_change_is_not_a_topology_success` |
| Only unsupported lines | `not_simulated` | `test_only_unsupported_operations_are_not_simulated` |
| Repeated identical input | Byte-identical JSON | `test_repeatability_for_identical_inputs` |
| Reordered device/link rows | Same topology fingerprint | `test_fingerprints_are_stable_and_input_sensitive` |
| Changed link status/interface or proposed lines | Fingerprint changes | `test_fingerprints_are_stable_and_input_sensitive` |
| Adapter engine exception | `error`; `simulation_error`; no raise | `test_adapter_fails_closed_on_engine_exception` |
| Adapter `topology_ref` mismatch | `invalid`; `topology_ref_mismatch` | `test_adapter_refuses_mismatched_topology_ref` |
| Invalid stored JSON | `input_error`; never `simulated` | `test_simulate_request_reports_invalid_stored_json` |
| Repository failure | `error`; no writes | `test_simulate_request_fails_closed_on_repository_error` |

---

## 7. Verification (executed in this D2 session)

All commands were run from `E:\Graduation Projects\NetPilot AI` with the
project virtual environment. “Exit” is the process exit code.

| Command | Result | Exit |
|---|---|---|
| `.\.venv\Scripts\python.exe -m py_compile services\digital_twin.py pages\11_Ops_Center.py tests\test_digital_twin.py ai\simulation\__init__.py` | compile OK | 0 |
| `.\.venv\Scripts\python.exe -m pytest tests\test_digital_twin.py --collect-only -q` | **59 tests collected** | 0 |
| `.\.venv\Scripts\python.exe -m pytest tests\test_digital_twin.py -q` | **59 passed** (quiet mode omits final summary; collect count 59) | 0 |
| `.\.venv\Scripts\python.exe -m pytest tests\test_workflow_routing.py tests\test_ai_evaluation.py tests\test_impact_analysis.py tests\test_ops_center_actions.py tests\test_change_service.py tests\test_change_execution.py -q` | **158 passed** (B1–B4 regressions) | 0 |
| `.\.venv\Scripts\python.exe -m pytest --collect-only -q` | **609 tests collected** | 0 |
| `.\.venv\Scripts\python.exe -m pytest --tb=short -q` | **609 collected / passed**, no failures/errors; 13 pre-existing `datetime.utcnow()` deprecation warnings | 0 |
| `.\.venv\Scripts\python.exe tests\ui_smoke_check.py` | **12/12 page targets `[OK]`; 16 action/flash guards `[OK]`; demo banner OK**; Digital Twin panel + “not approval” guard OK | 0 |
| `.\.venv\Scripts\python.exe tools\safety_scan.py` | OK: no execution/deployment primitives in `ai/`; evidence blocks wrapped | 0 |
| `.\.venv\Scripts\python.exe tools\secret_scan_check.py` | Secret scan clean | 0 |
| `.\.venv\Scripts\python.exe tools\ai_eval_safety_scan.py` | OK: evaluation harness has no shell/provider/approval execution primitives | 0 |
| `.\.venv\Scripts\python.exe tools\ai_quality_eval.py` | **27/27 cases passed**; 6/6 safety gates; groundedness 9/10 (90%), all other metrics 100%; `live_network_tested=False`, `streamlit_tested=False`, `provider_tested=False` | 0 |
| `git diff --check` | No whitespace errors; only CRLF advisory warnings | 0 |

### Checks not executed (blocked / out of scope)

- Live Gate 3 / Gate D acceptance.
- PostgreSQL-backed integration.
- Real-browser end-to-end test (UI smoke is headless Streamlit `AppTest`).
- Live-provider AI-quality evaluation (B4 is offline/synthetic).
- Docker, Containerlab, WSL, or any lab infrastructure startup.
- Real device access or network configuration changes.
- `tools/phase3_lab_validation.py` / `tools/phase4_change_loop_validation.py`
  live harness runs.
- Stale-topology/TTL/fingerprint-persistence tests (not implemented).

These are **not** claimed as passed anywhere in this report.

---

## 8. Remaining blockers

1. **No workflow integration:** `ai/graph.py` still uses
   `UnavailableDigitalTwin`; B4 and workflow tests still expect `not_run`.
2. **No snapshot persistence:** topology/change fingerprints are not stored
   with the change request or approval, so stale/mismatch detection cannot yet
   be enforced end to end.
3. **No live evidence:** Gate 3/Gate D, PostgreSQL, real browser E2E, live
   provider, and real lab I/O remain external and unavailable.
4. **Structural-only model:** routing, ACLs, NAT, services, traffic, timing,
   convergence, and packet loss are explicitly not modeled.
5. **Phase C conditional freeze remains:** D1/D2 close the E4 implementation
   gap but do not replace live Gate 3/Gate D acceptance evidence.

---

## 9. Acceptance criteria for a future integration phase

A later phase may integrate the engine only when all of the following are met:

1. **Snapshot persistence:** capture and store `topology_ref`,
   `topology_fingerprint`, capture time/source, proposed lines, and
   `change_fingerprint` with the change request and approval.
2. **Mismatch/staleness enforcement:** fingerprint mismatch, reference
   mismatch, stale TTL, and post-approval proposal changes fail closed with
   explicit conflicts and no success claim.
3. **Workflow wiring:** `ai/graph.py` uses `TopologyDigitalTwin` with the exact
   stored snapshot; action labels and status propagation are status-aware;
   `preserve_lab_routing` is unsupported; `preserve_reachability` is the only
   evaluated invariant.
4. **Status honesty:** every status (`not_run`, `simulated`, `partial`,
   `insufficient_evidence`, `invalid`, `not_simulated`, `error`) is represented
   distinctly in run detail, final answers, and UI; none is mapped to “safe” or
   “approved”.
5. **B4/test revision:** `evaluation/runner.py`, `tests/test_workflow_routing.py`,
   `tools/phase3_lab_validation.py`, B4 fakes/dataset, and UI smoke are
   deliberately updated and green. The old `digital_twin_not_run` assertion is
   not silently weakened.
6. **Execution remains gated:** no simulation result enables
   `change_loop_enabled`/`lab_execution_enabled`, approval, execution,
   verification, or rollback. Human approval and the existing gates remain
   mandatory.
7. **Verification:** focused tests, B1–B4 regressions, full suite, UI smoke,
   safety scans, secret scan, and `git diff --check` pass; live Gate 3/Gate D
   evidence is separately authorized and recorded.
8. **Documentation:** D1/D2 claims are updated where semantics changed (notably
   `preserve_lab_routing`, empty-topology status, metadata-only status, and
   duplicate handling).

---

## 10. Git state (precise)

| Item | Value |
|---|---|
| Branch | `main` |
| HEAD | `f5dcb5f5518a7d69af1bb36242536c60d2170526` |
| Upstream | `origin/main` = `f5dcb5f` |
| Staged changes | **0** |
| Tracked modifications | **18** files (B1–B4/D1/D2 working set) |
| Untracked entries | B1–B4 files, both Phase C audit documents, D1 report, D2 service/test/report, `evaluation/`, `services/impact_service.py`, `services/ops_center_actions.py`, and the B4 tools/tests |
| Commits / pushes / deploys | **none** |
| `git diff --check` | exit 0; CRLF advisory warnings only |

D2 touched only:

- `services/digital_twin.py` (hardening; version `d2-1.1`)
- `tests/test_digital_twin.py` (D2 regressions; 59 tests)
- `ai/simulation/__init__.py` (additive `details` field)
- `pages/11_Ops_Center.py` (neutral simulation styling)
- `docs/D2_Digital_Twin_Evidence_Review_and_Integration_Plan.md` (this report)

`ai/graph.py` was **not** changed in D2. All B1–B4 changes, both Phase C
documents, and the D1 report were preserved. Nothing was staged, committed,
pushed, reset, cleaned, discarded, or deployed.

---

## 11. Explicit non-actions

- No connection of the Digital Twin to `ai/graph.py`.
- No change to `digital_twin_status == "not_run"` expectations.
- No approval, execution, verification, or rollback action.
- No change to `change_loop_enabled` or `lab_execution_enabled`.
- No Docker, Containerlab, WSL, PostgreSQL, or lab infrastructure startup.
- No device access or real network configuration.
- No Streamlit Cloud/secrets change.
- No stage, commit, push, reset, clean, or deploy.
