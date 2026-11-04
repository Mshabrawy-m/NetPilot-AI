"""Offline validation of the generated deployment artifacts.

These tests read the committed templates under ``deploy/`` and ``docs/`` and
assert the invariants the program requires WITHOUT touching any database,
installing anything, or running a collector:

* the collector cadence is derived from the real ``NETPILOT_MAX_RATE_GAP_SECONDS``
  configuration (interval must stay strictly below the unreliable-gap bound),
  never hand-picked;
* the safety gates stay disabled in every template
  (``NETPILOT_CHANGE_LOOP_ENABLED`` / ``NETPILOT_LAB_EXECUTION_ENABLED`` = false,
  ``NETPILOT_DEMO_MODE`` = false on the live planes);
* TLS is enforced (``NETPILOT_PG_SSLMODE=require``) everywhere;
* least privilege: the collector template carries the collector role DSN only,
  the Cloud template the app-reader DSN only, and **neither** the administrator
  DSN (DDL must never be reachable from a scheduled/runtime plane);
* every connection string is a ``CHANGE_ME`` placeholder and no artifact trips
  the repository's own secret-scan patterns.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tools.secret_scan_check import _ASSIGNED, _GROQ

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "deploy/systemd/netpilot-collector.service"
TIMER = ROOT / "deploy/systemd/netpilot-collector.timer"
COLLECTOR_ENV = ROOT / "deploy/env/netpilot-collector.env.example"
CLOUD_TOML = ROOT / "deploy/env/streamlit-cloud-secrets.example.toml"
CHECKLIST = ROOT / "docs/Neon_Production_Provisioning_Checklist.md"
ENV_EXAMPLE = ROOT / ".env.example"

ALL_ARTIFACTS = [SERVICE, TIMER, COLLECTOR_ENV, CLOUD_TOML, CHECKLIST]


def _assignments(text: str) -> dict[str, str]:
    """Return ``{KEY: value}`` for real (uncommented) assignments only.

    ``KEY=value`` (env) and ``KEY = "value"`` (TOML); a leading ``#`` marks a
    comment line, so commented examples never count as active configuration.
    """
    found: dict[str, str] = {}
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = re.match(r"^\s*([A-Za-z][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", line)
        if m:
            found[m.group(1)] = m.group(2).strip().strip('"')
    return found


@pytest.fixture(scope="module")
def max_rate_gap_seconds() -> int:
    """The real unreliable-gap bound, read from the shipped ``.env.example``."""
    assign = _assignments(ENV_EXAMPLE.read_text(encoding="utf-8"))
    return int(assign["NETPILOT_MAX_RATE_GAP_SECONDS"])


@pytest.mark.parametrize("path", ALL_ARTIFACTS, ids=lambda p: p.name)
def test_artifacts_exist(path: Path):
    assert path.is_file(), f"missing artifact {path}"


def test_timer_interval_is_derived_not_invented(max_rate_gap_seconds: int):
    """OnUnitActiveSec == NETPILOT_COLLECT_INTERVAL and < the max-rate-gap bound."""
    timer = _assignments(TIMER.read_text(encoding="utf-8"))
    interval = int(timer["OnUnitActiveSec"])
    assert interval > 0
    # Grounded in config.py's documented default (60s), not an arbitrary pick.
    env_defaults = _assignments(ENV_EXAMPLE.read_text(encoding="utf-8"))
    assert interval == int(env_defaults["NETPILOT_COLLECT_INTERVAL"])
    # Rate intervals over the bound are marked unreliable -> stay strictly below.
    assert interval < max_rate_gap_seconds


def test_collector_env_respects_gap_and_safety():
    assign = _assignments(COLLECTOR_ENV.read_text(encoding="utf-8"))
    interval = int(assign["NETPILOT_COLLECT_INTERVAL"])
    gap = int(assign["NETPILOT_MAX_RATE_GAP_SECONDS"])
    assert interval < gap
    assert assign["NETPILOT_DEMO_MODE"] == "false"
    assert assign["NETPILOT_CHANGE_LOOP_ENABLED"] == "false"
    assert assign["NETPILOT_LAB_EXECUTION_ENABLED"] == "false"
    assert assign["NETPILOT_PG_SSLMODE"] == "require"
    assert assign["NETPILOT_MANAGE_SCHEMA"] == "false"


def test_collector_env_has_no_admin_dsn():
    """The scheduled plane must never be able to run DDL."""
    assign = _assignments(COLLECTOR_ENV.read_text(encoding="utf-8"))
    assert "NETPILOT_DB_URL_ADMIN" not in assign
    # It carries exactly the collector role DSN.
    assert "NETPILOT_DB_URL_COLLECTOR" in assign


def test_service_mounts_secrets_outside_repo_and_runs_collect():
    service = SERVICE.read_text(encoding="utf-8")
    m = re.search(r"^EnvironmentFile=(\S+)", service, re.MULTILINE)
    assert m, "service must reference an EnvironmentFile"
    envfile = m.group(1)
    assert envfile.startswith("/etc/netpilot/"), \
        "runtime secrets must live outside the repository"
    assert "collector.env" in envfile
    # It runs the one-shot collect cycle through the existing runner.
    assert "tools/run_collector.py collect" in service
    assert re.search(r"^\s*User=netpilot", service, re.MULTILINE)
    assert "NoNewPrivileges=true" in service


def test_cloud_template_is_reader_only_and_inert():
    assign = _assignments(CLOUD_TOML.read_text(encoding="utf-8"))
    assert assign["NETPILOT_DEMO_MODE"] == "false"
    assert assign["NETPILOT_CHANGE_LOOP_ENABLED"] == "false"
    assert assign["NETPILOT_LAB_EXECUTION_ENABLED"] == "false"
    assert assign["NETPILOT_PG_SSLMODE"] == "require"
    assert assign["NETPILOT_MANAGE_SCHEMA"] == "false"
    assert "NETPILOT_DB_URL_APP" in assign
    # Read-only hosted plane: no write/admin/retention/rollup role DSN.
    for forbidden in ("NETPILOT_DB_URL_ADMIN", "NETPILOT_DB_URL_COLLECTOR",
                      "NETPILOT_DB_URL_RETENTION", "NETPILOT_DB_URL_ROLLUP"):
        assert forbidden not in assign


def test_all_dsn_values_are_placeholders():
    """Every connection string is CHANGE_ME - no real host or credential."""
    for path in (COLLECTOR_ENV, CLOUD_TOML):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("#"):
                continue
            if "postgresql+psycopg2://" in line:
                assert "CHANGE_ME" in line, f"{path}: DSN must be a placeholder"


def test_no_artifact_trips_the_repo_secret_patterns():
    """Reuse the CI scanner's own patterns to prove the templates are clean."""
    for path in ALL_ARTIFACTS:
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            assert not _GROQ.search(line), f"{path}:{i}: Groq-key-shaped literal"
            if path.suffix == ".py":  # mirrors the scanner's .py-only rule
                assert not _ASSIGNED.search(line), f"{path}:{i}: assigned secret"


def test_checklist_forbids_disposable_branch_and_live_harness():
    text = CHECKLIST.read_text(encoding="utf-8")
    assert "m5-disposable" in text and "expires" in text.lower()
    assert "Do NOT run `tools/validate_pg_roles_m5.py` against production" in text
    # It must reference the real role/URL surface and the approved SQL order.
    assert "NETPILOT_DB_URL_ADMIN" in text
    assert "20_ownership.sql" in text and "10_create_roles.sql" in text


def test_timer_is_relative_without_persistent():
    """Persistent= is inert on a relative timer; cadence must stay 60s."""
    assign = _assignments(TIMER.read_text(encoding="utf-8"))
    # Comment lines (which mention the word) are excluded by _assignments, so a
    # key here would mean an ACTIVE Persistent= directive - it must be gone.
    assert "Persistent" not in assign
    assert assign["OnUnitActiveSec"] == "60"


def test_service_provides_sandbox_writable_home():
    """HOME/cache must sit under the unit's RuntimeDirectory (strict FS)."""
    service = SERVICE.read_text(encoding="utf-8")
    assert re.search(r"^Environment=HOME=/run/netpilot\s*$", service, re.MULTILINE)
    assert re.search(r"^Environment=XDG_CACHE_HOME=/run/netpilot/",
                     service, re.MULTILINE)
    assert re.search(r"^RuntimeDirectory=netpilot\s*$", service, re.MULTILINE)


def test_collector_env_does_not_redefine_the_service_lock():
    """The lock path is owned by the unit only (no dual-source ambiguity)."""
    assign = _assignments(COLLECTOR_ENV.read_text(encoding="utf-8"))
    assert "NETPILOT_COLLECTOR_LOCK" not in assign
    # ...and it is genuinely set in the service.
    assert "NETPILOT_COLLECTOR_LOCK=/run/netpilot/collector.lock" in \
        SERVICE.read_text(encoding="utf-8")


def test_service_no_longer_references_a_missing_readme():
    service = SERVICE.read_text(encoding="utf-8")
    assert "deploy/README" not in service
    assert "Neon_Production_Provisioning_Checklist.md" in service


def test_checklist_has_host_prep_and_docker_root_equivalence_warning():
    low = CHECKLIST.read_text(encoding="utf-8").lower()
    assert "linux host preparation" in low
    assert "root-equivalent" in low
    assert "dedicated" in low
    assert "co-host" in low


def test_checklist_preserves_no_create_for_runtime_roles():
    text = CHECKLIST.read_text(encoding="utf-8")
    assert "USAGE only" in text and "NO `CREATE`" in text


def test_checklist_provisioning_order_is_correct():
    """HIGH-1: table/sequence grants must be scheduled AFTER init-schema."""
    text = CHECKLIST.read_text(encoding="utf-8")
    ci = text.index("run_collector.py init-schema")
    for pre in ("20_ownership.sql", "21_schema_privileges.sql",
                "40_default_privileges.sql"):
        assert text.index(pre) < ci, f"{pre} must precede init-schema"
    for post in ("30_grant_collector.sql", "31_grant_app.sql",
                 "32_grant_retention_rollup.sql"):
        assert text.index(post) > ci, f"{post} must follow init-schema"
