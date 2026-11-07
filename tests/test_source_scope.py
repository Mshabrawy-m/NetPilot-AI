"""Tests for deterministic source scoping (live vs simulated structural rows).

The structural tables have no ``data_source`` column, so provenance is encoded
by the reserved ``sim:`` name prefix. These tests pin that convention down:
a simulated row must never be treated as live and the reverse.
"""
from __future__ import annotations

from types import SimpleNamespace

from database import repository, source_scope


def _device(name):
    return SimpleNamespace(name=name)


def _link(a, b):
    return SimpleNamespace(device_a=a, device_b=b)


def _incident(device):
    return SimpleNamespace(device_name=device)


def test_simulated_prefix_round_trip():
    assert source_scope.is_simulated_name("sim:R1") is True
    assert source_scope.is_simulated_name("R1") is False
    assert source_scope.is_simulated_name(None) is False
    assert source_scope.tag_simulated("R1") == "sim:R1"
    assert source_scope.tag_simulated("sim:R1") == "sim:R1"
    assert source_scope.strip_simulated("sim:R1") == "R1"
    assert source_scope.strip_simulated("R1") == "R1"


def test_structural_filters_never_mix_sources():
    devices = [_device("R1"), _device("sim:R1"), _device("sim:R2")]
    assert [d.name for d in source_scope.filter_devices(
        devices, source_scope.LIVE)] == ["R1"]
    assert [d.name for d in source_scope.filter_devices(
        devices, source_scope.SIMULATED)] == ["sim:R1", "sim:R2"]

    links = [_link("R1", "R2"), _link("sim:R1", "sim:R2"),
             _link("R1", "sim:R2")]
    live_links = source_scope.filter_links(links, source_scope.LIVE)
    sim_links = source_scope.filter_links(links, source_scope.SIMULATED)
    assert live_links == [links[0]]
    assert sim_links == [links[1]]

    incidents = [_incident("R1"), _incident("sim:R1")]
    assert [i.device_name for i in source_scope.filter_incidents(
        incidents, source_scope.LIVE)] == ["R1"]
    assert [i.device_name for i in source_scope.filter_incidents(
        incidents, source_scope.SIMULATED)] == ["sim:R1"]


def test_demo_filter_is_a_noop_for_structural_rows():
    # Demo mode is isolated by its own store/banner, so structural filtering
    # must not hide demo rows.
    devices = [_device("R1"), _device("sim:R1")]
    assert source_scope.filter_devices(
        devices, source_scope.DEMO) == devices


def test_available_sources_starts_with_live_and_adds_simulated(session):
    assert source_scope.available_sources(session) == [source_scope.LIVE]

    repository.add_metric_sample(
        session, "cloudflare-dns", "ping.avg_rtt_ms", 12.0, "ms",
        data_source="live")
    assert source_scope.available_sources(session) == [source_scope.LIVE]

    repository.add_metric_sample(
        session, "sim:R1", "ping.avg_rtt_ms", 15.0, "ms",
        data_source="simulated")
    assert source_scope.available_sources(session) == [
        source_scope.LIVE, source_scope.SIMULATED]


def test_available_sources_defaults_to_simulated_without_live_rows(session):
    repository.add_metric_sample(
        session, "sim:R1", "ping.avg_rtt_ms", 15.0, "ms",
        data_source="simulated")
    sources = source_scope.available_sources(session)
    assert sources == [source_scope.SIMULATED, source_scope.LIVE]
    assert source_scope.default_source(sources) == source_scope.SIMULATED


def test_has_rows_and_default_source(session):
    assert source_scope.has_rows(session, source_scope.LIVE) is False
    repository.add_metric_sample(
        session, "sim:R1", "ping.avg_rtt_ms", 15.0, "ms",
        data_source="simulated")
    assert source_scope.has_rows(session, source_scope.LIVE) is False
    assert source_scope.has_rows(session, source_scope.SIMULATED) is True

    assert source_scope.default_source(
        [source_scope.LIVE, source_scope.SIMULATED]) == source_scope.LIVE
    assert source_scope.default_source(
        [source_scope.SIMULATED]) == source_scope.SIMULATED
    assert source_scope.default_source(
        [source_scope.LIVE], demo_mode=True) == source_scope.DEMO
