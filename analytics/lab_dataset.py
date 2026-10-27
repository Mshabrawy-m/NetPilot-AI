"""Unified, validated telemetry representation for the AI Lab (Phase 5).

Every AI Lab source - the built-in simulator, a documented local historical
file, a browser CSV upload, measurements already stored in this project's
database, or a read-only Prometheus scrape - is converted into ONE
``LabDataset`` so that all four experiment tasks (anomaly comparison,
forecasting, fault classification, statistical analysis) run against the same
validated contract instead of five slightly different frames.

Design rules carried over from Phase 2 (never re-invented here):

* the per-series frame handed to the existing detectors/models is exactly
  ``analytics.time_series.FRAME_COLUMNS``, so
  ``analytics.anomaly_detection.detect_frame`` and
  ``analytics.forecasting.*`` consume lab series unchanged;
* readiness is decided by ``analytics.data_quality.summarize_quality`` - a
  short, empty or irregular series is reported as *not ready* with a concrete
  reason instead of producing a confident-looking number;
* a value that cannot be parsed is kept as NULL (missing), never silently
  turned into 0 (the project-wide "refuse instead of fabricate" rule);
* rows dropped by validation are counted and reported, never discarded
  silently;
* preprocessing (imputation / scaling) is fitted on TRAIN rows only;
* the dataset fingerprint is a one-way digest - storing it duplicates no
  measurements and leaks no raw content.

Lab frames are analysis-only: they are never written back into
``metric_samples`` and never ingested into ``anomaly_events``, so experimental
or uploaded data cannot contaminate the operational data model.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import math
import warnings
from typing import Iterable, Mapping

import pandas as pd

from analytics import data_quality
from analytics.evaluation import LeakageError
from analytics.time_series import FRAME_COLUMNS, median_interval_seconds
from config import get_settings

#: Canonical long-format columns. ``interface``/``unit``/``label`` are optional
#: on input and normalised to None (not "") so grouping behaves predictably.
LONG_COLUMNS = ["collected_at", "device", "interface", "metric", "value",
                "unit", "label"]

SOURCE_SIMULATED = "simulated"
SOURCE_HISTORICAL = "historical"
SOURCE_UPLOADED = "uploaded"
SOURCE_LIVE = "live"
SOURCE_TYPES = (SOURCE_SIMULATED, SOURCE_HISTORICAL, SOURCE_UPLOADED,
                SOURCE_LIVE)

#: Human-facing provenance wording. Synthetic data is never described as real.
SOURCE_LABELS = {
    SOURCE_SIMULATED: "Synthetic / simulated (ground-truth labels available, "
                      "NOT real-network traffic)",
    SOURCE_HISTORICAL: "Historical import (already recorded data; provenance "
                       "is whatever the source states, not verified here)",
    SOURCE_UPLOADED: "User-uploaded file (provenance supplied by the operator, "
                     "not verified here)",
    SOURCE_LIVE: "Live read-only telemetry (fetched at experiment time)",
}

TASK_ANOMALY = "anomaly"
TASK_FORECAST = "forecast"
TASK_CLASSIFICATION = "classification"
TASK_STATISTICS = "statistics"
TASKS = (TASK_ANOMALY, TASK_FORECAST, TASK_CLASSIFICATION, TASK_STATISTICS)

#: Tasks that cannot produce supervised quality numbers without labels.
LABEL_REQUIRED_TASKS = (TASK_CLASSIFICATION,)

#: Class names that mean "this sample is healthy" when labels are multi-class.
#: Anomaly/normal scoring on a multi-class source is only possible when one of
#: these names is present; otherwise the split is refused rather than guessed.
NORMAL_CLASS_NAMES = frozenset({"normal", "ok", "baseline", "none", "healthy"})

#: Label spellings recognised as an explicit binary anomaly flag.
_BINARY_LABELS = {"1": 1, "true": 1, "anomaly": 1, "abnormal": 1, "yes": 1,
                  "fault": 1, "0": 0, "false": 0, "normal": 0, "benign": 0,
                  "no": 0, "ok": 0, "healthy": 0}


class LabDataError(ValueError):
    """Raised when a source cannot be turned into a defensible dataset."""


def _epoch_unit(magnitude: float) -> str:
    """Infer the epoch unit of a numeric timestamp column from its magnitude.

    This is the only unit inference in the ingest path, and it is bounded: an
    epoch in seconds stays below 1e11 until year 5138, milliseconds below 1e14,
    microseconds below 1e17. Without the higher bands a nanosecond epoch would
    be read as milliseconds and land in the 58 000s - a wrong guess that is
    visible in the preview window, never presented as a plausible date.
    """
    for limit, unit in ((1e11, "s"), (1e14, "ms"), (1e17, "us")):
        if magnitude < limit:
            return unit
    return "ns"


def parse_timestamps(values) -> pd.Series:
    """Parse any timestamp-ish column to UTC, unparseable entries to NaT.

    Three shapes are handled. A column that is **already** datetime-like is
    normalised to UTC as-is: ``pd.to_numeric`` would turn it into an integer
    count of microseconds and the epoch branch would then re-read that as a
    much larger unit, shifting every row by tens of thousands of years. If
    **every** non-null value is numeric the column is read as an epoch in the
    unit implied by :func:`_epoch_unit` - the only case in which a unit is
    inferred, and the resulting window is shown in the upload preview so a wrong
    guess stays visible instead of silently shifting dates. Otherwise ``format``
    is deliberately not pinned: uploads arrive in mixed ISO/locale shapes and
    pandas falls back to per-element dateutil parsing. That fallback emits an
    informational warning which would otherwise reach the user as an error, so it
    is silenced here; the parse behaviour (and the NaT result for anything
    unparseable) is unchanged.
    """
    series = values if isinstance(values, pd.Series) else pd.Series(values)
    if pd.api.types.is_datetime64_any_dtype(series):
        return pd.to_datetime(series, utc=True)
    if pd.api.types.is_timedelta64_dtype(series):
        # A duration is not a point in time: refuse it rather than invent one.
        return pd.Series(pd.NaT, index=series.index,
                         dtype="datetime64[ns, UTC]")
    numeric = pd.to_numeric(series, errors="coerce")
    non_null = int(series.notna().sum())
    if non_null and int(numeric.notna().sum()) == non_null:
        unit = _epoch_unit(float(numeric.dropna().abs().max()))
        return pd.to_datetime(numeric, unit=unit, utc=True)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return pd.to_datetime(series, errors="coerce", utc=True)


# --------------------------------------------------------------------------
# Value objects
# --------------------------------------------------------------------------
@dataclasses.dataclass
class LineageStep:
    """One recorded transform in the ingest pipeline (audit trail)."""

    step: str
    detail: str
    rows_in: int
    rows_out: int


@dataclasses.dataclass
class SeriesRef:
    """Address of one canonical series inside a LabDataset."""

    device: str
    metric: str
    interface: str | None = None

    @property
    def key(self) -> tuple[str, str | None, str]:
        return (self.device, self.interface, self.metric)

    def __str__(self) -> str:  # pragma: no cover - presentation only
        scope = self.interface or "(device-level)"
        return f"{self.device} · {scope} · {self.metric}"


@dataclasses.dataclass
class SeriesInventory:
    """Per-series counts plus the readiness decision reused from Phase 2."""

    ref: SeriesRef
    unit: str | None
    n_rows: int
    n_values: int
    n_labels: int
    first_ts: dt.datetime | None
    last_ts: dt.datetime | None
    ready: dict[str, bool]
    reasons: list[str]


@dataclasses.dataclass
class TrainPreprocessor:
    """Median fill + robust (MAD) scaling, fitted on TRAIN rows only.

    ``fit`` is only ever given the chronological train slice; ``transform`` may
    then be applied to validation/test rows. The fitted constants and the train
    row count are recorded so a run can prove which numbers were learned from
    which subset (no leakage, and the effective preprocessing is inspectable).
    """

    medians: dict[str, float]
    scales: dict[str, float]
    n_fit_rows: int
    fit_range: tuple[str, str] | None = None

    @classmethod
    def fit(cls, features: pd.DataFrame, *,
            fit_rows: pd.DataFrame | None = None) -> "TrainPreprocessor":
        """Learn fill/scale values from ``features`` (caller passes TRAIN only).

        A column that is constant in train gets scale None: it carries no
        information, and dividing by 0 would invent huge values.
        """
        source = features if fit_rows is None else features[fit_rows]
        if source.shape[1] == 0 or len(source) == 0:
            raise LabDataError("preprocessing needs at least one feature and "
                               "one training row")
        medians: dict[str, float] = {}
        scales: dict[str, float] = {}
        for column in source.columns:
            values = pd.to_numeric(source[column], errors="coerce").dropna()
            if values.empty:
                medians[str(column)] = 0.0
                scales[str(column)] = None
                continue
            median = float(values.median())
            mad = float((values - median).abs().median()) * 1.4826
            spread = float(values.std(ddof=1)) if len(values) > 1 else 0.0
            scale = mad if mad > 1e-12 else (spread if spread > 1e-12 else None)
            medians[str(column)] = median
            scales[str(column)] = scale
        fit_range: tuple[str, str] | None = None
        if isinstance(source.index, pd.DatetimeIndex) and len(source):
            fit_range = (str(source.index.min()), str(source.index.max()))
        return cls(medians=medians, scales=scales, n_fit_rows=int(len(source)),
                   fit_range=fit_range)

    def transform(self, features: pd.DataFrame) -> pd.DataFrame:
        """Apply the train-only fill/scale to any (aligned) feature frame."""
        out = pd.DataFrame(index=features.index)
        for column in features.columns:
            name = str(column)
            series = pd.to_numeric(features[column], errors="coerce")
            series = series.fillna(self.medians.get(name, 0.0))
            scale = self.scales.get(name)
            if scale:
                series = (series - self.medians.get(name, 0.0)) / scale
            out[name] = series.astype(float)
        return out

    def config(self) -> dict:
        """Small, secret-free description recorded with every experiment."""
        return {
            "imputation": "train-median",
            "scaling": "train-robust-mad" if any(
                s for s in self.scales.values() if s) else "none",
            "n_fit_rows": self.n_fit_rows,
            "fit_range": list(self.fit_range) if self.fit_range else None,
            "columns": {str(c): {"median": round(self.medians[str(c)], 6),
                                 "scale": (round(self.scales[str(c)], 6)
                                           if self.scales[str(c)] else None)}
                        for c in self.medians},
        }


# --------------------------------------------------------------------------
# Dataset
# --------------------------------------------------------------------------
@dataclasses.dataclass
class LabDataset:
    """Validated, chronological, lineage-recorded telemetry for one source."""

    frame: pd.DataFrame                     # LONG_COLUMNS canonical form
    source_type: str
    source_id: str
    provenance: dict
    lineage: list[LineageStep]
    profile: dict
    warnings: list[str]
    inventory: list[SeriesInventory]
    label_kind: str                         # "none" | "binary" | "multiclass"
    fingerprint: str

    # -- accessors ---------------------------------------------------------
    def series_refs(self) -> list[SeriesRef]:
        return [item.ref for item in self.inventory]

    def devices(self) -> list[str]:
        return sorted({ref.device for ref in self.series_refs()})

    def metrics(self, device: str | None = None) -> list[str]:
        refs = self.series_refs()
        if device:
            refs = [r for r in refs if r.device == device]
        return sorted({r.metric for r in refs})

    def interfaces(self, device: str, metric: str) -> list[str | None]:
        return sorted({r.interface for r in self.series_refs()
                       if r.device == device and r.metric == metric},
                      key=lambda v: (v is not None, v or ""))

    def group(self, ref: SeriesRef) -> pd.DataFrame:
        mask = (
            (self.frame["device"] == ref.device)
            & (self.frame["metric"] == ref.metric)
        )
        if ref.interface is None:
            mask &= self.frame["interface"].isna()
        else:
            mask &= (self.frame["interface"] == ref.interface)
        return self.frame[mask].sort_values("collected_at").reset_index(
            drop=True)

    def series_frame(self, ref: SeriesRef) -> pd.DataFrame:
        """FRAME_COLUMNS contract for one series (drop-in for the detectors).

        ``sample_id`` is a positional 1..n index: lab series are not stored in
        ``metric_samples``, so there is no database id to carry, and the value
        is only used by the detectors for point traceability within one run.
        ``data_source`` holds the lab source_type token, which is deliberately
        outside the operational {"demo","live"} vocabulary.
        """
        rows = self.group(ref)
        values = rows["value"]
        imputed = pd.Series([False] * len(rows), dtype=bool)
        unit = rows["unit"].dropna().iloc[-1] if rows["unit"].notna().any() \
            else None
        frame = pd.DataFrame({
            "sample_id": range(1, len(rows) + 1),
            "collected_at": pd.to_datetime(rows["collected_at"]).to_numpy(),
            "value": pd.to_numeric(values, errors="coerce").to_numpy(
                dtype="float64"),
            "unit": unit,
            "data_source": self.source_type,
            "imputed": imputed.to_numpy(),
        })
        if not frame.empty:
            frame = frame.dropna(subset=["value"]).reset_index(drop=True)
            # Re-number after the NULL drop so ids stay contiguous/unique.
            frame["sample_id"] = range(1, len(frame) + 1)
        return frame[FRAME_COLUMNS]

    def series_values(self, ref: SeriesRef) -> pd.Series:
        """Time-indexed numeric series (NaN-free), the model task input."""
        frame = self.series_frame(ref)
        return pd.Series(frame["value"].to_numpy(),
                         index=pd.DatetimeIndex(frame["collected_at"]),
                         name=ref.metric)

    def series_labels(self, ref: SeriesRef) -> pd.Series | None:
        """Ground-truth labels aligned to ``series_values``; None when absent."""
        if self.label_kind == "none":
            return None
        rows = self.group(ref).dropna(subset=["label"])
        if rows.empty:
            return None
        return pd.Series(rows["label"].to_numpy(),
                         index=pd.DatetimeIndex(rows["collected_at"]),
                         name="label")

    def quality(self, ref: SeriesRef) -> data_quality.SeriesQuality:
        """Per-series Phase 2 quality report (readiness gates reused as-is)."""
        return data_quality.summarize_quality(
            self.series_frame(ref), ref.device, ref.metric,
            interface_name=ref.interface,
            expected_interval_seconds=(
                self.profile.get("median_interval_seconds")
                or float(get_settings().collect_interval_seconds)),
            now=self.profile.get("now"))

    def feature_frame(self, device: str, metrics: Iterable[str], *,
                      interface: str | None = None,
                      min_overlap: int | None = None,
                      ) -> tuple[pd.DataFrame, pd.Series, list[str]]:
        """Time-aligned multivariate features (complete-case rows only).

        Returns ``(features, timestamps, used_metrics)``. Timestamps present in
        only part of the requested metrics are dropped, not imputed; the
        dropped metric list is returned so the UI can state what was excluded.
        """
        wanted = [m for m in metrics]
        known = {item.ref.key for item in self.inventory}
        used: list[str] = []
        series: dict[pd.Timestamp, dict[str, float]] = {}
        for metric in wanted:
            ref = SeriesRef(device, metric, interface)
            if ref.key not in known:
                continue
            values = self.series_values(ref)
            if values.empty:
                continue
            used.append(metric)
            for ts, val in values.items():
                series.setdefault(ts, {})[metric] = float(val)
        if not used:
            return pd.DataFrame(), pd.Series(dtype="datetime64[ns]"), []
        rows = [(ts, record) for ts, record in sorted(series.items())
                if all(m in record for m in used)]
        floor = min_overlap if min_overlap is not None else \
            get_settings().min_points_analysis
        if len(rows) < floor:
            # Too little joint history for a multivariate view: abstain rather
            # than fit a model on a handful of aligned points.
            return (pd.DataFrame(), pd.Series(dtype="datetime64[ns]"), used)
        timestamps = pd.Series([ts for ts, _ in rows])
        features = pd.DataFrame([rec for _, rec in rows],
                                columns=used)
        return features, timestamps, used

    # -- summaries ---------------------------------------------------------
    def summary(self) -> dict:
        """Compact, persistable description (no raw rows, no secrets)."""
        return {
            "source_type": self.source_type,
            "source_id": self.source_id,
            "fingerprint": self.fingerprint,
            "n_rows": int(self.profile.get("n_rows", 0)),
            "n_values": int(self.profile.get("n_values", 0)),
            "n_series": len(self.inventory),
            "devices": self.devices(),
            "metrics": self.metrics(),
            "time_min": _iso(self.profile.get("first_ts")),
            "time_max": _iso(self.profile.get("last_ts")),
            "median_interval_seconds": self.profile.get(
                "median_interval_seconds"),
            "label_kind": self.label_kind,
            "n_labelled_rows": int(self.profile.get("n_labels", 0)),
            "warnings": list(self.warnings),
        }


def _iso(value) -> str | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, pd.Timestamp):
        return value.isoformat() if not value.tz else \
            value.tz_convert(None).isoformat()
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=None).isoformat()
    return str(value)


# --------------------------------------------------------------------------
# Canonicalisation
# --------------------------------------------------------------------------
def canonicalize(
    raw: pd.DataFrame | Iterable[Mapping],
    *,
    source_type: str,
    source_id: str,
    provenance: Mapping | None = None,
    metric_aliases: Mapping[str, str] | None = None,
    column_map: Mapping[str, str] | None = None,
    now: dt.datetime | None = None,
    max_rows: int | None = None,
) -> LabDataset:
    """Validate one raw frame into a ``LabDataset`` with a recorded lineage.

    ``column_map`` maps canonical names -> source column names (the CSV/upload
    adapter builds it from the user's mapping). Everything else is normalised
    here: timestamps parsed to naive UTC, values coerced (unparseable -> NULL,
    never 0), metric aliases applied, rows sorted chronologically, exact
    duplicate timestamps within a series collapsed (last wins, counted) and
    series with conflicting units quarantined.
    """
    if source_type not in SOURCE_TYPES:
        raise LabDataError(f"unknown source_type: {source_type!r}")
    if not source_id:
        raise LabDataError("source_id is required for provenance")

    lineage: list[LineageStep] = []
    warnings: list[str] = []
    df = raw if isinstance(raw, pd.DataFrame) else pd.DataFrame(list(raw))
    if df.empty:
        raise LabDataError("source returned no rows at all")
    if column_map:
        unknown = [src for src in column_map.values() if src not in df.columns]
        if unknown:
            raise LabDataError("mapping refers to absent columns: "
                               + ", ".join(map(str, unknown)))
        df = df.rename(columns={v: k for k, v in column_map.items()})
    missing = [c for c in ("collected_at", "device", "metric")
               if c not in df.columns]
    if missing:
        raise LabDataError("source is missing required column(s): "
                           + ", ".join(missing))
    for optional in ("interface", "value", "unit", "label"):
        if optional not in df.columns:
            df[optional] = None
    df = df[LONG_COLUMNS].copy()
    _lineage(lineage, "read", f"{source_type}:{source_id}", len(df), len(df))

    # -- input bound: refuse an oversized frame instead of silently truncating
    cap = max_rows if max_rows is not None else get_settings().analytics_max_points
    if cap and len(df) > cap:
        raise LabDataError(
            f"source supplied {len(df)} rows, above the configured bound of "
            f"{cap} (NETPILOT_ANALYTICS_MAX_POINTS). Narrow the time range or "
            "reduce the number of series rather than analysing a truncated "
            "sample without saying so.")

    # -- timestamps -------------------------------------------------------
    parsed = parse_timestamps(df["collected_at"])
    bad_ts = int(parsed.isna().sum())
    df = df[parsed.notna()].copy()
    df["collected_at"] = parsed[parsed.notna()].dt.tz_localize(None)
    _lineage(lineage, "timestamp-parse",
             f"{bad_ts} row(s) had an unparseable timestamp and were rejected",
             len(df) + bad_ts, len(df))
    if bad_ts:
        warnings.append(f"{bad_ts} row(s) rejected: unparseable timestamp.")

    # -- identity columns --------------------------------------------------
    for column in ("device", "metric"):
        text = df[column].astype("string").str.strip()
        blank = int((text.isna() | (text == "")).sum())
        if blank:
            df = df[~(text.isna() | (text == ""))].copy()
            warnings.append(f"{blank} row(s) rejected: empty {column}.")
            _lineage(lineage, f"identity-{column}",
                     f"{blank} row(s) had no {column}", len(df) + blank,
                     len(df))
        df[column] = df[column].astype(str).str.strip()

    aliases = {str(k).strip(): str(v).strip()
               for k, v in (metric_aliases or {}).items()}
    if aliases:
        before = df["metric"].copy()
        df["metric"] = df["metric"].map(lambda m: aliases.get(m, m))
        changed = int((before != df["metric"]).sum())
        _lineage(lineage, "metric-alias",
                 f"{len(aliases)} alias rule(s), {changed} row(s) renamed",
                 len(df), len(df))

    def _clean_optional(series: pd.Series) -> pd.Series:
        text = series.astype("object").where(series.notna(), None)
        text = text.map(lambda v: str(v).strip() if v is not None else None)
        return text.map(lambda v: v if v else None)

    df["interface"] = _clean_optional(df["interface"])
    df["unit"] = _clean_optional(df["unit"])

    # -- values ------------------------------------------------------------
    numeric = pd.to_numeric(df["value"], errors="coerce")
    non_numeric = int((df["value"].notna() & numeric.isna()).sum())
    df["value"] = numeric
    _lineage(lineage, "value-coercion",
             f"{non_numeric} non-numeric value(s) kept as NULL (never 0)",
             len(df), len(df))
    if non_numeric:
        warnings.append(f"{non_numeric} row(s) had a non-numeric value; kept "
                        "as missing (NULL), not as zero.")

    # -- labels ------------------------------------------------------------
    df["label"], label_kind = _normalize_labels(df["label"])

    # -- chronology + duplicates -------------------------------------------
    df = df.sort_values(["device", "interface", "metric", "collected_at"],
                        na_position="first").reset_index(drop=True)
    key_cols = ["device", "interface", "metric", "collected_at"]
    dupes = int(df.duplicated(subset=key_cols).sum())
    if dupes:
        df = df.drop_duplicates(subset=key_cols, keep="last").reset_index(
            drop=True)
        warnings.append(f"{dupes} duplicate timestamp(s) collapsed (last "
                        "reading kept).")
    _lineage(lineage, "dedupe", f"{dupes} duplicate row(s) removed",
             len(df) + dupes, len(df))

    # -- unit consistency --------------------------------------------------
    frame, unit_warnings = _enforce_unit_consistency(df, lineage)
    warnings.extend(unit_warnings)

    if frame.empty:
        raise LabDataError("no usable series remained after validation")

    inventory, inv_reasons = _build_inventory(frame, label_kind, now)
    warnings.extend(inv_reasons)
    if not inventory:
        raise LabDataError("every series failed the readiness check: "
                           + "; ".join(inv_reasons[:3]))

    profile = _profile(frame, inventory, now)
    digest = _fingerprint(frame, source_type)
    provenance_out = dict(provenance or {})
    provenance_out.setdefault("source_type", source_type)
    provenance_out.setdefault("source_id", source_id)
    provenance_out.setdefault("label", SOURCE_LABELS[source_type])

    return LabDataset(
        frame=frame, source_type=source_type, source_id=source_id,
        provenance=provenance_out, lineage=lineage, profile=profile,
        warnings=warnings, inventory=inventory, label_kind=label_kind,
        fingerprint=digest)


def _lineage(steps: list[LineageStep], step: str, detail: str,
             rows_in: int, rows_out: int) -> None:
    steps.append(LineageStep(step=step, detail=detail, rows_in=int(rows_in),
                             rows_out=int(rows_out)))


def _normalize_labels(labels: pd.Series) -> tuple[pd.Series, str]:
    """Decide the label semantics for the WHOLE column, not per value.

    A column whose every non-null value belongs to the binary vocabulary (0/1,
    true/false, anomaly/normal) becomes an int 0/1 flag. Anything else keeps
    its class names untouched: silently rewriting "normal" -> 0 would turn a
    multi-class fault label set into a bogus binary one, and the classes are
    exactly what the classification task needs.
    """
    def _text(value):
        if value is None:
            return None
        try:
            # pandas 3 materialises a missing text cell as NaN/NA, which is not
            # None, so the generic check has to come before any str() call.
            if pd.isna(value):
                return None
        except (TypeError, ValueError):  # non-scalar sentinel: not a missing cell
            pass
        if isinstance(value, float) and math.isnan(value):
            return None
        return str(value).strip() or None

    # Built as plain Python lists and returned with an explicit object dtype.
    # A mapped/constructed str column would turn the None sentinels back into
    # NaN and the class-name comparison below would crash on a float.
    cleaned = [_text(v) for v in labels.tolist()]
    present = {v for v in cleaned if v is not None}
    if not present:
        return (pd.Series(cleaned, index=labels.index, dtype="object"), "none")
    lowered = {v.lower() for v in present}
    if all(low in _BINARY_LABELS for low in lowered):
        mapped = [None if v is None else _BINARY_LABELS[v.lower()]
                  for v in cleaned]
        kinds = {v for v in mapped if v is not None}
        if kinds <= {0, 1}:
            return (pd.Series(mapped, index=labels.index, dtype="object"),
                    "binary")
    return (pd.Series(cleaned, index=labels.index, dtype="object"),
            "multiclass")


def binary_truth(dataset: "LabDataset", ref: SeriesRef, *,
                 from_ts=None, until_ts=None) -> dict | None:
    """Anomaly/normal ground truth for one series, or None when undefinable.

    Binary sources map directly. A multi-class source needs an explicit normal
    class name (see ``NORMAL_CLASS_NAMES``); without one the split is refused
    instead of assuming that the majority class is healthy.

    ``from_ts``/``until_ts`` narrow the truth to a time window. They exist so a
    detector that could only afford part of a long series is scored against
    exactly the rows it saw: leaving the denominator at the whole series would
    silently count unseen anomalies as false negatives.
    """
    if dataset.label_kind == "none":
        return None
    rows = dataset.group(ref)
    rows = rows[rows["value"].notna() & rows["label"].notna()]
    window_note = ""
    if from_ts is not None or until_ts is not None:
        from_ts = pd.Timestamp(from_ts) if from_ts is not None else None
        until_ts = pd.Timestamp(until_ts) if until_ts is not None else None
        stamps = pd.DatetimeIndex(rows["collected_at"])
        mask = pd.Series(True, index=rows.index)
        if from_ts is not None:
            mask &= stamps >= from_ts
        if until_ts is not None:
            mask &= stamps <= until_ts
        rows = rows[mask.to_numpy()]
        window_note = (", windowed to "
                       f"{from_ts.isoformat() if from_ts else 'start'}.."
                       f"{until_ts.isoformat() if until_ts else 'end'}")
    if rows.empty:
        return None
    if dataset.label_kind == "binary":
        anomaly = set(rows.loc[rows["label"] == 1, "collected_at"].tolist())
        normal = int((rows["label"] == 0).sum())
        basis = "binary label column"
    else:
        classes = {str(v).lower() for v in rows["label"].tolist()}
        normal_class = sorted(classes & set(NORMAL_CLASS_NAMES))
        if not normal_class:
            return None
        wanted = normal_class[0]
        match = rows["label"].map(lambda v: str(v).lower() == wanted)
        anomaly = set(rows.loc[~match, "collected_at"].tolist())
        normal = int(match.sum())
        basis = f"multi-class labels, normal class '{wanted}'"
    return {"anomaly": anomaly, "normal": normal, "basis": basis + window_note,
            "n_scored": int(len(rows))}


def _enforce_unit_consistency(frame: pd.DataFrame,
                              lineage: list[LineageStep]
                              ) -> tuple[pd.DataFrame, list[str]]:
    """Quarantine series that mix incompatible units instead of averaging them.

    A byte counter and a percentage that happen to share a metric name are not
    one series; silently combining them is the classic way an analytics
    dashboard produces a meaningless chart.
    """
    grouped = frame.groupby(["device", "interface", "metric"], dropna=False)
    keep_masks, warnings = [], []
    for (device, interface, metric), part in grouped:
        # an empty unit cell arrives as a missing value, not as text; counting it
        # as a unit would invent a "mixed units" conflict that does not exist
        units = sorted({u for u in part["unit"].tolist() if isinstance(u, str)
                        and u.strip()})
        if len(units) > 1:
            warnings.append(
                f"{device} · {interface or '(device-level)'} · {metric}: "
                f"mixed units {units}; series excluded from analysis (units "
                "cannot be reconciled without a documented conversion).")
            keep_masks.append(part.index.to_series().isin([]))
            continue
        keep_masks.append(pd.Series(True, index=part.index))
    if not warnings:
        return frame, []
    mask = pd.concat(keep_masks).sort_index().astype(bool)
    kept = frame[mask].reset_index(drop=True)
    _lineage(lineage, "unit-check",
             f"{int((~mask).sum())} row(s) dropped for mixed units",
             len(frame), len(kept))
    return kept, warnings


def _build_inventory(frame: pd.DataFrame, label_kind: str,
                     now: dt.datetime | None
                     ) -> tuple[list[SeriesInventory], list[str]]:
    inventory: list[SeriesInventory] = []
    reasons: list[str] = []
    settings = get_settings()
    reference_now = now or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    for (device, interface, metric), part in frame.groupby(
            ["device", "interface", "metric"], dropna=False):
        ref = SeriesRef(str(device), str(metric),
                        None if interface is None or pd.isna(interface)
                        else str(interface))
        values = part["value"]
        n_values = int(values.notna().sum())
        unit = next((u for u in part["unit"].tolist()
                     if isinstance(u, str) and u.strip()), None)
        interval = median_interval_seconds(
            pd.DataFrame({"collected_at": part["collected_at"],
                          "value": values}))
        ready = {
            TASK_STATISTICS: n_values >= settings.min_points_analysis,
            TASK_ANOMALY: n_values >= settings.min_points_detection,
            TASK_FORECAST: (n_values >= settings.min_points_forecast
                            and interval is not None),
            TASK_CLASSIFICATION: label_kind != "none" and int(
                part["label"].notna().sum()) >= settings.min_points_analysis,
        }
        why: list[str] = []
        if n_values < settings.min_points_detection:
            why.append(f"needs >= {settings.min_points_detection} numeric "
                       f"values for anomaly detection, has {n_values}")
        if n_values < settings.min_points_forecast:
            why.append(f"needs >= {settings.min_points_forecast} numeric "
                       f"values for forecasting, has {n_values}")
        if n_values >= 2 and interval is None:
            why.append("only one distinct timestamp: no sampling interval, "
                       "so no forecast")
        inventory.append(SeriesInventory(
            ref=ref, unit=unit, n_rows=int(len(part)), n_values=n_values,
            n_labels=int(part["label"].notna().sum()),
            first_ts=part["collected_at"].min(),
            last_ts=part["collected_at"].max(), ready=ready, reasons=why))
    return inventory, reasons


def _profile(frame: pd.DataFrame, inventory: list[SeriesInventory],
             now: dt.datetime | None) -> dict:
    reference_now = now or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    intervals = [
        median_interval_seconds(
            pd.DataFrame({"collected_at": part["collected_at"],
                          "value": part["value"]}))
        for _, part in frame.groupby(["device", "interface", "metric"],
                                     dropna=False)
    ]
    usable = [i for i in intervals if i]
    stale = [
        item for item in inventory
        if item.last_ts is not None
        and (reference_now - pd.Timestamp(item.last_ts)).total_seconds()
        > max(4 * (sum(usable) / len(usable) if usable else 0), 1800)
    ]
    return {
        "n_rows": int(len(frame)),
        "n_values": int(frame["value"].notna().sum()),
        "n_missing_values": int(frame["value"].isna().sum()),
        "n_labels": int(frame["label"].notna().sum()),
        "n_series": len(inventory),
        "n_devices": len({item.ref.device for item in inventory}),
        "n_metrics": len({item.ref.metric for item in inventory}),
        "first_ts": frame["collected_at"].min(),
        "last_ts": frame["collected_at"].max(),
        "median_interval_seconds": (float(pd.Series(usable).median())
                                    if usable else None),
        "n_stale_series": len(stale),
        "n_constant_series": sum(1 for item in inventory
                                 if item.n_values >= 2
                                 and _is_constant(frame, item.ref)),
        "now": reference_now,
    }


def _is_constant(frame: pd.DataFrame, ref: SeriesRef) -> bool:
    part = frame[(frame["device"] == ref.device)
                 & (frame["metric"] == ref.metric)]
    if ref.interface is None:
        part = part[part["interface"].isna()]
    else:
        part = part[part["interface"] == ref.interface]
    values = pd.to_numeric(part["value"], errors="coerce").dropna()
    return bool(len(values)) and float(values.std(ddof=0) if len(values) > 1
                                        else 0.0) == 0.0


def _fingerprint(frame: pd.DataFrame, source_type: str) -> str:
    """One-way digest of the validated content (no reversible raw data).

    Stable for identical inputs, changes with any value/timestamp/series
    change, and is short enough to store with every experiment.
    """
    digest = hashlib.sha256()
    digest.update(f"v1|{source_type}|".encode())
    ordered = frame.sort_values(["device", "interface", "metric",
                                 "collected_at"], na_position="first")
    for (device, interface, metric), part in ordered.groupby(
            ["device", "interface", "metric"], dropna=False, sort=True):
        values = pd.to_numeric(part["value"], errors="coerce")
        finite = values.dropna()
        digest.update(
            f"{device}|{interface}|{metric}|{len(part)}|"
            f"{part['collected_at'].iloc[0]}|{part['collected_at'].iloc[-1]}|"
            f"{finite.round(6).sum():.6f}|"
            f"{finite.mul(finite).round(6).sum():.6f}".encode())
    return digest.hexdigest()[:32]


# --------------------------------------------------------------------------
# Splitting helpers (all chronological, train-only fitting)
# --------------------------------------------------------------------------
def split_values(values: pd.Series, *, test_frac: float = 0.25,
                 min_train: int | None = None
                 ) -> tuple[pd.Series, pd.Series]:
    """Contiguous chronological train/test split; never a random shuffle."""
    settings = get_settings()
    series = pd.to_numeric(values, errors="coerce").dropna().sort_index()
    n = len(series)
    train_min = min_train if min_train is not None else \
        settings.min_points_forecast
    if n < train_min + 2:
        raise LabDataError(
            f"chronological split needs >= {train_min + 2} points, has {n}")
    n_test = max(1, int(round(n * test_frac)))
    n_test = min(n_test, n - train_min)
    return series.iloc[: n - n_test], series.iloc[n - n_test:]


def assert_chronological(train: pd.Series, test: pd.Series) -> None:
    """Hard leakage gate: every train timestamp must precede every test one."""
    if len(train) and len(test) and train.index.max() >= test.index.min():
        raise LeakageError("test window is not strictly after train window")


def task_gate(dataset: LabDataset, task: str, *, ref: SeriesRef | None = None
              ) -> tuple[bool, str]:
    """Explain in one sentence whether the dataset can support ``task``."""
    if task not in TASKS:
        raise LabDataError(f"unknown task: {task}")
    items = [item for item in dataset.inventory
             if ref is None or item.ref.key == ref.key]
    if not items:
        return False, "the selected series is not part of this dataset."
    if task == TASK_CLASSIFICATION and dataset.label_kind == "none":
        return False, ("no ground-truth labels exist in this source, so a "
                       "supervised classification cannot be run (and will not "
                       "be faked).")
    ready = [item for item in items if item.ready.get(task)]
    if not ready:
        first = items[0]
        detail = "; ".join(first.reasons) or "series below the task threshold"
        return False, (f"insufficient data for {task}: {detail} "
                       f"(best series has {first.n_values} usable values).")
    return True, f"{len(ready)}/{len(items)} series meet the {task} gate."
