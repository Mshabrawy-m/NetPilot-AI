"""AI Lab experiment runner, history and exports (Phase 5).

One entry point - :func:`run_experiment` - dispatches a task over a validated
:class:`analytics.lab_dataset.LabDataset`, times it, records the full protocol
and persists one additive ``lab_experiments`` row. Everything the brief asks to
remember per run is here: uuid, creation time, source type/id, dataset
fingerprint, time range, sample counts, features and target, split,
preprocessing, seed, metrics, runtime, status and warnings.

Deliberate limits, stated rather than hidden:

* the row stores the *protocol and the numbers*, never a copy of the raw
  samples - lab frames are analysis input and do not belong in
  ``metric_samples``/``anomaly_events``;
* chart points are stored only while they fit a fixed character budget, and the
  row records which way that went, so a history view can never imply it still
  has points it dropped;
* a refusal is persisted like any other outcome (with its reason), because
  "this could not be computed defensibly" is a result the reader needs;
* export cells are escaped against spreadsheet formula injection, and every
  export repeats the source provenance so a synthetic number cannot be read as
  a real-network measurement.

No part of this module executes anything on the network: it is analysis and
record-keeping only.
"""
from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import html
import inspect
import io
import json
import math
import time
import uuid

from sqlalchemy.orm import Session

from analytics import lab_classification, lab_dataset, lab_simulator
from config import get_settings
from database import repository
from services import lab_tasks

#: Character budget for the chart payload stored with an experiment. Points
#: beyond it are not written: the metrics and the table stay, and the row says
#: the plot data was not persisted.
MAX_POINTS_JSON_CHARS = 60_000
#: Bound on the stored human-readable message (refusal reasons stay complete
#: because they are short by construction; this only clips pathological input).
MAX_MESSAGE_CHARS = 4000

#: Task -> orchestrator. ``refs`` is passed positionally for the three
#: per-series tasks and the classifier takes keyword arguments only.
_TASK_FUNCTIONS = {
    lab_dataset.TASK_ANOMALY: lab_tasks.run_anomaly_comparison,
    lab_dataset.TASK_FORECAST: lab_tasks.run_forecast_comparison,
    lab_dataset.TASK_CLASSIFICATION: lab_tasks.run_fault_classification,
    lab_dataset.TASK_STATISTICS: lab_tasks.run_statistics,
}


# ==========================================================================
# Runner
# ==========================================================================
def accepted_params(task: str) -> list[str]:
    """Keyword names the task's orchestrator really accepts (UI form hint)."""
    fn = _TASK_FUNCTIONS.get(task)
    if fn is None:
        return []
    return [name for name, param in inspect.signature(fn).parameters.items()
            if param.kind in (inspect.Parameter.KEYWORD_ONLY,
                              inspect.Parameter.POSITIONAL_OR_KEYWORD)
            and name not in ("dataset", "refs")]


def run_experiment(
    session: Session,
    dataset: lab_dataset.LabDataset,
    task: str,
    *,
    refs: list[lab_dataset.SeriesRef] | None = None,
    params: dict | None = None,
    persist: bool = True,
    created_by: str | None = None,
    seed: int | None = None,
) -> dict:
    """Execute one task over ``dataset``, persist it, and return both views.

    Returns ``{"meta": {...}, "result": {...}, "row_id": int | None}``.
    Unknown parameter names raise instead of being ignored: a run that silently
    dropped a configured threshold would not describe itself honestly.
    """
    settings = get_settings()
    fn = _TASK_FUNCTIONS.get(task)
    if fn is None:
        raise ValueError(
            f"unknown lab task {task!r}; expected one of "
            f"{sorted(_TASK_FUNCTIONS)}")
    supplied = dict(params or {})
    signature = inspect.signature(fn)
    allowed = {name for name, p in signature.parameters.items()
               if p.kind in (inspect.Parameter.KEYWORD_ONLY,
                             inspect.Parameter.POSITIONAL_OR_KEYWORD)
               and name != "dataset"}
    unknown = sorted(set(supplied) - allowed)
    if unknown:
        raise ValueError(
            f"parameter(s) {unknown} are not accepted by the {task} task; "
            f"accepted: {sorted(allowed - {'refs'})}")
    clean = dict(supplied)

    # ---- series selection: bounded, and the bound is stated ---------------
    chosen = list(refs) if refs else []
    selection_note = ""
    if task != lab_dataset.TASK_CLASSIFICATION:
        if not chosen:
            chosen = dataset.series_refs()[:settings.scan_max_series]
            if len(dataset.series_refs()) > len(chosen):
                selection_note = (
                    f"no series were selected, so the first {len(chosen)} of "
                    f"{len(dataset.series_refs())} were used - the "
                    f"NETPILOT_SCAN_MAX_SERIES bound keeps one run finite. "
                    "Select series explicitly to widen it.")
        over_bound = chosen[:settings.scan_max_series]
        if len(over_bound) != len(chosen):
            selection_note = (
                f"{len(chosen)} series were requested but only the first "
                f"{len(over_bound)} were evaluated (NETPILOT_SCAN_MAX_SERIES = "
                f"{settings.scan_max_series}); the result is not a full-inventory "
                "scan and must not be read as one.")
            chosen = over_bound

    started = time.perf_counter()
    try:
        if task == lab_dataset.TASK_CLASSIFICATION:
            result = _run_classification(dataset, clean)
        else:
            result = fn(dataset, chosen, **clean)
    except lab_dataset.LabDataError as exc:
        # Source-level bounds/refusals are outcomes, not crashes.
        result = lab_tasks.TaskResult.refused(task, "source_refused", str(exc))
    except Exception as exc:  # pragma: no cover - defensive bookkeeping
        result = lab_tasks.TaskResult(
            task=task, status="error",
            message=f"{type(exc).__name__}: {str(exc)[:300]}",
            metrics={"reason": "unexpected_error"},
            warnings=["nothing was computed; no number in this run should be "
                      "quoted"])
    runtime_ms = int(round((time.perf_counter() - started) * 1000))

    meta = build_meta(dataset, result, params=clean, runtime_ms=runtime_ms,
                      seed=seed, refs=chosen, selection_note=selection_note,
                      created_by=created_by)
    row_id = None
    if persist:
        row_id = _persist(session, meta, result, created_by=created_by)
    return {"meta": meta, "result": result.as_dict(), "row_id": row_id}


def _run_classification(dataset, params: dict) -> lab_tasks.TaskResult:
    """The classifier needs a device + label metric; missing ones are refused."""
    device = params.get("device")
    label_metric = params.get("label_metric")
    feature_metrics = params.get("feature_metrics")
    if not device:
        return lab_tasks.TaskResult.refused(
            lab_dataset.TASK_CLASSIFICATION, "no_device",
            "classification labels belong to a device: select which device's "
            "fault classes are being learned.",
            needed={"device": sorted(dataset.devices())})
    if not label_metric:
        return lab_tasks.TaskResult.refused(
            lab_dataset.TASK_CLASSIFICATION, "no_label_metric",
            "select the labelled series that carries the fault class.",
            needed={"label_metric": dataset.metrics(device)})
    if not feature_metrics:
        feature_metrics = tuple(m for m in dataset.metrics(device)
                                if m != label_metric)
    cleaned = dict(params)
    cleaned.update({"device": device, "label_metric": label_metric,
                    "feature_metrics": tuple(feature_metrics)})
    cleaned.setdefault("interface", None)
    return lab_tasks.run_fault_classification(dataset, **cleaned)


# ==========================================================================
# Metadata (§5 checklist) - one shape used by persistence, UI and exports
# ==========================================================================
def build_meta(dataset: lab_dataset.LabDataset,
               result: lab_tasks.TaskResult, *,
               params: dict, runtime_ms: int,
               seed: int | None, refs: list[lab_dataset.SeriesRef],
               selection_note: str = "",
               created_by: str | None = None) -> dict:
    synthetic = is_synthetic(dataset)
    points_chars = len(json.dumps(result.points, default=str, sort_keys=True))
    points_stored = points_chars <= MAX_POINTS_JSON_CHARS
    protocol = _protocol(result, params)
    summary = dataset.summary()
    return {
        "experiment_uuid": str(uuid.uuid4()),
        "created_at": dt.datetime.now(dt.timezone.utc).replace(tzinfo=None),
        "task": result.task,
        "status": result.status,
        "message": (result.message or "")[:MAX_MESSAGE_CHARS],
        # ---- source identity ---------------------------------------------
        "source_type": dataset.source_type,
        "source_label": lab_dataset.SOURCE_LABELS.get(dataset.source_type,
                                                      dataset.source_type),
        "source_id": dataset.source_id,
        "data_source": "demo" if synthetic else "live",
        "is_synthetic": synthetic,
        "dataset_fingerprint": dataset.fingerprint,
        "label_kind": dataset.label_kind,
        # ---- shape of the data -------------------------------------------
        "time_range": {"start": summary.get("time_min"),
                       "end": summary.get("time_max")},
        "sample_counts": {"rows": summary.get("n_rows"),
                          "values": summary.get("n_values"),
                          "series": summary.get("n_series"),
                          "labelled_rows": summary.get("n_labelled_rows"),
                          "missing_values": int(
                              dataset.profile.get("n_missing_values", 0))},
        "median_interval_seconds": summary.get("median_interval_seconds"),
        "series": [str(r) for r in refs],
        "series_selection": selection_note or "operator-selected",
        # ---- features / target / protocol --------------------------------
        "features": list(result.effective_features),
        "target": result.config.get("label_metric") or result.config.get(
            "target") or None,
        "split": protocol["split"],
        "preprocessing": protocol["preprocessing"],
        "seed": seed if seed is not None else result.config.get("random_seed"),
        "params": _jsonable(params),
        # ---- results bookkeeping -----------------------------------------
        "metrics": result.metrics,
        "warnings": list(result.warnings),
        "dataset_warnings": list(dataset.warnings),
        "runtime_ms": runtime_ms,
        "points_stored": points_stored,
        "points_json_chars": points_chars,
        "created_by": created_by,
        "schema_version": "lab-1",
    }


def _protocol(result: lab_tasks.TaskResult, params: dict) -> dict:
    """Split + preprocessing actually applied, extracted from the task config."""
    config = result.config or {}
    artifacts = result.artifacts or {}
    split = {k: v for k, v in config.items()
             if k in ("test_frac", "split_mode", "split_note", "horizon",
                      "band_nominal_coverage")}
    per_series = artifacts.get("per_series") or {}
    if per_series:
        split["per_series"] = {k: v.get("split") for k, v in per_series.items()
                               if isinstance(v, dict)}
    preprocessing = (config.get("preprocessing")
                     or (artifacts.get("config") or {}).get("preprocessing"))
    if preprocessing is None:
        preprocessing = {
            "applied": "none",
            "explanation": (
                "single-series detectors and forecast models take one "
                "time-ordered value stream, so no matrix fill or scaling step "
                "exists to fit; any fitted parameter is estimated inside the "
                "training window only"),
        }
    return {"split": split, "preprocessing": preprocessing,
            "config": {k: v for k, v in config.items()
                       if k not in ("preprocessing",)}}


def is_synthetic(dataset: lab_dataset.LabDataset) -> bool:
    """Was this generated, as opposed to measured or supplied?

    ``is_lab_real: false`` is carried by uploads and imports too, but there it
    means "the app cannot verify this" - not "this is fabricated". Reading it
    as synthetic would file an operator's own CSV under demo data, so the
    explicit ``synthetic`` marker (set by the simulator and the demo loader)
    is what decides, alongside the simulated source type. The UI badge and the
    stored record call this one rule, so they can never disagree.
    """
    if dataset.source_type == lab_dataset.SOURCE_SIMULATED:
        return True
    return dataset.provenance.get("synthetic") is True


def _jsonable(value):
    """Make parameters JSON-safe without turning them into something else."""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (lab_dataset.SeriesRef,)):
        return str(value)
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, (int, float, str, bool)) or value is None:
        return value
    return str(value)


# ==========================================================================
# Persistence
# ==========================================================================
_JSON_FIELDS = ("features_json", "split_json", "preprocessing_json",
                "config_json", "metrics_json", "table_json", "warnings_json",
                "points_json")


def _persist(session: Session, meta: dict, result: lab_tasks.TaskResult, *,
             created_by: str | None) -> int:
    """Write one additive ``lab_experiments`` row (raw samples are never stored)."""
    points_stored = bool(meta["points_stored"])
    fields = {
        "experiment_uuid": meta["experiment_uuid"],
        "schema_version": meta["schema_version"],
        "task": meta["task"],
        "status": meta["status"],
        "message": meta["message"],
        "source_type": meta["source_type"],
        "source_id": (meta["source_id"] or "")[:128] or None,
        "data_source": meta["data_source"],
        "is_synthetic": meta["is_synthetic"],
        "dataset_fingerprint": meta["dataset_fingerprint"],
        "label_kind": meta["label_kind"],
        "range_start": _to_naive(meta["time_range"]["start"]),
        "range_end": _to_naive(meta["time_range"]["end"]),
        "n_rows": int(meta["sample_counts"]["rows"] or 0),
        "n_series": int(meta["sample_counts"]["series"] or 0),
        "n_values": int(meta["sample_counts"]["values"] or 0),
        "features_json": _dump(result.effective_features),
        "target": (str(meta["target"])[:128] if meta["target"] else None),
        "split_json": _dump(meta["split"]),
        "preprocessing_json": _dump(meta["preprocessing"]),
        "config_json": _dump({**meta["params"],
                              "series": meta["series"],
                              "series_selection": meta["series_selection"],
                              "task_config": _jsonable(result.config)}),
        "seed": meta["seed"],
        "metrics_json": _dump(_jsonable(meta["metrics"])),
        "table_json": _dump(_jsonable(result.table)),
        "warnings_json": _dump(list(result.warnings)),
        "points_json": (json.dumps(result.points, default=str, sort_keys=True)
                        if points_stored else None),
        "points_stored": points_stored,
        "runtime_ms": meta["runtime_ms"],
        "created_by": created_by,
    }
    row = repository.add_lab_experiment(session, **fields)
    session.commit()
    return int(row.id)


def _dump(value) -> str | None:
    if value is None:
        return None
    return json.dumps(_jsonable(value), default=str, sort_keys=True)


def _to_naive(value) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = dt.datetime.fromisoformat(value)
        except ValueError:
            return None
    return value.replace(tzinfo=None) if isinstance(
        value, dt.datetime) else None


# ==========================================================================
# History
# ==========================================================================
def history(session: Session, *, task: str | None = None,
            source_type: str | None = None,
            limit: int | None = None) -> list[dict]:
    """Recent experiments as plain dicts (JSON columns already parsed)."""
    cap = limit or get_settings().ai_lab_history_limit
    rows = repository.get_lab_experiments(
        session, task=task, source_type=source_type, limit=cap)
    return [_row_to_dict(row) for row in rows]


def get_experiment(session: Session, experiment_uuid: str) -> dict | None:
    row = repository.get_lab_experiment(session, experiment_uuid)
    return _row_to_dict(row) if row else None


def _row_to_dict(row) -> dict:
    stored = {c.name: getattr(row, c.name) for c in row.__table__.columns}
    parsed = {}
    for column in _JSON_FIELDS:
        raw = stored.pop(column, None)
        key = column[:-5]  # strip "_json"
        try:
            parsed[key] = json.loads(raw) if raw else None
        except (TypeError, ValueError):
            # A corrupt cell is reported as such rather than guessed at.
            parsed[key] = None
            parsed.setdefault("parse_errors", []).append(column)
    stored["json"] = parsed
    stored["schema_version_known"] = (
        stored.get("schema_version") == "lab-1")
    return stored


# ==========================================================================
# Exports
# ==========================================================================
#: Spreadsheet-active leading characters (formula injection).
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _escape_cell(value) -> str:
    """Render one CSV cell, neutralising spreadsheet formula injection.

    Numeric cells keep their sign so the file stays usable as data; only
    text-typed values that begin with an active character get a protective
    apostrophe, which is what makes ``=cmd|...`` inert in Excel/Sheets.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return ""
        return str(value)
    text = (json.dumps(value, default=str, sort_keys=True)
            if isinstance(value, (list, dict, tuple, set)) else str(value))
    if text[:1] in _FORMULA_PREFIXES and not _looks_numeric(text):
        text = "'" + text
    return text


def _looks_numeric(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True


def restore_result(payload: dict | lab_tasks.TaskResult) -> lab_tasks.TaskResult:
    """Rebuild a :class:`lab_tasks.TaskResult` from its ``as_dict()`` payload.

    The UI stores the dict form (session state / JSON columns), while the
    exports are written against the dataclass, so exactly one lossless mapping
    is needed - unknown keys are ignored, missing collections become empty.
    """
    if isinstance(payload, lab_tasks.TaskResult):
        return payload
    known = {f.name for f in dataclasses.fields(lab_tasks.TaskResult)}
    clean = {k: v for k, v in (payload or {}).items() if k in known}
    for name in ("metrics", "artifacts", "config"):
        clean.setdefault(name, {})
    for name in ("table", "points", "warnings", "effective_features"):
        clean.setdefault(name, [])
    clean["message"] = clean.get("message") or ""
    return lab_tasks.TaskResult(**clean)


def export_table_csv(result: lab_tasks.TaskResult) -> str:
    """The comparison table, one row per result line, header from all keys."""
    result = restore_result(result)
    if not result.table:
        return "# no table rows: the run produced no comparison rows\n"
    columns: list[str] = []
    for row in result.table:
        for key in row:
            if key not in columns:
                columns.append(key)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["# experiment table"] + [""] * (len(columns) - 1))
    writer.writerow(columns)
    for row in result.table:
        writer.writerow([_escape_cell(row.get(col)) for col in columns])
    return buffer.getvalue()


def export_metrics_csv(result: lab_tasks.TaskResult) -> str:
    """Flat key/value metrics, including nested protocol fields."""
    result = restore_result(result)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["section", "key", "value"])
    writer.writerow(["task", "task", _escape_cell(result.task)])
    writer.writerow(["task", "status", _escape_cell(result.status)])
    for key, value in (result.metrics or {}).items():
        writer.writerow(["metrics", key, _escape_cell(value)])
    for key, value in (result.config or {}).items():
        writer.writerow(["config", key, _escape_cell(value)])
    for index, warning in enumerate(result.warnings, start=1):
        writer.writerow(["warnings", str(index), _escape_cell(warning)])
    return buffer.getvalue()


def export_markdown(dataset: lab_dataset.LabDataset,
                    result: lab_tasks.TaskResult, *,
                    meta: dict | None = None) -> str:
    """Human-readable report: provenance first, numbers second, caveats last."""
    result = restore_result(result)
    meta = meta or build_meta(dataset, result, params={}, runtime_ms=0,
                              seed=None, refs=dataset.series_refs(),
                              selection_note="(export-time reconstruction)")
    lines: list[str] = [
        f"# NetPilot AI Lab - {result.task} experiment",
        "",
        f"- **Experiment id**: `{meta['experiment_uuid']}`",
        f"- **Run at (UTC)**: {meta['created_at'].isoformat(timespec='seconds')}",
        f"- **Status**: `{result.status}` - {result.message}",
        f"- **Runtime**: {meta['runtime_ms']} ms",
        "",
        "## 1. Where the data came from",
        "",
        f"- Source type: **{meta['source_label']}**",
        f"- Source id: `{meta['source_id'] or 'n/a'}`",
        f"- Recorded provenance: `{meta['data_source']}` "
        f"({'synthetic or demo data - not a real-network measurement' if meta['is_synthetic'] else 'operator-supplied or collected data; correctness is asserted by the source, not verified here'})",
        f"- Dataset fingerprint (content hash): `{meta['dataset_fingerprint']}`",
        f"- Labels present: `{meta['label_kind']}` "
        f"({meta['sample_counts']['labelled_rows']} labelled rows)",
        f"- Rows / usable values / series: {meta['sample_counts']['rows']} / "
        f"{meta['sample_counts']['values']} / {meta['sample_counts']['series']}"
        f" (missing values: {meta['sample_counts']['missing_values']})",
        f"- Time range: {meta['time_range']['start']} to "
        f"{meta['time_range']['end']} "
        f"(median interval {meta['median_interval_seconds']} s)",
        f"- Series evaluated: {_bullet_list(meta['series'])}",
        f"- Selection basis: {meta['series_selection']}",
        "",
        "## 2. Protocol actually applied",
        "",
        f"- Features the model received: {_bullet_list(result.effective_features)}",
        f"- Target: `{meta['target'] or 'none (unsupervised / descriptive)'}`",
        f"- Split: `{json.dumps(_jsonable(meta['split']), default=str)}`",
        f"- Preprocessing: `{json.dumps(_jsonable(meta['preprocessing']), default=str)}`",
        f"- Random seed: {meta['seed'] if meta['seed'] is not None else 'not used'}",
        f"- Requested parameters: `{json.dumps(_jsonable(meta['params']), default=str, sort_keys=True)}`",
        "",
        "## 3. Results",
        "",
    ]
    lines += _markdown_metrics(result)
    lines += ["", "### Comparison table", ""]
    lines += _markdown_table(result.table)
    if result.points:
        lines += [
            "",
            f"### Chart data",
            "",
            f"- {len(result.points)} chart series in the run payload "
            f"({meta['points_json_chars']} characters; "
            f"{'persisted with the experiment row' if meta['points_stored'] else 'NOT persisted - it exceeded the stored budget, so the history view shows numbers without plots'}).",
            "- Every plotted point is a computed result of this run; none of it "
            "is decorative or pre-set.",
        ]
    lines += ["", "## 4. How to read these numbers", ""]
    for warning in result.warnings:
        lines.append(f"- {warning}")
    for warning in dataset.warnings:
        lines.append(f"- (ingest) {warning}")
    if not result.warnings and not dataset.warnings:
        lines.append("- The run reported no qualifying warnings.")
    lines += [
        "",
        "## 5. Safety statement",
        "",
        "- This experiment is analysis only. No device configuration was "
        "proposed, validated, approved or applied, and the isolated-lab "
        "execution and change-loop gates remain exactly as configured "
        "(both disabled by default).",
        f"- Generated by NetPilot AI Lab for experiment "
        f"`{meta['experiment_uuid']}`.",
        "",
    ]
    return "\n".join(lines)


def _markdown_metrics(result: lab_tasks.TaskResult) -> list[str]:
    lines = ["| Metric | Value |", "|---|---|"]
    for key, value in (result.metrics or {}).items():
        lines.append(f"| {key} | {_inline(value)} |")
    if not result.metrics:
        lines.append("| (none) | nothing was computed |")
    return lines


def _markdown_table(rows: list[dict]) -> list[str]:
    if not rows:
        return ["_No comparison rows: the run refused or produced nothing._"]
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    lines = ["| " + " | ".join(columns) + " |",
             "|" + "|".join(["---"] * len(columns)) + "|"]
    for row in rows[:200]:
        lines.append("| " + " | ".join(_inline(row.get(col), pipe=True)
                                       for col in columns) + " |")
    if len(rows) > 200:
        lines.append(f"_{len(rows) - 200} further row(s) omitted from the "
                     "report; they remain in the stored experiment._")
    return lines


def _inline(value, *, pipe: bool = False) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    text = (json.dumps(value, default=str, sort_keys=True)
            if isinstance(value, (list, dict, tuple, set)) else str(value))
    if len(text) > 160:
        text = text[:157] + "..."
    return text.replace("|", "\\|") if pipe else text


def _bullet_list(items) -> str:
    items = list(items or [])
    if not items:
        return "(none)"
    return ", ".join(f"`{item}`" for item in items)


def export_html(dataset: lab_dataset.LabDataset,
                result: lab_tasks.TaskResult, *,
                meta: dict | None = None) -> str:
    """Self-contained HTML of the Markdown report (no external assets)."""
    result = restore_result(result)
    source = export_markdown(dataset, result, meta=meta)
    body: list[str] = []
    in_table = False
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if all(set(c) <= {"-", ""} for c in cells):
                continue
            if not in_table:
                body.append("<table><thead>")
                body.append("<tr>" + "".join(f"<th>{html.escape(c)}</th>"
                                             for c in cells) + "</tr>")
                body.append("</thead><tbody>")
                in_table = True
                continue
            body.append("<tr>" + "".join(f"<td>{html.escape(c)}</td>"
                                         for c in cells) + "</tr>")
            continue
        if in_table:
            body.append("</tbody></table>")
            in_table = False
        if stripped.startswith("# "):
            body.append(f"<h1>{html.escape(stripped[2:])}</h1>")
        elif stripped.startswith("## "):
            body.append(f"<h2>{html.escape(stripped[3:])}</h2>")
        elif stripped.startswith("### "):
            body.append(f"<h3>{html.escape(stripped[4:])}</h3>")
        elif stripped.startswith("- "):
            body.append(f"<li>{_md_inline(stripped[2:])}</li>")
        elif stripped:
            body.append(f"<p>{_md_inline(stripped)}</p>")
    if in_table:
        body.append("</tbody></table>")
    return ("<!DOCTYPE html>\n<html lang=\"en\">\n<head><meta charset=\"utf-8\">"
            "<title>NetPilot AI Lab experiment report</title>"
            "<style>body{font-family:system-ui,sans-serif;max-width:60rem;"
            "margin:2rem auto;padding:0 1rem;color:#1c2430}"
            "table{border-collapse:collapse;width:100%;margin:.75rem 0}"
            "th,td{border:1px solid #d5dce6;padding:.35rem .5rem;"
            "text-align:left;font-size:.9rem}h1{font-size:1.4rem}"
            "h2{font-size:1.1rem;margin-top:1.5rem}li{margin:.25rem 0}"
            "code{background:#f2f5f9;padding:.1rem .25rem;border-radius:3px}"
            "</style></head>\n<body>\n"
            + "\n".join(body) + "\n</body>\n</html>\n")


def _md_inline(text: str) -> str:
    """Escape, then restore the two Markdown spans the report uses."""
    out = html.escape(text)
    parts: list[str] = []
    for index, chunk in enumerate(out.split("`")):
        # Escaped text alternates: plain, code, plain, code...
        parts.append(f"<code>{chunk}</code>" if index % 2 else chunk)
    out = "".join(parts)
    parts = []
    for index, chunk in enumerate(out.split("**")):
        parts.append(f"<strong>{chunk}</strong>" if index % 2 else chunk)
    return "".join(parts)


# ==========================================================================
# Convenience used by the UI's "start from a known-good state" button
# ==========================================================================
def simulated_dataset(**overrides) -> lab_dataset.LabDataset:
    """Simulator passthrough (kept here so the page imports one service)."""
    return lab_simulator.simulate(**overrides)


def task_defaults(task: str, dataset: lab_dataset.LabDataset) -> dict:
    """Documented starting values, drawn from the same settings as the tasks."""
    from analytics import anomaly_detection

    settings = get_settings()
    if task == lab_dataset.TASK_ANOMALY:
        return {"detectors": list(anomaly_detection.DEFAULT_DETECTORS),
                "window": settings.detector_window, "sweep_curve": True,
                "include_multivariate": False}
    if task == lab_dataset.TASK_FORECAST:
        from services import forecast_service

        return {"horizon": settings.forecast_horizon_default,
                "models": list(forecast_service.DEFAULT_MODELS),
                "test_frac": 0.2, "ensemble": True}
    if task == lab_dataset.TASK_CLASSIFICATION:
        device = (dataset.devices() or [None])[0]
        return {"device": device, "test_frac": 0.25,
                "split_mode": lab_classification.SPLIT_CHRONOLOGICAL,
                "models": ["logistic_regression", "random_forest"]}
    if task == lab_dataset.TASK_STATISTICS:
        return {"lag_max": 12, "correlations": True, "group_by_labels": True}
    return {}
