"""AI Lab source adapters: stored data, uploads, local files, live endpoint.

Covers acceptance criteria 8.3 (source validation), 8.4 (the source type is
carried everywhere and never upgraded) and 8.14 (an optional live source stays
disabled until it is configured, and an allowlisted directory is the only
filesystem path this feature can read).

Everything here runs offline: the Prometheus path is exercised through an
injected ``httpx.MockTransport``, so no socket is opened.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

from analytics import lab_dataset, lab_sources
from analytics.lab_sources import (MAX_UPLOAD_COLUMNS, LabDataError)
from services import prometheus_source

BASE = dt.datetime(2026, 10, 1, 0, 0, 0)

#: Long-format upload with one defect per accounting bucket: 20 good rows, 1
#: non-numeric value (kept as missing), 1 unparseable timestamp (rejected) and
#: 1 line that does not match the header shape (skipped by the parser).
LONG_CSV = (
    "timestamp,device,interface,metric,value,unit,label\n"
    + "\n".join(
        f"2026-10-01T00:{i:02d}:00Z,R1,eth0,ping.avg_rtt_ms,{5.0 + i},ms,"
        + ("anomaly" if i >= 15 else "normal")
        for i in range(20))
    + "\n2026-10-01T00:20:00Z,R1,eth0,ping.avg_rtt_ms,oops,ms,normal"
    + "\n2026-10-01Tnot-a-timestamp,R1,eth0,ping.avg_rtt_ms,7,ms,normal"
    + "\nthis,row,has,too,many,fields,to,match,the,header\n"
)

#: Wide-format upload: one row per timestamp, one numeric column per metric.
WIDE_CSV = ("time,host,cpu,mem\n"
            + "\n".join(f"2026-10-01 00:{i:02d}:00,N1,{20 + i},{40 + i}"
                        for i in range(12)))


def seed_store(session, *, live=20, demo=8, interface=None, device="R1",
               metric="ping.avg_rtt_ms", unit="ms"):
    """Record samples exactly the way the collector does."""
    from database import repository
    repository.upsert_device(session, device, type_="router", status="up")
    for i in range(live):
        repository.add_metric_sample(session, device, metric, 5.0 + i,
                                     unit=unit, interface_name=interface,
                                     data_source="live",
                                     collected_at=BASE + dt.timedelta(minutes=i))
    for i in range(demo):
        repository.add_metric_sample(session, device, metric, 1.0 + i,
                                     unit=unit, interface_name=interface,
                                     data_source="demo",
                                     collected_at=BASE + dt.timedelta(minutes=i))
    session.commit()


# ==========================================================================
# B - existing project data
# ==========================================================================
def test_stored_inventory_keeps_each_provenance_as_its_own_row(session):
    seed_store(session)
    rows = lab_sources.stored_inventory(session)
    keyed = {(r["device_name"], r["metric"], r["data_source"]): r["n_samples"]
             for r in rows}
    assert keyed == {("R1", "ping.avg_rtt_ms", "live"): 20,
                     ("R1", "ping.avg_rtt_ms", "demo"): 8}
    assert [r["n_samples"] for r in
            lab_sources.stored_inventory(session, data_source="live")] == [20]


def test_stored_source_reports_collected_data_as_historical(session):
    seed_store(session)
    ds = lab_sources.load_stored_data(
        session, data_source="live",
        series=[{"device_name": "R1", "metric": "ping.avg_rtt_ms"}])
    assert ds.source_type == lab_dataset.SOURCE_HISTORICAL
    assert ds.provenance["data_source"] == "live"
    assert ds.provenance["is_lab_real"] is True
    assert ds.provenance["synthetic"] is False
    assert ds.profile["n_rows"] == 20
    assert ds.source_id.startswith("stored-live-")


def test_demo_rows_never_become_a_lab_measurement(session):
    seed_store(session)
    ds = lab_sources.load_stored_data(
        session, data_source="demo",
        series=[{"device_name": "R1", "metric": "ping.avg_rtt_ms"}])
    assert ds.source_type == lab_dataset.SOURCE_SIMULATED
    assert ds.provenance["is_lab_real"] is False
    assert ds.provenance["synthetic"] is True
    assert "demo_data.py" in ds.provenance["provenance_note"]
    assert ds.profile["n_rows"] == 8
    # the seeded fixture values (1.0..) are kept, the live ones (5.0..) are not
    values = ds.series_values(ds.series_refs()[0])
    assert values.iloc[0] == 1.0


def test_one_dataset_carries_exactly_one_provenance(session):
    seed_store(session)
    with pytest.raises(LabDataError) as exc:
        lab_sources.load_stored_data(
            session, data_source="mixed",
            series=[{"device_name": "R1", "metric": "ping.avg_rtt_ms"}])
    assert "exactly one provenance" in str(exc.value)
    with pytest.raises(LabDataError):
        lab_sources.load_stored_data(session, data_source=None, series=[{}])
    with pytest.raises(LabDataError):
        lab_sources.load_stored_data(session, data_source="live", series=[])
    with pytest.raises(LabDataError):
        lab_sources.load_stored_data(
            session, data_source="live",
            series=[{"device_name": "", "metric": "ping.avg_rtt_ms"}])


def test_stored_telemetry_states_that_it_has_no_ground_truth(session):
    seed_store(session)
    ds = lab_sources.load_stored_data(
        session, data_source="live",
        series=[{"device_name": "R1", "metric": "ping.avg_rtt_ms"}])
    assert ds.profile["n_labels"] == 0
    assert ds.label_kind == "none"
    assert "cannot be computed" in ds.provenance["labels"]
    allowed, reason = lab_dataset.task_gate(ds,
                                           lab_dataset.TASK_CLASSIFICATION)
    assert allowed is False and "label" in reason.lower()


def test_empty_selection_is_reported_instead_of_inventing_a_series(session):
    seed_store(session)
    with pytest.raises(LabDataError) as exc:
        lab_sources.load_stored_data(
            session, data_source="live",
            series=[{"device_name": "R9", "metric": "nothing.here"}])
    assert "no samples" in str(exc.value) and "Nothing is invented" in str(
        exc.value)


def test_time_window_is_applied_at_the_source(session):
    seed_store(session, demo=0)
    series = [{"device_name": "R1", "metric": "ping.avg_rtt_ms"}]
    ds = lab_sources.load_stored_data(
        session, data_source="live", series=series,
        since=BASE + dt.timedelta(minutes=10),
        until=BASE + dt.timedelta(minutes=14))
    assert ds.profile["n_rows"] == 5
    assert ds.provenance["window"]["since"].startswith("2026-10-01T00:10")

    # an interface-scoped series is addressable, not merged into the device
    from database import repository
    for i in range(12):
        repository.add_metric_sample(session, "R1", "if.rx_bps", 100.0 + i,
                                     unit="bps", interface_name="eth1",
                                     data_source="live",
                                     collected_at=BASE + dt.timedelta(minutes=i))
    session.commit()
    scoped = lab_sources.load_stored_data(
        session, data_source="live",
        series=[{"device_name": "R1", "metric": "if.rx_bps",
                 "interface_name": "eth1"}])
    refs = scoped.series_refs()
    assert [(r.device, r.interface, r.metric) for r in refs] == [
        ("R1", "eth1", "if.rx_bps")]


# ==========================================================================
# C - CSV upload: preview -> confirmed mapping -> validated dataset
# ==========================================================================
def test_preview_describes_the_file_without_computing_anything():
    preview = lab_sources.preview_upload(LONG_CSV, filename="long.csv")
    parse = dict(preview["parse"])
    note = parse.pop("rejected_note", "")
    assert parse == {
        "delimiter": "comma", "n_text_lines": 24, "n_parsed_rows": 22,
        "n_rejected_lines": 1, "n_columns": 7}
    assert "counted here rather than dropped" in note
    by_col = {p["column"]: p for p in preview["columns"]}
    assert by_col["timestamp"]["guess"] == "timestamp"
    assert by_col["value"]["guess"] == "numeric"
    assert by_col["device"]["guess"] == "category"
    assert preview["timestamp_candidates"] == ["timestamp"]
    assert preview["numeric_candidates"] == ["value"]
    coverage = preview["timestamp_coverage"]
    assert coverage["median_gap_seconds"] == 60.0
    assert coverage["n_rows"] == 21          # one parsed row has no valid date
    assert len(preview["head"]) == 8
    # the head is text, not a parsed result: nothing has been validated yet
    assert preview["head"][0]["value"] == "5.0"


def test_preview_counts_malformed_lines_and_duplicate_rows():
    text = "ts,device,metric,value\n" + "\n".join(
        f"2026-10-01T00:{i:02d}:00,R1,rtt,{i}" for i in range(6))
    text += "\nextra,columns,here,that,do not,belong\n"
    text += "\n2026-10-01T00:00:00,R1,rtt,0\n"        # duplicate of row 1
    preview = lab_sources.preview_upload(text, filename="messy.csv")
    assert preview["parse"]["n_rejected_lines"] == 1
    assert "counted here" in preview["parse"]["rejected_note"]
    assert preview["duplicate_rows"] == 1


def test_mapping_suggestion_prefers_long_and_falls_back_to_wide():
    long_map = lab_sources.suggest_upload_mapping(
        lab_sources.preview_upload(LONG_CSV, filename="long.csv"))
    assert long_map["format"] == "long"
    assert long_map["timestamp"] == "timestamp"
    assert (long_map["device"], long_map["metric"], long_map["value"],
            long_map["unit"], long_map["label"]) == (
        "device", "metric", "value", "unit", "label")
    assert long_map["metrics"] == []

    wide_map = lab_sources.suggest_upload_mapping(
        lab_sources.preview_upload(WIDE_CSV, filename="wide.csv"))
    assert wide_map["format"] == "wide"
    assert wide_map["timestamp"] == "time" and wide_map["device"] == "host"
    assert wide_map["metrics"] == ["cpu", "mem"]
    assert "confirm or change" in wide_map["notes"]
    # the suggestion is advice: it is never applied without the operator
    assert "Suggested by name/type heuristics" in wide_map["notes"]


def test_upload_validation_reports_retained_versus_rejected_counts():
    mapping = lab_sources.suggest_upload_mapping(
        lab_sources.preview_upload(LONG_CSV, filename="long.csv"))
    ds, report = lab_sources.load_uploaded_csv(LONG_CSV, filename="long.csv",
                                               mapping=mapping)
    assert report["lines_in_file"] == 24
    assert report["rows_parsed"] == 22
    assert report["rows_malformed_skipped"] == 1
    assert report["rows_after_mapping"] == 22
    assert report["rows_retained"] == ds.profile["n_rows"] == 21
    assert report["rows_rejected"] == 1               # the unparseable date
    assert report["values_missing"] == 1              # "oops" stayed missing
    assert report["rows_retained"] + report["rows_rejected"] == \
        report["rows_after_mapping"]
    assert any("unparseable timestamp" in w for w in report["warnings"])
    assert any("kept as missing" in w for w in report["warnings"])
    steps = [(line["step"], line["rows_in"], line["rows_out"])
             for line in report["lineage"]]
    assert ("timestamp-parse", 22, 21) in steps


def test_uploaded_provenance_is_recorded_as_an_unverified_claim():
    ds, _ = lab_sources.load_uploaded_csv(LONG_CSV, filename="long.csv")
    assert ds.source_type == lab_dataset.SOURCE_UPLOADED
    assert ds.source_id.startswith("upload-")
    assert ds.provenance["is_lab_real"] is False
    assert ds.provenance["verified_by_app"] is False
    assert ds.provenance["asserted_by"] == "operator (unverified claim)"
    assert "not verified here" in lab_dataset.SOURCE_LABELS[ds.source_type]
    ref = ds.series_refs()[0]
    # 21 points clear the anomaly gate but not the forecasting gate: the gate
    # says so instead of letting a short series look analysable
    allowed, reason = lab_dataset.task_gate(ds, lab_dataset.TASK_ANOMALY,
                                            ref=ref)
    assert allowed is True and "series meet the anomaly gate" in reason
    allowed, reason = lab_dataset.task_gate(ds, lab_dataset.TASK_FORECAST,
                                            ref=ref)
    assert allowed is False and "insufficient data" in reason


def test_wide_upload_is_melted_and_units_are_taken_from_the_declaration():
    mapping = lab_sources.suggest_upload_mapping(
        lab_sources.preview_upload(WIDE_CSV, filename="wide.csv"))
    ds, report = lab_sources.load_uploaded_csv(
        WIDE_CSV, filename="wide.csv", mapping=mapping,
        units={"cpu": "%", "mem": "%"})
    assert ds.metrics() == ["cpu", "mem"]
    assert report["rows_parsed"] == 12
    assert report["rows_after_mapping"] == 24 == report["rows_retained"]
    assert report["rows_rejected"] == 0
    units = {item.ref.metric: item.unit for item in ds.inventory}
    assert units == {"cpu": "%", "mem": "%"}
    refs = [(r.device, r.metric) for r in ds.series_refs()]
    assert refs == [("N1", "cpu"), ("N1", "mem")]
    # undeclared units stay unknown instead of being guessed from the numbers
    plain, _ = lab_sources.load_uploaded_csv(WIDE_CSV, filename="wide.csv",
                                             mapping=mapping)
    assert all(item.unit is None for item in plain.inventory)


def test_uploads_are_data_and_never_code():
    hostile = (
        "timestamp,device,metric,value\n"
        + "\n".join(
            f"2026-10-01T00:{i:02d}:00,R1,rtt,{i}" for i in range(8))
        + "\n2026-10-01T00:08:00,R1,=cmd|' /C calc'!A0,1"
        + "\n2026-10-01T00:09:00,R1,__import__('os').system('echo pwned'),2\n")
    mapping = {"format": "long", "timestamp": "timestamp", "device": "device",
               "metric": "metric", "value": "value"}
    ds, report = lab_sources.load_uploaded_csv(hostile, filename="x.csv",
                                               mapping=mapping)
    # the hostile cells became metric names (text), never values or commands
    assert "rtt" in ds.metrics()
    assert report["rows_retained"] == 10
    assert any("echo pwned" in name for name in ds.metrics())
    assert ds.profile["n_values"] == 10         # every numeric reading survived


def test_bad_mapping_and_bad_files_are_refused_with_a_reason():
    with pytest.raises(LabDataError) as exc:
        lab_sources.load_uploaded_csv(
            WIDE_CSV, filename="wide.csv",
            mapping={"format": "wide", "timestamp": "time", "metrics": []})
    assert "at least one metric column" in str(exc.value)

    with pytest.raises(LabDataError) as exc:
        lab_sources.load_uploaded_csv(
            WIDE_CSV, filename="wide.csv",
            mapping={"format": "long", "timestamp": "time"})
    assert "metric column" in str(exc.value)

    with pytest.raises(LabDataError) as exc:
        lab_sources.load_uploaded_csv(
            WIDE_CSV, filename="wide.csv",
            mapping={"format": "long", "timestamp": "not-in-file"})
    assert "not in the file" in str(exc.value)

    with pytest.raises(LabDataError) as exc:
        lab_sources.load_uploaded_csv("metric,value\nrtt,1\n",
                                      mapping={"format": "long"})
    assert "timestamp" in str(exc.value)


def test_upload_guards_are_enforced_and_advertised(monkeypatch):
    status = lab_sources.upload_guard_status()
    assert status["max_columns"] == MAX_UPLOAD_COLUMNS
    assert status["max_dataset_rows"] > 0

    wide = "ts," + ",".join(f"m{i}" for i in range(MAX_UPLOAD_COLUMNS + 10)) \
        + "\n" + ",".join(["2026-10-01T00:00:00"]
                          + ["1"] * (MAX_UPLOAD_COLUMNS + 10))
    with pytest.raises(LabDataError) as exc:
        lab_sources.preview_upload(wide, filename="wide.csv")
    assert "columns exceeds the bound" in str(exc.value)

    monkeypatch.setenv("NETPILOT_AI_LAB_UPLOAD_MAX_BYTES", "1024")
    big = "ts,device,metric,value\n" + "\n".join(
        f"2026-10-01T00:{i % 60:02d}:00,R1,rtt-{i},{i}" for i in range(200))
    with pytest.raises(LabDataError) as exc:
        lab_sources.load_uploaded_csv(big, filename="big.csv",
                                      mapping={"format": "long",
                                               "timestamp": "ts",
                                               "device": "device",
                                               "metric": "metric",
                                               "value": "value"})
    assert "NETPILOT_AI_LAB_UPLOAD_MAX_BYTES" in str(exc.value)


@pytest.mark.parametrize("raw,expect", [
    (b"\xff\xfe\x00\x01binary", "not UTF-8"),
    (b"ts,value\n2026-10-01\x00,1\n", "NUL bytes"),
])
def test_binary_uploads_are_refused_rather_than_decoded(raw, expect):
    with pytest.raises(LabDataError) as exc:
        lab_sources.preview_upload(raw, filename="x.bin")
    assert expect in str(exc.value)


def test_delimiter_is_detected_from_the_header():
    text = "ts;device;metric;value\n" + "\n".join(
        f"2026-10-01T00:{i:02d}:00;R1;rtt;{i}.5" for i in range(10))
    preview = lab_sources.preview_upload(text, filename="semi.csv")
    assert preview["parse"]["delimiter"] == "semicolon"
    assert preview["parse"]["n_columns"] == 4
    assert preview["parse"]["n_parsed_rows"] == 10


# ==========================================================================
# B - documented historical import from ONE allowlisted directory
# ==========================================================================
@pytest.fixture()
def lab_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("NETPILOT_AI_LAB_LOCAL_DIR", str(tmp_path))
    (tmp_path / "history.csv").write_text(LONG_CSV, encoding="utf-8")
    (tmp_path / "notes.md").write_text("# not a table\n", encoding="utf-8")
    (tmp_path / "capture.db").write_bytes(b"sqlite format string")
    return tmp_path


def test_local_import_is_disabled_until_the_directory_is_configured(monkeypatch):
    monkeypatch.delenv("NETPILOT_AI_LAB_LOCAL_DIR", raising=False)
    status = lab_sources.local_dir_status()
    assert status["enabled"] is False
    assert "NETPILOT_AI_LAB_LOCAL_DIR" in status["reason"]
    with pytest.raises(LabDataError):
        lab_sources.list_local_files()
    with pytest.raises(LabDataError) as exc:
        lab_sources.load_local_file("whatever.csv")
    assert "disabled" in str(exc.value)


def test_missing_configured_directory_is_reported_not_created(monkeypatch,
                                                              tmp_path):
    monkeypatch.setenv("NETPILOT_AI_LAB_LOCAL_DIR",
                       str(tmp_path / "does-not-exist"))
    status = lab_sources.local_dir_status()
    assert status["enabled"] is False
    assert "does not exist" in status["reason"]


def test_only_the_allowlisted_directory_can_be_read(lab_dir):
    status = lab_sources.local_dir_status()
    assert status["enabled"] is True
    assert status["csv_files"] == ["history.csv"]
    assert "Only files inside this one directory" in status["note"]
    listing = lab_sources.list_local_files()
    assert listing[0]["name"] == "history.csv"
    assert listing[0]["header_columns"] == 7

    with pytest.raises(LabDataError) as exc:
        lab_sources.load_local_file("../history.csv")
    assert "outside the allowlisted directory" in str(exc.value)
    with pytest.raises(LabDataError) as exc:
        lab_sources.load_local_file(lab_dir.parent / "secret.csv")
    assert "outside the allowlisted directory" in str(exc.value)
    with pytest.raises(LabDataError) as exc:
        lab_sources.load_local_file("nope.csv")
    assert "no such file" in str(exc.value)


def test_local_import_is_stamped_historical_with_an_audit_trail(lab_dir):
    mapping = lab_sources.suggest_upload_mapping(
        lab_sources.preview_upload(LONG_CSV, filename="history.csv"))
    ds, report = lab_sources.load_local_file("history.csv", mapping=mapping,
                                             dataset_label="lab capture 2026-10")
    assert ds.source_type == lab_dataset.SOURCE_HISTORICAL
    assert ds.source_id.startswith("local-")
    assert ds.provenance["allowlisted_directory"] == str(lab_dir.resolve())
    assert ds.provenance["is_lab_real"] is False
    assert ds.provenance["verified_by_app"] is False
    assert "documented historical import" in ds.provenance["note"]
    assert "lab capture 2026-10" in ds.provenance["note"]
    assert ds.provenance["imported_at"]
    assert report["rows_retained"] == 21        # same validation as an upload


@pytest.mark.parametrize("name", ["notes.md", "capture.db"])
def test_non_table_files_are_not_imported(lab_dir, name):
    with pytest.raises(LabDataError):
        lab_sources.load_local_file(name)


# ==========================================================================
# D - optional read-only live source (disabled until configured)
# ==========================================================================
@pytest.fixture()
def prom(monkeypatch):
    from services import prometheus_source
    monkeypatch.delenv("NETPILOT_AI_LAB_PROMETHEUS_URL", raising=False)
    return prometheus_source


QUERY_OK = {
    "status": "success",
    "data": {"result": [{
        "metric": {"__name__": "node_cpu_seconds", "instance": "R1:9100",
                   "ifName": "eth0"},
        "values": [[1759280400 + i * 60, str(10 + i)] for i in range(12)]
        + [[1759281120, "NaN"], [1759281180, "not-a-number"], "junk"],
    }]},
}


def make_transport(payload=None, status_code=200, body=None):
    import httpx

    def handler(request):
        if body is not None:
            content = body
        else:
            content = json.dumps(payload).encode()
        return httpx.Response(status_code, content=content)

    return httpx.MockTransport(handler)


def test_live_source_is_disabled_and_says_so(prom):
    status = prom.status()
    assert status["configured"] is False and status["enabled"] is False
    assert "NETPILOT_AI_LAB_PROMETHEUS_URL" in status["reason"]
    assert "unaffected" in status["reason"]
    with pytest.raises(prom.PrometheusError) as exc:
        prom.fetch_range(expression="up", transport=make_transport(QUERY_OK))
    assert "nothing was fetched" in str(exc.value)


def test_configured_live_source_yields_a_live_dataset(prom, monkeypatch):
    monkeypatch.setenv("NETPILOT_AI_LAB_PROMETHEUS_URL",
                       "http://user:secretpass@prom.invalid:9090")
    ds = prom.fetch_range(expression='rate(node_cpu_seconds{job="n"}[5m])',
                          transport=make_transport(QUERY_OK), hours=1.0,
                          end=dt.datetime(2025, 10, 1, 8, 0, 0))
    assert ds.source_type == lab_dataset.SOURCE_LIVE
    assert ds.source_id.startswith("prom-")
    assert ds.provenance["is_lab_real"] is True
    assert ds.provenance["read_only"] is True
    assert "no ground truth" in ds.provenance["labels"]
    refs = [(r.device, r.interface, r.metric) for r in ds.series_refs()]
    assert refs == [("R1:9100", "eth0", "node_cpu_seconds")]
    # Prometheus carries no unit metadata: unknown is recorded as unknown
    assert all(item.unit is None for item in ds.inventory)
    assert "no unit metadata" in ds.provenance["unit_source"]
    # a NaN / non-numeric / malformed point is missing, never 0
    assert ds.profile["n_missing_values"] == 2
    values = ds.series_values(ds.series_refs()[0])
    assert 0.0 not in [v for v in values if v == v]
    assert "3 point(s) were unusable" in ds.provenance["dropped_points_note"]
    # credentials embedded in the configured URL are never displayed
    assert "secretpass" not in json.dumps(ds.provenance, default=str)
    assert "secretpass" not in prom.status()["base_url"]
    assert "prom.invalid" in prom.status()["base_url"]
    redacted = prom.redact("http://user:secretpass@prom.invalid:9090")
    assert "secretpass" not in redacted and "user" not in redacted
    assert "prom.invalid" in redacted

    declared = prom.fetch_range(expression="node_cpu_seconds",
                                transport=make_transport(QUERY_OK), hours=1.0,
                                unit_default="s",
                                end=dt.datetime(2025, 10, 1, 8, 0, 0))
    assert [item.unit for item in declared.inventory] == ["s"]
    assert declared.provenance["unit_source"] == "declared by the operator"


def test_health_check_reports_identity_prom(prom, monkeypatch):
    monkeypatch.setenv("NETPILOT_AI_LAB_PROMETHEUS_URL", "http://prom.invalid")
    payload = {"status": "success", "data": {"version": "2.53.1"}}
    health = prom.health(transport=make_transport(payload))
    assert health["reachable"] is True
    assert health["build_version"] == "2.53.1"
    assert "prom.invalid" in health["base_url"]


@pytest.mark.parametrize("case", [
    ("http-500", dict(status_code=500, body=b"boom"), "HTTP 500"),
    ("not-json", dict(body=b"<html>nope</html>"), "not valid JSON"),
    ("too-big", dict(body=b"x" * (prometheus_source.MAX_BODY_BYTES + 10)),
     "above the bound"),
    ("empty-result", dict(payload={"status": "success",
                                   "data": {"result": []}}),
     "matched no data points"),
    ("error-status", dict(payload={"status": "error", "error": "bad"}),
     "status 'error'"),
    ("bad-shape", dict(payload={"status": "success",
                                "data": {"result": "not-a-list"}}),
     "unexpected 'result' shape"),
])
def test_live_failures_are_reported_instead_of_invented(prom, monkeypatch,
                                                        case):
    monkeypatch.setenv("NETPILOT_AI_LAB_PROMETHEUS_URL", "http://prom.invalid")
    _label, kwargs, expect = case
    with pytest.raises(prom.PrometheusError) as exc:
        prom.fetch_range(expression="up", transport=make_transport(**kwargs))
    assert expect in str(exc.value)
    # the failure text always says what was NOT used, never a silent success
    assert str(exc.value)


@pytest.mark.parametrize("kwargs,expect", [
    ({"expression": ""}, "expression is required"),
    ({"expression": "x" * 500}, "above the bound"),
    ({"expression": "up", "step_seconds": 0}, "step_seconds"),
    ({"expression": "up", "hours": 0}, "hours must be > 0"),
])
def test_invalid_live_requests_are_refused_before_any_request(prom, monkeypatch,
                                                              kwargs, expect):
    monkeypatch.setenv("NETPILOT_AI_LAB_PROMETHEUS_URL", "http://prom.invalid")
    with pytest.raises(prom.PrometheusError) as exc:
        prom.fetch_range(transport=make_transport(QUERY_OK), **kwargs)
    assert expect in str(exc.value)


def test_live_response_bounds_are_enforced(prom, monkeypatch):
    monkeypatch.setenv("NETPILOT_AI_LAB_PROMETHEUS_URL", "http://prom.invalid")
    many = {"status": "success", "data": {"result": [
        {"metric": {"instance": f"R{i}"},
         "values": [[1759280400, "1.0"], [1759280460, "2.0"]]}
        for i in range(prom.MAX_SERIES + 1)]}}
    with pytest.raises(prom.PrometheusError) as exc:
        prom.fetch_range(expression="up", transport=make_transport(many))
    assert "above the bound of" in str(exc.value)

    long_series = {"status": "success", "data": {"result": [{
        "metric": {"instance": "R1"},
        "values": [[1759280400 + i, "1.0"]
                   for i in range(prom.MAX_POINTS_PER_SERIES + 1)]}]}}
    with pytest.raises(prom.PrometheusError) as exc:
        prom.fetch_range(expression="up", transport=make_transport(long_series))
    assert "points, above the bound" in str(exc.value)


def test_live_label_values_stay_inert_text(prom, monkeypatch):
    """A hostile label value is stored as a name, never executed or pathed."""
    monkeypatch.setenv("NETPILOT_AI_LAB_PROMETHEUS_URL", "http://prom.invalid")
    hostile = {"status": "success", "data": {"result": [{
        "metric": {"instance": "$(whoami); ../../etc/passwd | id",
                   "ifName": "`echo pwned`"},
        "values": [[1759280400, "1.0"], [1759280460, "2.0"]],
    }]}}
    ds = prom.fetch_range(expression="up", transport=make_transport(hostile),
                          hours=1.0, end=dt.datetime(2025, 10, 1, 8, 0, 0))
    ref = ds.series_refs()[0]
    assert ref.device == "$(whoami); ../../etc/passwd | id"
    assert ref.interface == "`echo pwned`"
    assert ds.profile["n_rows"] == 2      # and it was still only data
