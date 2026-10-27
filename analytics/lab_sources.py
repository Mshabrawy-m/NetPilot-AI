"""Source adapters that feed the unified AI Lab dataset (Phase 5).

Four ways in, one validated representation out (``analytics.lab_dataset``):

``load_stored_data``
    Measurements this project already collected (``metric_samples``). This is
    the "existing project data" route: live lab samples are reported as
    ``is_lab_real=True`` with ``labels=none``, seeded demo samples are
    reported as simulated. One explicit provenance per dataset - demo and live
    rows are never combined, exactly as on pages 7/8/9.

``preview_upload`` / ``suggest_upload_mapping`` / ``load_uploaded_csv``
    Browser CSV upload: first a read-only preview (types, missing counts,
    duplicates, timestamp coverage, descriptive stats), then a column mapping
    the operator confirms, then validation with explicit rejected-vs-retained
    counts. The file is data, never code: nothing is executed, only parsed.

``load_local_file``
    Documented historical dataset import from ONE allowlisted directory
    (``NETPILOT_AI_LAB_LOCAL_DIR``). Empty setting = feature disabled; a path
    outside the directory is refused, so no arbitrary filesystem read is
    possible through this feature.

Live pull (Prometheus) lives in ``services.prometheus_source`` because it is
network I/O, and is disabled until it is configured.

Every adapter returns a dataset whose ``warnings`` and ``lineage`` state what
was dropped and why - a row is never discarded in silence, and a value that
cannot be parsed stays NULL instead of becoming 0.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
from pathlib import Path
from typing import Mapping

import pandas as pd

from analytics import time_series
from analytics.lab_dataset import (SOURCE_HISTORICAL, SOURCE_SIMULATED,
                                   SOURCE_UPLOADED, LabDataError, LabDataset,
                                   canonicalize, parse_timestamps)
from config import get_settings
from database import repository

#: Upload guards. The byte cap bounds memory, the column cap bounds the melt
#: width (a 500-column CSV would otherwise create 500 series per device).
MAX_UPLOAD_COLUMNS = 60
MAX_METRIC_COLUMNS = 40

#: Column-name heuristics used only to *suggest* a mapping the operator can
#: change. Nothing here decides silently - the preview always shows the guess.
_TIMESTAMP_HINTS = ("timestamp", "time", "ts", "date", "datetime", "collected",
                    "_at")
_DEVICE_HINTS = ("device", "host", "node", "router", "switch", "src", "name")
_INTERFACE_HINTS = ("interface", "iface", "port", "ifname")
_METRIC_HINTS = ("metric", "measure", "signal", "parameter")
_VALUE_HINTS = ("value", "reading", "metric_value", "y")
_UNIT_HINTS = ("unit", "units")
_LABEL_HINTS = ("label", "anomaly", "fault", "class", "type", "incident",
                "event")


# ==========================================================================
# B - existing project data (stored measurements)
# ==========================================================================
def stored_inventory(session, *, data_source: str | None = None,
                     device_name: str | None = None,
                     min_samples: int = 1) -> list[dict]:
    """Series this project already holds, richest first (no new query layer)."""
    return repository.get_series_inventory(
        session, data_source=data_source, device_name=device_name,
        min_samples=min_samples)


def load_stored_data(
    session,
    *,
    data_source: str,
    series: list[dict],
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
    max_points: int | None = None,
) -> LabDataset:
    """Turn stored ``metric_samples`` rows into a LabDataset.

    ``series`` items are ``{"device_name", "metric", "interface_name"}`` (the
    shape returned by :func:`stored_inventory`). ``data_source`` is required and
    selects exactly one provenance: mixing seeded demo rows with real lab rows
    inside one dataset would make every later conclusion unauditable.
    """
    if not series:
        raise LabDataError("no series selected for the stored-data source")
    if data_source not in ("live", "demo"):
        raise LabDataError(
            f"stored data must come from exactly one provenance ('live' or "
            f"'demo'), got {data_source!r}")
    settings = get_settings()
    cap = max_points or settings.analytics_max_points
    frames: list[pd.DataFrame] = []
    per_series_counts: dict[str, int] = {}
    for item in series:
        device = str(item.get("device_name") or "").strip()
        metric = str(item.get("metric") or "").strip()
        if not device or not metric:
            raise LabDataError(
                "each stored series needs device_name and metric")
        interface = item.get("interface_name")
        frame = time_series.load_metric_frame(
            session, device, metric,
            interface_name=None if interface in (None, "") else str(interface),
            device_level_only=interface in (None, ""),
            data_source=data_source, since=since, until=until,
            max_points=max(1, cap // max(1, len(series))))
        frame = time_series.dedupe_frame(frame)
        if frame.empty:
            continue
        per_series_counts[f"{device} · {interface or '(device-level)'} · "
                          f"{metric}"] = int(len(frame))
        frames.append(pd.DataFrame({
            "collected_at": frame["collected_at"],
            "device": device,
            "interface": interface if interface not in (None, "") else None,
            "metric": metric,
            "value": frame["value"],
            "unit": frame["unit"],
            "label": None,          # stored telemetry carries no ground truth
        }))
    if not frames:
        raise LabDataError(
            "the selected series returned no samples in this window - widen "
            "the time range or pick another series. Nothing is invented.")
    long_frame = pd.concat(frames, ignore_index=True)

    # Real collected data vs seeded demo fixtures: the wording matters, because
    # a demo series must never be presented as a lab measurement.
    is_simulated = data_source == "demo"
    source_type = SOURCE_SIMULATED if is_simulated else SOURCE_HISTORICAL
    selection_hash = hashlib.sha256(
        repr(sorted(per_series_counts.items())).encode()).hexdigest()[:12]
    provenance = {
        "origin": "project database · metric_samples",
        "data_source": data_source,
        "is_lab_real": not is_simulated,
        "labels": ("none - the collector records measurements, nobody labels "
                   "them, so supervised accuracy cannot be computed"
                   if not is_simulated else
                   "none - seeded demonstration fixtures carry no ground truth"),
        "synthetic": is_simulated,
        "window": {"since": since.isoformat() if since else None,
                   "until": until.isoformat() if until else None},
        "per_series_counts": per_series_counts,
        "provenance_note": (
            "Seeded by services/demo_data.py: synthetic demonstration data, "
            "not a lab measurement." if is_simulated else
            "Collected from the virtual lab by the NetPilot collector."),
    }
    return canonicalize(
        long_frame, source_type=source_type,
        source_id=f"stored-{data_source}-{selection_hash}",
        provenance=provenance, now=db_now(), max_rows=cap)


def db_now() -> dt.datetime:
    """Naive UTC 'now', matching the project storage convention."""
    from database.models import db_utcnow
    return db_utcnow()


# ==========================================================================
# C - CSV upload: preview first, then a confirmed mapping, then validation
# ==========================================================================
def _decode(raw: bytes | str) -> str:
    """Strict UTF-8 (BOM tolerated). Anything else is refused, not guessed."""
    if isinstance(raw, str):
        text = raw
    else:
        try:
            text = bytes(raw).decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise LabDataError(
                "upload is not UTF-8 text "
                f"({exc.reason} at byte {exc.start}); only CSV/TSV text files "
                "are parsed - no binary, no other encoding is guessed.") from exc
    if "\x00" in text:
        raise LabDataError("upload contains NUL bytes; it looks like a binary "
                           "file rather than CSV text")
    return text


def _detect_delimiter(text: str) -> str:
    header = text.splitlines()[0] if text.splitlines() else ""
    counts = {sep: header.count(sep) for sep in (",", ";", "\t", "|")}
    best = max(counts, key=counts.get)
    return best if counts[best] else ","


def _read_table(text: str, *, max_bytes: int) -> tuple[pd.DataFrame, dict]:
    """Parse CSV text and report malformed-line counts (nothing silent)."""
    if len(text.encode("utf-8")) > max_bytes:
        raise LabDataError(
            f"upload is {len(text.encode('utf-8'))} bytes, above the bound of "
            f"{max_bytes} (NETPILOT_AI_LAB_UPLOAD_MAX_BYTES). Split or reduce "
            "the file rather than analysing a truncated sample.")
    sep = _detect_delimiter(text)
    body_lines = sum(1 for line in text.splitlines() if line.strip())
    try:
        table = pd.read_csv(io.StringIO(text), sep=sep, dtype=object,
                            engine="python", on_bad_lines="skip",
                            skipinitialspace=True)
    except Exception as exc:  # pandas raises many types; all mean "not parseable"
        raise LabDataError(f"the file could not be parsed as CSV: "
                           f"{type(exc).__name__}: {exc}") from exc
    if table.columns.size > MAX_UPLOAD_COLUMNS:
        raise LabDataError(
            f"{table.columns.size} columns exceeds the bound of "
            f"{MAX_UPLOAD_COLUMNS}. Aggregate the export down to the series "
            "you actually want to experiment on.")
    report = {
        "delimiter": {",": "comma", ";": "semicolon",
                      "\t": "tab", "|": "pipe"}.get(sep, sep),
        "n_text_lines": int(body_lines),
        "n_parsed_rows": int(len(table)),
        "n_rejected_lines": int(max(0, body_lines - 1 - len(table))),
        "n_columns": int(table.columns.size),
    }
    if report["n_rejected_lines"]:
        report["rejected_note"] = (
            f"{report['n_rejected_lines']} line(s) did not match the header "
            "shape and were skipped by the parser; they are counted here "
            "rather than dropped silently.")
    return table, report


def _column_profile(table: pd.DataFrame, column: str) -> dict:
    series = table[column]
    non_null = series.notna().sum()
    numeric = pd.to_numeric(series, errors="coerce")
    numeric_hits = int(numeric.notna().sum())
    parsed = parse_timestamps(series)
    timestamp_hits = int(parsed.notna().sum())
    distinct = int(series.dropna().astype(str).nunique())
    values = numeric if numeric_hits else pd.Series(dtype="float64")
    return {
        "column": str(column),
        "non_null": int(non_null),
        "missing": int(len(series) - non_null),
        "missing_pct": round(100.0 * (len(series) - non_null)
                             / max(1, len(series)), 1),
        "distinct": distinct,
        "numeric_rows": numeric_hits,
        "timestamp_rows": timestamp_hits,
        "guess": ("timestamp" if timestamp_hits >= 0.9 * max(1, non_null)
                  and distinct > 1 else
                  "numeric" if numeric_hits >= 0.9 * max(1, non_null)
                  else "category" if distinct <= max(10, len(series) * 0.05)
                  else "text"),
        "min": (round(float(values.min()), 6) if numeric_hits and
                len(values.dropna()) else None),
        "max": (round(float(values.max()), 6) if numeric_hits and
                len(values.dropna()) else None),
        "mean": (round(float(values.mean()), 6) if numeric_hits and
                 len(values.dropna()) else None),
        "examples": [str(v)[:40] for v in series.dropna().head(3).tolist()],
    }


def preview_upload(raw: bytes | str, *, filename: str = "upload.csv",
                   sample_rows: int = 8) -> dict:
    """Read-only structural preview shown *before* any mapping decision.

    Reports types, missing/duplicate counts, timestamp coverage and descriptive
    statistics per column, plus exactly how many lines were parsed and skipped.
    """
    settings = get_settings()
    text = _decode(raw)
    table, parse_report = _read_table(text,
                                      max_bytes=settings.ai_lab_upload_max_bytes)
    if table.empty:
        raise LabDataError("the parsed CSV has no data rows")
    profiles = [_column_profile(table, c) for c in table.columns]
    duplicate_rows = int(table.duplicated().sum())
    ts_candidates = [p["column"] for p in profiles
                     if p["guess"] == "timestamp"]
    numeric_candidates = [p["column"] for p in profiles
                          if p["guess"] == "numeric" and p["distinct"] > 1]
    coverage = None
    if ts_candidates:
        parsed = parse_timestamps(table[ts_candidates[0]]).dropna()
        if len(parsed) > 1:
            gaps = parsed.diff().dt.total_seconds().dropna()
            coverage = {
                "column": ts_candidates[0],
                "first": parsed.min().isoformat(),
                "last": parsed.max().isoformat(),
                "span_seconds": round(
                    float((parsed.max() - parsed.min()).total_seconds()), 1),
                "median_gap_seconds": round(float(gaps.median()), 3),
                "n_gaps_over_3x_median": int(
                    (gaps > 3 * float(gaps.median())).sum()),
                "n_rows": int(len(parsed)),
            }
    return {
        "filename": filename,
        "parse": parse_report,
        "columns": profiles,
        "duplicate_rows": duplicate_rows,
        "timestamp_candidates": ts_candidates,
        "numeric_candidates": numeric_candidates,
        "timestamp_coverage": coverage,
        "head": table.head(sample_rows).astype(object).where(
            table.head(sample_rows).notna(), None).to_dict("records"),
    }


def _first_match(names, hints) -> str | None:
    lowered = {str(n).strip().lower(): n for n in names}
    for hint in hints:
        for low, original in lowered.items():
            if low == hint:
                return original
    for hint in hints:
        for low, original in lowered.items():
            if hint in low:
                return original
    return None


def suggest_upload_mapping(preview: dict) -> dict:
    """Propose a column mapping from the preview; the operator confirms it.

    Long format (a metric-name column plus a value column) is preferred when
    such columns exist, because it carries units and labels per row. Otherwise
    every unexplained numeric column becomes one metric (wide format).
    """
    names = [p["column"] for p in preview["columns"]]
    by_guess = {p["column"]: p["guess"] for p in preview["columns"]}
    candidates = preview.get("timestamp_candidates") or []
    timestamp = (_first_match(names, _TIMESTAMP_HINTS)
                 or next((n for n, g in by_guess.items() if g == "timestamp"),
                         None)
                 or (candidates[0] if candidates else None)
                 or names[0])
    device = _first_match(names, _DEVICE_HINTS)
    interface = _first_match(names, _INTERFACE_HINTS)
    metric_col = _first_match(names, _METRIC_HINTS)
    value_col = _first_match(names, _VALUE_HINTS)
    unit_col = _first_match(names, _UNIT_HINTS)
    label_col = _first_match(names, _LABEL_HINTS)
    reserved = {c for c in (timestamp, device, interface, metric_col, value_col,
                            unit_col, label_col) if c}
    mapping: dict = {
        "format": "long" if metric_col and value_col else "wide",
        "timestamp": timestamp, "device": device, "interface": interface,
        "metric": metric_col, "value": value_col, "unit": unit_col,
        "label": label_col, "metrics": [],
    }
    if mapping["format"] == "wide":
        mapping["metrics"] = [
            p["column"] for p in preview["columns"]
            if p["column"] not in reserved
            and (p["guess"] == "numeric" or p["numeric_rows"] >= 0.5 * max(
                1, p["non_null"]))][:MAX_METRIC_COLUMNS]
    mapping["notes"] = (
        "Suggested by name/type heuristics - confirm or change every field "
        "before anything is computed.")
    if len([p for p in preview["columns"]
            if p["column"] not in reserved]) > MAX_METRIC_COLUMNS:
        mapping["notes"] += (
            f" Only the first {MAX_METRIC_COLUMNS} numeric columns were "
            "proposed; drop the rest from the mapping if unneeded.")
    return mapping


def load_uploaded_csv(
    raw: bytes | str,
    *,
    filename: str = "upload.csv",
    mapping: Mapping | None = None,
    units: Mapping[str, str] | None = None,
    metric_aliases: Mapping[str, str] | None = None,
    device_default: str = "(uploaded)",
    source_note: str = "",
) -> tuple[LabDataset, dict]:
    """Validate an uploaded CSV into a LabDataset plus a retained/rejected report.

    ``mapping`` is the (possibly edited) output of
    :func:`suggest_upload_mapping`. The dataset is explicitly
    ``source_type='uploaded'``: the operator asserts provenance, the app
    records that assertion and does not upgrade it to "real lab data".
    """
    settings = get_settings()
    text = _decode(raw)
    table, parse_report = _read_table(text,
                                      max_bytes=settings.ai_lab_upload_max_bytes)
    map_ = dict(mapping or suggest_upload_mapping(
        preview_upload(raw, filename=filename, sample_rows=0)))
    missing = [key for key in ("timestamp",) if not map_.get(key)]
    if missing:
        raise LabDataError("mapping is missing required column(s): "
                           + ", ".join(missing))
    for key in ("timestamp", "device", "interface", "metric", "value", "unit",
                "label"):
        column = map_.get(key)
        if column and column not in table.columns:
            raise LabDataError(
                f"mapping column {column!r} for '{key}' is not in the file")

    label_note = None
    if map_.get("format") == "long":
        if not map_.get("metric") or not map_.get("value"):
            raise LabDataError("long-format mapping needs both a metric column "
                              "and a value column")
        long = pd.DataFrame({
            "collected_at": table[map_["timestamp"]],
            "device": (table[map_["device"]].astype(str)
                       if map_.get("device") else device_default),
            "interface": (table[map_["interface"]]
                          if map_.get("interface") else None),
            "metric": table[map_["metric"]].astype(str),
            "value": table[map_["value"]],
            "unit": (table[map_["unit"]] if map_.get("unit") else None),
            "label": table[map_["label"]] if map_.get("label") else None,
        })
    else:
        metrics = [m for m in (map_.get("metrics") or []) if m in table.columns]
        if not metrics:
            raise LabDataError(
                "wide-format mapping needs at least one metric column; none of "
                "the proposed columns survived validation")
        id_vars = {"collected_at": table[map_["timestamp"]],
                   "device": (table[map_["device"]].astype(str)
                              if map_.get("device") else device_default),
                   "interface": (table[map_["interface"]]
                                 if map_.get("interface") else None)}
        if map_.get("label"):
            id_vars["label"] = table[map_["label"]]
            label_note = (
                "the single label column was applied to every metric of that "
                "row (the file does not label metrics individually)")
        base = pd.DataFrame(id_vars)
        melted = base.assign(__row__=range(len(base))).merge(
            table[metrics].assign(__row__=range(len(table))),
            on="__row__").melt(
            id_vars=[c for c in base.columns] + ["__row__"],
            value_vars=metrics, var_name="metric", value_name="value")
        long = pd.DataFrame({
            "collected_at": melted["collected_at"],
            "device": melted["device"],
            "interface": melted["interface"],
            "metric": melted["metric"].astype(str),
            "value": melted["value"],
            "unit": (melted["metric"].map({k: v for k, v in (units or {}).items()})
                     if units else None),
            "label": melted.get("label"),
        })

    source_id = ("upload-" + hashlib.sha256(
        f"{filename}|{len(text)}|{parse_report['n_parsed_rows']}".encode()
    ).hexdigest()[:12])
    provenance = {
        "origin": f"operator upload · {filename}",
        "is_lab_real": False,          # not verified by this application
        "verified_by_app": False,
        "asserted_by": "operator (unverified claim)",
        "parse": parse_report,
        "mapping": {k: v for k, v in map_.items() if k != "notes"},
        "label_note": label_note,
        "note": source_note or (
            "Provenance, units and label meaning are as supplied by the "
            "operator; NetPilot cannot verify them."),
    }
    dataset = canonicalize(
        long, source_type=SOURCE_UPLOADED, source_id=source_id,
        provenance=provenance, metric_aliases=metric_aliases,
        now=db_now(), max_rows=settings.analytics_max_points)
    report = {
        "lines_in_file": parse_report["n_text_lines"],
        "rows_parsed": parse_report["n_parsed_rows"],
        "rows_malformed_skipped": parse_report["n_rejected_lines"],
        "rows_after_mapping": int(len(long)),
        "rows_retained": int(dataset.profile["n_rows"]),
        "rows_rejected": int(max(0, len(long) - dataset.profile["n_rows"])),
        "values_missing": int(dataset.profile["n_missing_values"]),
        "explanation": (
            "rows_parsed are CSV lines the parser accepted; rows_after_mapping "
            "is that table expanded into one row per (device, metric, "
            "timestamp) for a wide file; rows_retained is what survived "
            "validation; rows_rejected were dropped with the reasons listed in "
            "warnings.") if map_.get("format") == "wide" else (
            "rows_parsed == rows_after_mapping (long format); rows_retained is "
            "what survived validation and rows_rejected were dropped for the "
            "reasons listed in warnings."),
        "warnings": list(dataset.warnings),
        "lineage": [{"step": s.step, "detail": s.detail,
                     "rows_in": s.rows_in, "rows_out": s.rows_out}
                    for s in dataset.lineage],
    }
    return dataset, report


# ==========================================================================
# B - documented historical import from one allowlisted directory
# ==========================================================================
def local_dir_status() -> dict:
    """Is the local-file route configured and usable? (Never a silent guess.)"""
    settings = get_settings()
    raw = settings.ai_lab_local_dir
    if not raw:
        return {"enabled": False,
                "reason": "NETPILOT_AI_LAB_LOCAL_DIR is not set, so local "
                          "historical import is disabled."}
    path = Path(raw)
    try:
        resolved = path.resolve()
    except OSError as exc:  # pragma: no cover - depends on the OS
        return {"enabled": False, "reason": f"configured path unusable: {exc}"}
    if not resolved.is_dir():
        return {"enabled": False,
                "reason": f"configured directory does not exist: {resolved}"}
    files = sorted(p.name for p in resolved.glob("*.csv"))
    return {"enabled": True, "directory": str(resolved),
            "csv_files": files,
            "note": "Only files inside this one directory can be imported."}


def list_local_files() -> list[dict]:
    """Catalogue the allowlisted CSVs with size and a first-read shape."""
    status = local_dir_status()
    if not status["enabled"]:
        raise LabDataError(status["reason"])
    out: list[dict] = []
    for name in status["csv_files"]:
        path = status["directory"] and (Path(status["directory"]) / name)
        try:
            size = int(path.stat().st_size)
            head = path.read_text(encoding="utf-8-sig",
                                  errors="strict").splitlines()[:1]
            columns = len(head[0].split(",")) if head else 0
            out.append({"name": name, "size_bytes": size,
                        "header_columns": columns})
        except (OSError, UnicodeDecodeError) as exc:
            out.append({"name": name, "error": f"{type(exc).__name__}: {exc}"})
    return out


def load_local_file(name: str, *, mapping: Mapping | None = None,
                    units: Mapping[str, str] | None = None,
                    metric_aliases: Mapping[str, str] | None = None,
                    dataset_label: str = "") -> tuple[LabDataset, dict]:
    """Import one allowlisted CSV as a historical dataset.

    The path is resolved and re-checked against the allowlisted directory, so a
    ``..``/absolute path supplied by a caller cannot escape it.
    """
    status = local_dir_status()
    if not status["enabled"]:
        raise LabDataError(status["reason"])
    root = Path(status["directory"]).resolve()
    candidate = (root / str(name)).resolve()
    if not candidate.is_relative_to(root):
        raise LabDataError(
            f"{name!r} resolves outside the allowlisted directory; refusing "
            "to read it.")
    if not candidate.is_file():
        raise LabDataError(f"no such file in the allowlisted directory: {name}")
    if candidate.suffix.lower() not in (".csv", ".tsv", ".txt"):
        raise LabDataError("only .csv/.tsv/.txt text tables are importable")
    text = candidate.read_text(encoding="utf-8-sig", errors="strict")
    dataset, report = load_uploaded_csv(
        text, filename=str(candidate.name), mapping=mapping, units=units,
        metric_aliases=metric_aliases,
        source_note=(f"documented historical import from {root} "
                     f"(declared provenance: "
                     f"{dataset_label or 'as stated by the dataset owner'})"))
    # Re-stamp the source as a historical import rather than a browser upload.
    dataset.source_type = SOURCE_HISTORICAL
    dataset.source_id = "local-" + hashlib.sha256(
        str(candidate).encode()).hexdigest()[:12]
    dataset.provenance["origin"] = f"local file · {candidate}"
    dataset.provenance["allowlisted_directory"] = str(root)
    dataset.provenance["is_lab_real"] = False
    dataset.provenance["verified_by_app"] = False
    dataset.provenance["imported_at"] = db_now().isoformat()
    return dataset, report


def upload_guard_status() -> dict:
    """Bounds the UI echoes next to the upload widget (no hidden limits)."""
    settings = get_settings()
    return {
        "max_upload_bytes": settings.ai_lab_upload_max_bytes,
        "max_dataset_rows": settings.analytics_max_points,
        "max_columns": MAX_UPLOAD_COLUMNS,
        "max_metric_columns": MAX_METRIC_COLUMNS,
    }
