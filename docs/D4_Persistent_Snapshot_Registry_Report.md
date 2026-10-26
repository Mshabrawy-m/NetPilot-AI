# Phase D4 — Persistent Digital Twin Snapshot Registry: Implementation Report

**Date:** 2026-10-26
**Branch / HEAD:** `main` / `f5dcb5f` (upstream `origin/main` = `f5dcb5f`)
**Working tree:** uncommitted by instruction (see §10).
**Scope:** persistence of Digital Twin snapshot identities only. No engine
integration, no UI integration, no execution, no deployment, no infrastructure.

---

## 1. Objective and outcome

D3 (`docs/D3_Digital_Twin_Snapshot_Integrity_Report.md`) closed the snapshot
*integrity* gap with an in-memory `SnapshotBinding`, but explicitly left a
**persistence gap**: a binding could be validated only while the caller still
held it. Nothing durable recorded which topology/proposal identity a snapshot
belonged to, when it was captured, or which schema version described it.

Phase D4 audited the existing storage layer and concluded that the architecture
**can** support a safe, additive registry. The result is:

* a new append-only table, `digital_twin_snapshot_bindings`, created by the
  existing `Base` + `init_db()` convention;
* a small service, `services/digital_twin_registry.py`, that persists immutable
  bindings and re-validates them through the D3 integrity layer;
* an explicit, caller-supplied `captured_at`; the current clock is never
  substituted;
* conservative duplicate handling (idempotent only for byte-equivalent
  immutable content, otherwise a conflict — never an overwrite);
* fail-closed handling of missing, malformed, unsupported, mismatched, stale,
  future-dated and unreadable records;
* TTL disabled by default, with freshness explicitly unknown when no positive
  policy is available.

The registry is **not** wired into `ai/graph.py`. The workflow continues to
report `digital_twin_status == "not_run"` through `UnavailableDigitalTwin`.

---

## 2. Architecture audit

### 2.1 What already existed

| Concern | Existing mechanism | D4 decision |
|---|---|---|
| ORM / schema | SQLAlchemy 2.0 `Base` models in `database/models.py` | Add one model; no second database |
| Schema creation | `init_db()` → `Base.metadata.create_all` with a process lock and bounded retry | Reuse it; the new table is created on first boot |
| Additive migration | `_ensure_additive_columns()` adds only missing nullable columns; never drops/rewrites | No alteration of existing tables was needed |
| Migration framework | **None** (no Alembic, no migration directory) | Do not introduce one |
| Data access | `database/repository.py` is the only SQL/ORM layer used by services/UI | Add three repository functions |
| Transactions | Callers own the session; services flush and commit; `session_scope()` commits/rolls back | Registry flushes only; caller commits or rolls back |
| SQLite behaviour | WAL, foreign keys ON, single writer, `timeout=15` | Only backend exercised by D4 |
| PostgreSQL behaviour | URL switch exists (`postgresql+psycopg2://...`) | **Not exercised; no claim made** |
| Timestamp convention | Naive UTC in SQLite, timezone-aware on PostgreSQL; `config.utc_now()` is the only clock source | Store naive UTC; never use a clock for `captured_at` |

### 2.2 Audit conclusion

The existing abstractions are sufficient. D4 therefore implemented the registry
instead of stopping at a design report. The implementation deliberately does
**not**:

* create a second SQLite/PostgreSQL database;
* add Alembic or a migration framework;
* change `init_db()`, the engine, or the connection URL logic;
* add a background cleanup daemon;
* couple the binding to `change_requests` with a foreign key.

### 2.3 Transaction/concurrency finding (important)

While validating the "caller owns the transaction" contract, a SQLite-specific
savepoint quirk was found and fixed:

* pysqlite defers its `BEGIN` until the first DML statement;
* if the registry's `SAVEPOINT` is the first statement, the matching
  `RELEASE SAVEPOINT` commits the insert at the SQLite level;
* a caller `rollback()` would then leave the row behind.

The registry now calls `_ensure_outer_transaction(session)` before opening the
savepoint. On SQLite it emits `BEGIN` when the DBAPI connection is not already
in a transaction; on other backends it is a no-op. The rollback test in
`tests/test_digital_twin_registry.py` pins this behaviour.

Concurrency model:

* append-only insert with a unique `binding_uuid`;
* a nested savepoint isolates a duplicate-key race so the caller's transaction
  is not poisoned;
* after an `IntegrityError`, the registry re-reads and accepts only identical
  immutable content; different content raises `SnapshotBindingConflict`;
* if the re-read is impossible, it fails closed with
  `SnapshotRegistryPersistenceError`;
* SQLite is single-writer: a concurrent writer can still receive a fail-closed
  persistence error (e.g. `database is locked`). It never silently overwrites.

No cleanup is required for correctness. Bindings are evidence and are retained
indefinitely by design; TTL affects *validation freshness*, not deletion.

---

## 3. Implemented schema and migration

### 3.1 Model

`database/models.py`:

```python
DT_SNAPSHOT_BINDING_SCHEMA_VERSION = "d4-1"

class DigitalTwinSnapshotBinding(Base):
    __tablename__ = "digital_twin_snapshot_bindings"
    __table_args__ = (
        UniqueConstraint("binding_uuid", name="uq_dt_binding_uuid"),
        Index("ix_dt_binding_topology", "topology_ref", "captured_at"),
        Index("ix_dt_binding_request", "request_uuid"),
        Index("ix_dt_binding_captured", "captured_at"),
    )
    ...
```

| Column | Type | Purpose |
|---|---|---|
| `id` | Integer PK | Surrogate key |
| `binding_uuid` | String(64), unique | Stable public identifier |
| `schema_version` | String(16) | Registry record schema (`d4-1`) |
| `binding_version` | String(16) | D3 binding contract (`d3-1`) |
| `topology_ref` | String(256) | Snapshot reference |
| `topology_source` | String(16) | `stored` / `demo` / `snapshot` |
| `topology_fingerprint` | String(80) | `sha256:<64 hex>` |
| `change_fingerprint` | String(80), nullable | `sha256:<64 hex>` or NULL |
| `device_name` | String(128), nullable | Change target |
| `request_uuid` | String(64), nullable | Provenance only; **not** a FK |
| `scenario` | String(128), nullable | Optional scenario label |
| `captured_at` | DateTime, NOT NULL | Caller-supplied trusted capture time |
| `max_age_seconds` | Integer, nullable | Optional explicit TTL policy |
| `device_count` | Integer | Snapshot size |
| `link_count` | Integer | Snapshot size |
| `registered_at` | DateTime | Audit insertion time; **never** used for TTL |

### 3.2 Migration

No migration is required beyond the existing convention:

1. `init_db()` calls `Base.metadata.create_all(checkfirst=True)`;
2. the new table is created if it does not exist;
3. `_ensure_additive_columns()` is a no-op for a brand-new table and refuses
   non-nullable additions to pre-existing tables;
4. no existing table, column, index, or constraint is modified or dropped.

The table is additive and isolated. Existing databases gain it on the next
`init_db()` call.

### 3.3 Repository API

`database/repository.py` adds only:

* `add_dt_snapshot_binding(...)` — inserts and flushes; no update/delete;
* `get_dt_snapshot_binding(session, binding_uuid)`;
* `list_dt_snapshot_bindings(session, *, topology_ref, request_uuid, limit)`.

There is deliberately **no** update or delete function. Immutability is a
property of the API surface, not merely a convention.

---

## 4. Registry service and lifecycle contract

`services/digital_twin_registry.py` exposes:

| Function | Purpose |
|---|---|
| `binding_uuid_for(binding, *, captured_at)` | Deterministic `uuid5` identifier |
| `register_binding(session, binding, *, captured_at, max_age_seconds=None, binding_uuid=None)` | Persist or return the identical existing binding |
| `load_binding(session, binding_uuid)` | Reconstruct one binding, or `None` |
| `validate_persisted_binding(session, binding_uuid, ...)` | Load + D3 re-validation report |
| `list_bindings(session, ...)` | Audit listing with per-row problems |

Typed errors:

* `SnapshotRegistryError` (base)
* `SnapshotBindingConflict`
* `SnapshotRegistrySchemaError`
* `SnapshotRegistryMalformedError`
* `SnapshotRegistryPersistenceError`

### 4.1 Identity and determinism

* `binding_uuid` is caller-supplied or derived with
  `uuid5(NAMESPACE_URL, canonical_payload)`.
* The payload includes the registry schema, D3 binding version, topology
  reference/source/fingerprint, change fingerprint, device/request/scenario
  labels, `captured_at`, and device/link counts.
* The TTL policy is **excluded** from the identifier but **included** in the
  conflict comparison. Re-registering the same capture with a different policy
  is a conflict, not a second record.
* The same capture at a different timestamp is a distinct historical record.
* Validation recomputes the D3 fingerprints from the supplied topology and
  proposal; the result is deterministic for identical inputs and `now`.

### 4.2 Explicit capture time

* `captured_at` is mandatory. `None` raises
  `ValueError("captured_at is required; the registry never substitutes the
  current clock")`.
* If the `SnapshotBinding` already carries `captured_at`, the supplied value
  must agree exactly; a mismatch raises.
* `registered_at` is a separate audit timestamp produced by `db_utcnow()`. It
  is never used as a capture time and never used for TTL.

### 4.3 Duplicate and overwrite handling

| Situation | Behaviour |
|---|---|
| Same `binding_uuid`, identical immutable content | Idempotent: `created=False`, no write |
| Same `binding_uuid`, different content | `SnapshotBindingConflict`; existing row untouched |
| Same content, different TTL policy | `SnapshotBindingConflict` |
| Same content, different `captured_at` | New deterministic ID → distinct record |
| Concurrent insert wins the unique constraint | Savepoint rollback + re-read; identical → idempotent, different → conflict |
| Re-read impossible after a race | `SnapshotRegistryPersistenceError` (fail closed) |

No code path performs `UPDATE` or `DELETE` on the table.

### 4.4 Freshness and TTL

TTL is **disabled by default**:

* `register_binding(..., max_age_seconds=None)` stores `NULL`;
* `config.digital_twin_snapshot_max_age_seconds` defaults to `None`;
* a stored binding with a timestamp but no policy validates as
  `unknown_freshness` / `not_configured` and recommends `partial`.

TTL is evaluated only when a positive policy is available, either stored with
the binding or supplied explicitly at validation time. A policy must be a
positive integer; zero, negative, boolean, and non-numeric values are rejected
(or treated as malformed when read from a persisted row).

Freshness outcomes reuse the D3 vocabulary:

| Condition | Integrity status | Recommended simulation status |
|---|---|---|
| Exact match, fresh, policy present | `verified` | `None` |
| Exact match, no policy | `unknown_freshness` | `partial` |
| Age exceeds policy | `stale` | `insufficient_evidence` |
| Topology/proposal changed | `mismatch` | `insufficient_evidence` / `invalid` |
| Record missing | `insufficient_evidence` | `insufficient_evidence` |
| Malformed / unsupported version | `invalid` | `invalid` |
| Persistence/validation failure | `error` | `error` |

The D3 60-second future tolerance is preserved. A capture time more than 60
seconds in the future is `invalid` with `snapshot_capture_in_future`.

### 4.5 Fail-closed reporting

`validate_persisted_binding()` never raises for an unusable record; it returns a
D3-shaped report with an explicit conflict kind:

| Conflict kind | Meaning | `persisted` |
|---|---|---|
| `persisted_binding_missing` | No row for the identifier | `False` |
| `persisted_binding_malformed` | Row exists but cannot be reconstructed | `True` |
| `persisted_binding_schema_unsupported` | Registry schema or D3 binding version unsupported | `True` |
| `persisted_binding_persistence_error` | Read/validation infrastructure failed | `False` |
| `persisted_binding_validation_error` | Unexpected validation exception after load | `True` |

For missing, malformed and unsupported records, `persisted_binding` is `None`:
the registry does not present an unusable row as evidence. Every failure report
also states `database_written=False`, `device_contacted=False`,
`configuration_applied=False`, and `change_request_mutated=False`.

---

## 5. D3 additive helpers

To let the registry reuse the D3 report shape without changing D3 validation,
two additive public helpers were added to `services/digital_twin.py`:

* `parse_utc(value)` — public wrapper for the existing UTC parser;
* `integrity_failure_report(binding=None, *, status, reason, conflict_kind)` —
  public wrapper for the existing failure-report builder.

`_integrity_failure_report()` now maps every integrity status to the correct
recommended simulation status (`verified` → `None`, `unknown_freshness` →
`partial`, `stale`/`mismatch`/`insufficient_evidence` →
`insufficient_evidence`, `invalid` → `invalid`, `error` → `error`). This affects
failure-report recommendations only; D3 validation logic and status precedence
are unchanged. All 102 existing D1–D3 tests still pass.

---

## 6. Test evidence

All tests use the shared isolated temporary SQLite database from
`tests/conftest.py`; no external infrastructure was started.

### 6.1 Focused registry and Digital Twin tests

```text
.venv\Scripts\python.exe -m pytest tests\test_digital_twin_registry.py tests\test_digital_twin.py -q --junitxml=.pytest_cache\d4-focused.xml
```

* Exit code: **0**
* Result: **142 passed**, 0 failed, 0 errors, 0 skipped
* Breakdown: 40 new D4 registry tests + 102 existing D1–D3 tests

The 40 D4 tests cover:

| Requirement | Tests |
|---|---|
| Persist and retrieve | `test_register_persists_an_immutable_binding`, `test_restart_reload_uses_the_same_database` |
| Stable/deterministic fingerprints and IDs | `test_binding_uuid_is_deterministic_and_content_addressed`, `test_repeated_validation_is_deterministic` |
| Exact match | `test_exact_binding_match_is_verified`, `test_reordered_topology_rows_still_match` |
| Topology/proposal mismatch | `test_topology_mismatch_is_detected`, `test_proposal_mismatch_is_detected` |
| Missing record | `test_missing_record_is_insufficient_evidence` |
| Malformed row | `test_malformed_persisted_row_is_invalid` (8 parametrized field corruptions) |
| Unsupported schema/version | `test_unsupported_registry_schema_is_invalid`, `test_unsupported_binding_version_is_invalid` |
| Expired timestamp | `test_expired_binding_is_stale` |
| Future timestamp | `test_future_capture_is_invalid` |
| TTL disabled by default | `test_ttl_is_disabled_by_default`, `test_explicit_validation_policy_enables_expiry` |
| Duplicate identifiers | `test_identical_reregistration_is_idempotent`, `test_conflicting_content_is_never_overwritten`, `test_conflicting_ttl_policy_is_a_conflict_not_a_new_record`, `test_same_capture_with_a_different_timestamp_is_a_distinct_record` |
| Persistence failures | `test_read_failure_fails_closed`, `test_write_failure_fails_closed`, `test_list_failure_fails_closed` |
| Transaction failure | `test_rollback_leaves_no_binding` |
| Concurrent/repeated access | `test_duplicate_insert_race_is_idempotent`, `test_duplicate_insert_race_with_different_content_conflicts` |
| Explicit capture time | `test_captured_at_is_mandatory_and_never_clocked`, `test_captured_at_must_agree_with_the_binding` |
| No side effects | `test_registry_does_not_mutate_change_requests_or_touch_devices`, `test_registry_module_has_no_network_or_execution_imports` |
| Listing/filtering | `test_list_bindings_filters_and_orders` |
| Schema constant | `test_registry_schema_constant_matches_the_model` |

### 6.2 B1–B4 regressions

Both interpretations of the B batch were exercised: the recent B1–B4 work
(change service/execution, impact analysis, Ops Center actions, AI evaluation)
and the Phase 2 B1–B4 analytics items named in
`docs/FEATURE_ACCEPTANCE_MATRIX.md`.

```text
.venv\Scripts\python.exe -m pytest tests\test_change_service.py tests\test_change_execution.py tests\test_change_loop_harness.py tests\test_impact_analysis.py tests\test_ops_center_actions.py tests\test_ai_evaluation.py tests\test_data_quality.py tests\test_temporal_splits.py tests\test_derived_metrics.py tests\test_eda.py tests\test_anomaly_detection.py tests\test_analytics_persistence.py -q --junitxml=.pytest_cache\d4-b-regressions.xml
```

* Exit code: **0**
* Result: **228 passed**, 0 failed, 0 errors, 0 skipped

### 6.3 Full suite

```text
.venv\Scripts\python.exe -m pytest -q --junitxml=.pytest_cache\d4-full-final2.xml
```

* Exit code: **0**
* Result: **692 passed**, 0 failed, 0 errors, 0 skipped
* Rerun after adding this report, so document-discovery/RAG hygiene tests also
  cover the new file.
* Baseline before D4: 652 tests; D4 adds 40 → 692
* Only warnings: pre-existing `datetime.utcnow()` deprecation in
  `services/ai_workflow_service.py`

### 6.4 UI smoke

```text
.venv\Scripts\python.exe tests\ui_smoke_check.py
```

* Exit code: **0**
* All pages `[OK]`; all action/confirmation/approval/impact/expiry/cancel/
  invalid-JSON guards `[OK]`; demo banner `OK`
* The registry has no UI surface in D4, so this confirms no regression, not a
  registry UI.

### 6.5 Safety, secret, and AI evaluation scans

| Command | Exit | Result |
|---|---|---|
| `python tools/safety_scan.py` | 0 | No execution/deployment primitives in `ai/`; evidence wrapped in `<untrusted_data>` |
| `python tools/secret_scan_check.py` | 0 | `Secret scan clean.` |
| `python tools/ai_eval_safety_scan.py` | 0 | No shell/provider/approval execution or dynamic-code imports in the evaluation harness |
| `python tools/ai_quality_eval.py` | 0 | 27/27 cases; 6/6 safety gates; `live_network_tested=False`, `streamlit_tested=False`, `provider_tested=False` |

### 6.6 Diff hygiene

```text
git diff --check
```

* Exit code: **0**
* Only pre-existing LF→CRLF notices for already-modified tracked files; no
  whitespace errors.
* Untracked D4 files were scanned separately for trailing whitespace: **0
  findings**. The only trailing whitespace in untracked text files is in the
  pre-existing `docs/D1_Digital_Twin_Implementation_Report.md` (lines 3–5),
  which D4 did not modify.

---

## 7. SQLite vs PostgreSQL verification status

### SQLite (verified)

* All 692 tests, including the 40 D4 tests, run against isolated temporary
  SQLite databases.
* WAL and foreign-key pragmas are active.
* The registry's savepoint/transaction behavior is explicitly tested, including
  the rollback case that exposed the pysqlite `BEGIN` quirk.
* Concurrent-writer behavior is exercised by simulating the unique-constraint
  race within one session. Real multi-process SQLite contention can still
  produce a fail-closed persistence error.

### PostgreSQL (NOT verified)

* No PostgreSQL driver is installed in the environment
  (`psycopg2`/`psycopg` both absent).
* No PostgreSQL `DATABASE_URL` is configured.
* No server, container, or external infrastructure was started.
* The model and repository use portable SQLAlchemy types and no
  SQLite-only SQL, but the savepoint helper is SQLite-specific and the
  PostgreSQL path has not been executed.
* **D4 does not claim PostgreSQL support.** A future phase must run the same
  focused suite against a real PostgreSQL instance before making that claim.

---

## 8. Limitations and remaining work

1. **No engine integration.** `ai/graph.py` is untouched and still uses
   `UnavailableDigitalTwin`; `digital_twin_status` remains `not_run`.
2. **No UI integration.** `list_bindings()` is service-only; no page renders
   persisted bindings.
3. **No retention/cleanup policy.** Bindings are append-only evidence and are
   not auto-deleted. TTL is validation-only.
4. **No authorization layer.** Registry calls assume trusted in-process
   services; there is no per-user access control.
5. **Caller-supplied IDs are length-checked, not format-checked.** Deterministic
   IDs are UUIDs; an explicit caller ID may be any non-blank string up to 64
   characters.
6. **SQLite single-writer.** Genuine concurrent writers may receive a
   fail-closed persistence error rather than idempotent resolution.
7. **No live device evidence.** The registry stores identity metadata; it does
   not prove connectivity, routing, or configuration state.
8. **PostgreSQL remains unverified** (see §7).
9. **`registered_at` is audit metadata.** It uses the clock and must never be
   used for TTL; the code enforces this by keeping it separate from
   `captured_at`.

---

## 9. Files changed by D4

### Added

* `services/digital_twin_registry.py` — persistent registry service.
* `tests/test_digital_twin_registry.py` — 40 focused tests.
* `docs/D4_Persistent_Snapshot_Registry_Report.md` — this report.

### Modified (additive only)

* `database/models.py` — `DT_SNAPSHOT_BINDING_SCHEMA_VERSION` and
  `DigitalTwinSnapshotBinding`.
* `database/repository.py` — three registry functions.
* `services/digital_twin.py` — `parse_utc`, `integrity_failure_report`, and the
  failure-report recommended-status mapping.

These files already contained uncommitted earlier-phase work. D4 edits are
additive and do not revert, overwrite, or restage that work.

### Explicitly untouched

* `ai/graph.py`
* `config.py` flags (`change_loop_enabled`, `lab_execution_enabled` remain
  `False`; `digital_twin_snapshot_max_age_seconds` remains `None`)
* Streamlit Cloud settings and credentials
* Docker/lab/infrastructure files

---

## 10. Git state and prohibited-action confirmations

### Git state at verification time

| Item | Value |
|---|---|
| Branch | `main` |
| HEAD | `f5dcb5f` |
| `origin/main` | `f5dcb5f` |
| Staged files | **0** |
| Modified tracked files | 18 (all already modified before D4; D4 added lines to `database/models.py` and `database/repository.py` among them) |
| Untracked `git status` entries | 17 before this report; 18 after (D4 added 2 code/test files + this report) |

`git status --porcelain` shows the expected pre-existing modified/untracked
work plus the D4 additions. No file was staged.

### Prohibited actions — confirmed not performed

* No `git add`, commit, push, reset, clean, checkout, or discard.
* No deployment, no Streamlit Cloud changes, no secrets changes.
* No real device I/O, no configuration change, no packet injection.
* No Docker/Containerlab/WSL/infrastructure setup.
* No change-loop or lab-execution enablement.
* No `ai/graph.py` modification; `digital_twin_status` remains `not_run`.
* No second database and no migration framework.
* No PostgreSQL support claim.

---

## 11. Acceptance position

D4 closes the **persistence gap** documented in D3:

* snapshot identity, topology fingerprint, proposal fingerprint, capture time,
  and schema version are now durably stored;
* retrieval and re-validation are deterministic and fail closed;
* duplicate identifiers cannot overwrite evidence;
* TTL is opt-in and freshness stays explicitly unknown without a policy;
* the existing SQLite/ORM/migration conventions are reused without new
  infrastructure.

D4 does **not** claim full E4 acceptance, live-lab evidence, PostgreSQL support,
Gate 3/Gate D completion, or production readiness. The Digital Twin remains
outside the AI workflow, and the next phase must explicitly authorize any
integration.
