"""ORM models - NetPilot AI Phase 1 + Phase 2 schema.

Tables (extensible, kept intentionally simple):
  devices           - lab inventory with status
  links             - point-to-point links between devices
  metric_samples    - time-series measurements (nullable value!)
  connectivity_tests- ping/traceroute results
  collection_runs   - one row per periodic collection cycle
  incidents         - basic observed failures (no AI analysis in Phase 1)
  anomaly_events    - Phase 2: persisted, deduplicated detection events
  forecast_runs     - Phase 2: model/evaluation metadata (reproducibility)
  forecast_predictions - Phase 2: predicted values, kept apart from samples

Timestamp convention: ALL datetimes are UTC. SQLite stores them as naive
UTC values; on PostgreSQL the columns are timezone-aware. ``config.utc_now()``
is the only place time is taken.

Deliberately NO ``from __future__ import annotations`` in this module: with
that import every ``Mapped[...]`` annotation stays a *string*, and SQLAlchemy
resolves such strings by looking the defining module up in ``sys.modules``.
Hosted runtimes that execute application modules through their own loader
(observed on Streamlit Community Cloud) leave the module unregistered, which
surfaced as ``MappedAnnotationError: Could not interpret annotation
Mapped[int]`` at import time. Real annotation objects remove that dependency.
"""
import datetime as dt

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import (
    Mapped,
    declarative_base,
    mapped_column,
    relationship,
)

Base = declarative_base()

#: Allowed literal values documented for the ``status`` columns.
DEVICE_STATUSES = ("unknown", "up", "down")
LINK_STATUSES = ("unknown", "up", "down")
NETWORK_STATUSES = ("unknown", "healthy", "degraded", "down")


def db_utcnow() -> dt.datetime:
    """Naive UTC timestamp used for storage in both SQLite and PostgreSQL.

    Keeping every column naive-UTC avoids comparison bugs between the two
    backends (SQLite has no timezone support; PostgreSQL columns here are
    ``TIMESTAMP WITHOUT TIME ZONE`` by design).
    """
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    #: 'router' (FRR) or 'host' (linux PC) - extensible for later phases.
    type: Mapped[str] = mapped_column(String(32), default="unknown")
    #: Management IPv4 assigned by containerlab's Docker network (if running).
    mgmt_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Lab-internal loopback (routers) or primary LAN address (hosts).
    lab_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: One of DEVICE_STATUSES, computed by the collector from ping results.
    status: Mapped[str] = mapped_column(String(16), default="unknown")
    #: Container id/name reported by containerlab inspect (live mode only).
    container: Mapped[str | None] = mapped_column(String(128), nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=db_utcnow, onupdate=db_utcnow
    )

    interfaces: Mapped[list["Interface"]] = relationship(
        back_populates="device", cascade="all, delete-orphan"
    )


class Interface(Base):
    """Lab interface belonging to a device (documented in the topology)."""

    __tablename__ = "interfaces"
    __table_args__ = (
        UniqueConstraint("device_id", "name", name="uq_interface_device_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(64))
    ip_cidr: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: 'up'/'down' as last observed; unknown until first collection.
    status: Mapped[str] = mapped_column(String(16), default="unknown")

    device: Mapped[Device] = relationship(back_populates="interfaces")


class Link(Base):
    __tablename__ = "links"
    __table_args__ = (
        UniqueConstraint("device_a", "if_a", "device_b", "if_b", name="uq_link_endpoints"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_a: Mapped[str] = mapped_column(String(128), index=True)
    if_a: Mapped[str] = mapped_column(String(64))
    device_b: Mapped[str] = mapped_column(String(128), index=True)
    if_b: Mapped[str] = mapped_column(String(64))
    subnet: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: One of LINK_STATUSES; 'up' when both endpoints were reachable last run.
    status: Mapped[str] = mapped_column(String(16), default="unknown")
    discovered_at: Mapped[dt.datetime] = mapped_column(DateTime, default=db_utcnow)


class MetricSample(Base):
    """One measurement point. ``value`` is NULL when a metric was not
    available for that device/interface - values are never invented."""

    __tablename__ = "metric_samples"
    __table_args__ = (
        # Index for the common time-series queries (device+metric+time).
        Index("ix_metric_device_metric_time", "device_name", "metric", "collected_at"),
        Index("ix_metric_time", "collected_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_name: Mapped[str] = mapped_column(String(128), index=True)
    interface_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Dotted metric name, e.g. 'ping.avg_rtt_ms', 'if.rx_bytes'.
    metric: Mapped[str] = mapped_column(String(128), index=True)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: Provenance label shown in the UI: 'live' or 'demo'.
    data_source: Mapped[str] = mapped_column(String(16), default="live", index=True)
    collected_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=db_utcnow, index=True
    )


class ConnectivityTest(Base):
    __tablename__ = "connectivity_tests"
    __table_args__ = (Index("ix_conntest_time", "tested_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(128))
    destination: Mapped[str] = mapped_column(String(128), index=True)
    #: 'ping' or 'traceroute'.
    test_type: Mapped[str] = mapped_column(String(32), index=True)
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    avg_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    packet_loss_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: JSON-encoded traceroute path (list of hops) or NULL.
    path_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    data_source: Mapped[str] = mapped_column(String(16), default="live", index=True)
    tested_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=db_utcnow, index=True
    )


class CollectionRun(Base):
    """Audit row for every collection cycle - used by Overview/System Status."""

    __tablename__ = "collection_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    #: 'ok', 'partial' (some metrics unavailable) or 'error'.
    status: Mapped[str] = mapped_column(String(16), default="ok")
    devices_total: Mapped[int] = mapped_column(Integer, default=0)
    devices_up: Mapped[int] = mapped_column(Integer, default=0)
    #: Error summary only - never credentials or full command output.
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    data_source: Mapped[str] = mapped_column(String(16), default="live")


class Incident(Base):
    """Basic failure record (optional in Phase 1, no root-cause analysis)."""

    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    opened_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=db_utcnow, index=True
    )
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    device_name: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    #: Short machine-readable kind, e.g. 'device_unreachable'.
    kind: Mapped[str] = mapped_column(String(64))
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)


# ==========================================================================
# Phase 2 - analytics tables (additive only; Phase 1 tables unchanged).
# Created safely by the existing ``init_db()`` (CREATE TABLE IF NOT EXISTS
# semantics); no destructive migration is ever performed here.
# ==========================================================================
ANOMALY_STATUSES = ("open", "resolved")
ANOMALY_SEVERITIES = ("info", "warning", "critical")


class AnomalyEvent(Base):
    """One persisted detection event (consecutive points grouped by the
    anomaly service - NOT one row per collection cycle)."""

    __tablename__ = "anomaly_events"
    __table_args__ = (
        Index("ix_anomaly_key_status", "device_name", "metric",
              "detector", "data_source", "status"),
        Index("ix_anomaly_first_seen", "first_seen_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_name: Mapped[str] = mapped_column(String(128), index=True)
    interface_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metric: Mapped[str] = mapped_column(String(128), index=True)
    #: Detector identity + configuration snapshot (reproducibility).
    detector: Mapped[str] = mapped_column(String(64))
    detector_version: Mapped[str] = mapped_column(String(32), default="1")
    detector_config_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    data_source: Mapped[str] = mapped_column(String(16), default="live", index=True)

    first_seen_at: Mapped[dt.datetime] = mapped_column(DateTime, default=db_utcnow)
    last_seen_at: Mapped[dt.datetime] = mapped_column(DateTime, default=db_utcnow)
    occurrences: Mapped[int] = mapped_column(Integer, default=1)

    #: Value/baseline/threshold at the strongest observation of the event.
    observed_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    baseline_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    threshold_upper: Mapped[float | None] = mapped_column(Float, nullable=True)
    threshold_lower: Mapped[float | None] = mapped_column(Float, nullable=True)
    deviation: Mapped[float | None] = mapped_column(Float, nullable=True)

    severity: Mapped[str] = mapped_column(String(16), default="warning")
    status: Mapped[str] = mapped_column(String(16), default="open")
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    #: Plain-language "why was this flagged" - shown verbatim in the UI.
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Links back to the source measurements (sample ids), where practical.
    source_samples_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=db_utcnow)


#: Event states - 'insufficient_data' runs persist nothing misleading.
FORECAST_RUN_STATUSES = ("ok", "insufficient_data", "error")


class ForecastRun(Base):
    """One model application to one target series: stores the full
    evaluation protocol so results are reproducible and comparable."""

    __tablename__ = "forecast_runs"
    __table_args__ = (
        Index("ix_forecastrun_target", "device_name", "metric", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=db_utcnow, index=True)
    #: Rows produced in the same multi-model comparison share this id.
    comparison_group: Mapped[str | None] = mapped_column(String(36), index=True)

    device_name: Mapped[str] = mapped_column(String(128), index=True)
    interface_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metric: Mapped[str] = mapped_column(String(128), index=True)
    target_unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    data_source: Mapped[str] = mapped_column(String(16), default="live", index=True)

    model_name: Mapped[str] = mapped_column(String(64))
    model_version: Mapped[str] = mapped_column(String(32), default="1")
    model_config_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    horizon_steps: Mapped[int] = mapped_column(Integer, default=0)
    interval_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: Forecast origin = last observation used (naive-UTC).
    origin_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    n_train: Mapped[int] = mapped_column(Integer, default=0)
    n_val: Mapped[int] = mapped_column(Integer, default=0)
    n_test: Mapped[int] = mapped_column(Integer, default=0)
    train_start: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    train_end: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    val_end: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    test_end: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    #: Hold-out test metrics (NULL when the test segment was too short).
    mae: Mapped[float | None] = mapped_column(Float, nullable=True)
    rmse: Mapped[float | None] = mapped_column(Float, nullable=True)
    r2: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: Scale-free hold-out metrics added by the advanced forecasting pass. They
    #: are NULL whenever the metric is not defensible for the data (MAPE with a
    #: zero actual, MASE on a flat training history), never silently 0.
    mape: Mapped[float | None] = mapped_column(Float, nullable=True)
    smape: Mapped[float | None] = mapped_column(Float, nullable=True)
    mase: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: 1 - MAE / naive-MAE on the same holdout (NULL if not computable).
    skill: Mapped[float | None] = mapped_column(Float, nullable=True)
    n_eval: Mapped[int] = mapped_column(Integer, default=0)
    #: How the published uncertainty bands were produced (empirical half-width
    #: measured on the model's own holdout errors, or the documented normal
    #: approximation) plus the coverage actually achieved on that holdout.
    band_meta_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(24), default="ok")
    message: Mapped[str | None] = mapped_column(Text, nullable=True)


class ForecastPrediction(Base):
    """One predicted future value. NEVER mixed with metric_samples."""

    __tablename__ = "forecast_predictions"
    __table_args__ = (
        UniqueConstraint("run_id", "target_timestamp", name="uq_pred_run_target"),
        Index("ix_pred_target_time", "target_timestamp"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("forecast_runs.id", ondelete="CASCADE"), index=True)
    target_timestamp: Mapped[dt.datetime] = mapped_column(DateTime)
    step: Mapped[int] = mapped_column(Integer, default=1)
    predicted_value: Mapped[float] = mapped_column(Float)
    lower_bound: Mapped[float | None] = mapped_column(Float, nullable=True)
    upper_bound: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: Backfilled only after the target timestamp has an actual observation.
    actual_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    abs_error: Mapped[float | None] = mapped_column(Float, nullable=True)
    backfilled_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


# ==========================================================================
# Phase 3 - AI assistant run history & audit trail (additive only; no
# Phase 1/2 table is touched). Created by the existing ``init_db()``.
#
# These tables store references and structured summaries, NOT secrets and
# NOT duplicated large datasets. Large evidence bodies stay in the existing
# metric/anomaly/forecast tables and are referenced by id here.
# ==========================================================================
#: Explicit run statuses from the Phase 3 spec section 4.
AI_RUN_STATUSES = (
    "received", "investigating", "diagnosed", "planned",
    "configuration_proposed", "verification_passed",
    "verification_failed", "verification_inconclusive",
    "simulation_not_run", "awaiting_human_review", "completed", "failed_safe",
)
AI_REQUEST_TYPES = ("question", "investigation", "change_request")
#: Digital-twin outcomes - 'not_run' is the honest Phase 3 default.
DT_STATUSES = ("not_run", "passed", "failed", "inconclusive")


class AIRun(Base):
    """One end-to-end AI workflow run: request, intent, outputs, verdict.

    The whole audit trail for a run lives here plus its ``AIEvidence`` rows
    and ``AIStatusTransition`` history. ``data_source`` ('live' / 'demo')
    preserves provenance so demo evidence can never masquerade as live.
    """

    __tablename__ = "ai_runs"
    __table_args__ = (
        Index("ix_airun_created_source", "created_at", "data_source"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: Public identifier (uuid) referenced by the UI and logs.
    run_uuid: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=db_utcnow, index=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=db_utcnow, onupdate=db_utcnow)

    # ---- intake ----------------------------------------------------------
    request_text: Mapped[str] = mapped_column(Text)
    source_language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    data_source: Mapped[str] = mapped_column(String(16), default="demo", index=True)

    # ---- intent / routing ------------------------------------------------
    request_type: Mapped[str | None] = mapped_column(String(24), nullable=True)
    risk_level: Mapped[str | None] = mapped_column(String(16), nullable=True)
    intent_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ---- workflow outputs ------------------------------------------------
    investigation_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    diagnosis_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    plan_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    config_proposal_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    config_diff: Mapped[str | None] = mapped_column(Text, nullable=True)
    verifier_report_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_answer: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ---- digital twin (honest) ------------------------------------------
    digital_twin_status: Mapped[str] = mapped_column(String(16), default="not_run")
    digital_twin_report_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ---- lifecycle / provenance -----------------------------------------
    status: Mapped[str] = mapped_column(String(32), default="received")
    human_review_outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider: Mapped[str] = mapped_column(String(32), default="groq")
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: Redacted failure summary only - never secrets or raw exceptions with
    #: embedded credentials.
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class AIStatusTransition(Base):
    """Ordered audit of every status change during a run."""

    __tablename__ = "ai_status_transitions"
    __table_args__ = (
        Index("ix_aistatus_run", "run_id", "step_order"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("ai_runs.id", ondelete="CASCADE"), index=True)
    step_order: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32))
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    at: Mapped[dt.datetime] = mapped_column(DateTime, default=db_utcnow)


class AIEvidence(Base):
    """A read-only evidence reference gathered for a run.

    Keeps provenance (tool, source, timestamp, unit) and a link back to the
    originating rows (via ``source_ref_json``) instead of duplicating large
    datasets. ``content`` is truncated/bounded and treated as UNTRUSTED data.
    """

    __tablename__ = "ai_evidence"
    __table_args__ = (
        Index("ix_aievidence_run", "run_id", "evidence_uid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("ai_runs.id", ondelete="CASCADE"), index=True)
    #: Stable per-run reference the diagnosis/plan cite (e.g. 'ev-3').
    evidence_uid: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(32))     # topology|metric|connectivity|anomaly|forecast|incident
    tool_name: Mapped[str] = mapped_column(String(64))
    device_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    data_source: Mapped[str] = mapped_column(String(16), default="demo")
    observed_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: Human-readable, size-bounded summary (UNTRUSTED - never executed).
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Reference to underlying rows (sample/event ids), JSON list.
    source_ref_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    stale: Mapped[bool] = mapped_column(Boolean, default=False)
    missing: Mapped[bool] = mapped_column(Boolean, default=False)


# ==========================================================================
# v2 Slice 1 - alerting rules & events (additive only; no prior table is
# touched). Created by the existing ``init_db()`` (CREATE IF NOT EXISTS).
#
#: A rule is a durable threshold definition evaluated against the latest
#: telemetry; an event is one fired (deduplicated) alert with a lifecycle.
# ==========================================================================
ALERT_CONDITIONS = ("above", "below")
ALERT_STATUSES = ("open", "acknowledged", "resolved")
ALERT_SEVERITIES = ("info", "warning", "critical")


class AlertRule(Base):
    """A standing alert condition evaluated against metric values.

    ``metric`` is required; ``device_name`` / ``interface_name`` narrow the
    scope (NULL = apply to every device/interface). ``condition`` compares the
    latest value to ``threshold`` ('above' uses >, 'below' uses <).
    """

    __tablename__ = "alert_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    metric: Mapped[str] = mapped_column(String(128), index=True)
    device_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    interface_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    condition: Mapped[str] = mapped_column(String(16), default="above")
    threshold: Mapped[float] = mapped_column(Float)
    severity: Mapped[str] = mapped_column(String(16), default="warning")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=db_utcnow, index=True)


class AlertEvent(Base):
    """One fired alert, deduplicated per (rule, series, source) while open.

    ``rule_name``/``condition``/``threshold`` are denormalised so history
    stays readable even if the rule is later edited or disabled.
    """

    __tablename__ = "alert_events"
    __table_args__ = (
        Index("ix_alert_dedup", "rule_id", "device_name", "metric",
              "data_source", "status"),
        Index("ix_alert_last_fired", "last_fired_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("alert_rules.id", ondelete="SET NULL"), nullable=True, index=True)
    rule_name: Mapped[str] = mapped_column(String(128))

    device_name: Mapped[str] = mapped_column(String(128), index=True)
    interface_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metric: Mapped[str] = mapped_column(String(128), index=True)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    condition: Mapped[str] = mapped_column(String(16), default="above")

    severity: Mapped[str] = mapped_column(String(16), default="warning")
    status: Mapped[str] = mapped_column(String(16), default="open")
    data_source: Mapped[str] = mapped_column(String(16), default="live", index=True)

    first_fired_at: Mapped[dt.datetime] = mapped_column(DateTime, default=db_utcnow)
    last_fired_at: Mapped[dt.datetime] = mapped_column(DateTime, default=db_utcnow)
    occurrences: Mapped[int] = mapped_column(Integer, default=1)
    acknowledged_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    #: Human-readable, size-bounded reason (why the rule fired).
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=db_utcnow)


# ==========================================================================
# v2 Slice 2 - users & RBAC (additive). ``password_hash`` stores a salted
# PBKDF2-HMAC-SHA256 digest ONLY - never the plaintext password. Created by
# the existing ``init_db()``.
# ==========================================================================
#: Ordered roles; a higher role inherits every lower role's permissions.
USER_ROLES = ("viewer", "operator", "admin")
ROLE_LEVELS = {"viewer": 0, "operator": 1, "admin": 2}


class User(Base):
    """A dashboard operator account used when ``auth_enabled`` is on."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    #: Format 'pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>'.
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(16), default="viewer")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=db_utcnow, index=True)
    last_login: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


# ==========================================================================
# v2 Slice 7 - gated change loop (additive). A ChangeRequest is an approval-
# bounded record of a proposed device configuration change. This schema makes
# the audit trail explicit; actual application is owned by services.change_service
# and is OFF unless NETPILOT_CHANGE_LOOP_ENABLED and a lab executor are given.
# Created by the existing ``init_db()`` (CREATE IF NOT EXISTS).
# ==========================================================================
#: Approval/execution lifecycle. 'applied' can only follow 'approved'.
CHANGE_STATUSES = (
    "draft", "proposed", "validated", "rejected", "approved",
    "applied", "failed", "rolled_back", "cancelled", "expired",
)
CHANGE_RISK_LEVELS = ("low", "medium", "high")


class ChangeRequest(Base):
    """One proposed configuration change with a full approval/apply audit."""

    __tablename__ = "change_requests"
    __table_args__ = (
        Index("ix_change_status_created", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_uuid: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=db_utcnow, index=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=db_utcnow, onupdate=db_utcnow)

    device_name: Mapped[str] = mapped_column(String(128), index=True)
    data_source: Mapped[str] = mapped_column(String(16), default="live", index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: JSON list of the current/base config lines and the proposed lines.
    base_config_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposed_config_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Config to restore on rollback (the previously running config).
    rollback_config_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    diff: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: JSON validation findings from the deterministic config validator.
    validation_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    risk_level: Mapped[str] = mapped_column(String(16), default="low")
    status: Mapped[str] = mapped_column(String(16), default="draft")

    proposed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: True only when ``proposed_by`` came from the authenticated session.
    #: Open mode records no requester identity rather than inventing one.
    proposed_by_authenticated: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True, default=False)
    approved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approved_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    #: Human-entered rationale for the decision (bounded by the service).
    approval_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    rejected_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rejected_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancelled_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cancelled_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The approval window. The service refuses approval/application after it.
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    expired_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    applied_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    #: Redacted outcome summary only - never credentials or full device output.
    apply_detail: Mapped[str | None] = mapped_column(Text, nullable=True)


class ChangeAuditEvent(Base):
    """Append-only audit trail for one change request.

    Records who did what, whether that actor was authenticated, the status
    transition and the bounded human/system reason. Expiry is recorded with
    ``actor=None`` (a service action, not a fabricated person).
    """

    __tablename__ = "change_audit_events"
    __table_args__ = (
        Index("ix_change_audit_request", "request_id", "at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[int] = mapped_column(
        ForeignKey("change_requests.id", ondelete="CASCADE"), index=True)
    at: Mapped[dt.datetime] = mapped_column(DateTime, default=db_utcnow)
    #: proposed | approved | rejected | cancelled | expired | applied | failed |
    #: rolled_back
    action: Mapped[str] = mapped_column(String(32))
    #: Verified principal only; NULL means no authenticated identity was
    #: available (open mode) or the action was performed by the service.
    actor: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actor_authenticated: Mapped[bool] = mapped_column(Boolean, default=False)
    from_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


# ==========================================================================
# Phase D4 - persistent Digital Twin snapshot-binding registry (additive).
#
# A binding is immutable evidence: it records the snapshot/change identity,
# stable fingerprints and a caller-supplied trusted capture time. The registry
# only inserts and reads. It never updates or deletes a binding, and it never
# substitutes the current clock for a missing capture time. Created by the
# existing ``init_db()`` (CREATE IF NOT EXISTS); no existing table is altered.
# ==========================================================================
#: Version of the persisted snapshot-binding record schema. Readers must fail
#: closed on a version they do not understand instead of guessing.
DT_SNAPSHOT_BINDING_SCHEMA_VERSION = "d4-1"


class DigitalTwinSnapshotBinding(Base):
    """One immutable, persisted Digital Twin snapshot/change binding."""

    __tablename__ = "digital_twin_snapshot_bindings"
    __table_args__ = (
        UniqueConstraint("binding_uuid", name="uq_dt_binding_uuid"),
        Index("ix_dt_binding_topology", "topology_ref", "captured_at"),
        Index("ix_dt_binding_request", "request_uuid"),
        Index("ix_dt_binding_captured", "captured_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: Stable public identifier. Supplied by the caller or derived
    #: deterministically from the immutable binding content + capture time.
    binding_uuid: Mapped[str] = mapped_column(String(64))
    #: Registry schema version (``d4-1``); validated on read.
    schema_version: Mapped[str] = mapped_column(
        String(16), default=DT_SNAPSHOT_BINDING_SCHEMA_VERSION)
    #: D3 binding contract version (``d3-1``).
    binding_version: Mapped[str] = mapped_column(String(16))

    topology_ref: Mapped[str] = mapped_column(String(256))
    topology_source: Mapped[str] = mapped_column(String(16), default="stored")
    topology_fingerprint: Mapped[str] = mapped_column(String(80))
    change_fingerprint: Mapped[str | None] = mapped_column(
        String(80), nullable=True)
    device_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: Provenance only; deliberately not a foreign key. A binding may be
    #: captured before a request exists and must never mutate request state.
    request_uuid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scenario: Mapped[str | None] = mapped_column(String(128), nullable=True)

    #: Caller-supplied trusted capture time (naive UTC, project convention).
    #: Never defaulted to the current clock.
    captured_at: Mapped[dt.datetime] = mapped_column(DateTime)
    #: Optional explicit freshness policy captured with the binding. ``None``
    #: means TTL is disabled; freshness is then explicitly unknown.
    max_age_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    device_count: Mapped[int] = mapped_column(Integer, default=0)
    link_count: Mapped[int] = mapped_column(Integer, default=0)
    #: Record insertion time (audit only; never used for TTL).
    registered_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=db_utcnow)


# ==========================================================================
# Phase 5 - AI Lab experiment history (additive, single table).
#
# One row per experiment: the source identity, the evaluation protocol, the
# numbers and the caveats. Raw samples are deliberately NOT duplicated here -
# lab frames are analysis input only and never enter ``metric_samples`` or
# ``anomaly_events``. Chart points are stored only when they fit the bounded
# budget enforced by ``services.lab_experiment``, and the row says which way
# that went, so a history view can never imply it still has data it dropped.
# Created by the existing ``init_db()`` (CREATE IF NOT EXISTS); no existing
# table is altered.
# ==========================================================================
#: Version of the persisted experiment record. Readers must fail closed on a
#: version they do not understand rather than guess at the payload shape.
LAB_EXPERIMENT_SCHEMA_VERSION = "lab-1"
LAB_TASKS = ("anomaly", "forecast", "classification", "statistics")
LAB_SOURCE_TYPES = ("simulated", "historical", "uploaded", "live")
LAB_STATUSES = ("ok", "refused", "insufficient_data", "no_labels", "error")


class LabExperiment(Base):
    """One AI Lab experiment run and everything needed to judge it."""

    __tablename__ = "lab_experiments"
    __table_args__ = (
        UniqueConstraint("experiment_uuid", name="uq_lab_experiment_uuid"),
        Index("ix_lab_task_created", "task", "created_at"),
        Index("ix_lab_source_created", "source_type", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: Public identifier referenced by the UI, exports and logs.
    experiment_uuid: Mapped[str] = mapped_column(String(36), index=True)
    schema_version: Mapped[str] = mapped_column(
        String(16), default=LAB_EXPERIMENT_SCHEMA_VERSION)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=db_utcnow, index=True)

    # ---- what was run, and how it ended ---------------------------------
    task: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32))
    #: Refusals are stored results: the reason stays readable here.
    message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ---- source identity and provenance ---------------------------------
    #: simulated | historical | uploaded | live (analytics.lab_dataset).
    source_type: Mapped[str] = mapped_column(String(16), index=True)
    source_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: 'live' or 'demo' exactly as elsewhere in the schema: a demo-simulated
    #: experiment is never recorded as live evidence.
    data_source: Mapped[str] = mapped_column(String(16), default="live")
    #: True when the numbers come from synthetic or demo data.
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    #: Content hash of the validated dataset (short, secret-free).
    dataset_fingerprint: Mapped[str | None] = mapped_column(String(64),
                                                            nullable=True)
    #: none | binary | multiclass - drives which scores were defensible.
    label_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # ---- shape of the data ----------------------------------------------
    range_start: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    range_end: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    n_rows: Mapped[int] = mapped_column(Integer, default=0)
    n_series: Mapped[int] = mapped_column(Integer, default=0)
    n_values: Mapped[int] = mapped_column(Integer, default=0)
    #: The features the model actually received, not the ones offered.
    features_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    target: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # ---- protocol -------------------------------------------------------
    split_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    preprocessing_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    config_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    seed: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ---- results --------------------------------------------------------
    metrics_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    table_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    warnings_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Bounded chart payload; NULL when it exceeded the stored budget.
    points_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Says explicitly whether the chart data above is the real payload.
    points_stored: Mapped[bool] = mapped_column(Boolean, default=False)

    runtime_ms: Mapped[int] = mapped_column(Integer, default=0)
    #: Verified principal only; NULL in open mode (no identity is invented).
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
