# NetPilot AI — V2 Integration & Acceptance Gate Report

**Date:** 2026-10-14 · **Host:** Windows (no Docker/Containerlab) · **Runner:** offline suite + live local probes
**Scope:** verify shipped V2 capabilities work together. No new features were added during this gate run.

Verdict legend: **PASS** = executed here with recorded evidence · **FAIL** = executed, criterion not met ·
**BLOCKED** = cannot be executed on this host; requires the named environment (per rule 7, not closed by inspection).

---

## 0. Executive verdict

| Gate | Result | One-line evidence |
|---|---|---|
| A — Secret hygiene | **FAIL (action required)** | `.env` still holds the **exact key that was exposed** on 2026-10-13 (SHA-256 prefixes identical) — it has **not been rotated**; the live provider check passes (exit 0), so the exposed key is still valid |
| B — Docker & CI | **BLOCKED** | Docker/WSL distro absent on this host; repo has **no commits and no remote**, so the CI pipeline has never run |
| C — Live isolated lab | **BLOCKED** | No Docker, no Containerlab, no WSL distro on this host |
| D — Change-loop E2E | **PARTIAL** (offline gates PASS · live apply/rollback BLOCKED) | 11/11 state-machine tests pass incl. all refusal gates; apply/rollback on real nodes needs the lab host |
| E — Streamlit integration | **PASS in demo mode; live mode honest-blocked** | 311/311 tests, UI smoke exit 0 (12 pages, demo), live-honesty probe exit 0, real uvicorn server probes all green |

**Overall: V2 is NOT acceptance-ready yet.** One security FAIL and three environment BLOCKEDs must be resolved (see §7).

---

## 1. Gate A — Secret hygiene

| Check | Command executed | Result | Exit |
|---|---|---|---|
| Tree secret scan | `python tools/secret_scan_check.py` | "Secret scan clean." | 0 |
| Scan has test coverage | `pytest` (test_secret_scan.py, 5 tests) | all pass (within 311) | 0 |
| Key rotation verification (no secrets printed) | hash-compare of `.env` key vs the key exposed on 2026-10-13, printing only `sha256[:12]` | `sha256(cur)[:12] == sha256(old)[:12] == 2dc8fa…` → **ROTATED = False** | — |
| Live provider check (outcomes only) | `python tools/phase3_live_check.py` (EN + AR intent) | `LIVE Groq integration OK.` — EN→question/low, AR→change_request/high | 0 |

**Findings**
1. **FAIL:** the locally configured key is **byte-identical to the previously exposed key**, and the live check succeeds — therefore the exposed key has **not been revoked** either.
   **Required action (operator, in the Groq console — cannot be done from this repo):** revoke that key, issue a new one, place it **only** in the git-ignored `.env`, then re-run the two checks above (rotation hash-compare must show `ROTATED=True`, live check must show exit 0).
2. PASS: no secret ever entered git (`.env` is git-ignored; verified previously with `git ls-files`), the scanner keeps this gate in CI, and no secret was printed during this verification (only non-mutating hash prefixes were shown).

## 2. Gate B — Docker & CI

| Check | Command | Result | Exit |
|---|---|---|---|
| Docker available? | `docker --version` | CommandNotFoundException | non-zero |
| WSL distro (local Docker path)? | `wsl -l -v` | "no installed distributions" | — |
| CI ever run? | `git remote -v` / `git log` | **no remote; branch `master` has 0 commits** → pipeline never triggered | — |
| Config parse (static, explicitly NOT a pass) | YAML parse of `docker-compose.yml` + `.github/workflows/ci.yml`; `Dockerfile` reviewed | both parse | 0 |

**BLOCKED evidence requirement:** on a Docker host run `docker compose up --build` and record: dashboard `http://localhost:8501` renders with demo banner; API `/health` returns `{"status":"ok", "demo_mode":true, "database":…}`; `/api/*` responds; `db` service healthy. On GitHub: push to a repo with the `ci` workflow attached and attach the run permalink (jobs: secret-scan, test matrix 3.11/3.12, ui-smoke, docker-build).

## 3. Gate C — Live isolated lab

**BLOCKED on this host** (Docker/Containerlab absent — commands and outputs above). No live telemetry was collected and no claim of it is made.
On the Linux VM, in order:
1. Install Docker + Containerlab (README §2).
2. `containerlab deploy -t lab/topology.clab.yml` → expected nodes **R1, R2, R3** (FRR) + **PC1, PC2** (linux) all `running` in `containerlab inspect`.
3. Run ≥2 collector cycles (sidebar scheduler or manual) with `NETPILOT_DEMO_MODE=false` against the live SQLite/Postgres store.
4. Execute the documented harness: `python tools/phase3_lab_validation.py` (checks L-1…L-6) and archive its full output; label each of L-1…L-6 **PASS/FAIL/BLOCKED** with raw evidence attached.

## 4. Gate D — Change-loop E2E

**Offline safety gates — PASS (executed):** `pytest tests/test_change_service.py` → 11 passed, inside the 311-run (exit 0).
They prove by execution: validation rejects command smuggling; self-approval forbidden; **apply refused** when the loop flag is off, when no executor is injected, and when the request isn't approved; executor receives only the stored proposed config; failures recorded; rollback restores base; cancel works.
Live dashboard behaviour was additionally verified in Gate E (page 11 renders the change loop; apply is a no-op there because no executor is injected).

**Live E2E — BLOCKED:** proposal → approval → **real lab apply** → post-change verification → rollback-on-regression requires Gate C's environment. Record on the VM: config hashes before/after each apply, the `change_requests` audit rows, and an injected-regression rollback. Production devices are never in scope (hard allowlist + lab-only design).

## 5. Gate E — Streamlit integration acceptance

| Journey / criterion | Evidence (executed) | Result |
|---|---|---|
| Full offline suite | `pytest -p no:warnings` → **311 passed in 76.84s** | PASS (exit 0) |
| All 12 UI targets render (demo mode) | `python tests/ui_smoke_check.py` → every page `[OK]`, detection + forecast buttons executed, demo banner shown | PASS (exit 0, re-confirmed after probes) |
| Live-mode honesty (lab absent) | temp AppTest probe (created → run → deleted): 12/12 pages render with `DEMO_MODE=false`; landing page states *"Containerlab/Docker is not available on this host, so live lab data cannot be collected yet"* — no fabricated live claim | PASS (exit 0) |
| Alerting / graph / anomaly / forecast / RCA / metrics / devices | covered by `test_alerts`, `test_graph_intelligence`, `test_anomaly_detection`, `test_ml_anomaly`, `test_rca`, `test_backtest`, `test_forecasting`, `test_collector`, `test_derived_metrics` (all pass in-suite) + UI smoke render | PASS (demo-mode functional; live pending Gate C) |
| RAG + NL queries + AI review | `test_rag`, `test_nlquery`, `test_ai_persistence` pass; page 10 renders in both smoke runs; live Groq intent classification verified end-to-end (§1) | PASS |
| Provenance separation | `test_demo_mode` (5 tests, incl. cross-store separation) passes; API health echoes `demo_mode:true`; `/api/ask` grounded on demo rows only | PASS |
| Auth/RBAC opt-in | `test_auth` (13 tests) passes; UI smoke unaffected in open mode (by design) | PASS |
| Secret redaction | live check prints outcomes only; this report prints no secrets; CI scanner clean | PASS |

**Optional API surface — verified with a REAL server** (`uvicorn api.app:app --port 8765`, then cleanly stopped and confirmed down):

| Endpoint | Response (abridged) |
|---|---|
| `GET /health` | `{"status":"ok","database":"sqlite:///…/netpilot_demo.db","api_version":"1","demo_mode":true}` |
| `GET /metrics` | JSON `{"counters":{"api_requests_total":…},"uptime_seconds":…}` |
| `GET /metrics` + `Accept: text/plain` | `Content-Type: text/plain; version=0.0.4` with `netpilot_api_requests_total 3` … (Prometheus negotiation works over the wire, not just TestClient) |
| `GET /api/devices` | `{"count":5, items:[PC1,PC2,R1,R2,R3 …]}` |
| `POST /api/ask` `{"question":"how many devices","data_source":"demo"}` | `{"intent":"device_count","answer":"5 device(s) recorded.", rows…}` |
| `GET /api/risk` | articulation nodes `[R1,R2,R3]` + 4 bridges — true SPOFs of the documented star lab |
| `GET /api/alerts` | `{"count":0,"open_total":0}` (no rules configured — honest empty) |

## 6. Test counts and failures

- **311 passed, 0 failed, 0 skipped** in 76.84 s (pytest exit 0) — Python 3.12.10, Windows.
- UI smoke: exit **0** (two runs: pre-probe and final post-cleanup).
- Live-honesty probe: exit **0** (12/12 pages).
- Live Groq check: exit **0** (2/2 classifications correct).
- No existing test was weakened or removed during this gate run; the repository tree is unchanged apart from this report file.

## 7. Remaining blockers, risks & required actions

| # | Item | Severity | Required action |
|---|---|---|---|
| 1 | **Exposed Groq key is still the configured, valid one (Gate A FAIL)** | **Critical — security** | Revoke in Groq console → new key into `.env` only → re-run §1 checks, record `ROTATED=True` + live exit 0 |
| 2 | CI pipeline has never executed (no commits/remote) | High | Commit, push to GitHub, attach green CI run permalink |
| 3 | Compose stack never started on a Docker host | High | Run `docker compose up --build` on Docker host; capture health/demo evidence (§2) |
| 4 | Live lab (Gate C) never executed; zero real-telemetry evidence exists | High | Linux VM: deploy lab, ≥2 cycles, run `tools/phase3_lab_validation.py`, archive L-1…L-6 results |
| 5 | Change-loop apply/rollback on real nodes unproven (Gate D live half) | Medium | After C passes: lab-only E2E with before/after hashes + injected-regression rollback |
| 6 | Streamlit *live-mode* journeys beyond honesty (real charts on real data) unproven | Medium | After C passes: repeat demo-mode journeys in live mode |
| 7 | Stray `data/_auth_smoke.db` from an earlier session | Low | git-ignored/disposable; may be deleted any time |
| 8 | Repo lives inside a parent folder with `master` branch & 0 commits | Low | Decide repo boundary before first push (avoid shipping the parent dir) |

**Until items 1–5 have executed evidence, the V2 platform must not be declared acceptance-ready or production-grade.**
