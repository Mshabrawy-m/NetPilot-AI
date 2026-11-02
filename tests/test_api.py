"""Tests for the API read-models and the optional FastAPI surface (Slice 6).

The read-model tests need no web framework. The HTTP/WebSocket tests use
``pytest.importorskip`` so a machine without FastAPI simply skips them and the
offline suite stays green.
"""
import datetime as dt

import pytest

from api import readmodels
from database import repository


def _seed(session):
    repository.upsert_device(session, "R1", type_="router", status="up")
    repository.upsert_device(session, "R2", type_="router", status="down")
    repository.upsert_link(session, "R1", "eth0", "R2", "eth0")
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    repository.add_metric_sample(session, "R1", "ping.avg_rtt_ms", 4.0,
                                 unit="ms", data_source="live", collected_at=now)
    session.commit()


# ------------------------------------------------------- pure read-models
def test_readmodel_devices_and_topology(session):
    _seed(session)
    devs = readmodels.devices(session)
    assert devs["count"] == 2
    topo = readmodels.topology(session)
    assert topo["connectivity"]["nodes"] == 2
    assert topo["connectivity"]["is_connected"] is True


def test_readmodel_device_detail_carries_metrics(session):
    _seed(session)
    detail = readmodels.device_detail(session, "R1", "live")
    assert detail["found"] is True
    assert detail["metrics"][0]["metric"] == "ping.avg_rtt_ms"
    assert readmodels.device_detail(session, "nope")["found"] is False


def test_readmodel_ask_is_data_grounded(session):
    _seed(session)
    res = readmodels.ask(session, "how many devices", data_source="live")
    assert res["understood"] and "2 device" in res["answer"]


def test_readmodel_risk_detects_bridge(session):
    _seed(session)
    # A two-node single-link graph makes both endpoints articulation-free but
    # the one edge a bridge.
    risk = readmodels.risk(session)
    assert ["R1", "R2"] in [sorted(pair) for pair in risk["bridge_links"]]


def test_api_filter_parameters_document_supported_values():
    """Read-only filters reject typos instead of returning misleading empties."""
    pytest.importorskip("fastapi")
    from api.app import app

    paths = app.openapi()["paths"]
    device_params = {
        item["name"]: item["schema"]
        for item in paths["/api/devices/{name}"]["get"]["parameters"]
    }
    alert_params = {
        item["name"]: item["schema"]
        for item in paths["/api/alerts"]["get"]["parameters"]
    }
    assert device_params["data_source"]["pattern"] == "^(live|demo)$"
    assert alert_params["status"]["anyOf"][0]["pattern"] == (
        "^(open|acknowledged|resolved)$")
    assert alert_params["data_source"]["anyOf"][0]["pattern"] == (
        "^(live|demo)$")


def test_ask_request_rejects_invalid_boundary_inputs():
    pytest.importorskip("fastapi")
    from pydantic import ValidationError
    from api.app import AskRequest

    invalid_requests = [
        {},
        {"question": ""},
        {"question": "x" * 2001},
        {"question": 42},
        {"question": "status", "data_source": "other"},
    ]
    for payload in invalid_requests:
        with pytest.raises(ValidationError):
            AskRequest.model_validate(payload)


# ------------------------------------------------------ HTTP / WebSocket
@pytest.fixture()
def client(session, db_url):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from api.app import app
    with TestClient(app) as c:
        yield c


def test_health_and_metrics_endpoints(client, session):
    _seed(session)
    h = client.get("/health")
    assert h.status_code == 200 and h.json()["api_version"]
    m = client.get("/metrics")
    assert m.status_code == 200 and "counters" in m.json()


def test_metrics_prometheus_exposition(client, session):
    """Slice 8 polish: the same registry rendered as Prometheus text when
    the scraper asks with Accept: text/plain."""
    _seed(session)
    client.get("/health")  # guarantee at least one counter fired
    r = client.get("/metrics", headers={"Accept": "text/plain"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    body = r.text
    assert "netpilot_api_requests_total" in body
    assert "# TYPE netpilot_uptime_seconds gauge" in body
    # Every value line must follow the '<name> <number>' exposition shape.
    for line in body.splitlines():
        if line and not line.startswith("#"):
            name, _, value = line.partition(" ")
            assert name and float(value) >= 0.0


def test_devices_endpoint(client, session):
    _seed(session)
    r = client.get("/api/devices")
    assert r.status_code == 200
    assert r.json()["count"] == 2


def test_ask_endpoint_post(client, session):
    _seed(session)
    r = client.post("/api/ask", json={"question": "how many devices",
                                      "data_source": "live"})
    assert r.status_code == 200
    assert "2 device" in r.json()["answer"]


def test_token_guard_when_configured(client, session, monkeypatch):
    _seed(session)
    monkeypatch.setenv("NETPILOT_API_TOKEN", "s3cret")
    assert client.get("/api/devices").status_code == 401
    ok = client.get("/api/devices", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200


def test_websocket_topology_snapshot(client, session):
    _seed(session)
    with client.websocket_connect("/ws/topology") as ws:
        ws.send_text("tick")
        payload = ws.receive_json()
    assert payload["connectivity"]["nodes"] == 2


def test_websocket_requires_the_api_token_when_configured(client, session,
                                                          monkeypatch):
    from fastapi import WebSocketDisconnect
    _seed(session)
    monkeypatch.setenv("NETPILOT_API_TOKEN", "s3cret")
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/topology?token=wrong") as ws:
            ws.send_text("tick")
            ws.receive_json()
    with client.websocket_connect("/ws/topology?token=s3cret") as ws:
        ws.send_text("tick")
        assert ws.receive_json()["connectivity"]["nodes"] == 2
