"""Stage A6b-1 M3 - PostgreSQL least-privilege role/GRANT matrix.

This module is the SINGLE SOURCE OF TRUTH for the PostgreSQL role privileges
used by ``database/sql/roles/*.sql``. The SQL files are generated from (and
verified against) the maps below so the committed grants and the offline tests
can never drift. Every entry here is *derived from code*, not assumed: the
comment on each table names the repository function / service that performs
the operation, which is the justification for granting it.

Roles
-----
``netpilot_admin``      - schema owner; runs ``init_db``/migrations (full DDL +
                          DML). Not represented in the DML matrix below because
                          it owns the objects (ownership, not GRANT, confers
                          rights).
``netpilot_collector``  - the standalone one-shot collector runner.
``netpilot_app``        - the hosted Streamlit application (reader + analytics
                          writer). NO DELETE, NO DDL.
``netpilot_retention``  - the retention runner (SELECT/DELETE metric_samples).
``netpilot_rollup``     - the daily-rollup runner.

Design rules encoded here
-------------------------
* A role may only touch the tables its own code path actually reads/writes.
* ``DELETE`` is granted to ``netpilot_retention`` (and admin) ONLY. The app and
  collector get none (the collector uses idempotent upserts, never deletes).
* Any table a role INSERTs into also needs ``USAGE`` on its ``SERIAL`` sequence
  ``<table>_id_seq`` (see :func:`sequence_tables`). Sequences for tables the role
  cannot insert into are deliberately NOT granted.
* Foreign-key enforcement (``interfaces.device_id``, ``*.run_id``,
  ``alert_events.rule_id``, ``change_audit_events.request_id``) is performed by
  PostgreSQL as the *table owner*, so runtime roles need no ``REFERENCES`` grant
  to insert child rows - only the ``SELECT``/``INSERT`` they already hold.
"""
from __future__ import annotations

SCHEMA = "public"
ADMIN_ROLE = "netpilot_admin"
#: Runtime (non-owner) roles, in the order they are granted.
RUNTIME_ROLES: tuple[str, ...] = (
    "netpilot_collector", "netpilot_app", "netpilot_retention",
    "netpilot_rollup",
)

# Privilege ordering used when rendering a GRANT, so output is deterministic.
_PRIV_ORDER = ("SELECT", "INSERT", "UPDATE", "DELETE")


def _ops(*privileges: str) -> tuple[str, ...]:
    """Return the privileges in canonical order (dedup, stable)."""
    return tuple(p for p in _PRIV_ORDER if p in privileges)


# ---------------------------------------------------------------------------
# netpilot_collector - network/metrics_collector.collect_once
#   upsert_device / upsert_interface / upsert_link / set_device_status
#       -> devices, interfaces, links : SELECT + INSERT + UPDATE (merge-style)
#   add_metric_sample                  -> metric_samples : SELECT + INSERT
#   start_/finish_collection_run       -> collection_runs : SELECT + INSERT + UPDATE
#   open_incident / resolve_open_incidents -> incidents : SELECT + INSERT + UPDATE
# The collector reads devices/interfaces to resolve names before sampling, and
# merges existing rows, hence SELECT + UPDATE alongside INSERT. No DELETE.
# ---------------------------------------------------------------------------
COLLECTOR_GRANTS: dict[str, tuple[str, ...]] = {
    "devices": _ops("SELECT", "INSERT", "UPDATE"),
    "interfaces": _ops("SELECT", "INSERT", "UPDATE"),
    "links": _ops("SELECT", "INSERT", "UPDATE"),
    "metric_samples": _ops("SELECT", "INSERT"),
    "collection_runs": _ops("SELECT", "INSERT", "UPDATE"),
    "incidents": _ops("SELECT", "INSERT", "UPDATE"),
}

# ---------------------------------------------------------------------------
# netpilot_app - the Streamlit UI + services (analytics / AI / alerting /
# changes / lab / users). Reads broadly (SELECT on every table it displays),
# writes the analytics + audit tables below. NO DELETE (row removal is the
# admin-only reset / retention concern) and NO DDL.
#   * SELECT-only inventory tables (topology, metrics, runs, incidents, rollups)
#   * forecast:   add_forecast_run (INSERT) ; add_forecast_predictions (INSERT)
#                 backfill_actuals (UPDATE forecast_predictions)
#   * anomaly:    create_anomaly_event (INSERT) ; update_/resolve_anomaly_event
#                 (UPDATE)
#   * AI:         create_ai_run (INSERT) ; update_ai_run / set_human_review
#                 (UPDATE) ; add_status_transition / add_ai_evidence (INSERT)
#   * alerts:     create_alert_rule / set_alert_rule_enabled (INSERT/UPDATE) ;
#                 record_alert_firing / acknowledge / resolve (INSERT/UPDATE)
#   * changes:    create_change_request (INSERT) ; update_change_request
#                 (UPDATE) ; add_change_audit_event (INSERT)
#   * users:      create_user (INSERT) ; set_last_login / set_user_active (UPDATE)
#   * lab / dt / agentic / connectivity: append-only INSERTs
# ---------------------------------------------------------------------------
_SELECT_ONLY_FOR_APP = (
    "devices", "interfaces", "links", "metric_samples", "collection_runs",
    "incidents", "metric_daily_rollups",
)
_INSERT_ONLY_FOR_APP = (
    "forecast_runs", "ai_status_transitions", "ai_evidence",
    "change_audit_events", "digital_twin_snapshot_bindings",
    "lab_experiments", "agentic_analysis_runs", "connectivity_tests",
)
_INSERT_UPDATE_FOR_APP = (
    "forecast_predictions", "anomaly_events", "ai_runs", "alert_rules",
    "alert_events", "change_requests", "users",
)

APP_GRANTS: dict[str, tuple[str, ...]] = {}
for _t in _SELECT_ONLY_FOR_APP:
    APP_GRANTS[_t] = _ops("SELECT")
for _t in _INSERT_ONLY_FOR_APP:
    APP_GRANTS[_t] = _ops("SELECT", "INSERT")
for _t in _INSERT_UPDATE_FOR_APP:
    APP_GRANTS[_t] = _ops("SELECT", "INSERT", "UPDATE")

# ---------------------------------------------------------------------------
# netpilot_retention - tools/run_retention.py -> delete_expired_live_samples.
# The function SELECTs candidate ids then DELETEs expired live rows. It touches
# metric_samples ONLY (never demo history, never any other table). No INSERT,
# so no sequence privilege.
# ---------------------------------------------------------------------------
RETENTION_GRANTS: dict[str, tuple[str, ...]] = {
    "metric_samples": _ops("SELECT", "DELETE"),
}

# ---------------------------------------------------------------------------
# netpilot_rollup - tools/run_rollup.py -> upsert_daily_rollups.
#   SELECT metric_samples          : the aggregation query
#   SELECT/INSERT/UPDATE metric_daily_rollups : the ON CONFLICT DO UPDATE upsert
# Needs USAGE on metric_daily_rollups_id_seq because the upsert INSERTs new
# rows (SERIAL id). Reads back rollups via SELECT.
# ---------------------------------------------------------------------------
ROLLUP_GRANTS: dict[str, tuple[str, ...]] = {
    "metric_samples": _ops("SELECT"),
    "metric_daily_rollups": _ops("SELECT", "INSERT", "UPDATE"),
}

#: Full role -> {table -> privileges} matrix for the four runtime roles.
ROLE_GRANTS: dict[str, dict[str, tuple[str, ...]]] = {
    "netpilot_collector": COLLECTOR_GRANTS,
    "netpilot_app": APP_GRANTS,
    "netpilot_retention": RETENTION_GRANTS,
    "netpilot_rollup": ROLLUP_GRANTS,
}

#: Human-readable code path that justifies each role's grants (used by the
#: runbook and asserted by the tests so the mapping stays documented).
ROLE_CODE_PATHS: dict[str, str] = {
    "netpilot_collector":
        "tools/run_collector.py -> network.metrics_collector.collect_once "
        "(repository.upsert_device/interface/link, set_device_status, "
        "add_metric_sample, start_/finish_collection_run, open_incident, "
        "resolve_open_incidents)",
    "netpilot_app":
        "Streamlit UI + services.forecast_service / anomaly_service / "
        "ai_workflow_service / alert_service / change_service / lab / "
        "auth / connectivity (all analytics + audit INSERT/UPDATE paths)",
    "netpilot_retention":
        "tools/run_retention.py -> repository.delete_expired_live_samples "
        "(metric_samples only)",
    "netpilot_rollup":
        "tools/run_rollup.py -> repository.upsert_daily_rollups",
}


def sequence_name(table: str) -> str:
    """PostgreSQL ``SERIAL`` sequence name for a table's integer PK.

    SQLAlchemy maps ``Integer primary_key autoincrement=True`` to ``SERIAL``,
    whose backing sequence is ``<table>_id_seq`` (an ``IDENTITY`` column uses
    the same name, so this holds for either).
    """
    return f"{SCHEMA}.{table}_id_seq"


def insert_tables(role: str) -> tuple[str, ...]:
    """Tables ``role`` may INSERT into (therefore need sequence USAGE)."""
    return tuple(sorted(t for t, ops in ROLE_GRANTS[role].items()
                        if "INSERT" in ops))


def sequence_tables(role: str) -> tuple[str, ...]:
    """Alias of :func:`insert_tables` named for the sequence-grant context."""
    return insert_tables(role)


def table_grant_statements(role: str) -> tuple[str, ...]:
    """One ``GRANT ... ON TABLE`` per table, ordered for a stable diff."""
    out = []
    for table in sorted(ROLE_GRANTS[role]):
        privileges = ", ".join(ROLE_GRANTS[role][table])
        out.append(f"GRANT {privileges} ON TABLE {SCHEMA}.{table} TO {role};")
    return tuple(out)


def sequence_grant_statements(role: str) -> tuple[str, ...]:
    """One ``GRANT USAGE ON SEQUENCE`` per INSERT target of ``role``."""
    return tuple(
        f"GRANT USAGE ON SEQUENCE {sequence_name(table)} TO {role};"
        for table in sequence_tables(role)
    )


def all_runtime_grant_statements() -> tuple[str, ...]:
    """Every table + sequence GRANT the runtime roles require."""
    statements: list[str] = []
    for role in RUNTIME_ROLES:
        statements.extend(table_grant_statements(role))
    for role in RUNTIME_ROLES:
        statements.extend(sequence_grant_statements(role))
    return tuple(statements)
