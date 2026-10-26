# Phase D3 — Digital Twin Snapshot Integrity and Staleness Detection

**Date:** 2026-10-26
**Branch / HEAD:** `main` / `f5dcb5f5518a7d69af1bb36242536c60d2170526`
**Upstream:** `origin/main` = `f5dcb5f`
**Working tree:** uncommitted by instruction (see §11).
**Scope:** deterministic, fail-closed snapshot/change identity and freshness
validation for the Phase D1/D2 Digital Twin engine. No workflow wiring, no
execution, no deployment, no infrastructure, no Gate 3/Gate D.

---

## 1. Verdict

D3 adds an **in-memory, additive integrity layer** to
`services/digital_twin.py`. It can identify a topology snapshot and a proposed
change, re-derive both fingerprints, evaluate a freshness policy when a genuine
capture timestamp is supplied, and refuse to report an unqualified success for
a missing, malformed, mismatched, stale, future-dated, or unverifiable binding.

**D3 verdict: PASS for the implemented in-memory snapshot-integrity and
staleness contract. NOT VERIFIED / NOT IMPLEMENTED for persisted snapshot
identity, end-to-end TTL enforcement, and live infrastructure evidence.** No
database table, migration, or external service was added. The persistence gap
is documented as an integration contract in §6 rather than papered over.

The engine remains **unwired** from `ai/graph.py`. `digital_twin_status ==
"not_run"` and B4 `digital_twin_not_run` are unchanged. No approval, execution,
verification, rollback, or runtime flag was enabled.

---

## 2. Canonical requirements reviewed

| Source | Requirement used in this phase |
|---|---|
| `docs/NetPilot_AI_Advanced_Roadmap.md` v2.0 | Safety chain: propose → deterministic verify → Digital-Twin simulate → human approval → isolated lab apply → post-change verify → auto-rollback. Evidence over vibes; honesty; no fabricated freshness. |
| `docs/FEATURE_ACCEPTANCE_MATRIX.md` | E3/E4/E5 acceptance and evidence rules; B1–B4 preservation; Phase C evidence discipline. |
| `docs/FEATURE_FREEZE_REPORT.md` | E4 was the confirmed Phase 4 gap; conditional freeze; live Gate 3/Gate D and PostgreSQL remain external evidence. |
| `docs/NetPilot_Phase_3_Multi_Agent_AI_Safe_Operations_EN.md` §7 | Digital-Twin boundary; `not_run` is the honest default until a real simulator is integrated. |
| `docs/D1_Digital_Twin_Implementation_Report.md` | D1 engine claims, status vocabulary, and fingerprint/ref contract. |
| `docs/D2_Digital_Twin_Evidence_Review_and_Integration_Plan.md` §5 | D3 gap definition: snapshot persistence, stale/mismatch detection, status propagation, and the requirement that no snapshot means `not_run`. |

D2 defined the gap; D3 implements the **validation primitive** only. It does
not claim that the persistence or workflow parts of the D2 contract are done.

---

## 3. What D3 adds (and does not add)

### 3.1 Added to the engine

- `SNAPSHOT_BINDING_VERSION = "d3-1"` and `SIMULATOR_VERSION = "d3-1.0"`.
- Integrity statuses: `verified`, `unknown_freshness`, `stale`, `mismatch`,
  `insufficient_evidence`, `invalid`, `error`.
- Freshness statuses: `verified`, `unknown`, `stale`, `not_configured`,
  `invalid`.
- `SnapshotBinding` — a frozen in-memory identity object with `as_dict()` and
  `from_mapping()`.
- `capture_snapshot(...)` — pure capture of a topology/proposal identity.
- `validate_snapshot(...)` — pure re-derivation and fail-closed validation.
- `capture_request_snapshot(session, request, ...)` — read-only capture from
  stored topology plus a `ChangeRequest`.
- Optional `snapshot_binding`, `integrity_now`, and `max_age_seconds`
  parameters on `simulate_change`, `simulate_request`, and
  `TopologyDigitalTwin`.
- Additive report fields: `snapshot_integrity_status`, `integrity`, an
  `snapshot_integrity` check, and an integrity evidence line.

### 3.2 Deliberately not added

- No database table, column, migration, background job, or external service.
- No persisted snapshot identity, fingerprint, or capture-time store.
- No workflow wiring; `ai/graph.py` is unchanged and still constructs
  `UnavailableDigitalTwin`.
- No change to `change_loop_enabled`, `lab_execution_enabled`, approval,
  execution, verification, rollback, or expiry behavior.
- No live network I/O, device configuration, Docker/Containerlab/WSL startup,
  PostgreSQL run, or Streamlit Cloud/secrets change.

### 3.3 Configuration

`config.py` adds an **optional** policy:

- Field: `digital_twin_snapshot_max_age_seconds: int | None = None`.
- Environment: `NETPILOT_DIGITAL_TWIN_MAX_AGE_SECONDS`.
- Parsing: `_env_optional_int` treats absent, blank, invalid, zero, or negative
  values as `None` (“not configured”).
- Default `None` means no TTL is evaluated. Freshness then remains explicitly
  `unknown`/`not_configured`; it is never assumed fresh.
- The setting does not enable the change loop, lab execution, approval, or any
  network operation.

---

## 4. Implemented contract

### 4.1 Snapshot identity

`capture_snapshot` records:

| Field | Meaning |
|---|---|
| `binding_version` | `d3-1`; a future persistence layer can reject incompatible records. |
| `topology_ref` | Caller-supplied stable identifier, required, non-blank, ≤ 256 chars. |
| `topology_source` | `stored`, `demo`, or `snapshot`. |
| `topology_fingerprint` | SHA-256 over the normalized, sorted device/link rows. |
| `change_fingerprint` | SHA-256 over the normalized proposal, base lines, device, request UUID, and scenario. |
| `device_name`, `request_uuid`, `scenario` | Identity context used by the change fingerprint. |
| `captured_at` | Caller-supplied capture time; **never defaulted to the clock**. |
| `max_age_seconds` | Optional positive freshness policy. |
| `device_count`, `link_count` | Snapshot size evidence. |

`capture_snapshot` is pure and read-only. It raises `ValueError` for a missing
or non-string `topology_ref`, an invalid `captured_at`, an invalid
`max_age_seconds`, or unusable topology rows. It never writes and never
contacts a device.

### 4.2 Validation and fail-closed mapping

`validate_snapshot` recomputes the topology/change fingerprints from the
supplied rows and proposal, then applies these rules in order:

| Condition | Integrity status | Recommended simulation status |
|---|---|---|
| All identity fields present, ref/fingerprints match, freshness verified | `verified` | unchanged |
| Identity fields missing | `insufficient_evidence` | `insufficient_evidence` |
| Malformed field, unsupported binding version, future capture, ref mismatch | `invalid` | `invalid` |
| Topology fingerprint mismatch | `mismatch` | `insufficient_evidence` |
| Change fingerprint mismatch | `mismatch` | `invalid` |
| Snapshot older than the policy | `stale` | `insufficient_evidence` |
| No timestamp or no policy (matching snapshot) | `unknown_freshness` | `partial` |
| Validation exception | `error` | `error` |

Conflict kinds are explicit: `topology_ref_mismatch`,
`topology_fingerprint_mismatch`, `change_fingerprint_mismatch`,
`stale_topology`, `snapshot_capture_in_future`, `snapshot_identity_missing`,
`snapshot_identity_invalid`, `snapshot_binding_invalid`,
`topology_snapshot_unusable`, and `snapshot_integrity_error`.

### 4.3 Freshness is never invented

Freshness is `verified` **only** when the caller supplies both a genuine
`captured_at` and a positive `max_age_seconds`. Every other combination is
explicitly not fresh:

- timestamp only → `not_configured`;
- policy only → `unknown`;
- neither → `not_configured`;
- malformed timestamp/policy → `invalid`;
- capture time in the future beyond a 60-second tolerance → `invalid`.

`capture_snapshot` never calls a clock to fill `captured_at`. A caller that
cannot supply a real capture time gets an honest “unknown freshness” result, not
a synthetic timestamp.

### 4.4 Integrity can only make the result more conservative

`simulate_change` merges the integrity recommendation with the normal engine
status using a severity order:

`simulated` < `partial` < `not_simulated` < `insufficient_evidence` <
`invalid` < `error`.

The merged status is the **more severe** of the two. Integrity validation can
never upgrade `invalid` to `simulated`, never turn `partial` into a pass, and
never removes a conflict or reason. A clean simulation with a freshness-unknown
binding becomes `partial`; a stale binding becomes `insufficient_evidence`; a
changed proposal becomes `invalid`.

### 4.5 Backward compatibility

When no binding is supplied, `snapshot_integrity_status` and `integrity` are
`None`, and the D1/D2 simulation status is unchanged. The additive fields do
not alter existing callers or B4 expectations.

---

## 5. Findings (PASS / FAIL / NOT VERIFIED)

| # | Finding | Verdict | Evidence |
|---|---|---|---|
| D3-F1 | Snapshot identity is deterministic and reproducible | **PASS** | `capture_snapshot` (`services/digital_twin.py:821`); reordered rows produce the same fingerprint; `test_validate_reordered_rows_are_equivalent`. |
| D3-F2 | Proposed-change identity is deterministic and input-sensitive | **PASS** | `_change_fingerprint`; changed proposal lines are detected; `test_validate_changed_proposal_is_detected`. |
| D3-F3 | TTL is evaluated only with a genuine timestamp and positive policy | **PASS** | `_parse_utc`, `_positive_int`, `_validate_binding` freshness branch (`:889`); `test_validate_expired_snapshot_is_stale`, `test_validate_policy_without_timestamp_is_unknown_freshness`. |
| D3-F4 | Missing freshness metadata fails closed as explicitly unknown | **PASS** | `unknown_freshness` → `partial`; `test_validate_matching_snapshot_without_freshness_is_explicitly_unknown`, `test_simulate_change_unknown_freshness_is_partial`. |
| D3-F5 | Missing identity metadata fails closed as `insufficient_evidence` | **PASS** | `test_validate_missing_identity_is_insufficient_evidence`. |
| D3-F6 | Malformed identity, unsupported binding version, future timestamp, or ref mismatch is `invalid` | **PASS** | `test_validate_malformed_identity_is_invalid`, `test_validate_future_capture_is_invalid`, `test_validate_ref_mismatch_is_invalid`. |
| D3-F7 | Topology fingerprint mismatch cannot produce success | **PASS** | Device/link/interface changes → `mismatch` → `insufficient_evidence`; `test_validate_device_and_link_changes_are_detected`, `test_simulate_change_topology_mismatch_is_not_success`. |
| D3-F8 | Post-capture proposal change cannot produce success | **PASS** | `mismatch` → `invalid`; `test_simulate_change_proposal_mismatch_is_invalid`. |
| D3-F9 | Expired snapshot cannot produce success | **PASS** | `stale` → `insufficient_evidence` with `stale_topology`; `test_simulate_change_stale_binding_is_not_success`. |
| D3-F10 | Integrity gating only worsens status, never upgrades it | **PASS** | `_STATUS_SEVERITY` / `_more_severe_status` (`:1273`); stale/mismatch/unknown cases downgrade a clean `simulated` result. |
| D3-F11 | Validation exceptions fail closed as `error` | **PASS** | `validate_snapshot` and `simulate_change` catch exceptions into `_integrity_failure_report`; `snapshot_integrity_error`. |
| D3-F12 | Integrity functions are read-only and isolated | **PASS** | No writes, no sockets/subprocess/executor/apply/probe; `test_integrity_functions_are_read_only`; `tools/safety_scan.py` exit 0. |
| D3-F13 | Session/adapter integration is additive and optional | **PASS** | `capture_request_snapshot`, `simulate_request`, `TopologyDigitalTwin`; `test_simulate_request_with_binding_is_verified_and_read_only`, `test_adapter_passes_binding_and_fails_closed`. |
| D3-F14 | Workflow remains intentionally unwired | **PASS** | `ai/graph.py:43` still constructs `UnavailableDigitalTwin`; `ai/graph.py:173` still logs `simulation_not_run`; `tests/test_workflow_routing.py:86` still asserts `not_run`; B4 `digital_twin_not_run` passes. |
| D3-F15 | Freshness policy is optional and cannot enable execution | **PASS** | `config.py:161`, `:231`; default `None`; `change_loop_enabled` / `lab_execution_enabled` unchanged (`:145`, `:154`); `test_freshness_policy_is_configurable`. |
| D3-F16 | Persisted snapshot identity/capture-time store and end-to-end TTL | **NOT VERIFIED / NOT IMPLEMENTED** | No table, migration, or service added. The engine can validate a binding only when a caller supplies one. See §6. |
| D3-F17 | Link update/capture timestamp availability | **NOT VERIFIED** | `Device.updated_at` exists, but `Link` has only `discovered_at` (creation metadata). Neither is a snapshot capture time. |
| D3-F18 | Live Gate 3/Gate D, PostgreSQL, real browser E2E, live provider, Docker/Containerlab/WSL, real device I/O | **NOT VERIFIED** | Not executed by design; see §9. |

---

## 6. Persistence limitation and integration contract

### 6.1 Why D3 does not persist

D3 was scoped to the engine boundary. Adding a snapshot table, migration, or
service would have required infrastructure decisions and a schema change that
were not authorized. More importantly, the current schema has no genuine
snapshot capture time for links:

- `Device.updated_at` is a row-update timestamp, not a topology capture time.
- `Link.discovered_at` is creation metadata only; link updates do not carry a
  trustworthy “observed at” value.
- `ChangeRequest` has `created_at` / `updated_at`, but no snapshot binding.

Therefore D3 does **not** invent a capture time from these columns. A binding
must be captured by the caller with an explicit `captured_at`, and it remains an
in-memory object until a future persistence layer stores it.

### 6.2 Contract for a future persistence/integration phase

A later, separately reviewed phase must:

1. **Capture** a `SnapshotBinding` at proposal/verification time, with a real
   `captured_at` and an explicit policy.
2. **Store** the binding with the `ChangeRequest` and with the human approval
   record: `binding_version`, `topology_ref`, `topology_fingerprint`,
   `captured_at`, `topology_source`, `device_count`, `link_count`,
   `change_fingerprint`, normalized proposal lines, `request_uuid`, and
   approval identity.
3. **Revalidate** before simulation by reloading the snapshot, recomputing both
   fingerprints, and calling `validate_snapshot` (or `simulate_request` with the
   stored binding).
4. **Fail closed** on any of: missing identity, malformed identity, unsupported
   binding version, future capture, ref mismatch, topology mismatch, change
   mismatch, stale TTL, or validation error. No normal success may be reported.
5. **Persist the integrity report** alongside the simulation report, including
   both fingerprints, the freshness status, conflicts, and
   `not_proof_of_safety: true`.
6. **Revise B4/workflow expectations deliberately** when wiring is authorized:
   `evaluation/runner.py`, `tests/test_workflow_routing.py`,
   `tools/phase3_lab_validation.py`, B4 fakes/dataset, and UI smoke. The old
   `digital_twin_not_run` assertion must not be silently weakened.
7. **Keep execution gated:** no integrity result enables
   `change_loop_enabled`, `lab_execution_enabled`, approval, execution,
   verification, or rollback.

### 6.3 Status propagation (unchanged from D2, for reference)

| Status | Meaning | Must not be treated as |
|---|---|---|
| `not_run` | No simulation was performed | safe / approved / verified |
| `simulated` | Structural model ran on the stated snapshot | real-network success, approval, execution, verification |
| `partial` | Missing/unknown evidence, unsupported operation/invariant, or unknown freshness | a pass |
| `insufficient_evidence` | The snapshot/target cannot support the simulation, or is stale/mismatched | a pass |
| `invalid` | Bad input, target absence, malformed identity, or proposal mismatch | a pass |
| `error` | Simulation/integrity validation failed closed | a pass |
| `not_simulated` | No supported state change was proposed | a pass |

---

## 7. Edge-case matrix (executed)

| Case | Observed D3 behavior | Test |
|---|---|---|
| Matching snapshot/proposal, fresh | `verified`; simulation stays `simulated` | `test_validate_matching_snapshot_with_freshness_is_verified`, `test_simulate_change_verified_binding_stays_simulated` |
| Matching, no timestamp and no policy | `unknown_freshness` / `not_configured`; simulation `partial` | `test_validate_matching_snapshot_without_freshness_is_explicitly_unknown`, `test_simulate_change_unknown_freshness_is_partial` |
| Policy configured, no timestamp | `unknown_freshness` / `unknown`; `partial` | `test_validate_policy_without_timestamp_is_unknown_freshness` |
| Timestamp present, no policy | `unknown_freshness` / `not_configured`; `partial` | `test_validate_timestamp_without_policy_is_unknown_freshness` |
| Snapshot older than policy | `stale` / `stale`; `insufficient_evidence`; `stale_topology` | `test_validate_expired_snapshot_is_stale`, `test_simulate_change_stale_binding_is_not_success` |
| Capture time in the future | `invalid`; `snapshot_capture_in_future` | `test_validate_future_capture_is_invalid` |
| Device status changed after capture | `mismatch`; `insufficient_evidence` | `test_validate_device_and_link_changes_are_detected` |
| Link status/interface changed after capture | `mismatch`; `insufficient_evidence` | `test_validate_device_and_link_changes_are_detected` |
| Proposal changed after capture | `mismatch`; `invalid` | `test_validate_changed_proposal_is_detected`, `test_simulate_change_proposal_mismatch_is_invalid` |
| `topology_ref` mismatch | `invalid`; `topology_ref_mismatch` | `test_validate_ref_mismatch_is_invalid` |
| Empty/missing identity mapping | `insufficient_evidence`; `snapshot_identity_missing` | `test_validate_missing_identity_is_insufficient_evidence` |
| Blank ref, non-string ref, bad fingerprint, bad timestamp, zero TTL, wrong binding version | `invalid`; `snapshot_identity_invalid` | `test_validate_malformed_identity_is_invalid` |
| Non-mapping binding | `invalid`; `snapshot_binding_invalid` | covered by validation failure path |
| Unusable topology rows | `invalid`; `topology_snapshot_unusable` | validation failure path |
| Validation exception | `error`; `snapshot_integrity_error` | failure path |
| Reordered device/link rows | Same fingerprint; `verified` | `test_validate_reordered_rows_are_equivalent` |
| Topology-only binding (no proposal) | `verified`; no change fingerprint required | `test_topology_only_binding_can_be_verified` |
| No binding supplied | Integrity fields `None`; status unchanged | `test_simulate_change_without_binding_is_unchanged` |
| Repeated validation | Byte-identical JSON | `test_integrity_report_is_deterministic` |
| Session capture + simulate | Verified, read-only, request/topology unchanged | `test_simulate_request_with_binding_is_verified_and_read_only` |
| Topology changes after session capture | `mismatch`; no success | `test_simulate_request_detects_topology_change_after_capture` |
| Invalid stored request JSON during capture | `ValueError`; no fabricated binding | `test_capture_request_snapshot_rejects_invalid_json` |
| Adapter with binding | Verified → `simulated`; changed proposal → `invalid` | `test_adapter_passes_binding_and_fails_closed` |
| Env policy `120` / `0` / invalid / blank | `120` / `None` / `None` / `None` | `test_freshness_policy_is_configurable` |
| Session uses configured policy | Verified with `captured_at` only | `test_simulate_request_uses_configured_freshness_policy` |

---

## 8. Verification (executed in this D3 session)

All commands were run from `E:\Graduation Projects\NetPilot AI` with the
project virtual environment. “Exit” is the process exit code. Focused-suite
counts are from `--collect-only`; the full-suite count is from JUnit XML because
the repository’s quiet pytest configuration suppresses the final summary line.

| Command | Result | Exit |
|---|---|---|
| `.\.venv\Scripts\python.exe -m py_compile services\digital_twin.py config.py tests\test_digital_twin.py` | compile OK | 0 |
| `.\.venv\Scripts\python.exe -m pytest tests\test_digital_twin.py --collect-only -q` | **102 tests collected** (59 D2 baseline + 43 D3) | 0 |
| `.\.venv\Scripts\python.exe -m pytest tests\test_digital_twin.py -q` | **102 passed** (all dots; quiet summary suppressed) | 0 |
| `.\.venv\Scripts\python.exe -m pytest tests\test_workflow_routing.py tests\test_ai_evaluation.py tests\test_impact_analysis.py tests\test_ops_center_actions.py tests\test_change_service.py tests\test_change_execution.py -q` | **158 passed** (B1–B4 regressions) | 0 |
| `.\.venv\Scripts\python.exe -m pytest --collect-only -q` | **652 tests collected** | 0 |
| `.\.venv\Scripts\python.exe -m pytest -q --junitxml=pytest_d3_final.xml` | **tests=652, failures=0, errors=0, skipped=0, time=90.431s** | 0 |
| `.\.venv\Scripts\python.exe tests\ui_smoke_check.py` | **12/12 page targets `[OK]`; 16 action/flash guards `[OK]`; demo banner OK** | 0 |
| `.\.venv\Scripts\python.exe tools\safety_scan.py` | OK: no execution/deployment primitives in `ai/`; evidence blocks wrapped | 0 |
| `.\.venv\Scripts\python.exe tools\secret_scan_check.py` | Secret scan clean | 0 |
| `.\.venv\Scripts\python.exe tools\ai_eval_safety_scan.py` | OK: evaluation harness has no shell/provider/approval execution primitives | 0 |
| `.\.venv\Scripts\python.exe tools\ai_quality_eval.py` | **27/27 cases passed**; 6/6 safety gates; groundedness 9/10 (90%), all other metrics 100%; `live_network_tested=False`, `streamlit_tested=False`, `provider_tested=False` | 0 |
| `git diff --check` | No whitespace errors; only CRLF advisory warnings | 0 |
| Manual trailing-whitespace scan of the three changed/untracked files | **0** lines in `services/digital_twin.py`, `tests/test_digital_twin.py`, `config.py` | 0 |

The full suite retains 13 pre-existing `datetime.utcnow()` deprecation warnings
(7 `test_ai_persistence`, 2 `test_prompt_injection`, 4 `test_workflow_routing`).
They are unrelated to D3 and were not introduced by it.

D3-specific focused tests: **43 new cases** in `tests/test_digital_twin.py`
(30 test functions, 4 parametrized blocks). The focused file grew from the D2
baseline of 59 to **102**.

---

## 9. Checks not executed (blocked / out of scope)

These are **not** claimed as passed anywhere in this report:

- Live Gate 3 / Gate D acceptance.
- PostgreSQL-backed integration.
- Real-browser end-to-end test (UI smoke is headless Streamlit `AppTest`).
- Live-provider AI-quality evaluation (B4 is offline/synthetic).
- Docker, Containerlab, WSL, or any lab infrastructure startup.
- Real device access or network configuration changes.
- `tools/phase3_lab_validation.py` / `tools/phase4_change_loop_validation.py`
  live harness runs.
- End-to-end TTL enforcement against a persisted snapshot store (not
  implemented; see §6).
- A trustworthy link-observation timestamp (not present in the schema).

---

## 10. Remaining blockers

1. **No snapshot persistence:** the binding is in-memory only. Stale/mismatch
   detection cannot be enforced across process boundaries or approvals until a
   future persistence layer stores it.
2. **No link capture timestamp:** `Link.discovered_at` is creation metadata, and
   `Device.updated_at` is a row-update timestamp. Neither is a real snapshot
   capture time.
3. **No workflow integration:** `ai/graph.py` still uses
   `UnavailableDigitalTwin`; B4 and workflow tests still expect `not_run`.
4. **No live evidence:** Gate 3/Gate D, PostgreSQL, real browser E2E, live
   provider, and real lab I/O remain external and unavailable.
5. **Structural-only model:** routing, ACLs, NAT, services, traffic, timing,
   convergence, and packet loss remain explicitly unmodeled.
6. **Phase C conditional freeze remains:** D3 closes part of the E4 integrity
   gap but does not replace live acceptance evidence.

---

## 11. Git state (precise)

| Item | Value |
|---|---|
| Branch | `main` |
| HEAD | `f5dcb5f5518a7d69af1bb36242536c60d2170526` |
| Upstream | `origin/main` = `f5dcb5f` |
| Staged changes | **0** |
| Tracked modifications | **18** files (B1–B4/D1/D2 working set) |
| Untracked entries before this report | **14** (B1–B4 files, both Phase C audit documents, D1/D2 reports, `evaluation/`, `services/impact_service.py`, `services/ops_center_actions.py`, D1/D2 service/test, B4 tools/tests) |
| Untracked entries after this report | **15** (the above plus `docs/D3_Digital_Twin_Snapshot_Integrity_Report.md`) |
| Commits / pushes / deploys | **none** |
| `git diff --check` | exit 0; CRLF advisory warnings only |

D3 touched only:

- `services/digital_twin.py` (integrity layer; version `d3-1.0`; untracked D1/D2
  work)
- `tests/test_digital_twin.py` (43 D3 cases; untracked D1/D2 work)
- `config.py` (optional freshness policy; already modified by B1)
- `docs/D3_Digital_Twin_Snapshot_Integrity_Report.md` (this report)

`ai/graph.py` was **not** changed in D3. All B1–B4 changes, both Phase C
documents, the D1 report, the D2 report, and the D2 service/test work were
preserved. Nothing was staged, committed, pushed, reset, cleaned, discarded, or
deployed.

---

## 12. Acceptance criteria for D3

D3 is accepted only because all of the following are true:

1. **Deterministic identity:** snapshot and proposal identities are stable
   across row order and change when relevant inputs change.
2. **Fail-closed:** missing, malformed, mismatched, stale, future-dated, or
   erroring bindings cannot produce an unqualified success.
3. **No invented freshness:** TTL is evaluated only with a genuine capture time
   and positive policy; all other cases are explicitly unknown or
   not configured.
4. **Conservative merge:** integrity validation can only worsen a simulation
   status, never upgrade it.
5. **Read-only isolation:** no writes, device I/O, shell execution, approval,
   execution, verification, or rollback.
6. **Workflow unchanged:** `digital_twin_status == "not_run"` and B4
   `digital_twin_not_run` still pass.
7. **Flags unchanged:** `change_loop_enabled` and `lab_execution_enabled` remain
   disabled by default; the new policy cannot enable them.
8. **Verification:** focused tests, B1–B4 regressions, full suite, UI smoke,
   safety/secret scans, AI eval safety/quality, and `git diff --check` pass.
9. **Honest limitation:** the persistence/end-to-end TTL gap is documented with
   an integration contract rather than fabricated evidence.

---

## 13. Explicit non-actions

- No connection of the Digital Twin to `ai/graph.py`.
- No change to `digital_twin_status == "not_run"` expectations.
- No approval, execution, verification, or rollback action.
- No change to `change_loop_enabled` or `lab_execution_enabled`.
- No database table, migration, background job, or external service.
- No Docker, Containerlab, WSL, PostgreSQL, or lab infrastructure startup.
- No device access or real network configuration.
- No Streamlit Cloud/secrets change.
- No stage, commit, push, reset, clean, or deploy.
