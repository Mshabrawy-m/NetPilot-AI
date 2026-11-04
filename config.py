"""NetPilot AI - central configuration (Phases 1-2).

All settings are read from environment variables (optionally loaded from a
local ``.env`` file, see ``.env.example``). No credentials are hard-coded;
the default database is a local SQLite file, which needs no secrets.

Timezone convention: every timestamp in NetPilot is UTC.
``utc_now()`` is the single source of time for the whole project.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent

# ``python-dotenv`` is a LOCAL DEVELOPMENT convenience only: it loads a
# git-ignored ``.env`` into the process environment. It is deliberately
# optional so that a hosted deploy which does not install it (Streamlit
# Community Cloud promotes Secrets to env vars before the script runs, so no
# ``.env`` is ever needed there) can never crash at import time. If it is
# absent we simply read configuration from the real environment.
try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - depends on the deployment platform
    load_dotenv = None

if load_dotenv is not None:
    # Load .env from the project root if present (never commit it).
    load_dotenv(PROJECT_ROOT / ".env")


def utc_now() -> datetime:
    """Timezone-aware current UTC time (project-wide timestamp convention)."""
    return datetime.now(timezone.utc)


def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_optional_int(name: str) -> int | None:
    """Optional positive integer; absent/invalid/zero means 'not configured'."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass
class Settings:
    """Runtime settings snapshot (read once per process start)."""

    demo_mode: bool
    database_url: str
    collect_interval_seconds: int
    ping_timeout_seconds: int
    ping_count: int
    traceroute_timeout_seconds: int
    command_output_limit_bytes: int
    lab_name: str
    lab_topology_file: str
    # ---- Phase 2 analytics settings (see NetPilot Phase 2 spec) ----------
    #: Hard cap on rows loaded per analytics query (keeps UI responsive).
    analytics_max_points: int
    #: Readiness thresholds - series shorter than these are reported as
    #: "insufficient data" instead of producing misleading results.
    min_points_analysis: int
    min_points_detection: int
    min_points_forecast: int
    #: Rate intervals computed over larger gaps are marked unreliable.
    max_rate_gap_seconds: int
    #: Rolling anomaly-detection parameters (documented defaults).
    detector_window: int
    mad_threshold: float
    zscore_threshold: float
    #: Consecutive anomaly points within this window group into one event.
    anomaly_group_window_seconds: int
    #: Default forecast horizon (steps) offered by the UI.
    forecast_horizon_default: int
    # ---- Phase 3 AI assistant settings (see NetPilot Phase 3 spec) --------
    #: Groq API key read ONLY from the environment / git-ignored .env.
    #: Never hard-coded, never logged, never stored in graph state or the DB.
    groq_api_key: str
    #: Configurable Groq model name (free-tier availability can change).
    groq_model: str
    #: Per-call timeout and bounded retry count for transient failures.
    groq_timeout_seconds: int
    groq_max_retries: int
    #: Hard bounds enforced inside tools/graph (defence, not just prompts).
    ai_request_max_chars: int
    ai_evidence_max_rows: int
    ai_evidence_max_bytes: int
    #: Maximum graph steps before the orchestrator stops (bounded loops).
    ai_graph_max_steps: int
    # ---- v2 Slice 2 - authentication & RBAC (opt-in, off by default) ------
    #: When False the dashboard runs open (current Phase 1-3 behaviour, and
    #: the test/UI-smoke suite). When True a login is required and actions are
    #: gated by role (viewer < operator < admin).
    auth_enabled: bool = False
    #: PBKDF2-HMAC-SHA256 work factor for password hashing.
    pbkdf2_iterations: int = 240_000
    #: Optional bootstrap admin created on first run when auth is enabled.
    #: Read from env only; never logged or stored in plaintext anywhere.
    auth_bootstrap_admin_user: str = ""
    auth_bootstrap_admin_password: str = ""
    # ---- v2 Slice 6 - REST/WS API (opt-in, read-only) --------------------
    #: When set, mutating/sensitive API routes require header
    #: ``Authorization: Bearer <token>``. Empty = open (read-only) access.
    #: Read from env only; never logged, never committed.
    api_token: str = ""
    #: Comma-separated origins for CORS on the API (empty = no cross-origin).
    api_cors_origins: str = ""
    # ---- v2 Slice 7 - gated change loop (opt-in, OFF by default) ---------
    #: When False the change service can only PROPOSE/validate; it refuses to
    #: apply anything. Even when True, apply additionally requires an explicit
    #: human approval AND an injected executor bound to the virtual lab - a
    #: production device is never reachable from this path.
    change_loop_enabled: bool = False
    #: How long a validated change request may wait for a decision before the
    #: service marks it ``expired`` and refuses approval/application.
    change_request_ttl_minutes: int = 1440
    # ---- Phase 4: isolated-lab execution (opt-in, OFF by default) ---------
    #: When False no lab executor may touch a device at all. This is a SECOND
    #: gate on top of ``change_loop_enabled``: both must be true, on a host
    #: that runs the Containerlab lab, before a single config line is sent
    #: anywhere. Leaving both false keeps the hosted/demo deployment inert.
    lab_execution_enabled: bool = False
    #: Hard timeout (seconds) for one device config operation (apply/verify).
    lab_execution_timeout_seconds: int = 30
    #: Optional freshness policy for Digital Twin snapshot integrity checks.
    #: None/absent means no TTL is evaluated; freshness then stays explicitly
    #: unknown unless the caller supplies both a capture timestamp and a max
    #: age. It never enables approval, execution, or the change loop.
    digital_twin_snapshot_max_age_seconds: int | None = None
    # ---- Phase 2 v2: richer forecast/detector tuning (all optional) --------
    #: Nominal coverage of the empirical forecast bands the service cuts from
    #: holdout errors. It is a *target*, always reported next to the coverage
    #: that was actually measured, never as a guarantee.
    band_nominal_coverage: float = 0.95
    #: Hours that define one seasonal cycle for the seasonal-aware model and
    #: the seasonal-residual detector (24 = a daily rhythm).
    season_hours: float = 24.0
    #: EWMA detector sensitivity: flagged when the deviation exceeds this many
    #: robust scales of the exponentially weighted past.
    ewma_threshold: float = 3.0
    #: CUSUM drift allowance and decision boundary, in standard deviations of
    #: the strictly-past window. Smaller ``k`` / smaller ``h`` = more sensitive.
    cusum_drift_k: float = 0.5
    cusum_threshold_h: float = 4.0
    #: Seasonal-residual detector sensitivity: flagged when a value departs from
    #: the fitted seasonal rhythm by more than this many robust residual scales.
    seasonal_threshold: float = 3.5
    #: Hard cap on how many series one batch scan evaluates (bounded work, the
    #: UI never loops over an unbounded inventory).
    scan_max_series: int = 12
    # ---- Phase 5: AI Lab (data & experimentation) bounds ------------------
    #: Simulator input bounds. The generator refuses a request whose
    #: duration/interval/device count would exceed either cap, instead of
    #: silently producing a smaller (and misleadingly labelled) dataset.
    ai_lab_sim_max_points: int = 20000
    ai_lab_sim_max_devices: int = 12
    #: Per-series evaluation cap for detectors that refit for every judged
    #: point (Isolation Forest). The lab scores only the most recent N samples
    #: of a longer series and says so in the result - a bounded, disclosed
    #: evaluation instead of an unbounded stall or a silent truncation.
    ai_lab_ml_max_points: int = 150
    #: Upload guard: how many bytes of CSV text one browser upload may carry.
    ai_lab_upload_max_bytes: int = 20_000_000
    #: Allowlisted directory for documented historical imports. Empty (the
    #: default) disables local file import entirely - no arbitrary path read.
    ai_lab_local_dir: str = ""
    #: Read-only Prometheus endpoint. Empty (the default) keeps the live
    #: telemetry source disabled and reported as "not configured".
    ai_lab_prometheus_url: str = ""
    ai_lab_prometheus_timeout_seconds: int = 10
    #: Cap on experiment rows listed from the history table in the UI.
    ai_lab_history_limit: int = 50
    # ---- Stage A3: schema-management control (see Stage A3 P2) ------------
    #: When True (the default, preserving local development and Demo Mode) the
    #: app may run ``init_db()``: ``create_all`` + additive column migrations.
    #: When False the app performs NO DDL at all - it only verifies the schema
    #: already exists. This is what lets a hosted Streamlit reader run against an
    #: already-initialized PostgreSQL using a SELECT-only role, without ever
    #: being granted (or silently attempting) CREATE/ALTER.
    manage_schema: bool = True
    # ---- Stage A6: telemetry retention (see tools/run_retention.py) --------
    #: Age in days beyond which raw ``data_source='live'`` metric_samples become
    #: eligible for deletion. 0 or negative disables the retention command (it
    #: refuses to run). NOTE: retention stays operationally OFF until the A6-2
    #: daily-rollup job is approved - this value only bounds *what* a manually
    #: invoked run would delete; it never schedules the deletion.
    retention_raw_days: int = 30
    #: Rows removed per independent transaction. The runner loops in bounded
    #: batches so a long run never holds a write lock across the whole table.
    retention_batch_size: int = 5000
    # ---- Stage A6-2: daily rollups (see tools/run_rollup.py) ---------------
    #: Number of *completed* UTC days to (re)aggregate on each rollup run.
    #: 1 = rebuild yesterday only. A larger window re-derives older days too, so
    #: late-arriving telemetry is picked up. The current, still-incomplete UTC
    #: day is never aggregated. Non-negative; 0 makes the run a no-op.
    rollup_lookback_days: int = 1
    # ---- Stage A6b-1 M1: PostgreSQL TLS / connection settings --------------
    #: libpq ``sslmode`` enforced on PostgreSQL connections. Validated in
    #: ``database.connection`` (never silently ignored). SQLite is unaffected.
    #: Default ``require`` (encryption without full CA verification). It never
    #: downgrades an operator-supplied stronger ``sslmode`` already present in
    #: ``DATABASE_URL``.
    pg_sslmode: str = "require"
    #: Raw ``NETPILOT_PG_CONNECT_TIMEOUT`` value (TCP connection-establishment
    #: seconds). Kept as a string so the engine builder can parse and reject an
    #: invalid value with a clear, credential-safe error instead of defaulting.
    pg_connect_timeout: str = "10"
    #: Optional CA bundle path used with ``verify-ca`` / ``verify-full``. Only
    #: the path is handed to the driver; its contents are never read into the
    #: repository. Empty = not configured (those modes are then rejected).
    pg_sslrootcert: str = ""
    # ---- Stage A6b-1 M2: PostgreSQL serverless pool tuning -----------------
    #: All PostgreSQL-only, read as raw strings and validated/parsed by
    #: ``database.connection`` so a malformed value fails with a clear,
    #: credential-safe error rather than a silent default. SQLite ignores them.
    #: Engine pool: hard ceiling on persistent connections.
    pg_pool_size: str = "3"
    #: Extra connections allowed to spike above ``pg_pool_size`` under load.
    #: ``0`` is valid (no overflow); negative is rejected.
    pg_max_overflow: str = "3"
    #: Recycle a connection older than this many seconds - keep it BELOW the
    #: managed/serverless idle timeout so dead sockets do not accumulate.
    pg_pool_recycle: str = "60"
    #: Seconds to wait for a free pooled connection before giving up.
    pg_pool_timeout: str = "30"
    #: PostgreSQL ``statement_timeout`` (milliseconds) applied per connection
    #: through psycopg2 ``options``. ``0`` disables it (legitimate - long
    #: forecasting/rollup writes must not be cut off); negative/invalid rejected.
    pg_statement_timeout_ms: str = "15000"
    data_source: str = field(init=False)

    def __post_init__(self) -> None:
        self.data_source = "demo" if self.demo_mode else "live"


def is_external_database(url: str) -> bool:
    """True when the configured database is a shared server, not a local file.

    SQLite is a per-machine file already isolated by the separate demo store
    (``data/netpilot_demo.db``), so demo fixtures can never reach another host's
    data through it. Any other backend (PostgreSQL, MySQL, ...) is a networked,
    shared engine: a demo bootstrap pointed at it would purge and overwrite
    real live telemetry. That is exactly the distinction the isolation guard
    needs, and it deliberately allows SQLite test/deploy URLs so the offline
    suite and local runs keep working.
    """
    return bool(url) and not url.startswith("sqlite")


def demo_isolation_violation(settings: "Settings") -> str | None:
    """Return a safe, credential-free reason demo seeding must not proceed.

    ``None`` means demo mode is allowed against the configured database. A
    non-empty message is raised *before* any schema creation or metric purge so
    a misconfigured demo run can never erase live ``metric_samples`` on a shared
    database. The message names only the offending scheme, never the DSN.
    """
    if settings.demo_mode and is_external_database(settings.database_url):
        scheme = settings.database_url.split("://", 1)[0] or "external"
        return (
            "DEMO/LIVE ISOLATION: NETPILOT_DEMO_MODE=true while DATABASE_URL "
            f"points at a shared {scheme!r} database. Demo seeding purges "
            "metric_samples and would destroy real live telemetry. Point "
            "DATABASE_URL at a local SQLite file (or unset it) for demos, or "
            "set NETPILOT_DEMO_MODE=false for live collection."
        )
    return None


def get_settings() -> Settings:
    """Build settings from the environment.

    In demo mode, if the user did not provide an explicit DATABASE_URL, the
    data is kept in a *separate* SQLite file so demo fixtures can never mix
    with live lab measurements.
    """
    demo_mode = _env_bool("NETPILOT_DEMO_MODE", False)
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        db_name = "netpilot_demo.db" if demo_mode else "netpilot.db"
        data_dir = PROJECT_ROOT / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        database_url = f"sqlite:///{(data_dir / db_name).as_posix()}"

    return Settings(
        demo_mode=demo_mode,
        database_url=database_url,
        manage_schema=_env_bool("NETPILOT_MANAGE_SCHEMA", True),
        retention_raw_days=_env_int("NETPILOT_RETENTION_RAW_DAYS", 30),
        retention_batch_size=_env_int("NETPILOT_RETENTION_BATCH_SIZE", 5000),
        rollup_lookback_days=max(0, _env_int("NETPILOT_ROLLUP_LOOKBACK_DAYS", 1)),
        # ---- Stage A6b-1 M1: PostgreSQL TLS / connection settings -----------
        # Read as raw strings; database.connection validates sslmode/timeout and
        # fails with an operator-facing, credential-free message on bad input.
        pg_sslmode=os.getenv("NETPILOT_PG_SSLMODE", "require").strip().lower(),
        pg_connect_timeout=os.getenv("NETPILOT_PG_CONNECT_TIMEOUT", "10").strip(),
        pg_sslrootcert=os.getenv("NETPILOT_PG_SSLROOTCERT", "").strip(),
        # ---- Stage A6b-1 M2: serverless pool tuning + statement timeout ------
        # Raw strings again; database.connection validates each and fails closed
        # on a non-integer / out-of-range value without echoing the DSN.
        pg_pool_size=os.getenv("NETPILOT_PG_POOL_SIZE", "3").strip(),
        pg_max_overflow=os.getenv("NETPILOT_PG_MAX_OVERFLOW", "3").strip(),
        pg_pool_recycle=os.getenv("NETPILOT_PG_POOL_RECYCLE", "60").strip(),
        pg_pool_timeout=os.getenv("NETPILOT_PG_POOL_TIMEOUT", "30").strip(),
        pg_statement_timeout_ms=os.getenv(
            "NETPILOT_PG_STATEMENT_TIMEOUT_MS", "15000").strip(),
        collect_interval_seconds=max(10, _env_int("NETPILOT_COLLECT_INTERVAL", 60)),
        ping_timeout_seconds=max(1, _env_int("NETPILOT_PING_TIMEOUT", 5)),
        ping_count=max(1, _env_int("NETPILOT_PING_COUNT", 3)),
        traceroute_timeout_seconds=max(5, _env_int("NETPILOT_TRACEROUTE_TIMEOUT", 30)),
        command_output_limit_bytes=max(1024, _env_int("NETPILOT_CMD_OUTPUT_LIMIT", 65536)),
        lab_name=os.getenv("NETPILOT_LAB_NAME", "netpilot"),
        lab_topology_file=os.getenv(
            "NETPILOT_LAB_TOPOLOGY", str(PROJECT_ROOT / "lab" / "topology.clab.yml")
        ),
        analytics_max_points=max(100, _env_int("NETPILOT_ANALYTICS_MAX_POINTS", 20000)),
        min_points_analysis=max(2, _env_int("NETPILOT_MIN_POINTS_ANALYSIS", 5)),
        min_points_detection=max(4, _env_int("NETPILOT_MIN_POINTS_DETECTION", 12)),
        min_points_forecast=max(6, _env_int("NETPILOT_MIN_POINTS_FORECAST", 30)),
        max_rate_gap_seconds=max(1, _env_int("NETPILOT_MAX_RATE_GAP_SECONDS", 300)),
        detector_window=max(3, _env_int("NETPILOT_DETECTOR_WINDOW", 20)),
        mad_threshold=max(0.5, _env_float("NETPILOT_MAD_THRESHOLD", 3.5)),
        zscore_threshold=max(0.5, _env_float("NETPILOT_ZSCORE_THRESHOLD", 3.0)),
        anomaly_group_window_seconds=max(
            1, _env_int("NETPILOT_ANOMALY_GROUP_WINDOW_SECONDS", 300)
        ),
        forecast_horizon_default=max(1, _env_int("NETPILOT_FORECAST_HORIZON", 10)),
        # ---- Phase 3 AI assistant ----------------------------------------
        groq_api_key=os.getenv("GROQ_API_KEY", "").strip(),
        groq_model=os.getenv("GROQ_MODEL", "openai/gpt-oss-20b").strip(),
        groq_timeout_seconds=max(1, _env_int("GROQ_TIMEOUT", 30)),
        groq_max_retries=max(0, min(5, _env_int("GROQ_MAX_RETRIES", 2))),
        ai_request_max_chars=max(200, _env_int("NETPILOT_AI_REQUEST_MAX_CHARS", 4000)),
        ai_evidence_max_rows=max(1, _env_int("NETPILOT_AI_EVIDENCE_MAX_ROWS", 50)),
        ai_evidence_max_bytes=max(256, _env_int("NETPILOT_AI_EVIDENCE_MAX_BYTES", 8192)),
        ai_graph_max_steps=max(3, _env_int("NETPILOT_AI_GRAPH_MAX_STEPS", 12)),
        # ---- v2 Slice 2 - auth & RBAC --------------------------------------
        auth_enabled=_env_bool("NETPILOT_AUTH_ENABLED", False),
        pbkdf2_iterations=max(50_000, _env_int("NETPILOT_PBKDF2_ITERATIONS", 240_000)),
        auth_bootstrap_admin_user=os.getenv("NETPILOT_ADMIN_USER", "").strip(),
        auth_bootstrap_admin_password=os.getenv("NETPILOT_ADMIN_PASSWORD", ""),
        # ---- v2 Slice 6 - API --------------------------------------------
        api_token=os.getenv("NETPILOT_API_TOKEN", "").strip(),
        api_cors_origins=os.getenv("NETPILOT_API_CORS_ORIGINS", "").strip(),
        change_loop_enabled=_env_bool("NETPILOT_CHANGE_LOOP_ENABLED", False),
        change_request_ttl_minutes=max(
            1, _env_int("NETPILOT_CHANGE_REQUEST_TTL_MINUTES", 1440)),
        # ---- Phase 4: isolated-lab execution ------------------------------
        lab_execution_enabled=_env_bool("NETPILOT_LAB_EXECUTION_ENABLED", False),
        lab_execution_timeout_seconds=max(
            5, _env_int("NETPILOT_LAB_EXECUTION_TIMEOUT", 30)),
        digital_twin_snapshot_max_age_seconds=_env_optional_int(
            "NETPILOT_DIGITAL_TWIN_MAX_AGE_SECONDS"),
        # ---- Phase 2 v2: forecast bands, seasonal & change-point tuning ------
        band_nominal_coverage=min(0.999, max(
            0.5, _env_float("NETPILOT_BAND_COVERAGE", 0.95))),
        season_hours=max(1.0, _env_float("NETPILOT_SEASON_HOURS", 24.0)),
        ewma_threshold=max(0.5, _env_float("NETPILOT_EWMA_THRESHOLD", 3.0)),
        cusum_drift_k=max(0.05, _env_float("NETPILOT_CUSUM_DRIFT", 0.5)),
        cusum_threshold_h=max(1.0, _env_float("NETPILOT_CUSUM_THRESHOLD", 4.0)),
        seasonal_threshold=max(
            0.5, _env_float("NETPILOT_SEASONAL_THRESHOLD", 3.5)),
        scan_max_series=max(1, _env_int("NETPILOT_SCAN_MAX_SERIES", 12)),
        # ---- Phase 5: AI Lab bounds (analysis-only, no lab execution) ------
        ai_lab_sim_max_points=max(50, _env_int("NETPILOT_AI_LAB_SIM_MAX_POINTS", 20000)),
        ai_lab_sim_max_devices=max(1, _env_int("NETPILOT_AI_LAB_SIM_MAX_DEVICES", 12)),
        ai_lab_ml_max_points=max(20, _env_int("NETPILOT_AI_LAB_ML_MAX_POINTS", 150)),
        ai_lab_upload_max_bytes=max(
            1024, _env_int("NETPILOT_AI_LAB_UPLOAD_MAX_BYTES", 20_000_000)),
        ai_lab_local_dir=os.getenv("NETPILOT_AI_LAB_LOCAL_DIR", "").strip(),
        ai_lab_prometheus_url=os.getenv("NETPILOT_AI_LAB_PROMETHEUS_URL", "").strip(),
        ai_lab_prometheus_timeout_seconds=max(
            1, _env_int("NETPILOT_AI_LAB_PROMETHEUS_TIMEOUT", 10)),
        ai_lab_history_limit=max(1, _env_int("NETPILOT_AI_LAB_HISTORY_LIMIT", 50)),
    )
