"""Phase D4 persistent Digital Twin snapshot-binding registry tests.

These tests pin the registry contract added in D4:

* a binding is immutable and append-only (no update/delete path);
* an explicit trusted ``captured_at`` is mandatory and is never replaced by
  the current clock;
* identical re-registration is idempotent, conflicting content is refused
  instead of overwritten;
* missing, malformed, unsupported, mismatched, stale and unverifiable records
  fail closed through the D3 status vocabulary;
* freshness stays explicitly unknown unless a positive TTL policy is stored
  with the binding or supplied at validation time;
* registration never mutates change-request state, contacts a device, applies
  configuration or touches the network.

Every test uses the shared isolated temporary SQLite database from
``tests/conftest.py``; no external infrastructure is started.
"""
from __future__ import annotations

import ast
import datetime as dt
import re
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from database import repository
from database.models import (
    DT_SNAPSHOT_BINDING_SCHEMA_VERSION,
    ChangeAuditEvent,
    ChangeRequest,
    DigitalTwinSnapshotBinding,
)
from services import digital_twin as dtwin
from services import digital_twin_registry as registry

ROOT = Path(__file__).resolve().parents[1]

CAPTURED = "2026-10-26T10:00:00+00:00"
NOW = "2026-10-26T10:30:00+00:00"
LATER = "2026-10-26T12:00:00+00:00"
LINES = ["interface eth3", "shutdown"]
_UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
def _devices():
    return [
        {"name": "R1", "type": "router", "status": "up"},
        {"name": "R2", "type": "router", "status": "up"},
        {"name": "R3", "type": "router", "status": "up"},
        {"name": "PC1", "type": "host", "status": "up"},
    ]


def _links():
    return [
        {"device_a": "R1", "if_a": "eth1", "device_b": "R2", "if_b": "eth1",
         "subnet": "10.0.12.0/30", "status": "up"},
        {"device_a": "R2", "if_a": "eth2", "device_b": "R3", "if_b": "eth1",
         "subnet": "10.0.23.0/30", "status": "up"},
        {"device_a": "R2", "if_a": "eth3", "device_b": "PC1", "if_b": "eth0",
         "subnet": "10.0.2.0/24", "status": "up"},
    ]


def _binding(**kwargs):
    params = dict(
        devices=_devices(),
        links=_links(),
        topology_ref="snapshot:test",
        device_name="R2",
        proposed_lines=list(LINES),
    )
    params.update(kwargs)
    return dtwin.capture_snapshot(**params)


def _register(session, binding=None, **kwargs):
    params = dict(
        binding=_binding() if binding is None else binding,
        captured_at=CAPTURED,
        max_age_seconds=3600,
    )
    params.update(kwargs)
    return registry.register_binding(session, **params)


def _validate(session, binding_uuid, **kwargs):
    params = dict(
        devices=_devices(),
        links=_links(),
        topology_ref="snapshot:test",
        device_name="R2",
        proposed_lines=list(LINES),
        now=NOW,
    )
    params.update(kwargs)
    return registry.validate_persisted_binding(
        session, binding_uuid, **params)


def _count(session, model):
    return session.scalar(select(func.count()).select_from(model))


# ---------------------------------------------------------------------------
# Persist / retrieve
# ---------------------------------------------------------------------------
def test_register_persists_an_immutable_binding(session):
    result = _register(session)
    session.commit()

    assert result["created"] is True
    assert result["schema_version"] == DT_SNAPSHOT_BINDING_SCHEMA_VERSION
    assert result["binding_version"] == dtwin.SNAPSHOT_BINDING_VERSION
    assert result["captured_at"] == CAPTURED
    assert result["max_age_seconds"] == 3600
    assert result["database_written"] is True
    assert result["change_request_mutated"] is False
    assert result["device_contacted"] is False
    assert result["configuration_applied"] is False
    assert result["approval_changed"] is False
    assert _UUID_PATTERN.fullmatch(result["binding_uuid"])

    row = repository.get_dt_snapshot_binding(
        session, result["binding_uuid"])
    assert row is not None
    assert row.schema_version == DT_SNAPSHOT_BINDING_SCHEMA_VERSION
    assert row.binding_version == dtwin.SNAPSHOT_BINDING_VERSION
    assert row.topology_ref == "snapshot:test"
    assert row.topology_source == dtwin.TOPOLOGY_STORED
    assert row.topology_fingerprint.startswith("sha256:")
    assert row.change_fingerprint.startswith("sha256:")
    assert row.device_count == 4
    assert row.link_count == 3
    assert row.captured_at == dt.datetime(2026, 10, 26, 10, 0)
    assert row.registered_at is not None

    loaded = registry.load_binding(session, result["binding_uuid"])
    assert loaded is not None
    assert loaded.topology_ref == "snapshot:test"
    assert loaded.topology_fingerprint == row.topology_fingerprint
    assert loaded.change_fingerprint == row.change_fingerprint
    assert loaded.captured_at == CAPTURED
    assert loaded.max_age_seconds == 3600
    assert loaded.binding_version == dtwin.SNAPSHOT_BINDING_VERSION


def test_binding_uuid_is_deterministic_and_content_addressed():
    binding = _binding()

    first = registry.binding_uuid_for(binding, captured_at=CAPTURED)
    second = registry.binding_uuid_for(_binding(), captured_at=CAPTURED)
    assert first == second
    assert _UUID_PATTERN.fullmatch(first)

    different_time = registry.binding_uuid_for(
        _binding(), captured_at="2026-10-26T10:00:01+00:00")
    different_change = registry.binding_uuid_for(
        _binding(proposed_lines=["interface eth3", "no shutdown"]),
        captured_at=CAPTURED)
    different_topology = registry.binding_uuid_for(
        _binding(devices=_devices()[1:]), captured_at=CAPTURED)
    assert len({first, different_time, different_change,
                different_topology}) == 4


def test_captured_at_is_mandatory_and_never_clocked(session):
    with pytest.raises(ValueError, match="captured_at is required"):
        registry.register_binding(
            session, _binding(), captured_at=None, max_age_seconds=3600)
    with pytest.raises(ValueError, match="captured_at is required"):
        registry.binding_uuid_for(_binding(), captured_at=None)

    assert _count(session, DigitalTwinSnapshotBinding) == 0


def test_captured_at_must_agree_with_the_binding(session):
    binding = _binding(captured_at=CAPTURED)

    with pytest.raises(ValueError, match="does not match"):
        registry.register_binding(
            session, binding, captured_at="2026-10-26T10:05:00+00:00")

    assert _count(session, DigitalTwinSnapshotBinding) == 0


def test_ttl_is_disabled_by_default(session):
    result = registry.register_binding(
        session, _binding(), captured_at=CAPTURED)
    session.commit()
    assert result["max_age_seconds"] is None

    report = _validate(session, result["binding_uuid"])

    assert report["status"] == dtwin.INTEGRITY_UNKNOWN_FRESHNESS
    assert report["freshness"]["status"] == dtwin.FRESHNESS_NOT_CONFIGURED
    assert report["recommended_simulation_status"] == dtwin.STATUS_PARTIAL
    assert report["persisted"] is True
    assert report["schema_version"] == DT_SNAPSHOT_BINDING_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Exact / mismatched identity
# ---------------------------------------------------------------------------
def test_exact_binding_match_is_verified(session):
    result = _register(session)
    session.commit()

    report = _validate(session, result["binding_uuid"])

    assert report["status"] == dtwin.INTEGRITY_VERIFIED
    assert report["freshness"]["status"] == dtwin.FRESHNESS_VERIFIED
    assert report["recommended_simulation_status"] is None
    assert report["persisted"] is True
    assert report["persistence_source"] == registry.REGISTRY_SOURCE
    assert report["binding_uuid"] == result["binding_uuid"]
    assert report["expected_topology_fingerprint"] == \
        report["actual_topology_fingerprint"]
    assert report["expected_change_fingerprint"] == \
        report["actual_change_fingerprint"]
    assert report["not_proof_of_safety"] is True
    assert report["read_only"] is True
    assert report["database_written"] is False
    assert report["device_contacted"] is False
    assert report["configuration_applied"] is False
    assert report["change_request_mutated"] is False


def test_reordered_topology_rows_still_match(session):
    result = _register(session)
    session.commit()

    report = _validate(
        session, result["binding_uuid"],
        devices=list(reversed(_devices())),
        links=list(reversed(_links())))

    assert report["status"] == dtwin.INTEGRITY_VERIFIED


def test_topology_mismatch_is_detected(session):
    result = _register(session)
    session.commit()
    changed = _devices()
    changed[0] = dict(changed[0], status="down")

    report = _validate(session, result["binding_uuid"], devices=changed)

    assert report["status"] == dtwin.INTEGRITY_MISMATCH
    assert report["recommended_simulation_status"] == dtwin.STATUS_INSUFFICIENT
    assert any(c["kind"] == "topology_fingerprint_mismatch"
               for c in report["conflicts"])
    assert report["expected_topology_fingerprint"] != \
        report["actual_topology_fingerprint"]


def test_proposal_mismatch_is_detected(session):
    result = _register(session)
    session.commit()

    report = _validate(
        session, result["binding_uuid"],
        proposed_lines=["interface eth3", "no shutdown"])

    assert report["status"] == dtwin.INTEGRITY_MISMATCH
    assert report["recommended_simulation_status"] == dtwin.STATUS_INVALID
    assert any(c["kind"] == "change_fingerprint_mismatch"
               for c in report["conflicts"])


# ---------------------------------------------------------------------------
# Missing / malformed / unsupported records
# ---------------------------------------------------------------------------
def test_missing_record_is_insufficient_evidence(session):
    report = registry.validate_persisted_binding(
        session, "does-not-exist")

    assert report["status"] == dtwin.INTEGRITY_INSUFFICIENT
    assert report["recommended_simulation_status"] == dtwin.STATUS_INSUFFICIENT
    assert any(c["kind"] == "persisted_binding_missing"
               for c in report["conflicts"])
    assert report["persisted"] is False
    assert report["persisted_binding"] is None
    assert report["database_written"] is False


@pytest.mark.parametrize("attribute,value", [
    ("topology_ref", ""),
    ("topology_fingerprint", "md5:not-a-sha256"),
    ("change_fingerprint", "sha256:short"),
    ("topology_source", ""),
    ("max_age_seconds", 0),
    ("max_age_seconds", -1),
    ("device_count", -1),
    ("link_count", -1),
])
def test_malformed_persisted_row_is_invalid(session, attribute, value):
    result = _register(session)
    session.commit()
    row = repository.get_dt_snapshot_binding(
        session, result["binding_uuid"])
    setattr(row, attribute, value)
    session.commit()

    report = _validate(session, result["binding_uuid"])

    assert report["status"] == dtwin.INTEGRITY_INVALID
    assert report["recommended_simulation_status"] == dtwin.STATUS_INVALID
    assert any(c["kind"] == "persisted_binding_malformed"
               for c in report["conflicts"])
    assert report["persisted"] is True
    assert report["persisted_binding"] is None
    with pytest.raises(registry.SnapshotRegistryMalformedError):
        registry.load_binding(session, result["binding_uuid"])


def test_unsupported_registry_schema_is_invalid(session):
    result = _register(session)
    session.commit()
    row = repository.get_dt_snapshot_binding(
        session, result["binding_uuid"])
    row.schema_version = "d4-0"
    session.commit()

    report = _validate(session, result["binding_uuid"])

    assert report["status"] == dtwin.INTEGRITY_INVALID
    assert report["recommended_simulation_status"] == dtwin.STATUS_INVALID
    assert any(c["kind"] == "persisted_binding_schema_unsupported"
               for c in report["conflicts"])
    assert report["schema_version"] == "d4-0"
    assert report["persisted"] is True
    assert report["persisted_binding"] is None
    with pytest.raises(registry.SnapshotRegistrySchemaError) as excinfo:
        registry.load_binding(session, result["binding_uuid"])
    assert excinfo.value.schema_version == "d4-0"

    listed = registry.list_bindings(session)
    assert len(listed) == 1
    assert listed[0]["problem"]
    assert listed[0]["binding"] is None


def test_unsupported_binding_version_is_invalid(session):
    result = _register(session)
    session.commit()
    row = repository.get_dt_snapshot_binding(
        session, result["binding_uuid"])
    row.binding_version = "d2-1"
    session.commit()

    report = _validate(session, result["binding_uuid"])

    assert report["status"] == dtwin.INTEGRITY_INVALID
    assert any(c["kind"] == "persisted_binding_schema_unsupported"
               for c in report["conflicts"])
    with pytest.raises(registry.SnapshotRegistrySchemaError):
        registry.load_binding(session, result["binding_uuid"])


# ---------------------------------------------------------------------------
# Freshness / TTL
# ---------------------------------------------------------------------------
def test_expired_binding_is_stale(session):
    result = _register(session)
    session.commit()

    report = _validate(session, result["binding_uuid"], now=LATER)

    assert report["status"] == dtwin.INTEGRITY_STALE
    assert report["freshness"]["status"] == dtwin.FRESHNESS_STALE
    assert report["freshness"]["age_seconds"] == 7200
    assert report["recommended_simulation_status"] == dtwin.STATUS_INSUFFICIENT
    assert any(c["kind"] == "stale_topology"
               for c in report["conflicts"])


def test_future_capture_is_invalid(session):
    result = _register(session, captured_at=LATER)
    session.commit()

    report = _validate(session, result["binding_uuid"], now=NOW)

    assert report["status"] == dtwin.INTEGRITY_INVALID
    assert report["recommended_simulation_status"] == dtwin.STATUS_INVALID
    assert any(c["kind"] == "snapshot_capture_in_future"
               for c in report["conflicts"])


def test_explicit_validation_policy_enables_expiry(session):
    result = registry.register_binding(
        session, _binding(), captured_at=CAPTURED)
    session.commit()

    fresh = _validate(
        session, result["binding_uuid"], max_age_seconds=3600)
    stale = _validate(
        session, result["binding_uuid"], now=LATER, max_age_seconds=3600)

    assert fresh["status"] == dtwin.INTEGRITY_VERIFIED
    assert stale["status"] == dtwin.INTEGRITY_STALE


def test_stored_policy_and_explicit_policy_must_agree(session):
    with pytest.raises(ValueError, match="max_age_seconds"):
        registry.register_binding(
            session, _binding(max_age_seconds=3600), captured_at=CAPTURED,
            max_age_seconds=60)

    assert _count(session, DigitalTwinSnapshotBinding) == 0


# ---------------------------------------------------------------------------
# Duplicate identifiers / overwrite protection
# ---------------------------------------------------------------------------
def test_identical_reregistration_is_idempotent(session):
    first = _register(session)
    session.commit()

    for _ in range(3):
        repeated = _register(session)
        session.commit()
        assert repeated["created"] is False
        assert repeated["binding_uuid"] == first["binding_uuid"]
        assert repeated["database_written"] is False

    assert _count(session, DigitalTwinSnapshotBinding) == 1


def test_conflicting_content_is_never_overwritten(session):
    first = _register(session)
    session.commit()
    row = repository.get_dt_snapshot_binding(
        session, first["binding_uuid"])
    original_fingerprint = row.topology_fingerprint
    original_change = row.change_fingerprint

    changed = _binding(proposed_lines=["interface eth3", "no shutdown"])
    with pytest.raises(registry.SnapshotBindingConflict):
        registry.register_binding(
            session, changed, captured_at=CAPTURED,
            max_age_seconds=3600, binding_uuid=first["binding_uuid"])

    session.expire_all()
    row = repository.get_dt_snapshot_binding(
        session, first["binding_uuid"])
    assert row.topology_fingerprint == original_fingerprint
    assert row.change_fingerprint == original_change
    assert _count(session, DigitalTwinSnapshotBinding) == 1


def test_conflicting_ttl_policy_is_a_conflict_not_a_new_record(session):
    first = _register(session)
    session.commit()

    with pytest.raises(registry.SnapshotBindingConflict):
        registry.register_binding(
            session, _binding(), captured_at=CAPTURED,
            max_age_seconds=60, binding_uuid=first["binding_uuid"])

    row = repository.get_dt_snapshot_binding(
        session, first["binding_uuid"])
    assert row.max_age_seconds == 3600
    assert _count(session, DigitalTwinSnapshotBinding) == 1


def test_same_capture_with_a_different_timestamp_is_a_distinct_record(session):
    first = _register(session)
    session.commit()
    second = _register(
        session, captured_at="2026-10-26T10:00:01+00:00")
    session.commit()

    assert first["binding_uuid"] != second["binding_uuid"]
    assert _count(session, DigitalTwinSnapshotBinding) == 2


# ---------------------------------------------------------------------------
# Persistence / transaction failures
# ---------------------------------------------------------------------------
def test_read_failure_fails_closed(session, monkeypatch):
    def boom(*args, **kwargs):
        raise OperationalError("SELECT", {}, Exception("database is down"))

    monkeypatch.setattr(repository, "get_dt_snapshot_binding", boom)

    with pytest.raises(registry.SnapshotRegistryPersistenceError):
        _register(session)
    with pytest.raises(registry.SnapshotRegistryPersistenceError):
        registry.load_binding(session, "any-id")

    report = registry.validate_persisted_binding(session, "any-id")
    assert report["status"] == dtwin.INTEGRITY_ERROR
    assert report["recommended_simulation_status"] == dtwin.STATUS_ERROR
    assert any(c["kind"] == "persisted_binding_persistence_error"
               for c in report["conflicts"])
    assert report["persisted"] is False
    assert report["persisted_binding"] is None
    assert report["database_written"] is False


def test_write_failure_fails_closed(session, monkeypatch):
    def boom(*args, **kwargs):
        raise OperationalError("INSERT", {}, Exception("database is down"))

    monkeypatch.setattr(repository, "add_dt_snapshot_binding", boom)

    with pytest.raises(registry.SnapshotRegistryPersistenceError):
        _register(session)

    assert _count(session, DigitalTwinSnapshotBinding) == 0


def test_list_failure_fails_closed(session, monkeypatch):
    def boom(*args, **kwargs):
        raise OperationalError("SELECT", {}, Exception("database is down"))

    monkeypatch.setattr(repository, "list_dt_snapshot_bindings", boom)

    with pytest.raises(registry.SnapshotRegistryPersistenceError):
        registry.list_bindings(session)


def test_rollback_leaves_no_binding(session):
    result = _register(session)
    assert registry.load_binding(session, result["binding_uuid"]) is not None

    session.rollback()

    assert registry.load_binding(session, result["binding_uuid"]) is None
    assert _count(session, DigitalTwinSnapshotBinding) == 0


# ---------------------------------------------------------------------------
# Concurrency / repeated access
# ---------------------------------------------------------------------------
def test_duplicate_insert_race_is_idempotent(session, monkeypatch):
    first = _register(session)
    session.commit()

    original_get = repository.get_dt_snapshot_binding
    calls = {"n": 0}

    def miss_once(db_session, identifier):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return original_get(db_session, identifier)

    monkeypatch.setattr(repository, "get_dt_snapshot_binding", miss_once)

    repeated = _register(session)
    session.commit()

    assert calls["n"] == 2
    assert repeated["created"] is False
    assert repeated["binding_uuid"] == first["binding_uuid"]
    assert _count(session, DigitalTwinSnapshotBinding) == 1


def test_duplicate_insert_race_with_different_content_conflicts(
        session, monkeypatch):
    first = _register(session)
    session.commit()

    original_get = repository.get_dt_snapshot_binding
    calls = {"n": 0}

    def miss_once(db_session, identifier):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return original_get(db_session, identifier)

    monkeypatch.setattr(repository, "get_dt_snapshot_binding", miss_once)

    with pytest.raises(registry.SnapshotBindingConflict):
        registry.register_binding(
            session,
            _binding(proposed_lines=["interface eth3", "no shutdown"]),
            captured_at=CAPTURED, max_age_seconds=3600,
            binding_uuid=first["binding_uuid"])

    assert calls["n"] == 2
    assert _count(session, DigitalTwinSnapshotBinding) == 1


# ---------------------------------------------------------------------------
# Restart / reload
# ---------------------------------------------------------------------------
def test_restart_reload_uses_the_same_database(db_url):
    from database.connection import get_session

    writer = get_session()
    try:
        result = _register(writer)
        writer.commit()
        identifier = result["binding_uuid"]
    finally:
        writer.close()

    reader = get_session()
    try:
        loaded = registry.load_binding(reader, identifier)
        assert loaded is not None
        assert loaded.topology_ref == "snapshot:test"

        report = _validate(reader, identifier)
        assert report["status"] == dtwin.INTEGRITY_VERIFIED
        assert _count(reader, DigitalTwinSnapshotBinding) == 1
    finally:
        reader.close()


# ---------------------------------------------------------------------------
# Determinism / no side effects
# ---------------------------------------------------------------------------
def test_repeated_validation_is_deterministic(session):
    result = _register(session)
    session.commit()

    first = _validate(session, result["binding_uuid"])
    second = _validate(session, result["binding_uuid"])

    assert first == second
    assert first["status"] == dtwin.INTEGRITY_VERIFIED


def test_registry_does_not_mutate_change_requests_or_touch_devices(session):
    requests_before = _count(session, ChangeRequest)
    audit_before = _count(session, ChangeAuditEvent)

    result = _register(session)
    session.commit()
    report = _validate(session, result["binding_uuid"])

    assert _count(session, ChangeRequest) == requests_before
    assert _count(session, ChangeAuditEvent) == audit_before
    assert result["change_request_mutated"] is False
    assert result["device_contacted"] is False
    assert result["configuration_applied"] is False
    assert result["approval_changed"] is False
    assert report["change_request_mutated"] is False
    assert report["device_contacted"] is False
    assert report["configuration_applied"] is False


def test_registry_module_has_no_network_or_execution_imports():
    source = (ROOT / "services" / "digital_twin_registry.py").read_text(
        encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    forbidden = {
        "socket", "subprocess", "telnetlib", "paramiko", "netmiko",
        "scrapli", "ncclient", "requests", "httpx", "aiohttp", "lab",
        "network", "api",
    }
    assert imported.isdisjoint(forbidden), imported & forbidden
    assert "utc_now" not in source


def test_list_bindings_filters_and_orders(session):
    older = _register(
        session, binding=_binding(topology_ref="snapshot:older"),
        captured_at="2026-10-26T09:00:00+00:00")
    newer = _register(
        session, binding=_binding(topology_ref="snapshot:newer"),
        captured_at="2026-10-26T11:00:00+00:00")
    session.commit()

    everything = registry.list_bindings(session)
    assert [item["binding_uuid"] for item in everything] == [
        newer["binding_uuid"], older["binding_uuid"]]

    filtered = registry.list_bindings(session, topology_ref="snapshot:older")
    assert len(filtered) == 1
    assert filtered[0]["binding_uuid"] == older["binding_uuid"]
    assert filtered[0]["problem"] is None
    assert filtered[0]["binding"] is not None

    assert registry.list_bindings(
        session, request_uuid="no-such-request") == []


def test_registry_schema_constant_matches_the_model():
    assert registry.REGISTRY_SCHEMA_VERSION == \
        DT_SNAPSHOT_BINDING_SCHEMA_VERSION == "d4-1"
    assert registry.REGISTRY_SOURCE == "digital_twin_snapshot_registry"
