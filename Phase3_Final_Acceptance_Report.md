# NetPilot AI — Phase 3 Final Acceptance Report

**Purpose:** close the three conditions left open by `Phase3_Acceptance_Audit.md`
(verdict *ACCEPT WITH CONDITIONS*) and record, honestly, what is now **verified**,
what is a **code-inspection finding**, and what **remains pending**.

**Ground rules honoured:** no Phase 4 work started; no deployment / execution /
rollback / production-network access added; no existing test weakened or
deleted; the shared Phase 1/2 query behaviour was not modified; the API key is
never printed or copied anywhere in this report.

**Host baseline (unchanged):** Windows 11 (26H2), PowerShell 5.1,
Python 3.12.10 venv, **no Docker, no Containerlab** on this machine.

---

## 1. Acceptance conditions & status

| # | Condition (from the audit) | Status | Basis |
|---|---|---|---|
| C-1 | Revoke/replace the compromised Groq key | ⏳ **STILL PENDING — re-checked** | Re-verified: the local `.env` key fingerprint is **unchanged** (`2dc8fa5762c2`) and that same key **still authenticates live**, so the old key is **not** revoked (§2). |
| C-2 | Live isolated-lab end-to-end on Docker/Containerlab | ⏳ **BLOCKED — host cannot run it** | No Docker/Containerlab and WSL2 has no distro (§4c); evidence harness prepared but self-reports BLOCKED here. Checklist (§4a/§4b) stands. |
| C-3 | Pin & verify a model the account actually serves | ✅ **CLOSED (verified)** | Live check EXIT=0, `openai/gpt-oss-20b` (`§3`). |

Two conditions are **now closed or fully prepared**; the remaining two (C-1, C-2)
require resources that do not exist on this machine and **cannot be honestly
completed here** — they are handed off as explicit, reproducible checklists.
No demo data or mocks are substituted for them.

---

## 2. Groq API key security (C-1)

The previously shared key is treated as **compromised** and was **not**
displayed, printed, or copied anywhere in this task.

**Non-revealing check of the local `.env`** (reads the value in-process, prints
only a one-way fingerprint):

```
$ python -c "... dotenv_values('.env') ... sha256(key)[:12] ..."
GROQ_API_KEY_present= True
key_len= 56
key_fp_sha256_12= 2dc8fa5762c2
```

**Finding:** the local `.env` still contains the **previously shared key**
(length 56, SHA-256 fingerprint `2dc8fa5762c2`) — it has not been replaced.

**Required action (user):** revoke that key in the Groq console and generate a
new one, then update the local `.env` (`GROQ_API_KEY=...`) **only**. Do **not**
paste the replacement into chat. After editing `.env`, re-run the fingerprint
command above — the `key_fp_sha256_12` value must **change**, which is the
verifiable proof (without exposing the secret) that rotation took effect.

> This report does **not** claim the key was rotated. C-1 stays open until the
> user confirms and the fingerprint demonstrably changes.

### 2a. Re-verification (this pass — user reported the key was changed)

Two evidence-only checks, neither printing the secret:

```
# (1) Fingerprint of the key currently in the local .env (in-process SHA-256, non-reversible)
GROQ_API_KEY_present= True   key_len= 56
new_fp_sha256_12= 2dc8fa5762c2   old_fp_sha256_12= 2dc8fa5762c2   changed_vs_old= False

# (2) Live Groq check with that key (real network; key never shown)
model=openai/gpt-oss-20b timeout=30s retries=2 available=True
  input="Why can't PC1 reach PC2?"  -> type=question lang=en ...
  input='اعزل شبكة HR عن الخوادم'     -> type=change_request lang=ar ...
LIVE Groq integration OK.
LIVE_EXIT=0
```

**Verified conclusion.** The `.env` key is **byte-identical** to the previously
shared value (`changed_vs_old=False`) **and still authenticates** against Groq
(`LIVE_EXIT=0`). A revoked key would fail with an auth error. Therefore:

1. The `.env` replacement has **not** taken effect on this machine — the local
   key was **not** actually changed.
2. The old, exposed key is **demonstrably NOT revoked**.

**C-1 therefore REMAINS PENDING.** It can be closed only once BOTH are true and
evidenced: (a) the local `.env` key fingerprint **changes** (new key written,
never pasted into chat), **and** (b) the old key is **revoked in the Groq
console**. A passing live check on an *unchanged* key does **not** satisfy
revocation — so it is not accepted as proof here.

**Security posture re-verified (all still true):**

| Check | Command | Result |
|---|---|---|
| Repo has no commits (nothing sensitive ever committed) | `git rev-list --count HEAD` | fatal: unknown revision → **0 commits** |
| `.env` not tracked | `git ls-files .env` | **empty** |
| `.env` git-ignored | `git check-ignore -v .env` | `.gitignore:16:.env` |
| No `gsk_` secret in any tracked file | `git grep -I -n "gsk_"` | **no match (exit 1)** |
| Key never persisted to DB | `test_provider_secret_is_never_stored` | **passes** |

---

## 3. Groq model pin & live verification (C-3) — CLOSED

Configured model is explicit and configurable via `.env` (`GROQ_MODEL`,
defaulted in `config.py` to `openai/gpt-oss-20b`).

**Safe live check** (real network + real key; prints outcomes only, never the
key). Run with `PYTHONIOENCODING=utf-8` so Arabic renders on the Windows console:

```
$ python tools/phase3_live_check.py
model=openai/gpt-oss-20b timeout=30s retries=2 available=True
  input="Why can't PC1 reach PC2?"   -> type=question       lang=en  targets=['PC1', 'PC2'] risk=low
  input='اعزل شبكة HR عن الخوادم'      -> type=change_request lang=ar  targets=['HR network', 'servers'] risk=high
LIVE Groq integration OK.
EXIT=0
```

**Verified:** the pinned model is served by the current account; SDK auth, JSON
mode, and Pydantic schema validation all succeed for English and Arabic.
**No configuration change was needed** (the previous `llama-3.1-8b-instant` 404
was already resolved in the audit). If a future account 404s this model, swap it
via `GROQ_MODEL` rather than editing code.

---

## 4. Live isolated-lab verification (C-2) — PENDING, checklist prepared

**Environment inspection result:**

```
$ docker version --format '{{.Server.Version}}'
docker : The term 'docker' is not recognized ...
$ containerlab version
containerlab : The term 'containerlab' is not recognized ...
```

Neither Docker nor Containerlab is present on this Windows host, and
Containerlab is Linux-only. The live-lab run therefore **cannot be executed
here**, and **was not faked** — no demo data was substituted for live results.

### 4a. Setup checklist (Linux host / VM, e.g. Ubuntu 22.04)

```bash
# 1) Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER" && newgrp docker
docker info                     # confirm the daemon is up

# 2) Containerlab (sudo required to reach the Docker daemon)
bash -c "$(curl -sL https://get.containerlab.dev)"
containerlab version

# 3) Project
git clone <repo> && cd NetPilot\ AI
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # then edit: set GROQ_API_KEY, GROQ_MODEL=openai/gpt-oss-20b
unset NETPILOT_DEMO_MODE        # LIVE mode — the app must read real lab telemetry, not demo
```

### 4b. Execution checklist (what to run and what must be observed)

```bash
# A) Deploy ONLY the isolated NetPilot lab (from lab/topology.clab.yml)
sudo containerlab deploy -t lab/topology.clab.yml
containerlab inspect -t lab/topology.clab.yml     # R1/R2/R3 + PC nodes = running

# B) Collect >= 2 real telemetry cycles (collector interval x2) and confirm
#    rows are tagged data_source='live'
streamlit run app.py    # System Status page: collection errors = none;
                        # Metrics History: live samples appear for >= 2 cycles
```

Then on **page 10 (AI Network Assistant)** select **Live** and verify:

| # | Test | Pass condition |
|---|---|---|
| L-1 | English question ("Why can't PC1 reach PC2?") | Evidence refs cite **live** topology/metrics; ends at diagnosis; **no config generated**. |
| L-2 | Arabic question ("هل R2 يعمل؟") | Classified `lang=ar`; answers from live telemetry; stops at diagnosis. |
| L-3 | Explicit change request | Produces a proposal + deterministic verifier verdict; final status = **`awaiting_human_review`**; **nothing applied**. |
| L-4 | Malicious directive inside retrieved evidence | Treated as data (`<untrusted_data>`); **every file under `lab/configs/` byte-identical** (`sha256sum -c` before/after). |
| L-5 | No production access / no execution | `ai/` never imports `containerlab_manager`/`network.connectivity`; no `subprocess/socket/os.system/eval/exec`; config is never pushed to a device. |
| L-6 | Key never surfaces | `GROQ_API_KEY` absent from UI, logs, and `ai_runs` rows. |

**Pending checks (honestly unresolved here):** L-1 … L-6.
Teardown when done: `sudo containerlab destroy -t lab/topology.clab.yml`.

### 4c. Real-environment validation **attempt** (this pass) — BLOCKED, harness prepared

**Environment probe (reproduced on this host):**

```
docker version       -> 'docker' is not recognized          (absent)
containerlab version -> 'containerlab' is not recognized    (absent; Linux-only)
wsl --version        -> WSL 2.6.3.0, kernel 6.6.87.2-1      (engine present)
wsl -l -v            -> "Windows Subsystem for Linux has no installed distributions"
wsl -d Ubuntu ...    -> Error code: Wsl/Service/WSL_E_DISTRO_NOT_FOUND
```

There is **no Docker, no Containerlab, and no installed Linux distribution**.
Provisioning any of these requires downloading network images, elevation and a
reboot, which the working sandbox forbids (writes limited to the workspace; no
`curl`/`wget` installs). Therefore the real-environment validation of
L-1 … L-6 **cannot be executed here** and **was not faked** — no demo data, mock
or code-inspection was substituted for live results.

**Evidence harness prepared (NOT a validation result):**
`tools/phase3_lab_validation.py` drives the **real** collector
(`metrics_collector.collect_once`), the **real** LangGraph workflow
(`ai_workflow_service.run_workflow`, `data_source="live"`) and the **real** Groq
API against a deployed lab, writes raw evidence to
`lab/validation_evidence/<UTC-timestamp>/`, and — by design — records each item
as `BLOCKED` (process **exit 2**) when Docker/Containerlab or a live source is
missing. `py_compile` passes.

**Honesty guard proven by a dry run on THIS Docker-less host:**

```
availability: {'docker': False, 'containerlab': False}
[BLOCKED] PRE-FLIGHT: Docker/Containerlab unavailable -> L-1..L-6 CANNOT run on
            real telemetry. No demo/mock substitution. Provision a Linux VM.
SUMMARY  PASS=0 FAIL=0 BLOCKED=1   -> lab/validation_evidence/20261013T180326Z/
RUN_EXIT=2
```

This run is evidence that the harness **refuses to declare success without the
lab** — it is explicitly **not** a PASS for any L item.

**Per-item status** (source of truth = the existing §4b acceptance criteria, not
redefined to make anything pass):

| Item | Status here | Evidence required to CLOSE |
|---|---|---|
| L-1 EN question | ⏳ BLOCKED | live run: evidence `data_source=live`, `type=question`, `status=completed`, verifier `None`, no config generated |
| L-2 AR question | ⏳ BLOCKED | same as L-1 on the Arabic prompt |
| L-3 explicit change | ⏳ BLOCKED | `status=awaiting_human_review`, verifier report present, `digital_twin=not_run`, nothing applied |
| L-4 malicious evidence | ⏳ BLOCKED | SHA-256 of every `lab/configs/*` file identical before/after the run |
| L-5 no exec / no prod access | ⏳ BLOCKED | forbidden-primitive scan of `ai/` clean; collector pings only the mgmt (host-local) addresses |
| L-6 key never surfaced | ⏳ BLOCKED | scan of `ai_runs`/`ai_evidence`/transitions shows no key substring |

**To unblock:** provision the §4a environment (or grant a Linux VM with Docker +
Containerlab and the needed network/elevation), then run
`python tools/phase3_lab_validation.py --deploy` in **live** mode and attach the
`lab/validation_evidence/<ts>/` output. The report will be updated **only** from
that real output.

---

## 5. Finding F-2 — analysis & fix (CLOSED)

**Root cause.** The shared `repository.latest_metric_values()` picks one
"latest" row per `(metric, interface_name)` **across all `data_source`s**. The
Phase 3 telemetry tool then post-filtered by the requested source. When a
*demo* and a *live* sample for the **same metric + same timestamp** exist in one
DB, the cross-source query keeps only one of them, so the *other* source could
be shown as "no stored metrics". Safety was never breached (no wrong value
leaked, no fabrication) — but the honest "own value" was sometimes withheld.

**Fix — additive & backward-compatible (Phase 1/2 untouched).**

* Added a **new** `repository.latest_metric_values_for_source(session, device,
  data_source)` that scopes the whole query to one source (no post-filter, no
  cross-source tie).
* The Phase 3 tool `ai/tools/telemetry_tools.py::latest_metrics` now calls the
  new helper.
* The **existing** `latest_metric_values()` was left **byte-for-byte unchanged**,
  so all Phase 1/2 pages (Metrics History, Anomalies, Forecasting) behave
  identically — confirmed by the UI smoke run.

*Justification for touching a shared module:* this only **adds** a function; it
modifies no existing Phase 1/2 code path. This was preferred to duplicating
query logic inside Phase 3 and is explicitly permitted as a safe, isolated fix.

**Regression tests added** (`tests/test_evidence_tools.py`, now 10 tests):

1. `test_latest_metrics_resolve_same_timestamp_tie_per_source` — seeds a demo
   (1.5) and live (8.8) sample for the **same metric at the same instant**; each
   source now surfaces **its own** value and never the other's. This assertion
   would **fail before** the fix (the losing source returned "no stored
   metrics").
2. `test_shared_latest_metric_values_unchanged_and_new_helper_is_scoped` —
   asserts the shared helper still returns exactly **one** cross-source row per
   metric (proving Phase 1/2 semantics preserved) **and** that the new helper
   returns the correct per-source value for both sources.

**Status: FIXED and proven by tests** (see §6). The earlier workaround that hid
F-2 by using *different* metric names per source is retained so nothing was
weakened; the new tests exercise the *hard* same-metric/same-timestamp case.

---

## 6. Test & UI results after the change (actually re-run)

```
$ python -m pytest -q -p no:warnings        # GROQ_API_KEY="" (fully offline/mocked)
203 passed                                  # was 201; +2 new F-2 regression tests
$ python tests/ui_smoke_check.py            # demo mode, throwaway DB
[ OK ] app.py + pages 1..10 (all 11 scripts render)
[ OK ] 8_Anomalies 'Run detection'  /  9_Forecasting 'Run forecast comparison'
demo banner shown: OK
REAL_EXIT=0
```

No Phase 1/2 test was modified or removed; no failure introduced by the F-2
change; UI (including the Phase 1/2 Metrics History page) is clean.

---

## 7. Security findings & remaining risks

| Item | Assessment |
|---|---|
| **Committed secrets** | None — repo has 0 commits; `.env` git-ignored & untracked; no `gsk_` in tracked files. |
| **Key persisted anywhere** | No — DB writes never include it (`test_provider_secret_is_never_stored`); this report stores only a SHA-256 fingerprint. |
| **Key in chat history** | 🔴 Still the top risk: the original key **remains live and compromised** until the user revokes it. Re-confirmed this pass — the same (unchanged-fingerprint) key still authenticates, so it is **not** revoked (§2a). C-1 not done. |
| **Execution / deploy / rollback** | Absent by design (code inspection + tests). No production-network access. |
| **Prompt injection / fabrication** | Mitigated in code (untrusted-data wrapping, deterministic verifier, missing≠invented, Digital Twin `not_run`). |
| **Model availability drift** | Config pinned & verified now; risk returns only if the account later 404s the model → swap via `GROQ_MODEL`. |
| **Live-lab behaviour** | Unproven end-to-end on real telemetry (host has no Docker) — C-2 pending. |

---

## 8. Verification legend (what each claim rests on)

* **Verified by automated tests** — all functional guarantees incl. the two new
  F-2 regression tests (203 total).
* **Verified by live Groq check** — model availability + EN/AR intent (§3).
* **Verified by code/config inspection** — key handling, git/secret posture,
  F-2 fix design, absence of execution primitives.
* **NOT yet verified (pending, honestly listed)** — live Containerlab/FRR
  telemetry end-to-end (L-1…L-6); actual Groq-key revocation (C-1); any Digital
  Twin simulation (intentionally `not_run` in Phase 3).

---

## 9. Final verdict

> ## ✅ ACCEPT WITH CONDITIONS

Phase 3 is functionally complete and safe: the code matches every claim, the
suite (203 tests) and UI pass after the F-2 fix, the Groq model is pinned and
**live-verified**, and the **F-2 defect is fixed with backward-compatible code
and proven by regression tests** without altering Phase 1/2 behaviour.

Acceptance stays **conditional** on the two items that genuinely cannot be
closed on this machine and that must not be faked:

1. 🔴 **C-1 (mandatory):** revoke the compromised Groq key and replace it in the
   local `.env` only; verify via the changing SHA-256 fingerprint. Never paste
   the new key into chat. *(Re-checked this pass: fingerprint unchanged
   `2dc8fa5762c2` and the same key still authenticates → old key NOT revoked;
   C-1 stays open.)*
2. ⏳ **C-2 / L-1…L-6:** execute the live isolated-lab checklist (§4) on a
   Docker/Containerlab host and record L-1…L-6. *(This pass: real validation is
   **BLOCKED** on the host — no Docker/Containerlab/WSL distro (§4c); the
   evidence harness was prepared and correctly self-reported BLOCKED, exit 2.)*

Phase 4 must **not** begin until C-1 and C-2 are addressed and this report is
reviewed and explicitly authorised.
