"""Optional read-only Prometheus source for the AI Lab (Phase 5).

Live telemetry can be pulled into an experiment **only** when the operator
configures an endpoint (``NETPILOT_AI_LAB_PROMETHEUS_URL``). Until then this
module reports ``configured=False`` and every page shows the live source as
disabled - the app never pretends a live scrape happened.

What it does and does not do:

* one HTTP ``GET`` against ``/api/v1/query_range`` and, for an instant check,
  ``/api/v1/status/buildinfo``. No POST, no admin API, no rules, no writes, and
  nothing that could change anything: Prometheus is read as-is;
* a hard timeout (``NETPILOT_AI_LAB_PROMETHEUS_TIMEOUT``) and response-size
  guards, so a hostile or misconfigured endpoint cannot make the dashboard
  buffer an unbounded body;
* the response is **untrusted input**: label values are treated as text,
  never interpolated into a shell command, a file path or a query, and no
  value is executed or evaluated;
* Prometheus does not carry unit metadata, so the operator must declare units.
  An undeclared unit is stored as ``None`` and the dataset says so; nothing is
  inferred from the numbers;
* failures are returned as an explicit ``PrometheusError`` with the transport
  detail, and the UI shows that instead of a chart. ``transport`` is injectable
  so tests can exercise the full path offline.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
from typing import Mapping

from analytics.lab_dataset import SOURCE_LIVE, canonicalize
from config import get_settings

#: Response guards. A scrape above these is refused before it is parsed.
MAX_SERIES = 200
MAX_POINTS_PER_SERIES = 5000
MAX_BODY_BYTES = 4_000_000
MAX_EXPRESSION_CHARS = 400

#: Label names tried (in order) when mapping a stream to a device/interface.
#: The operator can override both, because exporter conventions differ.
DEVICE_LABELS = ("nodename", "instance", "job", "device", "hostname", "host")
INTERFACE_LABELS = ("ifName", "ifname", "interface", "name", "dev")


class PrometheusError(RuntimeError):
    """Raised for any failure to reach or parse the endpoint."""


def status() -> dict:
    """Report the live-source boundary. Never fabricates availability."""
    settings = get_settings()
    url = settings.ai_lab_prometheus_url
    if not url:
        return {
            "configured": False, "enabled": False,
            "reason": "No endpoint configured (NETPILOT_AI_LAB_PROMETHEUS_URL "
                      "is empty), so the live telemetry source is disabled. "
                      "Simulated, historical and uploaded sources are "
                      "unaffected.",
            "timeout_seconds": settings.ai_lab_prometheus_timeout_seconds,
        }
    return {
        "configured": True, "enabled": True,
        "base_url": redact(url),
        "timeout_seconds": settings.ai_lab_prometheus_timeout_seconds,
        "read_only": True,
        "boundary": ("Only GET /api/v1/query_range and "
                     "/api/v1/status/buildinfo are used. Response values are "
                     "treated as untrusted data."),
    }


def redact(url: str) -> str:
    """Strip any credentials a URL might embed before displaying it."""
    try:
        import httpx
        parsed = httpx.URL(url)
        if parsed.username or parsed.password:
            parsed = parsed.copy_with(username=None, password=None)
        return str(parsed)
    except Exception:  # pragma: no cover - malformed URL is still displayable
        return url.split("@")[-1]


def health(*, transport=None) -> dict:
    """Ask the endpoint who it is. Returns identity, timestamps and freshness."""
    settings = get_settings()
    payload = _get("/api/v1/status/buildinfo", {}, transport=transport)
    data = payload.get("data") or {}
    return {
        "reachable": True,
        "build_version": data.get("version"),
        "revision": data.get("revision"),
        "base_url": redact(settings.ai_lab_prometheus_url),
        "checked_at": dt.datetime.now(dt.timezone.utc)
                      .replace(tzinfo=None).isoformat(),
        "status": payload.get("status"),
    }


def fetch_range(
    *,
    expression: str,
    hours: float = 3.0,
    step_seconds: int = 60,
    device_label: str | None = None,
    interface_label: str | None = None,
    units: Mapping[str, str] | None = None,
    unit_default: str | None = None,
    metric_name: str | None = None,
    end: dt.datetime | None = None,
    transport=None,
) -> LabDataset:
    """Query one range and return a validated ``source_type='live'`` dataset.

    ``expression`` is a PromQL string written by the operator. It is sent as a
    query *parameter* (never assembled into a URL by hand, never into a
    command), length-bounded, and its response is parsed as data only.
    """
    settings = get_settings()
    if not settings.ai_lab_prometheus_url:
        raise PrometheusError(
            "live telemetry source is not configured "
            "(NETPILOT_AI_LAB_PROMETHEUS_URL is empty); nothing was fetched")
    expression = str(expression or "").strip()
    if not expression:
        raise PrometheusError("a PromQL expression is required")
    if len(expression) > MAX_EXPRESSION_CHARS:
        raise PrometheusError(
            f"expression is {len(expression)} characters, above the bound of "
            f"{MAX_EXPRESSION_CHARS}")
    if step_seconds < 1:
        raise PrometheusError("step_seconds must be >= 1")
    if hours <= 0:
        raise PrometheusError("hours must be > 0")

    finish = end or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    if finish.tzinfo is not None:
        finish = finish.astimezone(dt.timezone.utc).replace(tzinfo=None)
    start = finish - dt.timedelta(hours=hours)
    params = {
        "query": expression,
        "start": _epoch(start),
        "end": _epoch(finish),
        "step": str(step_seconds),
    }
    payload = _get("/api/v1/query_range", params, transport=transport)
    if payload.get("status") != "success":
        raise PrometheusError(
            f"endpoint returned status {payload.get('status')!r}: "
            f"{str(payload.get('error') or payload.get('warnings') or '')[:180]}")
    result = (payload.get("data") or {}).get("result") or []
    if not isinstance(result, list):
        raise PrometheusError("unexpected 'result' shape (not a list); the "
                              "response was not parsed")
    if len(result) > MAX_SERIES:
        raise PrometheusError(
            f"expression matched {len(result)} series, above the bound of "
            f"{MAX_SERIES}. Narrow the selector before running experiments.")

    rows: list[dict] = []
    series_seen: dict[str, int] = {}
    dropped_points = 0
    for stream in result:
        labels = stream.get("metric") or {}
        if not isinstance(labels, dict):
            dropped_points += len(stream.get("values") or [])
            continue
        device = _pick(labels, device_label, DEVICE_LABELS)
        interface = _pick(labels, interface_label, INTERFACE_LABELS)
        metric = str(metric_name or _metric_from_expression(expression))[:80]
        values = stream.get("values") or []
        if len(values) > MAX_POINTS_PER_SERIES:
            raise PrometheusError(
                f"series {labels} returned {len(values)} points, above the "
                f"bound of {MAX_POINTS_PER_SERIES}")
        declared_unit = (units or {}).get(metric) or unit_default or None
        for pair in values:
            if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                dropped_points += 1
                continue
            timestamp = _from_epoch(pair[0])
            try:
                value = float(pair[1])
                if math.isnan(value) or math.isinf(value):
                    # Prometheus emits NaN for "no data" on some expressions.
                    # A missing reading stays missing: it is never 0.
                    value = None
                    dropped_points += 1
            except (TypeError, ValueError):
                value = None
                dropped_points += 1
            rows.append({
                "collected_at": timestamp, "device": device,
                "interface": interface, "metric": metric, "value": value,
                "unit": declared_unit, "label": None,
            })
        key = f"{device} · {interface or '(device-level)'} · {metric}"
        series_seen[key] = series_seen.get(key, 0) + 1
    if not rows:
        raise PrometheusError(
            "the query succeeded but matched no data points, so there is "
            "nothing to analyse. This is reported as an empty live result, "
            "not as a fabricated series.")

    identity = hashlib.sha256(expression.encode()).hexdigest()[:12]
    provenance = {
        "origin": f"Prometheus range query · {redact(settings.ai_lab_prometheus_url)}",
        "expression": expression,
        "step_seconds": step_seconds,
        "window": {"start": start.isoformat(), "end": finish.isoformat()},
        "fetched_at": dt.datetime.now(dt.timezone.utc)
                      .replace(tzinfo=None).isoformat(),
        "matched_series": series_seen,
        "labels_source": "endpoint response (unverified)",
        "unit_source": (("declared by the operator (per-metric map)" if units
                         else "declared by the operator")
                        if (units or unit_default) else
                        "undeclared - Prometheus carries no unit metadata, so "
                        "units are recorded as unknown"),
        "dropped_points_note": (
            f"{dropped_points} point(s) were unusable (malformed pair or "
            "non-numeric value) and are recorded as missing"
            if dropped_points else "all points parsed"),
        "is_lab_real": True,
        "read_only": True,
        "labels": ("none - a live scrape has no ground truth, so supervised "
                   "accuracy cannot be computed from it"),
        "boundary": status().get("boundary"),
    }
    return canonicalize(
        rows, source_type=SOURCE_LIVE, source_id=f"prom-{identity}",
        provenance=provenance, now=dt.datetime.now(
            dt.timezone.utc).replace(tzinfo=None),
        max_rows=settings.analytics_max_points)


# --------------------------------------------------------------------------
# HTTP plumbing (kept tiny, injectable, bounded)
# --------------------------------------------------------------------------
def _get(path: str, params: Mapping[str, str], *, transport=None) -> dict:
    """One bounded read-only GET returning parsed JSON."""
    settings = get_settings()
    base = settings.ai_lab_prometheus_url.rstrip("/")
    url = f"{base}{path}"
    try:
        import httpx
    except Exception as exc:  # pragma: no cover - httpx is a listed dependency
        raise PrometheusError(f"httpx is unavailable: {exc}") from exc
    try:
        kwargs: dict = {"timeout": settings.ai_lab_prometheus_timeout_seconds}
        if transport is not None:
            kwargs["transport"] = transport
        with httpx.Client(**kwargs) as client:
            response = client.get(url, params=dict(params))
    except Exception as exc:
        raise PrometheusError(
            f"read-only request to {redact(url)} failed: "
            f"{type(exc).__name__}: {str(exc)[:200]}") from exc
    if response.status_code >= 400:
        raise PrometheusError(
            f"{redact(url)} returned HTTP {response.status_code}; no data was "
            "used")
    body = response.content or b""
    if len(body) > MAX_BODY_BYTES:
        raise PrometheusError(
            f"response body is {len(body)} bytes, above the bound of "
            f"{MAX_BODY_BYTES}; it was discarded rather than parsed")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PrometheusError(
            f"response was not valid JSON ({type(exc).__name__}); nothing was "
            "inferred from it") from exc
    if not isinstance(payload, dict):
        raise PrometheusError("response JSON was not an object")
    return payload


def _pick(labels: Mapping, override: str | None, defaults) -> str | None:
    names = [override] if override else list(defaults)
    for name in names:
        if name and name in labels and str(labels[name]).strip():
            return str(labels[name]).strip()
    return None


def _metric_from_expression(expression: str) -> str:
    """Name the series after the longest metric-like token in the expression.

    Prometheus has no result-name field for a range query, so the label shown
    in the UI is derived from the expression text and is always overridable via
    ``metric_name``. It is a display name only, never a claim about units.
    """
    tokens = re.findall(r"[A-Za-z_:][A-Za-z0-9_:]*", expression) or []
    tokens = [t for t in tokens if not t.isdigit() and t.lower() not in
              ("rate", "sum", "avg", "max", "min", "increase", "by", "over")]
    return max(tokens, key=len) if tokens else "prometheus_query"


def _epoch(value: dt.datetime) -> str:
    return str(int(value.replace(tzinfo=dt.timezone.utc).timestamp()))


def _from_epoch(raw) -> dt.datetime:
    try:
        seconds = float(raw)
    except (TypeError, ValueError) as exc:
        raise PrometheusError(f"timestamp {raw!r} is not numeric") from exc
    return dt.datetime.fromtimestamp(seconds, dt.timezone.utc).replace(
        tzinfo=None, microsecond=0)
