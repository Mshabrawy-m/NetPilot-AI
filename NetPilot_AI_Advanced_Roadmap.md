# NetPilot AI — Advanced Capabilities Roadmap (v2.0)

> **Purpose:** turn NetPilot from a solid foundation (lab + telemetry + analytics +
> a review-only AI assistant) into a **powerful, production-grade network
> operations platform.** This is a *plan and menu*, not a changelog — nothing here
> is implemented yet. Pick the tracks and I build them, test-driven, on top of
> Phases 1–3.

**Design principles (non-negotiable, carried from Phase 3):**
1. **Safety first** — any change/automation is gated: propose → deterministic
   verify → (real) Digital-Twin simulate → **human approval** → apply to the
   *isolated lab only* → post-change verify → auto-rollback on regression.
2. **Evidence over vibes** — every AI claim cites real telemetry; every metric is
   reproducible; every model/forecast is evaluated, not asserted.
3. **Additive, tested** — no rewrite of working Phase 1–3 code; new capability
   ships with automated tests + UI smoke, and the suite must stay green.
4. **Honesty** — unavailable data/sources are reported as such, never fabricated.

Legend: **P0** must-have next · **P1** high value · **P2** later · Effort **S/M/L/XL**.

---

## 1. Honest current baseline (what exists today)

| Area | Today | Gap |
|---|---|---|
| Lab & collection | Containerlab+FRR topology, periodic collector, live/demo DB | single-process scheduler, no backpressure/retention |
| Analytics | derived metrics, EDA, z-score/IQR anomalies, ARIMA-style forecast, data-quality | no ML models, no multivariate/root-cause, no drift |
| Topology | static graph + status | no graph algorithms (path, blast-radius, dependency) |
| AI assistant | LangGraph multi-agent, AR/EN, verifier, review-only | no RAG, no memory, no NL→query, no eval harness, no cost/latency mgmt |
| Automation | none (by design) | no closed-loop remediation, no approvals workflow beyond a flag |
| Platform | Streamlit, SQLite, env config | no auth/RBAC, no REST/WS API, no alerting/integrations, no containerization, no observability |
| UX | new dark theme (this session) | pages 2–10 not yet branded; no live views/maps/exports |

---

## 2. Capability pillars & feature menu

### A. Real Digital Twin & safe change automation  *(the "Phase 4" upgrade, gated)*
| Feature | What / Value | Pri | Eff | Acceptance criteria |
|---|---|---|---|---|
| Digital Twin engine | Wire a real pre-change verifier (routing reachability/config analysis) behind the existing `DigitalTwin` ABC so `not_run` becomes `simulated` with results | P1 | L | A proposed change yields a simulated reachability/routing diff; deterministic verifier still authoritative |
| Approval workflow | Role-gated approve/reject with reason, expiry, and full audit (who/what/when) | P1 | M | AI proposal can only apply after an authenticated approval record |
| Gated apply (lab-only) | Apply approved config to containerlab nodes; hard allowlist; never prod | P1 | L | Applied only to lab; e2e test asserts prod is unreachable |
| Auto-rollback | Snapshot pre-state; verify post-change; roll back on regression | P1 | L | Injected failure triggers rollback and is recorded |
| Post-change verification | Re-collect telemetry, compare to pre, pass/fail gate | P1 | M | Green/red verdict persisted with the run |

### B. Graph & topology intelligence
| Feature | Value | Pri | Eff | AC |
|---|---|---|---|---|
| Network graph engine (networkx) | reachability, shortest path, cut/link-sensitivity, dependency | P0 | M | Answer "which devices lose path if R1 eth1 fails?" from real topology |
| Blast-radius / impact analysis | show downstream impact of a change before approval | P1 | M | Proposal annotates affected nodes/subnets |
| Path troubleshooting | hop-by-hop diagnosis between two endpoints with telemetry overlay | P1 | M | Visual + textual path with per-hop metrics/health |
| Redundancy / SPOF detection | surface single points of failure | P2 | S | Ranked SPOF list |

### C. Advanced analytics & ML
| Feature | Value | Pri | Eff | AC |
|---|---|---|---|---|
| Modern forecasting (sktime/Prophet) + backtesting | better accuracy, honest error metrics | P1 | L | beats current baseline on MAE in a rolling backtest |
| ML anomaly detection (Isolation Forest, multivariate, autoencoder) | catch correlated/fault signatures, not just univariate spikes | P1 | L | precision/recall on labelled demo events ≥ baseline |
| Root-cause correlation | link anomalies across devices/links to a probable cause | P1 | L | RCA ranked list grounded in evidence refs |
| Concept-drift detection | notice when traffic patterns change | P2 | M | drift flag + explanation |
| Capacity & trend insights | 95th-percentile, utilization, growth, SLA/jitter budgets | P1 | M | per-device/link capacity report |
| Retention & downsampling | rollups so long ranges stay fast, bounded DB | P1 | M | 12-month window renders <2s |

### D. Agentic AI upgrades
| Feature | Value | Pri | Eff | AC |
|---|---|---|---|---|
| RAG over runbooks/configs/docs | cite the exact SOP/config clause in answers (Chroma + embeddings) | P1 | L | answers include a retrieved-source citation with provenance |
| NL → telemetry query (AR/EN) | "avg ping loss per router last 6h" → safe SQL/pandas | P1 | M | read-only, allowlisted, returns a chart/table |
| Resolution memory | recall similar past incidents + outcomes; suggest proven fixes | P2 | M | "similar to run #X resolved by Y" |
| Self-critique / guardrail loop | second-pass check reduces hallucination | P2 | M | lower unverified-claim rate in eval |
| Provider routing, caching & fallback | cost/latency control, retries, model fallback when a model 404s | P1 | M | per-run token/cost/latency logged; fallback exercised in tests |
| Confidence calibration & eval harness | a scored golden-set eval for intent/diagnosis/verifier | P1 | M | CI reports eval score; regression gates merge |
| Streaming responses | token-by-token UI during long runs | P2 | S | first token <1s, progressive render |

### E. Alerting, integrations & automation
| Feature | Value | Pri | Eff | AC |
|---|---|---|---|---|
| Alert rules engine | thresholds/trends/detection → alert lifecycle (open/ack/resolve) | P0 | M | rule fires, dedups, persists, shows on UI |
| Notifications | email + Slack/Telegram/webhook, templated | P1 | M | test-send verified off an event |
| Escalation / on-call policy | time-based escalation, severity routing | P2 | M | simulated escalation trace |
| Scheduled reports | daily/weekly PDF/HTML digest | P1 | M | generated artifact stored & linked |

### F. Enterprise readiness (platform)
| Feature | Value | Pri | Eff | AC |
|---|---|---|---|---|
| Auth + RBAC | login, roles (viewer/operator/admin), per-action guards | P0 | L | unauthenticated blocked; role enforced in tests |
| REST + WebSocket API + live streaming | integrate external tools; push telemetry updates | P1 | L | FastAPI endpoints + WS feed contract-tested |
| Multi-user / Postgres path | production storage (already SQLAlchemy-portable) | P1 | M | integration test against Postgres |
| Audit & data lineage | tamper-evident trail, provenance per metric | P1 | M | lineage view from source → chart |
| Secrets & KMS hygiene | no secret in logs/UI/DB; rotate flow | P0 | S | automated scan (already partly present) |

### G. UX / visualization overhaul
| Feature | Value | Pri | Eff | AC |
|---|---|---|---|---|
| Brand all pages (2–10) | consistent `page_header`, KPI tiles, spacing | P0 | S | every page uses the theme kit |
| Plotly interactive charts + dark theme | zoom/hover/cross-filter vs static | P0 | M | replace key matplotlib views |
| **Live topology view** | animated graph, color by health, click-through to device | P1 | M | nodes update from live/demo state |
| Command-center home | unified overview with alerts, health, AI inbox | P1 | L | one-glance operational posture |
| Cross-page drill-downs | metric → device → path → AI explain | P1 | M | linked navigation preserves filters |
| CSV/PDF export + shareable links | take analysis with you | P2 | M | export verified |

### H. Reliability, security & DevOps
| Feature | Value | Pri | Eff | AC |
|---|---|---|---|---|
| Containerize the app (docker-compose: app+db+redis) | one-command spin-up | P1 | M | compose up runs the stack |
| CI + coverage + eval gate | tests, lint, security scan, model-eval on PR | P0 | M | green pipeline required |
| Self-observability | Prometheus metrics, structured logs, OTel traces | P1 | M | /metrics exposes collection & API health |
| Async/parallel collection + queue (Redis/Celery or asyncio) | scale collectors, backpressure | P1 | L | sustained higher device count without overlap |
| Threat model & hardening review | injection, authz, supply-chain | P1 | M | documented mitigations in place |

---

## 3. Phased delivery roadmap

**Sprint 1 — "feel powerful now" (safe, quick, high-visibility):**
1. Brand pages 2–10 + Plotly interactive charts (G).
2. Alerts rules engine v1 + lifecycle + sidebar bell (E).
3. Network-graph engine + reachability/path + blast-radius (B) — powers the AI too.
4. Auth/RBAC v1 (local) (F). *(Foundational; unblocks API/automation later.)*

**Phase 4 — Safe change loop (A + gated):** Digital Twin engine → approval workflow →
lab-only apply → post-change verify → auto-rollback.

**Phase 5 — Intelligence & integration (C + D + E):** ML anomaly + forecasting + RCA;
RAG + NL-query + eval harness + cost/latency; notifications + scheduled reports.

**Phase 6 — Platform & scale (F + G + H):** REST/WS API + live streaming + command
center; Postgres/Redis/queue; containerize; CI/observability/threat-model.

*(Each phase: tests + UI smoke must stay green; demo/live provenance preserved.)*

---

## 4. Proposed tech-stack additions (Windows-friendly)
- **Charts/UX:** `plotly`.
- **Graph:** `networkx`.
- **ML/forecast:** `scikit-learn`, `statsmodels`/`sktime` (optionally `prophet`), `numpy` (present).
- **RAG:** `chromadb` (embedded) + a Groq-hosted embedding/chat choice; `rank-bm25` fallback.
- **Auth/API:** `fastapi` + `uvicorn` + `passlib`/`pyjwt`; `websockets`.
- **Scale/persistence:** `redis` + `celery` (or asyncio), `PostgreSQL` (SQLAlchemy URL switch).
- **Ops:** `docker`/`compose`, `prometheus_client`, `structlog`, `opentelemetry` (optional).
- **Reports:** `weasyprint`/`matplotlib` PDF or `streamlit`→HTML export.

> No heavy dependency is added without a locked, tested use in the same PR.

---

## 5. Data-model additions (additive tables)
`alert_rules`, `alerts` (lifecycle), `approvals` (workflow), `change_records`
(pre/post/rollback), `graph_edges_snapshot`, `embeddings/rag_docs`, `nl_queries`
(audit), `run_costs` (tokens/latency/model), `users`/`roles`, `notifications`,
`report_jobs`, `retention_rollups`.

---

## 6. Testing & evaluation strategy
- Keep the **offline mock** discipline (suite needs no key/network).
- Add a **golden-set eval** for AI intent/diagnosis/verifier (scored, CI-gated).
- Property tests for graph/path & blast-radius; contract tests for the API.
- e2e for the safety loop using containerlab on the Linux VM (the L-1…L-6 harness
  already prepared) — and it must refuse/fail honestly when the lab is absent.

## 7. Success metrics (KPIs)
- **MTTR ↓** (time to diagnose an incident), **% anomalies auto-root-caused**.
- **Forecast accuracy** (MAE/MAPE vs baseline) and **anomaly precision/recall**.
- **Change safety:** 0 un-gated applications; rollback success = 100%.
- **AI quality:** verified-claim rate, hallucination rate, cost/latency per run.
- **Platform:** API uptime, chart render time, dashboard load < 2s on 12-month range.

## 8. Risks & mitigations
| Risk | Mitigation |
|---|---|
| Automation touches real networks | Hard lab-only allowlist; approval+verify+rollback; kill-switch; prod unreachable by design (tested). |
| Model 404/cost blowups | Provider routing + caching + fallback + per-run budget caps. |
| Scope sprawl | Ship per-pillar, test-first; keep Phase 1–3 green every merge. |
| Windows can't run the live lab | Live-lab items stay behind the Docker/Containerlab host (harness + checklist ready). |

---

## 9. Recommended immediate next step
Start with **Sprint 1** (branding + Plotly + alert engine + graph engine + local
auth). These are safe, deliver a large "powerful" leap fast, and lay rails for the
AI and automation phases.

## 10. Decisions I need from you
1. Which track first — **Sprint 1** (recommended), or straight to **Phase 4 change-loop**, or **ML/analytics (C)**?
2. Deployment target: keep **SQLite+Streamlit** (simple) or move toward **Postgres + FastAPI API** (multi-user)?
3. AI extras priority: **RAG over runbooks** and/or **NL→query** now?
4. Auth needed now (local users/RBAC) or later with the API phase?

> I will not implement until you choose; Phase 4's apply/rollback path stays
> **approval-gated and lab-only** exactly as in Phase 3.

---

## 11. Delivery log (v2 build) — evidence only

The all-in track was confirmed (Sprint 1 + Phase 4 + ML + RAG + REST/WS+Postgres
+ best auth). Heavy libraries were found **already installed** (`networkx 3.7`,
`plotly 7.1.0`, `scikit-learn 1.5.2`, `scipy`, `statsmodels`, `numpy`, `uvicorn`),
so those slices need no risky new installs. Slices ship test-first; the whole
suite and the Streamlit UI smoke must stay green.

| Slice | Shipped | Files | Verification (actual) |
|---|---|---|---|
| **1a Graph intelligence** | reachability, shortest-path, blast-radius (node/link), SPOF (articulation nodes + bridge links), connectivity summary | `analytics/graph.py` | `tests/test_graph_intelligence.py` — 11 tests pass |
| **1b Alerting engine** | `alert_rules`/`alert_events` tables; pure matcher; source-scoped firing with de-dup + open/ack/resolve lifecycle; run-cycle service | `database/models.py`, `database/repository.py`, `analytics/alerts.py`, `services/alert_service.py` | `tests/test_alerts.py` — 11 tests pass (incl. live/demo provenance never merges) |
| **1c Ops Center UI** | new page: rules CRUD, run-cycle, fired-alert queue, graph insights/impact | `pages/11_Ops_Center.py`, nav in `app.py` | UI smoke: all 12 targets `[OK]`, `EXIT=0` |
| **2 Auth + RBAC (opt-in)** | `users` table; PBKDF2-HMAC-SHA256 (240k) + constant-time verify; viewer<operator<admin; authenticate; login gate + logout; admin-only Access Control | `database/models.py`, `services/auth.py`, `database/repository.py`, `services/app_state.py`, `pages/11_Ops_Center.py`, `config.py` | `tests/test_auth.py` — 13 tests pass; auth-on UI check: gate renders, content blocked pre-login, admin signs in, bad creds rejected |
| **3 Visual polish** | global dark Plotly template (all `st.plotly_chart` inherit it) + branded `page_header` on pages 2–10 | `ui/theme.py`, `services/app_state.py`, pages 2–10 | UI smoke: all pages `[OK]`, `EXIT=0` |
| **4a Multivariate anomaly** | seeded Isolation Forest over aligned multi-metric frames — catches joint deviations a single-metric view misses; complete-case only | `analytics/ml_anomaly.py` | `tests/test_ml_anomaly.py` — 5 tests |
| **4b Root-cause correlation** | cluster anomalies by time + network topology, rank probable origin (precedence + betweenness) using the graph engine | `analytics/rca.py` | `tests/test_rca.py` — 6 tests; surfaced in Ops Center 🧠 Insights |
| **4c Forecast backtest** | rolling-origin replay — each baseline fit only on pre-window data, scored per next-step; MAE/RMSE/MAPE + skill-vs-naive + best-model; origins anchored at start so appended future can't leak | `analytics/backtest.py` | `tests/test_backtest.py` — 7 tests (incl. no-leakage & determinism); surfaced in Forecasting 📐 expander |
| **5a RAG (offline retrieval)** | deterministic TF-IDF retrieval over the project's own Markdown (roadmaps/runbooks/reports) — verbatim quoted chunks with source+heading provenance; no vector DB, no embeddings, no network, no new dependency; honest no-match (never guesses) | `services/rag.py` | `tests/test_rag.py` — 5 tests (relevance, empty corpus, stopword-only query, determinism, overlong-paragraph chunking) |
| **5b NL → data query** | deterministic AR/EN question → structured read over repository + graph engine (down devices, inventory, open alerts, SPOF, blast-radius, reachability, worst/best metric, device status, connectivity); every answer from stored rows, unrecognised intent reported, not fabricated; works without a Groq key | `services/nlquery.py` | `tests/test_nlquery.py` — 7 tests; both surfaced as page 10 tab **📚 Knowledge & Ask** |
| **6 REST/WS API + Postgres** | opt-in, thin, read-only FastAPI surface (`/health`, `/metrics`, `/api/devices[/name]`, `/alerts`, `/topology`, `/risk`, `/ask` GET+POST) + `/ws/topology` request/response socket; Bearer-token guard when `NETPILOT_API_TOKEN` is set, CORS opt-in; storage is whatever `DATABASE_URL` names (SQLite default, PostgreSQL by URL only — no code change); no Streamlit import, mutates nothing | `api/app.py`, `api/readmodels.py`, `services/telemetry.py`, `database/connection.py` | `tests/test_api.py` — 10 tests (read-models + HTTP + WS via `TestClient`, `importorskip` so a keyless/offline box simply skips them) |
| **7 Gated change loop** | approval-bound, lab-only config workflow: propose → **same deterministic FRR validator** rejects command-smuggling → human `approve` (self-approval forbidden) → `apply` only if `NETPILOT_CHANGE_LOOP_ENABLED` **and** status `approved` **and** an injected lab executor is present (the UI injects none, so apply is always a no-op there) → `rollback` restores base config; full audit persisted | `services/change_service.py`, `database/models.py` (`change_requests`), `database/repository.py`, `ai/verification/config_validation.py`, page 11 **🔁 Change loop** tab | `tests/test_change_service.py` — 11 tests (valid/malicious, self-approval refusal, apply refused when disabled / no executor / not approved, executor-called-with-proposed-config only, failure recorded, rollback, cancel) |
| **8 Containerize + CI + observability** | one image, two roles (`dashboard`/`api`) behind an entrypoint, non-root, `psycopg2` in the image, `/data` for SQLite; compose stack `db + dashboard + api` (boots in labelled demo mode) with an optional `--profile lab` Containerlab service; hand-rolled **Prometheus text exposition** on `/metrics` (`Accept: text/plain`, JSON otherwise) — no compiled client (App Control); GitHub Actions CI = secret-hygiene gate + pytest matrix (3.11/3.12) + headless UI smoke + build-only docker smoke | `Dockerfile`, `docker/entrypoint.sh`, `docker-compose.yml`, `.dockerignore`, `.github/workflows/ci.yml`, `tools/secret_scan_check.py`, `services/telemetry.py` | `tests/test_secret_scan.py` — 5 tests (live tree clean = exactly what CI asserts; planted-secret detection; placeholder/fake ignored); `test_api.py` Prometheus-exposition test; compose/Dockerfile/CI YAML parse-verified; full suite + UI smoke below |

| **Enh · Multi-layout Topology** | page 2 now renders the **same stored graph in four layout types** — layered tree / force-directed / circular / grid — as a richer interactive view: KPI tiles (devices/links/components/SPOFs/down), markers sized by degree & shaped by type, health-coloured links, hover detail; a **demo-only, operator-gated** button expands the view with a large deterministic synthetic campus fabric (~90–200 devices) written to the separate demo DB so the layouts have real scale (live mode shows the real lab only) | `services/topology_service.py` (`compute_layout`/`degree_map`), `services/demo_data.py` (`seed_demo_fabric`), `pages/2_Topology.py` | `tests/test_topology_views.py` — 18 tests (every layout positions all nodes & is deterministic; ring/grid/tree geometry; `seed_demo_fabric` refuses outside demo, validates args, builds a connected all-demo fabric, is idempotent). Suite now **311 passed**, UI smoke `EXIT=0` |

**Suite after the topology enhancement: `311 passed`, UI smoke `EXIT=0`
(all 12 pages `[OK]`, both Phase-2 button actions clean, demo banner shown).**

Notes:
- Auth is **opt-in** (`NETPILOT_AUTH_ENABLED`, default `false`) so the existing
  open dashboard, the demo flow, and the offline test suite are unchanged.
- All new tables are additive and created by the existing `init_db()`
  (CREATE-IF-NOT-EXISTS); no destructive migration. No secret is stored anywhere.

**Next (pending):** live-lab e2e for the change loop on the Docker/Containerlab
host (the L-1…L-6 harness) — every other v2 slice (1–8) is shipped and the
offline suite + UI smoke are green. The stack (`docker compose up`) and CI
pipeline are the operator's to run on a Docker host / GitHub respectively.
