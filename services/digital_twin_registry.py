"""Phase D4 - persistent Digital Twin snapshot-binding registry.

This module closes the persistence gap documented in D3 without wiring the
Digital Twin into ``ai/graph.py``. It stores immutable snapshot/change
identities in the existing SQLAlchemy database and re-validates them through
the D3 integrity layer.

Safety properties
-----------------
* A trusted ``captured_at`` is required and caller-supplied; the current clock
  is never substituted for it.
* The registry is append-only: there is no update or delete API. A duplicate
  identifier is accepted only when the immutable content is byte-equivalent;
  conflicting content raises instead of overwriting.
* Missing, malformed, unsupported-schema, mismatched, stale and unverifiable
  records fail closed through the same status vocabulary as D3.
* TTL is disabled unless an explicit positive policy is stored with the
  binding or supplied at validation time. With no policy, freshness stays
  explicitly unknown and the simulation recommendation is ``partial``.
* No network I/O, device contact, approval, execution, or change-request
  mutation is performed here.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from collections.abc import Mapping

from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from database import repository
from database.models import DT_SNAPSHOT_BINDING_SCHEMA_VERSION
from services import digital_twin as dtwin

#: Version of the persisted registry record contract.
REGISTRY_SCHEMA_VERSION = DT_SNAPSHOT_BINDING_SCHEMA_VERSION
#: Provenance label included in validation reports.
REGISTRY_SOURCE = "digital_twin_snapshot_registry"

_FINGERPRINT_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_REF_LENGTH = 256
_MAX_IDENTIFIER_LENGTH = 64


class SnapshotRegistryError(RuntimeError):
    """Base class for registry failures that must not be swallowed silently."""


class SnapshotBindingConflict(SnapshotRegistryError):
    """An existing immutable binding has different content (no overwrite)."""


class SnapshotRegistrySchemaError(SnapshotRegistryError):
    """A persisted/requested schema or binding version is unsupported."""

    def __init__(self, message: str, *, schema_version: str | None = None):
        super().__init__(message)
        self.schema_version = schema_version


class SnapshotRegistryMalformedError(SnapshotRegistryError):
    """A persisted binding row is incomplete or malformed."""


class SnapshotRegistryPersistenceError(SnapshotRegistryError):
    """The database could not be read or written; fail closed."""


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------
def _text(value) -> str:
    return str(value).strip() if value is not None else ""


def _optional_text(value) -> str | None:
    text = _text(value)
    return text or None


def _valid_fingerprint(value) -> bool:
    return isinstance(value, str) and bool(
        _FINGERPRINT_PATTERN.fullmatch(value.strip()))


def _positive_policy(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("max_age_seconds must be a positive integer")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
    else:
        raise ValueError("max_age_seconds must be a positive integer")
    if number <= 0:
        raise ValueError("max_age_seconds must be a positive integer")
    return number


def _non_negative_int(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _naive_utc(value) -> dt.datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return value


def _iso_utc(value) -> str | None:
    naive = _naive_utc(value)
    if naive is None:
        return None
    return naive.replace(tzinfo=dt.timezone.utc).isoformat()


def _as_binding(binding) -> dtwin.SnapshotBinding:
    if isinstance(binding, dtwin.SnapshotBinding):
        return binding
    if isinstance(binding, Mapping):
        return dtwin.SnapshotBinding.from_mapping(binding)
    raise ValueError("snapshot binding must be a SnapshotBinding or a mapping")


def _normalise_captured_at(value) -> dt.datetime:
    if value is None:
        raise ValueError(
            "captured_at is required; the registry never substitutes the "
            "current clock")
    parsed, error = dtwin.parse_utc(value)
    if error:
        raise ValueError(f"captured_at: {error}")
    if parsed is None:
        raise ValueError("captured_at is required")
    return parsed


def _ensure_outer_transaction(session) -> None:
    """Make sure a real DB-level transaction exists before a SAVEPOINT.

    pysqlite (the SQLite driver) defers its ``BEGIN`` until the first DML
    statement. If the registry's ``SAVEPOINT`` is the first statement, the
    matching ``RELEASE SAVEPOINT`` commits the insert at the SQLite level, so
    a caller rollback would no longer remove it. Emitting ``BEGIN`` first keeps
    the insert inside the caller's transaction and preserves the documented
    "caller owns the transaction" contract.

    Other backends either already have a real transaction (the pre-check
    ``SELECT`` starts one) or handle savepoints without this quirk, so the
    helper is a no-op there.
    """
    bind = getattr(session, "bind", None)
    dialect = getattr(bind, "dialect", None)
    if getattr(dialect, "name", None) != "sqlite":
        return
    connection = session.connection()
    dbapi = getattr(connection.connection, "dbapi_connection", None)
    if getattr(dbapi, "in_transaction", None) is False:
        connection.exec_driver_sql("BEGIN")


def _assert_binding_timestamp_consistent(
        binding: dtwin.SnapshotBinding, captured: dt.datetime) -> None:
    if binding.captured_at is None:
        return
    bound, error = dtwin.parse_utc(binding.captured_at)
    if error:
        raise ValueError(f"binding captured_at: {error}")
    if bound != captured:
        raise ValueError(
            "captured_at does not match the binding's captured_at; refusing "
            "to silently change the capture time")


def _effective_policy(binding: dtwin.SnapshotBinding,
                      explicit: int | None) -> int | None:
    explicit_policy = _positive_policy(explicit)
    if binding.max_age_seconds is None:
        return explicit_policy
    bound_policy = _positive_policy(binding.max_age_seconds)
    if explicit_policy is not None and explicit_policy != bound_policy:
        raise ValueError(
            "max_age_seconds does not match the binding's captured policy")
    return bound_policy


def _normalised_fields(binding: dtwin.SnapshotBinding,
                       captured: dt.datetime,
                       policy: int | None) -> dict:
    """Validate and normalise the immutable fields stored by the registry."""
    if binding.binding_version != dtwin.SNAPSHOT_BINDING_VERSION:
        raise SnapshotRegistrySchemaError(
            f"binding_version {binding.binding_version!r} is not supported "
            f"(expected {dtwin.SNAPSHOT_BINDING_VERSION!r})")

    ref = _text(binding.topology_ref)
    if not ref:
        raise ValueError("topology_ref is required")
    if len(ref) > _MAX_REF_LENGTH:
        raise ValueError(
            f"topology_ref is longer than {_MAX_REF_LENGTH} characters")

    fingerprint = _text(binding.topology_fingerprint)
    if not _valid_fingerprint(fingerprint):
        raise ValueError(
            "topology_fingerprint must be a sha256:<64 hex> fingerprint")

    change_fingerprint = (
        _text(binding.change_fingerprint)
        if binding.change_fingerprint is not None else None)
    if change_fingerprint is not None and not _valid_fingerprint(
            change_fingerprint):
        raise ValueError(
            "change_fingerprint must be None or a sha256:<64 hex> fingerprint")

    source = _text(binding.topology_source)
    if not source:
        raise ValueError("topology_source is required")

    return {
        "binding_version": binding.binding_version,
        "topology_ref": ref,
        "topology_source": source,
        "topology_fingerprint": fingerprint,
        "change_fingerprint": change_fingerprint,
        "device_name": _optional_text(binding.device_name),
        "request_uuid": _optional_text(binding.request_uuid),
        "scenario": _optional_text(binding.scenario),
        "captured_at": captured.replace(tzinfo=None),
        "max_age_seconds": policy,
        "device_count": _non_negative_int(
            binding.device_count, "device_count"),
        "link_count": _non_negative_int(binding.link_count, "link_count"),
    }


def _uuid_payload(fields: dict) -> str:
    """Canonical identity payload. Policy is deliberately excluded.

    The capture timestamp is part of the identity, so two captures at different
    times are distinct records. The TTL policy is *not* part of the identity:
    re-registering the same capture with a different policy is a conflict, not
    a second record, which keeps the record immutable.
    """
    payload = {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "binding_version": fields["binding_version"],
        "topology_ref": fields["topology_ref"],
        "topology_source": fields["topology_source"],
        "topology_fingerprint": fields["topology_fingerprint"],
        "change_fingerprint": fields["change_fingerprint"],
        "device_name": fields["device_name"],
        "request_uuid": fields["request_uuid"],
        "scenario": fields["scenario"],
        "captured_at": fields["captured_at"].isoformat(),
        "device_count": fields["device_count"],
        "link_count": fields["link_count"],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def binding_uuid_for(binding, *, captured_at) -> str:
    """Derive a deterministic identifier from the immutable binding content.

    Repeating the same capture with the same timestamp yields the same
    identifier, so registration is idempotent. A different timestamp yields a
    different identifier (a distinct historical capture).
    """
    candidate = _as_binding(binding)
    captured = _normalise_captured_at(captured_at)
    _assert_binding_timestamp_consistent(candidate, captured)
    fields = _normalised_fields(
        candidate, captured, _effective_policy(candidate, None))
    return str(uuid.uuid5(uuid.NAMESPACE_URL, _uuid_payload(fields)))


# ---------------------------------------------------------------------------
# Row reconstruction / comparison
# ---------------------------------------------------------------------------
def _reconstruct_binding(row) -> dtwin.SnapshotBinding:
    if row is None:
        raise SnapshotRegistryMalformedError("snapshot binding row is missing")

    if row.binding_version != dtwin.SNAPSHOT_BINDING_VERSION:
        raise SnapshotRegistrySchemaError(
            f"persisted binding_version {row.binding_version!r} is not "
            f"supported (expected {dtwin.SNAPSHOT_BINDING_VERSION!r})",
            schema_version=row.schema_version)

    ref = _text(row.topology_ref)
    if not ref:
        raise SnapshotRegistryMalformedError(
            "persisted binding has no topology_ref")
    if len(ref) > _MAX_REF_LENGTH:
        raise SnapshotRegistryMalformedError(
            "persisted binding topology_ref is too long")

    fingerprint = _text(row.topology_fingerprint)
    if not _valid_fingerprint(fingerprint):
        raise SnapshotRegistryMalformedError(
            "persisted binding has a malformed topology_fingerprint")

    change_fingerprint = (
        _text(row.change_fingerprint)
        if row.change_fingerprint is not None else None)
    if change_fingerprint is not None and not _valid_fingerprint(
            change_fingerprint):
        raise SnapshotRegistryMalformedError(
            "persisted binding has a malformed change_fingerprint")

    source = _text(row.topology_source)
    if not source:
        raise SnapshotRegistryMalformedError(
            "persisted binding has no topology_source")

    captured = _naive_utc(row.captured_at)
    if captured is None:
        raise SnapshotRegistryMalformedError(
            "persisted binding has no captured_at")

    policy = row.max_age_seconds
    if policy is not None:
        try:
            policy = _positive_policy(policy)
        except ValueError as exc:
            raise SnapshotRegistryMalformedError(
                "persisted binding has an invalid max_age_seconds") from exc

    device_count = row.device_count
    link_count = row.link_count
    for value, name in ((device_count, "device_count"),
                        (link_count, "link_count")):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise SnapshotRegistryMalformedError(
                f"persisted binding has an invalid {name}")

    return dtwin.SnapshotBinding(
        topology_ref=ref,
        topology_fingerprint=fingerprint,
        topology_source=source,
        change_fingerprint=change_fingerprint,
        device_name=row.device_name,
        request_uuid=row.request_uuid,
        scenario=row.scenario,
        captured_at=_iso_utc(captured),
        max_age_seconds=policy,
        device_count=device_count,
        link_count=link_count,
        binding_version=row.binding_version,
    )


def _row_matches_fields(row, fields: dict) -> bool:
    return (
        row.schema_version == REGISTRY_SCHEMA_VERSION
        and row.binding_version == fields["binding_version"]
        and _text(row.topology_ref) == fields["topology_ref"]
        and _text(row.topology_source) == fields["topology_source"]
        and _text(row.topology_fingerprint)
        == fields["topology_fingerprint"]
        and (None if row.change_fingerprint is None
             else _text(row.change_fingerprint))
        == fields["change_fingerprint"]
        and _optional_text(row.device_name) == fields["device_name"]
        and _optional_text(row.request_uuid) == fields["request_uuid"]
        and _optional_text(row.scenario) == fields["scenario"]
        and _naive_utc(row.captured_at) == fields["captured_at"]
        and row.max_age_seconds == fields["max_age_seconds"]
        and row.device_count == fields["device_count"]
        and row.link_count == fields["link_count"]
    )


def _registration_dict(row, *, created: bool) -> dict:
    binding = _reconstruct_binding(row)
    return {
        "binding_uuid": row.binding_uuid,
        "created": created,
        "schema_version": row.schema_version,
        "binding_version": row.binding_version,
        "binding": binding,
        "captured_at": binding.captured_at,
        "max_age_seconds": row.max_age_seconds,
        "registered_at": _iso_utc(row.registered_at),
        "database_written": created,
        "change_request_mutated": False,
        "device_contacted": False,
        "configuration_applied": False,
        "approval_changed": False,
    }


def _resolve_existing(row, fields: dict, identifier: str) -> dict:
    if row.schema_version != REGISTRY_SCHEMA_VERSION:
        raise SnapshotRegistrySchemaError(
            f"persisted binding {identifier!r} uses unsupported schema "
            f"version {row.schema_version!r}",
            schema_version=row.schema_version)
    # Reconstruct first so a malformed existing row is never treated as a
    # successful idempotent insert.
    _reconstruct_binding(row)
    if not _row_matches_fields(row, fields):
        raise SnapshotBindingConflict(
            f"binding {identifier!r} already exists with different immutable "
            "content; the registry never overwrites a binding")
    return _registration_dict(row, created=False)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def register_binding(
    session,
    binding,
    *,
    captured_at,
    max_age_seconds: int | None = None,
    binding_uuid: str | None = None,
) -> dict:
    """Persist one immutable binding, or return the identical existing one.

    ``captured_at`` is mandatory and must agree with the binding's own
    ``captured_at`` when it has one. A duplicate ``binding_uuid`` with different
    content raises ``SnapshotBindingConflict``; nothing is overwritten.

    The caller owns the transaction: this function flushes but never commits.
    A caller rollback therefore leaves no binding behind.
    """
    candidate = _as_binding(binding)
    captured = _normalise_captured_at(captured_at)
    _assert_binding_timestamp_consistent(candidate, captured)
    policy = _effective_policy(candidate, max_age_seconds)
    fields = _normalised_fields(candidate, captured, policy)

    identifier = _text(binding_uuid)
    if identifier and len(identifier) > _MAX_IDENTIFIER_LENGTH:
        raise ValueError(
            f"binding_uuid is longer than {_MAX_IDENTIFIER_LENGTH} characters")
    if not identifier:
        identifier = str(uuid.uuid5(
            uuid.NAMESPACE_URL, _uuid_payload(fields)))

    try:
        existing = repository.get_dt_snapshot_binding(session, identifier)
    except SQLAlchemyError as exc:
        raise SnapshotRegistryPersistenceError(
            "could not read the snapshot-binding registry") from exc

    if existing is not None:
        return _resolve_existing(existing, fields, identifier)

    try:
        # A savepoint isolates the insert attempt: a concurrent winner can
        # raise IntegrityError without poisoning the caller's transaction.
        # The outer transaction is forced first so SQLite cannot turn the
        # savepoint release into an implicit commit.
        _ensure_outer_transaction(session)
        with session.begin_nested():
            row = repository.add_dt_snapshot_binding(
                session,
                binding_uuid=identifier,
                schema_version=REGISTRY_SCHEMA_VERSION,
                binding_version=fields["binding_version"],
                topology_ref=fields["topology_ref"],
                topology_source=fields["topology_source"],
                topology_fingerprint=fields["topology_fingerprint"],
                change_fingerprint=fields["change_fingerprint"],
                device_name=fields["device_name"],
                request_uuid=fields["request_uuid"],
                scenario=fields["scenario"],
                captured_at=fields["captured_at"],
                max_age_seconds=fields["max_age_seconds"],
                device_count=fields["device_count"],
                link_count=fields["link_count"],
            )
    except IntegrityError as exc:
        # Another writer won the race. Re-read and accept only identical
        # content; a different binding is a conflict, never an overwrite.
        try:
            existing = repository.get_dt_snapshot_binding(session, identifier)
        except SQLAlchemyError as read_exc:
            raise SnapshotRegistryPersistenceError(
                "binding insert conflicted and the registry could not be "
                "re-read") from read_exc
        if existing is None:
            raise SnapshotRegistryPersistenceError(
                "binding insert conflicted but no existing row is visible") \
                from exc
        return _resolve_existing(existing, fields, identifier)
    except SQLAlchemyError as exc:
        raise SnapshotRegistryPersistenceError(
            "could not persist the snapshot binding") from exc

    return _registration_dict(row, created=True)


def load_binding(session, binding_uuid: str) -> dtwin.SnapshotBinding | None:
    """Load one persisted binding, or ``None`` when it does not exist.

    Raises ``SnapshotRegistrySchemaError`` for an unsupported schema/binding
    version and ``SnapshotRegistryMalformedError`` for an unusable row. Both are
    fail-closed signals; neither is converted into a usable binding.
    """
    identifier = _text(binding_uuid)
    if not identifier:
        raise ValueError("binding_uuid is required")
    try:
        row = repository.get_dt_snapshot_binding(session, identifier)
    except SQLAlchemyError as exc:
        raise SnapshotRegistryPersistenceError(
            "could not read the snapshot-binding registry") from exc
    if row is None:
        return None
    if row.schema_version != REGISTRY_SCHEMA_VERSION:
        raise SnapshotRegistrySchemaError(
            f"persisted binding {identifier!r} uses unsupported schema "
            f"version {row.schema_version!r}",
            schema_version=row.schema_version)
    return _reconstruct_binding(row)


def _registry_failure(binding_uuid, *, status: str, reason: str,
                      conflict_kind: str,
                      schema_version: str | None = None,
                      persisted: bool = False) -> dict:
    report = dtwin.integrity_failure_report(
        None, status=status, reason=reason, conflict_kind=conflict_kind)
    report["binding_uuid"] = binding_uuid
    report["persisted"] = persisted
    report["persistence_source"] = REGISTRY_SOURCE
    report["schema_version"] = schema_version
    #: Only a successfully reconstructed record can be described here. A
    #: malformed/unsupported row exists but is deliberately not presented as
    #: usable evidence.
    report["persisted_binding"] = None
    report["change_request_mutated"] = False
    return report


def validate_persisted_binding(
    session,
    binding_uuid: str,
    *,
    devices=(),
    links=(),
    topology_ref: str | None = None,
    device_name: str | None = None,
    proposed_lines=None,
    base_lines=None,
    request_uuid: str | None = None,
    scenario: str | None = None,
    now=None,
    max_age_seconds: int | None = None,
) -> dict:
    """Load a persisted binding and re-validate it against current inputs.

    Returns a D3-shaped integrity report. Missing, malformed, unsupported and
    persistence-error records fail closed with explicit conflicts. TTL is
    evaluated only when the stored binding or the caller supplies a positive
    policy; otherwise freshness is explicitly unknown.
    """
    identifier = _text(binding_uuid)
    if not identifier:
        raise ValueError("binding_uuid is required")

    try:
        binding = load_binding(session, identifier)
    except SnapshotRegistrySchemaError as exc:
        return _registry_failure(
            identifier, status=dtwin.INTEGRITY_INVALID, reason=str(exc),
            conflict_kind="persisted_binding_schema_unsupported",
            schema_version=getattr(exc, "schema_version", None),
            persisted=True)
    except SnapshotRegistryMalformedError as exc:
        return _registry_failure(
            identifier, status=dtwin.INTEGRITY_INVALID, reason=str(exc),
            conflict_kind="persisted_binding_malformed", persisted=True)
    except SnapshotRegistryPersistenceError as exc:
        return _registry_failure(
            identifier, status=dtwin.INTEGRITY_ERROR, reason=str(exc),
            conflict_kind="persisted_binding_persistence_error",
            persisted=False)

    if binding is None:
        return _registry_failure(
            identifier, status=dtwin.INTEGRITY_INSUFFICIENT,
            reason=(
                "no persisted snapshot binding exists for this identifier; "
                "the snapshot cannot be verified"),
            conflict_kind="persisted_binding_missing", persisted=False)

    try:
        report = dtwin.validate_snapshot(
            binding,
            devices=devices,
            links=links,
            topology_ref=topology_ref,
            device_name=device_name,
            proposed_lines=proposed_lines,
            base_lines=base_lines,
            request_uuid=request_uuid,
            scenario=scenario,
            now=now,
            max_age_seconds=max_age_seconds,
        )
    except Exception as exc:  # fail closed; never raise into the caller
        return _registry_failure(
            identifier, status=dtwin.INTEGRITY_ERROR,
            reason=(
                "persisted binding validation failed "
                f"({type(exc).__name__})"),
            conflict_kind="persisted_binding_validation_error")

    report = dict(report)
    report["binding_uuid"] = identifier
    report["persisted"] = True
    report["persistence_source"] = REGISTRY_SOURCE
    report["schema_version"] = REGISTRY_SCHEMA_VERSION
    report["persisted_binding"] = {
        "binding_uuid": identifier,
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "captured_at": binding.captured_at,
        "max_age_seconds": binding.max_age_seconds,
    }
    report["change_request_mutated"] = False
    report["database_written"] = False
    report["device_contacted"] = False
    report["configuration_applied"] = False
    return report


def list_bindings(
    session, *, topology_ref: str | None = None,
    request_uuid: str | None = None, limit: int = 100,
) -> list[dict]:
    """List persisted bindings for audit/UI, without raising on bad rows.

    Each item has ``binding_uuid``, ``schema_version``, ``binding`` (or
    ``None``) and ``problem``. A malformed/unsupported row is reported as a
    problem instead of being hidden or guessed at.
    """
    try:
        rows = repository.list_dt_snapshot_bindings(
            session, topology_ref=topology_ref, request_uuid=request_uuid,
            limit=limit)
    except SQLAlchemyError as exc:
        raise SnapshotRegistryPersistenceError(
            "could not list the snapshot-binding registry") from exc

    items = []
    for row in rows:
        problem = None
        binding = None
        try:
            if row.schema_version != REGISTRY_SCHEMA_VERSION:
                raise SnapshotRegistrySchemaError(
                    f"unsupported schema version {row.schema_version!r}",
                    schema_version=row.schema_version)
            binding = _reconstruct_binding(row)
        except (SnapshotRegistrySchemaError,
                SnapshotRegistryMalformedError) as exc:
            problem = str(exc)
        items.append({
            "binding_uuid": row.binding_uuid,
            "schema_version": row.schema_version,
            "binding": binding,
            "problem": problem,
        })
    return items


__all__ = [
    "REGISTRY_SCHEMA_VERSION", "REGISTRY_SOURCE",
    "SnapshotRegistryError", "SnapshotBindingConflict",
    "SnapshotRegistrySchemaError", "SnapshotRegistryMalformedError",
    "SnapshotRegistryPersistenceError",
    "binding_uuid_for", "register_binding", "load_binding",
    "validate_persisted_binding", "list_bindings",
]
