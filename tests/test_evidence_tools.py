"""Read-only evidence tools (spec §7).

Guarantees under test: the tools never fabricate values, never trigger a live
probe, keep 'demo' and 'live' provenance separate, bound their output, and tag
every record so the assistant can cite it.
"""
from __future__ import annotations

import datetime as dt

from ai.tools import (connectivity_tools, evidence_tools, telemetry_tools,
                      topology_tools)
from database import repository


NOW = dt.datetime(2026, 1, 1, 12, 0, 0)
ROWS = dict(max_rows=50, max_bytes=8192)


def _seed(session):
    repository.upsert_device(session, "R1", "router", lab_ip="10.1.1.1",
                             status="up")
    repository.upsert_device(session, "PC1", "host", lab_ip="10.1.1.5",
                             status="up")
    repository.upsert_link(session, "R1", "eth1", "PC1", "eth0",
                           subnet="10.1.1.0/24", status="up")
    # A demo metric and a *different* live-only metric for the same device so
    # each source's latest value is independent (no cross-source collapse).
    repository.add_metric_sample(session, "R1", "ping.avg_rtt_ms", 5.0,
                                 unit="ms", data_source="demo",
                                 collected_at=NOW)
    repository.add_metric_sample(session, "R1", "sys.cpu_pct", 999.0,
                                 unit="%", data_source="live",
                                 collected_at=NOW)
    # Stored connectivity history only (demo + live).
    repository.add_connectivity_test(session, "R1", "PC1", "ping", True,
                                     avg_latency_ms=4.0, data_source="demo",
                                     tested_at=NOW)
    repository.add_connectivity_test(session, "R1", "PC1", "ping", False,
                                     data_source="live", tested_at=NOW)
    session.commit()


def test_known_devices_from_inventory(session):
    _seed(session)
    assert evidence_tools.known_devices(session) == {"R1", "PC1"}


def test_device_and_link_records_are_read_only_summaries(session):
    _seed(session)
    recs = topology_tools.device_status_records(
        session, data_source="demo", now=NOW, **ROWS)
    content = " ".join(r["content"] for r in recs)
    assert "device R1 type=router status=up" in content
    # Every record carries provenance and is not marked missing.
    assert all(r["data_source"] == "demo" and not r["missing"] for r in recs)

    links = topology_tools.link_records(session, data_source="demo", now=NOW,
                                        **ROWS)
    assert links and "link R1:eth1 <-> PC1:eth0" in links[0]["content"]


def test_metrics_never_leak_across_data_sources(session):
    _seed(session)
    demo = telemetry_tools.latest_metrics(session, device_name="R1",
                                          data_source="demo", now=NOW, **ROWS)
    assert all(r["data_source"] == "demo" for r in demo)
    # The demo value (5.0) is present; the live-only value (999.0) is NOT.
    assert any("= 5.0" in r["content"] for r in demo)
    assert not any("999" in r["content"] for r in demo)
    # Symmetric: the live scope shows its own value and never the demo one.
    live = telemetry_tools.latest_metrics(session, device_name="R1",
                                          data_source="live", now=NOW, **ROWS)
    assert any("= 999.0" in r["content"] for r in live)
    assert not any("5.0" in r["content"] for r in live)


def test_latest_metrics_resolve_same_timestamp_tie_per_source(session):
    """F-2 regression: a demo and a live sample for the SAME metric captured at
    the SAME instant must each resolve to that source's own latest value.

    Before the source-scoped fix the shared cross-source ``latest_metric_values``
    collapsed the tie to one row and the other source reported 'no stored
    metrics'. This proves the fix without touching Phase 1/2 behavior.
    """
    repository.upsert_device(session, "R9", "router", status="up")
    repository.add_metric_sample(session, "R9", "ping.avg_rtt_ms", 1.5,
                                 unit="ms", data_source="demo", collected_at=NOW)
    repository.add_metric_sample(session, "R9", "ping.avg_rtt_ms", 8.8,
                                 unit="ms", data_source="live", collected_at=NOW)
    session.commit()

    demo = telemetry_tools.latest_metrics(session, device_name="R9",
                                          data_source="demo", now=NOW, **ROWS)
    live = telemetry_tools.latest_metrics(session, device_name="R9",
                                          data_source="live", now=NOW, **ROWS)
    # Each source surfaces its OWN value and never the other's.
    assert any("= 1.5" in r["content"] for r in demo)
    assert not any("8.8" in r["content"] for r in demo)
    assert any("= 8.8" in r["content"] for r in live)
    assert not any("1.5" in r["content"] for r in live)


def test_shared_latest_metric_values_unchanged_and_new_helper_is_scoped(session):
    """Prove the shared Phase 1/2 query is untouched and the new additive
    helper is the one that is source-scoped."""
    repository.upsert_device(session, "R9", "router", status="up")
    repository.add_metric_sample(session, "R9", "ping.avg_rtt_ms", 1.5,
                                 unit="ms", data_source="demo", collected_at=NOW)
    repository.add_metric_sample(session, "R9", "ping.avg_rtt_ms", 8.8,
                                 unit="ms", data_source="live", collected_at=NOW)
    session.commit()

    # Shared helper keeps its documented cross-source semantics: exactly ONE
    # row for the (metric, device-level) pair regardless of the two sources.
    shared = repository.latest_metric_values(session, "R9")
    assert len([r for r in shared if r.metric == "ping.avg_rtt_ms"]) == 1

    # New helper returns that source's own row for each source.
    d = repository.latest_metric_values_for_source(session, "R9", "demo")
    l = repository.latest_metric_values_for_source(session, "R9", "live")
    assert [r.value for r in d if r.metric == "ping.avg_rtt_ms"] == [1.5]
    assert [r.value for r in l if r.metric == "ping.avg_rtt_ms"] == [8.8]


def test_missing_metric_is_reported_not_invented(session):
    _seed(session)
    # No metrics stored for PC1 at all.
    recs = telemetry_tools.latest_metrics(session, device_name="PC1",
                                          data_source="demo", now=NOW, **ROWS)
    assert len(recs) == 1
    assert recs[0]["missing"] is True
    assert "NOT AVAILABLE" not in recs[0]["content"]  # explicit no-data record
    assert "no stored metrics" in recs[0]["content"]


def test_stale_metric_flagged(session):
    _seed(session)
    old = NOW - dt.timedelta(hours=6)
    repository.add_metric_sample(session, "R2", "ping.avg_rtt_ms", 3.0,
                                 unit="ms", data_source="demo", collected_at=old)
    session.commit()
    recs = telemetry_tools.latest_metrics(session, device_name="R2",
                                          data_source="demo", now=NOW, **ROWS)
    assert recs and recs[0]["stale"] is True


def test_connectivity_reads_only_stored_tests(session):
    _seed(session)
    # A fresh probe would hit the network; this tool must only read history.
    recs = connectivity_tools.recent_tests(session, data_source="demo",
                                           now=NOW, **ROWS)
    assert any(r["data_source"] == "demo" for r in recs)
    # The live-only FAILED test is not surfaced in the demo scope.
    assert not any("FAILED" in r["content"] for r in recs)


def test_anomaly_records_source_filtered(session):
    _seed(session)
    repository.create_anomaly_event(
        session, device_name="R1", metric="ping.avg_rtt_ms",
        detector="zscore", data_source="demo", severity="warning",
        status="open", last_seen_at=NOW, first_seen_at=NOW,
        explanation="high rtt")
    repository.create_anomaly_event(
        session, device_name="R1", metric="ping.avg_rtt_ms",
        detector="zscore", data_source="live", severity="critical",
        status="open", last_seen_at=NOW, first_seen_at=NOW,
        explanation="live only")
    session.commit()
    recs = evidence_tools.anomaly_records(session, data_source="demo",
                                          now=NOW, **ROWS)
    assert len(recs) == 1
    assert recs[0]["data_source"] == "demo"
    assert "high rtt" in recs[0]["content"]


def test_gather_evidence_caps_and_assigns_stable_uids(session):
    _seed(session)
    intent = {"targets": ["R1", "GHOST"]}  # GHOST is not a known device
    recs = evidence_tools.gather_evidence(session, data_source="demo",
                                          intent=intent, now=NOW)
    # Stable, contiguous references the diagnosis can cite.
    uids = [r["uid"] for r in recs]
    assert uids == [f"ev-{i}" for i in range(1, len(recs) + 1)]
    # Bounded by the configured cap.
    assert len(recs) <= 50
    # Unknown target 'GHOST' never produces a telemetry record.
    assert not any(r.get("device_name") == "GHOST" for r in recs)
