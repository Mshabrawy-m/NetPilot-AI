"""Offline tests for the Windows-native live collector.

No real ping or real PostgreSQL is required: ping output and subprocess results
are mocked, and persistence uses the suite's temporary SQLite database. The
tests cover the five required cases (success, packet loss, timeout, malformed
output, injection in a target name) plus identity mapping, idempotency, retry
and the live-only write guard.
"""
from __future__ import annotations

import datetime as dt
import subprocess

import pytest
from sqlalchemy.exc import OperationalError

from database import connection, repository, roles
from tools import live_collector

WINDOWS_OK = """Pinging 1.1.1.1 with 32 bytes of data:
Reply from 1.1.1.1: bytes=32 time=12ms TTL=57
Reply from 1.1.1.1: bytes=32 time=14ms TTL=57
Reply from 1.1.1.1: bytes=32 time=10ms TTL=57
Reply from 1.1.1.1: bytes=32 time=12ms TTL=57

Ping statistics for 1.1.1.1:
    Packets: Sent = 4, Received = 4, Lost = 0 (0% loss),
Approximate round trip times in milli-seconds:
    Minimum = 10ms, Maximum = 14ms, Average = 12ms
"""

WINDOWS_LOSS = """Pinging 8.8.8.8 with 32 bytes of data:
Reply from 8.8.8.8: bytes=32 time=9ms TTL=118
Reply from 8.8.8.8: bytes=32 time=11ms TTL=118
Reply from 8.8.8.8: bytes=32 time=10ms TTL=118
Request timed out.

Ping statistics for 8.8.8.8:
    Packets: Sent = 4, Received = 3, Lost = 1 (25% loss),
Approximate round trip times in milli-seconds:
    Minimum = 9ms, Maximum = 11ms, Average = 10ms
"""

WINDOWS_LOCALIZED_LOSS = """Ping-Statistik für 8.8.8.8:
    Pakete: Gesendet = 4, Empfangen = 3, Verloren = 1 (25% Verlust),
    Antwort von 8.8.8.8: Bytes=32 Zeit=15ms TTL=118
    Antwort von 8.8.8.8: Bytes=32 Zeit=17ms TTL=118
    Antwort von 8.8.8.8: Bytes=32 Zeit=16ms TTL=118
"""


@pytest.fixture(autouse=True)
def _isolate_role_environment(monkeypatch):
    """Keep role URLs from the developer shell out of these tests."""
    for variable in roles.ROLE_URL_ENV.values():
        monkeypatch.delenv(variable, raising=False)
    connection.set_database_role(None)
    yield
    connection.set_database_role(None)


class _Stats:
    isup = True


class _Counters:
    bytes_recv = 100
    bytes_sent = 50
    errin = 1
    errout = 2
    dropin = 3
    dropout = 4


class _FakePsutil:
    @staticmethod
    def net_io_counters(pernic=True):
        return {"Ethernet 1": _Counters()}

    @staticmethod
    def net_if_stats():
        return {"Ethernet 1": _Stats()}


def _ok_ping(address, count, timeout):
    return live_collector.PingSample(
        target=address, success=True, packets_sent=count,
        packets_received=count, loss_pct=0.0, avg_rtt_ms=12.0,
        jitter_ms=1.5)


def _failed_ping(address, count, timeout):
    return live_collector.PingSample(
        target=address, success=False, packets_sent=count,
        packets_received=0, loss_pct=100.0,
        error="destination unreachable (100% packet loss)")


def _cycle(fixed_time):
    return live_collector.run_cycle(
        [live_collector.TargetSpec("1.1.1.1", "cloudflare-dns")],
        ping_fn=_ok_ping, interface_fn=lambda: [],
        now_fn=lambda: fixed_time)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def test_parse_windows_success():
    parsed = live_collector.parse_ping_output(WINDOWS_OK, 4)
    assert parsed.loss_pct == 0.0
    assert parsed.sent == 4 and parsed.received == 4
    assert parsed.avg_rtt_ms == 12.0
    assert parsed.rtts == (12.0, 14.0, 10.0, 12.0)


def test_parse_windows_packet_loss():
    parsed = live_collector.parse_ping_output(WINDOWS_LOSS, 4)
    assert parsed.loss_pct == 25.0
    assert parsed.sent == 4 and parsed.received == 3
    assert parsed.avg_rtt_ms == 10.0


def test_parse_localized_windows_falls_back_to_protocol_tokens():
    parsed = live_collector.parse_ping_output(WINDOWS_LOCALIZED_LOSS, 4)
    assert parsed.loss_pct == 25.0
    assert parsed.received == 3
    assert parsed.avg_rtt_ms == pytest.approx(16.0)


def test_parse_localized_total_loss_uses_summary_percentage():
    output = ("Pakete: Gesendet = 4, Empfangen = 0, "
              "Verloren = 4 (100% Verlust),")
    parsed = live_collector.parse_ping_output(output, 4)
    assert parsed.loss_pct == 100.0
    assert parsed.received == 0


@pytest.mark.parametrize("output", ["", "garbage output", "???\nno numbers"])
def test_parse_malformed_output_never_raises(output):
    parsed = live_collector.parse_ping_output(output, 4)
    assert parsed.loss_pct is None
    assert parsed.avg_rtt_ms is None
    assert parsed.rtts == ()


# ---------------------------------------------------------------------------
# Target validation and identity mapping
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", [
    "1.1.1.1; rm -rf /",
    "8.8.8.8 && whoami",
    "$(whoami)",
    "1.1.1.1 | cat",
    "a b",
    "../etc/passwd",
    "1.1.1.1\n2.2.2.2",
    "::1",
])
def test_validate_target_rejects_injection_and_non_ipv4(bad):
    with pytest.raises(ValueError):
        live_collector.validate_target(bad)


@pytest.mark.parametrize("good", ["1.1.1.1", "example.com",
                                  "router-1.lab.local"])
def test_validate_target_accepts_ipv4_and_hostnames(good):
    assert live_collector.validate_target(good) == good


def test_build_target_specs_maps_gateway_and_known_targets():
    specs = live_collector.build_target_specs(
        "gateway,1.1.1.1,8.8.8.8",
        gateway_detect=lambda: "192.168.1.1")
    assert [(s.label, s.address) for s in specs] == [
        ("gateway", "192.168.1.1"),
        ("cloudflare-dns", "1.1.1.1"),
        ("google-dns", "8.8.8.8"),
    ]


def test_build_target_specs_supports_explicit_label_for_real_anomaly():
    specs = live_collector.build_target_specs(
        "demo-target=192.0.2.1", gateway_detect=lambda: None)
    assert specs == [live_collector.TargetSpec("192.0.2.1", "demo-target")]


def test_build_target_specs_rejects_duplicate_labels():
    with pytest.raises(ValueError, match="duplicate target label"):
        live_collector.build_target_specs(
            "same=1.1.1.1,same=8.8.8.8", gateway_detect=lambda: None)


def test_build_target_specs_rejects_injection():
    with pytest.raises(ValueError):
        live_collector.build_target_specs(
            "1.1.1.1; rm -rf /", gateway_detect=lambda: None)


# ---------------------------------------------------------------------------
# Ping execution
# ---------------------------------------------------------------------------
def test_ping_target_success_with_mocked_subprocess(monkeypatch):
    monkeypatch.setattr(live_collector, "_ping_binary", lambda: "ping")
    monkeypatch.setattr(live_collector, "_WINDOWS", True)
    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=WINDOWS_OK.encode("utf-8"), stderr=b"")
    monkeypatch.setattr(live_collector.subprocess, "run",
                        lambda *args, **kwargs: completed)

    result = live_collector.ping_target("1.1.1.1", count=4,
                                        timeout_seconds=1.0)
    assert result.success is True
    assert result.loss_pct == 0.0
    assert result.avg_rtt_ms == 12.0
    assert result.jitter_ms == pytest.approx(1.414, abs=0.01)


def test_ping_target_timeout_falls_back_to_failure_sample(monkeypatch):
    monkeypatch.setattr(live_collector, "_ping_binary", lambda: "ping")
    monkeypatch.setattr(live_collector, "_WINDOWS", True)

    def _timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="ping", timeout=1)

    monkeypatch.setattr(live_collector.subprocess, "run", _timeout)
    result = live_collector.ping_target("1.1.1.1", count=4,
                                        timeout_seconds=1.0)
    assert result.success is False
    assert result.loss_pct == 100.0
    assert result.avg_rtt_ms is None
    assert "timed out" in result.error


def test_ping_target_malformed_output_is_an_honest_failure(monkeypatch):
    monkeypatch.setattr(live_collector, "_ping_binary", lambda: "ping")
    monkeypatch.setattr(live_collector, "_WINDOWS", True)
    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=b"not ping output", stderr=b"")
    monkeypatch.setattr(live_collector.subprocess, "run",
                        lambda *args, **kwargs: completed)

    result = live_collector.ping_target("1.1.1.1")
    assert result.success is False
    assert result.loss_pct is None
    assert result.avg_rtt_ms is None
    assert "could not be parsed" in result.error


def test_ping_target_missing_binary_is_unsupported(monkeypatch):
    monkeypatch.setattr(live_collector, "_ping_binary", lambda: None)
    result = live_collector.ping_target("1.1.1.1")
    assert result.success is False
    assert result.unsupported is True
    assert result.loss_pct is None


def test_ping_target_rejects_injection_before_subprocess(monkeypatch):
    called = {"n": 0}

    def _run(*args, **kwargs):
        called["n"] += 1
        raise AssertionError("subprocess must not run")

    monkeypatch.setattr(live_collector.subprocess, "run", _run)
    result = live_collector.ping_target("1.1.1.1; rm -rf /")
    assert result.success is False
    assert "not allowed" in result.error
    assert called["n"] == 0


# ---------------------------------------------------------------------------
# Cycle assembly and optional interface counters
# ---------------------------------------------------------------------------
def test_run_cycle_maps_real_metrics_to_a_stable_device():
    fixed = dt.datetime(2026, 1, 1, 0, 0, 0, 123456)
    result = live_collector.run_cycle(
        [live_collector.TargetSpec("1.1.1.1", "cloudflare-dns")],
        ping_fn=_ok_ping, interface_fn=lambda: [],
        now_fn=lambda: fixed)

    assert result.status == "ok"
    assert result.collected_at.microsecond == 0
    assert result.devices_up == 1 and result.devices_total == 1
    values = {(s["metric"], s["value"]) for s in result.samples}
    assert values == {
        ("ping.avg_rtt_ms", 12.0),
        ("ping.jitter_ms", 1.5),
        ("ping.loss_pct", 0.0),
    }
    assert all(s["device_name"] == "cloudflare-dns"
               for s in result.samples)
    assert all(s["data_source"] == "live" for s in result.samples)


def test_run_cycle_keeps_unmeasured_values_null():
    result = live_collector.run_cycle(
        [live_collector.TargetSpec("192.0.2.1", "demo-target")],
        ping_fn=_failed_ping, interface_fn=lambda: [],
        now_fn=lambda: dt.datetime(2026, 1, 1))
    assert result.status == "error"
    by_metric = {s["metric"]: s["value"] for s in result.samples}
    assert by_metric["ping.avg_rtt_ms"] is None
    assert by_metric["ping.jitter_ms"] is None
    assert by_metric["ping.loss_pct"] == 100.0


def test_collect_interface_samples_uses_optional_psutil():
    rows = live_collector.collect_interface_samples(_FakePsutil)
    assert len(rows) == 7
    assert {row["metric"] for row in rows} == {
        "if.rx_bytes", "if.tx_bytes", "if.rx_errors", "if.tx_errors",
        "if.rx_dropped", "if.tx_dropped", "if.operstate",
    }
    assert all(row["device_name"] == "collector-host" for row in rows)
    assert all(row["interface_name"] == "Ethernet_1" for row in rows)


def test_collect_interface_samples_is_empty_without_psutil():
    assert live_collector.collect_interface_samples(psutil_module=False) == []


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def test_persist_cycle_is_idempotent(db_url):
    fixed = dt.datetime(2026, 1, 1, 0, 0, 0)
    result = _cycle(fixed)

    first = live_collector.persist_cycle(result)
    second = live_collector.persist_cycle(result)

    assert first.ok is True and first.inserted == 3
    assert second.ok is True and second.inserted == 0 and second.skipped == 3

    session = connection.get_session()
    try:
        rows = repository.get_metric_series(
            session, "cloudflare-dns", "ping.avg_rtt_ms",
            data_source="live")
        assert len(rows) == 1
        assert rows[0].value == 12.0
        assert len(repository.get_recent_runs(session, limit=10)) == 2
    finally:
        session.close()


def test_persist_cycle_records_local_host_and_interface(db_url):
    result = live_collector.run_cycle(
        [live_collector.TargetSpec("1.1.1.1", "cloudflare-dns")],
        ping_fn=_ok_ping,
        interface_fn=lambda: live_collector.collect_interface_samples(
            _FakePsutil),
        now_fn=lambda: dt.datetime(2026, 1, 2))

    outcome = live_collector.persist_cycle(result)
    assert outcome.ok is True
    assert outcome.inserted == 10  # 3 ping metrics + 7 interface metrics

    session = connection.get_session()
    try:
        devices = {device.name: device
                   for device in repository.get_devices(session)}
        assert devices["collector-host"].type == "host"
        assert devices["cloudflare-dns"].type == "target"
        assert devices["cloudflare-dns"].mgmt_ip == "1.1.1.1"
    finally:
        session.close()


def test_persist_cycle_retries_a_transient_database_error(db_url):
    result = _cycle(dt.datetime(2026, 1, 3))
    real_scope = connection.session_scope
    calls = {"n": 0}

    def flaky_scope():
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("stmt", {}, Exception("temporary"))
        return real_scope()

    outcome = live_collector.persist_cycle(
        result, scope_factory=flaky_scope, sleep_fn=lambda _seconds: None)
    assert outcome.ok is True
    assert outcome.inserted == 3
    assert calls["n"] >= 2


def test_persist_cycle_reports_database_unavailable():
    result = _cycle(dt.datetime(2026, 1, 4))

    def always_fail():
        raise OperationalError("stmt", {}, Exception("down"))

    outcome = live_collector.persist_cycle(
        result, scope_factory=always_fail, sleep_fn=lambda _seconds: None,
        max_attempts=2)
    assert outcome.ok is False
    assert outcome.error == "OperationalError"


def test_persist_cycle_refuses_non_live_data_source():
    result = _cycle(dt.datetime(2026, 1, 5))
    with pytest.raises(ValueError, match="data_source='live'"):
        live_collector.persist_cycle(result, data_source="demo")
