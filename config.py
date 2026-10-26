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
    data_source: str = field(init=False)

    def __post_init__(self) -> None:
        self.data_source = "demo" if self.demo_mode else "live"


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
    )
