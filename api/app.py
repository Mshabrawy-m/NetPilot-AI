"""NetPilot REST/WS API (Slice 6) - thin, opt-in, read-only HTTP layer.

Runs standalone (``uvicorn api.app:app``); the Streamlit dashboard is never
required to be running and nothing here imports Streamlit. Storage is whatever
``DATABASE_URL`` points at (SQLite by default, PostgreSQL by just setting the
URL) - the same repository the UI uses. The API mutates nothing: it exposes
device/telemetry/alert/topology reads, an NL-query ``/ask`` endpoint, a
``/health`` probe, a ``/metrics`` counter snapshot and a ``/ws/topology``
request/response socket.

Security is opt-in and honest: set ``NETPILOT_API_TOKEN`` and every ``/api/*``
route then requires ``Authorization: Bearer <token>``. With no token the routes
stay open but remain read-only.
"""
from __future__ import annotations

import asyncio

from fastapi import (Depends, FastAPI, Header, HTTPException, Query,
                     WebSocket, WebSocketDisconnect)
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from api import readmodels
from config import get_settings
from database import connection
from services import telemetry


def get_db() -> Session:
    session = connection.get_session()
    try:
        yield session
    finally:
        session.close()


def require_token(authorization: str | None = Header(default=None)) -> bool:
    token = get_settings().api_token
    if not token:
        return True  # auth not configured - read-only open access
    if authorization != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="invalid API token")
    return True


def _topo_snapshot() -> dict:
    """Open a short-lived session and build a JSON topology snapshot (sync),
    run from the WebSocket via a worker thread so the event loop never blocks
    on database I/O."""
    session = connection.get_session()
    try:
        return readmodels.topology(session)
    finally:
        session.close()


settings = get_settings()
app = FastAPI(title="NetPilot AI API", version=readmodels.API_VERSION)

if settings.api_cors_origins:
    from fastapi.middleware.cors import CORSMiddleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.api_cors_origins.split(",")
                       if o.strip()],
        allow_methods=["GET", "POST"], allow_headers=["Authorization",
                                                       "Content-Type"])


@app.middleware("http")
async def _count_requests(request, call_next):
    telemetry.inc("api_requests_total")
    try:
        response = await call_next(request)
        if response.status_code >= 500:
            telemetry.inc("api_server_errors_total")
        return response
    except Exception:
        telemetry.inc("api_server_errors_total")
        raise


# -------------------------------------------------------------------- infra
@app.get("/health")
def health(db: Session = Depends(get_db)) -> dict:
    return readmodels.health(db)


@app.get("/metrics")
def metrics(accept: str = Header(default="")):
    """Observability snapshot. JSON by default; Prometheus text exposition
    when the scraper asks for it (``Accept: text/plain[/...]``). Same
    in-process registry either way (Slice 6 + Slice 8 polish)."""
    if "text/plain" in accept.lower():
        return PlainTextResponse(telemetry.render_prometheus(),
                                 media_type="text/plain; version=0.0.4")
    return telemetry.snapshot()


# --------------------------------------------------------------------- reads
@app.get("/api/devices", dependencies=[Depends(require_token)])
def list_devices(db: Session = Depends(get_db)) -> dict:
    return readmodels.devices(db)


@app.get("/api/devices/{name}", dependencies=[Depends(require_token)])
def device_detail(name: str, data_source: str = Query("live"),
                  db: Session = Depends(get_db)) -> dict:
    return readmodels.device_detail(db, name, data_source)


@app.get("/api/alerts", dependencies=[Depends(require_token)])
def alerts(status: str | None = Query("open"),
           data_source: str | None = Query(None),
           db: Session = Depends(get_db)) -> dict:
    return readmodels.alerts(db, status, data_source)


@app.get("/api/topology", dependencies=[Depends(require_token)])
def topology(db: Session = Depends(get_db)) -> dict:
    return readmodels.topology(db)


@app.get("/api/risk", dependencies=[Depends(require_token)])
def risk(db: Session = Depends(get_db)) -> dict:
    return readmodels.risk(db)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    data_source: str = Field(default="live", pattern="^(live|demo)$")


@app.get("/api/ask", dependencies=[Depends(require_token)])
def ask_q(question: str = Query(min_length=1, max_length=2000),
          data_source: str = Query("live", pattern="^(live|demo)$"),
          db: Session = Depends(get_db)) -> dict:
    return readmodels.ask(db, question, data_source)


@app.post("/api/ask", dependencies=[Depends(require_token)])
def ask_post(body: AskRequest, db: Session = Depends(get_db)) -> dict:
    return readmodels.ask(db, body.question, body.data_source)


# ----------------------------------------------------------------- websocket
@app.websocket("/ws/topology")
async def ws_topology(websocket: WebSocket) -> None:
    """Request/response socket: each client message returns a fresh topology
    snapshot. Deterministic and easy to test; no background timer pushing
    unsolicited data."""
    await websocket.accept()
    telemetry.inc("ws_connections_total")
    try:
        while True:
            await websocket.receive_text()
            payload = await asyncio.to_thread(_topo_snapshot)
            await websocket.send_json(payload)
    except WebSocketDisconnect:
        telemetry.inc("ws_disconnects_total")
