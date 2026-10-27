"""AI Lab runner, history and exports: the record must be the computation.

Covers acceptance criteria 12 (history persists and retrieves valid metadata),
13 (exports carry the actual computed results and accurate source labels),
14 (a missing external service cannot break the simulation or CSV workflow) and
15 (the isolated-lab execution and change-loop gates stay disabled and the lab
code never reaches the execution layer).

Everything here is offline: no Prometheus, no provider API, no containerlab.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import subprocess
import sys

import pytest

from analytics import lab_dataset, lab_simulator, lab_sources
from config import get_settings
from database import repository
from services import lab_experiment, lab_tasks, prometheus_source

LABEL_METRIC = "ping.avg_rtt_ms"
FEATURE_METRICS = ("if.rx_bps", "if.tx_bps", LABEL_METRIC, "ping.loss_pct",
                   "cpu.usage_pct")
CLS_FEATURES = ("if.rx_bps", "if.tx_bps", "ping.loss_pct", "cpu.usage_pct")
BASE = dt.datetime(2026, 10, 1)

CSV_ROWS = ("timestamp,device,metric,value,unit\n" + "\n".join(
    f"2026-10-01T00:{i:02d}:00Z,R1,ping.avg_rtt_ms,{8.0 + i % 5},ms"
    for i in range(40)) + "\n")


@pytest.fixture(scope="module")
def sim() -> lab_dataset.LabDataset:
    return lab_simulator.simulate(
        devices=("R1", "R2"), duration_hours=6.0, interval_seconds=120,
        seed=3, events={"congestion": 2, "packet_loss": 2, "latency_spike": 2},
        metrics=FEATURE_METRICS)


@pytest.fixture()
def uploaded() -> lab_dataset.LabDataset:
    dataset, _report = lab_sources.load_uploaded_csv(CSV_ROWS,
                                                     filename="rtt.csv")
    return dataset


def ref(dataset, device, metric, interface="eth0"):
    return lab_dataset.SeriesRef(device, metric, interface)


def run(session, dataset, task, *, refs=None, params=None, persist=True,
        seed=None):
    return lab_experiment.run_experiment(session, dataset, task, refs=refs,
                                        params=params, persist=persist,
                                        seed=seed, created_by="tester")


def stored(session, payload) -> dict:
    row = lab_experiment.get_experiment(session, payload["meta"]["experiment_uuid"])
    assert row is not None, "the run did not persist a retrievable row"
    return row


# ==========================================================================
# §5 runner: one entry point, no silent parameter loss
# ==========================================================================
def test_run_experiment_returns_protocol_result_and_a_persisted_row(session, sim):
    refs = [ref(sim, "R1", "if.rx_bps")]
    out = run(session, sim, lab_dataset.TASK_ANOMALY, refs=refs,
              params={"detectors": ("rolling_mad",)}, seed=7)
    assert set(out) == {"meta", "result", "row_id"}
    assert out["row_id"]
    meta, result = out["meta"], out["result"]
    assert meta["task"] == lab_dataset.TASK_ANOMALY
    assert meta["status"] == result["status"] == "ok"
    assert meta["runtime_ms"] >= 0
    assert meta["seed"] == 7
    assert meta["created_by"] == "tester"
    assert meta["schema_version"] == "lab-1"
    # §5 checklist, in the record rather than only on screen
    for key in ("experiment_uuid", "created_at", "source_type", "source_id",
                "dataset_fingerprint", "time_range", "sample_counts",
                "features", "split", "preprocessing", "metrics", "warnings",
                "median_interval_seconds", "label_kind"):
        assert key in meta, f"metadata is missing {key}"
    assert meta["sample_counts"]["rows"] > 0
    assert meta["time_range"]["start"] and meta["time_range"]["end"]

    row = stored(session, out)
    assert row["task"] == lab_dataset.TASK_ANOMALY
    assert row["status"] == "ok"
    assert row["dataset_fingerprint"] == meta["dataset_fingerprint"]
    assert row["seed"] == 7
    assert row["data_source"] == "demo" and row["is_synthetic"] is True
    assert row["json"]["metrics"] == json.loads(
        json.dumps(result["metrics"], default=str))
    assert row["json"]["features"] == result["effective_features"]


def test_persist_false_computes_the_run_without_writing_history(session, sim):
    refs = [ref(sim, "R1", "if.rx_bps")]
    assert lab_experiment.history(session) == []
    out = run(session, sim, lab_dataset.TASK_FORECAST, refs=refs,
              params={"horizon": 4, "models": ("naive",)}, persist=False)
    assert out["result"]["status"] == "ok"
    assert out["row_id"] is None
    assert lab_experiment.history(session) == []


def test_unknown_task_is_rejected_with_the_supported_list(session, sim):
    with pytest.raises(ValueError, match="unknown lab task"):
        lab_experiment.run_experiment(session, sim, "send_traffic")
    assert lab_experiment.history(session) == []


def test_unknown_parameter_raises_instead_of_being_silently_dropped(session, sim):
    refs = [ref(sim, "R1", "if.rx_bps")]
    with pytest.raises(ValueError, match="not accepted by the anomaly task"):
        run(session, sim, lab_dataset.TASK_ANOMALY, refs=refs,
            params={"mad_threshhold": 3.0})
    assert lab_experiment.history(session) == []


def test_accepted_params_reflect_the_real_task_signatures():
    assert {"detectors", "window", "sweep_curve"} <= set(
        lab_experiment.accepted_params(lab_dataset.TASK_ANOMALY))
    assert {"horizon", "models", "ensemble"} <= set(
        lab_experiment.accepted_params(lab_dataset.TASK_FORECAST))
    assert {"device", "label_metric", "feature_metrics", "split_mode"} <= set(
        lab_experiment.accepted_params(lab_dataset.TASK_CLASSIFICATION))
    # "dataset"/"refs" are positional plumbing, not operator knobs
    for task in lab_dataset.TASKS:
        names = lab_experiment.accepted_params(task)
        assert "dataset" not in names and "refs" not in names
    assert lab_experiment.accepted_params("nonsense") == []


def test_same_seed_and_same_source_reproduce_the_same_numbers(session, sim):
    refs = [ref(sim, "R1", m) for m in ("if.rx_bps", LABEL_METRIC)]
    first = run(session, sim, lab_dataset.TASK_ANOMALY, refs=refs, seed=5,
                params={"detectors": ("rolling_mad", "rolling_zscore")})
    second = run(session, sim, lab_dataset.TASK_ANOMALY, refs=refs, seed=5,
                params={"detectors": ("rolling_mad", "rolling_zscore")})
    a, b = first["result"], second["result"]
    assert a["metrics"] == b["metrics"]
    assert a["table"] == b["table"]
    assert a["points"] == b["points"]
    # the identity that changed is the run, not the measurement
    assert (first["meta"]["experiment_uuid"]
            != second["meta"]["experiment_uuid"])
    assert (first["meta"]["dataset_fingerprint"]
            == second["meta"]["dataset_fingerprint"])
    forecast = run(session, sim, lab_dataset.TASK_FORECAST,
                   refs=[ref(sim, "R1", "if.rx_bps")],
                   params={"horizon": 4, "models": ("naive",)})
    again = run(session, sim, lab_dataset.TASK_FORECAST,
               refs=[ref(sim, "R1", "if.rx_bps")],
               params={"horizon": 4, "models": ("naive",)})
    assert forecast["result"]["table"] == again["result"]["table"]


def test_the_runner_classifies_when_given_a_complete_label_protocol(session, sim):
    out = run(session, sim, lab_dataset.TASK_CLASSIFICATION, params={
        "device": "R1", "interface": "eth0", "feature_metrics": CLS_FEATURES,
        "label_metric": LABEL_METRIC, "split_mode": "stratified"})
    result = out["result"]
    assert result["status"] == "ok", result["message"]
    row = stored(session, out)
    assert row["target"] == LABEL_METRIC
    assert row["source_type"] == lab_dataset.SOURCE_SIMULATED
    assert LABEL_METRIC not in row["json"]["features"]
    assert row["json"]["config"]["task_config"]["split_mode"] == "stratified"


def test_classification_without_a_device_is_refused_and_recorded(session, sim):
    out = run(session, sim, lab_dataset.TASK_CLASSIFICATION,
              params={"label_metric": LABEL_METRIC})
    assert out["meta"]["status"] == "refused"
    assert out["result"]["metrics"] == {"reason": "no_device"}
    assert any("Precondition: device" in w for w in out["result"]["warnings"])
    row = stored(session, out)
    assert row["status"] == "refused"
    # a refusal is a stored outcome, not a silently missing run
    assert row["message"] and "device" in row["message"]


def test_series_selection_bound_is_disclosed_in_the_record(session, sim,
                                                           monkeypatch):
    monkeypatch.setenv("NETPILOT_SCAN_MAX_SERIES", "2")
    out = run(session, sim, lab_dataset.TASK_STATISTICS)
    assert len(out["meta"]["series"]) == 2
    assert len(sim.series_refs()) > 2
    note = out["meta"]["series_selection"]
    assert "NETPILOT_SCAN_MAX_SERIES" in note
    assert "keeps one run finite" in note
    assert "NETPILOT_SCAN_MAX_SERIES" in json.dumps(
        stored(session, out)["json"]["config"])


# ==========================================================================
# criterion 12: history persists and retrieves valid metadata
# ==========================================================================
def test_history_lists_runs_and_filters_by_task_and_source(session, sim,
                                                           uploaded):
    run(session, sim, lab_dataset.TASK_ANOMALY,
        refs=[ref(sim, "R1", "if.rx_bps")],
        params={"detectors": ("rolling_mad",)})
    run(session, uploaded, lab_dataset.TASK_STATISTICS,
        refs=uploaded.series_refs())
    everything = lab_experiment.history(session)
    assert len(everything) == 2
    assert {row["source_type"] for row in everything} == {
        lab_dataset.SOURCE_SIMULATED, lab_dataset.SOURCE_UPLOADED}
    only_uploaded = lab_experiment.history(
        session, source_type=lab_dataset.SOURCE_UPLOADED)
    assert [row["source_type"] for row in only_uploaded] == [
        lab_dataset.SOURCE_UPLOADED]
    only_stats = lab_experiment.history(session, task=lab_dataset.TASK_STATISTICS)
    assert {row["task"] for row in only_stats} == {lab_dataset.TASK_STATISTICS}
    assert lab_experiment.history(session, limit=1)[0]["experiment_uuid"] in {
        row["experiment_uuid"] for row in everything}
    assert everything[0]["created_at"] >= everything[1]["created_at"]


def test_stored_row_carries_the_protocol_and_never_the_raw_samples(session, sim):
    from database.models import MetricSample

    refs = [ref(sim, "R1", "if.rx_bps")]
    samples_before = session.query(MetricSample).count()
    out = run(session, sim, lab_dataset.TASK_FORECAST, refs=refs,
              params={"horizon": 4, "models": ("naive",)})
    row = stored(session, out)
    assert session.query(MetricSample).count() == samples_before
    # the row describes the data; it does not copy it
    blob = json.dumps({k: v for k, v in row.items() if k != "json"},
                      default=str)
    for value in list(sim.series_values(refs[0]).to_numpy())[:20]:
        assert str(value) not in blob
    assert set(row["json"]) >= {"features", "split", "preprocessing", "config",
                               "metrics", "table", "warnings", "points"}
    assert row["schema_version_known"] is True


def test_points_are_stored_within_budget_and_dropped_overtly_when_too_large(
        session, sim, monkeypatch):
    refs = [ref(sim, "R1", m) for m in ("if.rx_bps", LABEL_METRIC)]
    out = run(session, sim, lab_dataset.TASK_ANOMALY, refs=refs)
    row = stored(session, out)
    assert row["points_stored"] is True
    assert row["json"]["points"] == out["result"]["points"]

    monkeypatch.setattr(lab_experiment, "MAX_POINTS_JSON_CHARS", 32)
    clipped = run(session, sim, lab_dataset.TASK_ANOMALY, refs=refs)
    assert clipped["meta"]["points_stored"] is False
    second = stored(session, clipped)
    assert second["json"]["points"] is None
    assert second["points_stored"] is False
    # the numbers survive even when the plot payload does not
    assert second["json"]["metrics"] == clipped["result"]["metrics"]
    assert second["json"]["table"] == json.loads(
        json.dumps(clipped["result"]["table"], default=str))


def test_a_corrupt_json_cell_is_reported_rather_than_guessed_at(session, sim):
    out = run(session, sim, lab_dataset.TASK_STATISTICS,
              refs=[ref(sim, "R1", "if.rx_bps")])
    row = repository.get_lab_experiment(
        session, out["meta"]["experiment_uuid"])
    row.metrics_json = "{this is not json"
    session.commit()
    recovered = lab_experiment.get_experiment(
        session, out["meta"]["experiment_uuid"])
    assert recovered["json"]["metrics"] is None
    assert "metrics_json" in recovered["json"]["parse_errors"]


# ==========================================================================
# criterion 13: exports contain the actual computed results and labels
# ==========================================================================
def test_table_csv_carries_the_exact_computed_numbers(session, sim):
    refs = [ref(sim, "R1", "if.rx_bps")]
    out = run(session, sim, lab_dataset.TASK_FORECAST, refs=refs,
              params={"horizon": 4, "models": ("naive", "holt_damped"),
                      "ensemble": False})
    text = lab_experiment.export_table_csv(out["result"])
    rows = list(csv.reader(io.StringIO(text)))
    header = rows[1]
    body = [dict(zip(header, r)) for r in rows[2:]]
    assert {b["model"] for b in body} == {"naive", "holt_damped"}
    for row in out["result"]["table"]:
        match = next(b for b in body if b["model"] == row["model"])
        assert match["status"] == row["status"]
        assert match["mae"] == str(row["mae"])
        assert match["rmse"] == str(row["rmse"])
        assert match["n_test"] == str(row["n_test"])
        assert match["n_train"] == str(row["n_train"])
    assert "# no table rows" not in text


def test_table_csv_of_an_empty_table_says_so_instead_of_inventing_rows():
    refused = lab_tasks.TaskResult.refused(lab_dataset.TASK_FORECAST,
                                           "insufficient_data",
                                           "needs >= 30 points")
    text = lab_experiment.export_table_csv(refused)
    assert text.startswith("# no table rows")
    assert "insufficient_data" in lab_experiment.export_metrics_csv(refused)


def test_metrics_csv_flattens_status_metrics_protocol_and_warnings(session, sim):
    out = run(session, sim, lab_dataset.TASK_STATISTICS,
              refs=[ref(sim, "R1", "if.rx_bps")])
    rows = list(csv.DictReader(io.StringIO(
        lab_experiment.export_metrics_csv(out["result"]))))
    by_section = {s: [r for r in rows if r["section"] == s]
                  for s in ("task", "metrics", "config", "warnings")}
    assert by_section["task"][0]["value"] == lab_dataset.TASK_STATISTICS
    assert {r["key"] for r in by_section["metrics"]} == set(
        out["result"]["metrics"])
    assert by_section["config"]
    assert by_section["warnings"], "the causation disclaimer must be exported"
    assert any("causation" in r["value"].lower() for r in by_section["warnings"])


def test_markdown_report_puts_provenance_before_numbers(session, sim):
    out = run(session, sim, lab_dataset.TASK_FORECAST,
              refs=[ref(sim, "R1", "if.rx_bps")],
              params={"horizon": 4, "models": ("naive",)})
    text = lab_experiment.export_markdown(sim, out["result"],
                                         meta=out["meta"])
    assert text.index("Where the data came from") < text.index("## 3. Results")
    assert lab_dataset.SOURCE_LABELS[lab_dataset.SOURCE_SIMULATED].split(
        " (")[0] in text
    assert "NOT real-network traffic" in text
    assert out["meta"]["dataset_fingerprint"] in text
    assert out["meta"]["experiment_uuid"] in text
    for key in out["result"]["metrics"]:
        assert f"| {key} |" in text
    assert "## 5. Safety statement" in text
    assert "No device configuration was proposed" in text
    # the plotted payload is described as it was stored, not implied
    assert "Every plotted point is a computed result of this run" in text


def test_uploaded_source_is_labelled_as_operator_asserted_in_exports(session,
                                                                     uploaded):
    out = run(session, uploaded, lab_dataset.TASK_STATISTICS,
              refs=uploaded.series_refs())
    text = lab_experiment.export_markdown(uploaded, out["result"],
                                        meta=out["meta"])
    assert "User-uploaded file" in text
    assert "not verified here" in text
    assert "NOT real-network traffic" not in text
    assert out["meta"]["is_synthetic"] is False
    assert out["meta"]["data_source"] == "live"


def test_html_export_escapes_hostile_names_and_keeps_the_report_readable(
        session, sim):
    hostile = lab_dataset.canonicalize(
        [{"collected_at": BASE + dt.timedelta(minutes=i),
          "device": "<script>alert(1)</script>", "interface": "eth0",
          "metric": "rtt", "value": float(i % 6), "unit": "ms"}
         for i in range(40)],
        source_type=lab_dataset.SOURCE_UPLOADED, source_id="hostile")
    out = run(session, hostile, lab_dataset.TASK_STATISTICS,
              refs=hostile.series_refs())
    html_text = lab_experiment.export_html(hostile, out["result"],
                                           meta=out["meta"])
    assert "<script>alert" not in html_text
    assert "&lt;script&gt;" in html_text
    assert html_text.startswith("<!DOCTYPE html>")
    assert "<table>" in html_text and "</tbody></table>" in html_text


def test_csv_export_neutralises_spreadsheet_formula_injection():
    hostile = lab_tasks.TaskResult(
        task=lab_dataset.TASK_STATISTICS, status="ok", message="m",
        table=[{"series": "=cmd|'/C calc'!A0", "mean": "+1+1",
                "median": "-2", "n": 5, "model": "@SUM(1+9)"},
               {"series": "ping.rtt_ms", "mean": -3.5, "n": 9}])
    text = lab_experiment.export_table_csv(hostile)
    parsed = list(csv.reader(io.StringIO(text)))
    flat = {cell for row in parsed for cell in row}
    assert "'=cmd|'/C calc'!A0" in flat
    assert "'+1+1" in flat and "'@SUM(1+9)" in flat
    # a genuinely numeric cell keeps its sign so the file stays usable
    assert "-3.5" in flat and "-2" in flat
    assert not any(cell.startswith("=") for cell in flat)


def test_restore_result_round_trips_the_stored_payload(session, sim):
    out = run(session, sim, lab_dataset.TASK_ANOMALY,
              refs=[ref(sim, "R1", "if.rx_bps")],
              params={"detectors": ("rolling_mad",)})
    rebuilt = lab_experiment.restore_result(out["result"])
    assert isinstance(rebuilt, lab_tasks.TaskResult)
    assert rebuilt.as_dict() == out["result"]
    # exports work from the stored row too, without the original object
    row = stored(session, out)
    from_payload = lab_experiment.restore_result({
        "task": row["task"], "status": row["status"], "message": row["message"],
        "metrics": row["json"]["metrics"], "table": row["json"]["table"],
        "warnings": row["json"]["warnings"], "config": {}, "artifacts": {}})
    assert "rolling_mad" in lab_experiment.export_table_csv(from_payload)


# ==========================================================================
# criterion 14: no external service is required for simulation or CSV work
# ==========================================================================
def test_simulation_and_csv_workflows_run_with_no_external_service(
        session, sim, uploaded, monkeypatch):
    for name in ("NETPILOT_AI_LAB_PROMETHEUS_URL",
                 "NETPILOT_AI_LAB_LOCAL_DIR", "GROQ_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    live = prometheus_source.status()
    assert live["configured"] is False and live["enabled"] is False
    assert "NETPILOT_AI_LAB_PROMETHEUS_URL" in live["reason"]
    assert "unaffected" in live["reason"]
    assert lab_sources.upload_guard_status()["max_upload_bytes"] > 0

    for dataset, expected in ((sim, lab_dataset.SOURCE_SIMULATED),
                             (uploaded, lab_dataset.SOURCE_UPLOADED)):
        for task in (lab_dataset.TASK_ANOMALY, lab_dataset.TASK_FORECAST,
                     lab_dataset.TASK_STATISTICS):
            out = run(session, dataset, task, refs=dataset.series_refs()[:1])
            assert out["meta"]["source_type"] == expected
            assert out["meta"]["status"] in ("ok", "insufficient_data")
    # the uploaded file has no labels, so classification is refused - not the
    # workflow that fails
    cls = run(session, uploaded, lab_dataset.TASK_CLASSIFICATION,
              params={"device": "R1", "label_metric": "ping.avg_rtt_ms"})
    assert cls["meta"]["status"] == "no_labels"


def test_lab_workflows_do_not_call_out_when_subprocess_is_poisoned(session, sim,
                                                                    uploaded,
                                                                    monkeypatch):
    """Nothing in the lab path shells out: any subprocess call is a defect."""
    def explode(*args, **kwargs):
        raise AssertionError("the lab must not execute anything")

    monkeypatch.setattr(subprocess, "run", explode, raising=False)
    monkeypatch.setattr(subprocess, "Popen", explode, raising=False)
    for dataset in (sim, uploaded):
        for task in (lab_dataset.TASK_ANOMALY, lab_dataset.TASK_FORECAST,
                     lab_dataset.TASK_STATISTICS):
            out = run(session, dataset, task, refs=dataset.series_refs()[:1],
                      persist=False)
            assert out["meta"]["runtime_ms"] >= 0


def test_lab_modules_never_import_the_execution_layer():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    files = sorted((root / "analytics").glob("lab_*.py")) + sorted(
        (root / "services").glob("lab_*.py")) + [
        root / "services" / "prometheus_source.py",
        root / "pages" / "12_AI_Lab.py"]
    assert files, "the lab modules must exist to be scanned"
    banned = ("lab_executor", "containerlab", "change_service",
              "change_execution", "ops_center_actions", "netmiko",
              "napalm", "paramiko", "subprocess")
    for path in files:
        source = path.read_text(encoding="utf-8")
        for token in banned:
            assert token not in source, f"{path.name} references {token}"


def test_importing_the_lab_does_not_load_the_execution_layer():
    """Same claim in a clean interpreter, where import order cannot hide it.

    The rest of the test suite exercises the lab executor on purpose, so a
    process-wide ``sys.modules`` check here would say nothing; a fresh
    interpreter proves the lab packages do not pull it in themselves.
    """
    from pathlib import Path

    code = (
        "import sys\n"
        "mods = ['analytics.lab_dataset', 'analytics.lab_simulator',\n"
        "        'analytics.lab_sources', 'analytics.lab_classification',\n"
        "        'services.lab_tasks', 'services.lab_experiment']\n"
        "__import__('analytics'); [__import__(m) for m in mods]\n"
        "print(sorted(m for m in sys.modules if 'lab_executor' in m or\n"
        "             'containerlab' in m))\n"
    )
    proc = subprocess.run([sys.executable, "-c", code],
                          cwd=str(Path(__file__).resolve().parents[1]),
                          capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr[-400:]
    assert proc.stdout.strip() == "[]", proc.stdout


# ==========================================================================
# criterion 15: the safety gates stay exactly as configured
# ==========================================================================
def test_execution_gates_are_disabled_before_and_after_every_task(session, sim):
    before = (get_settings().change_loop_enabled,
              get_settings().lab_execution_enabled)
    assert before == (False, False), (
        "these tests assume the default-off posture the brief requires")
    refs = sim.series_refs()[:1]
    run(session, sim, lab_dataset.TASK_ANOMALY, refs=refs)
    run(session, sim, lab_dataset.TASK_FORECAST, refs=refs)
    run(session, sim, lab_dataset.TASK_STATISTICS, refs=refs)
    run(session, sim, lab_dataset.TASK_CLASSIFICATION, params={
        "device": "R1", "interface": "eth0", "feature_metrics": CLS_FEATURES,
        "label_metric": LABEL_METRIC})
    after = (get_settings().change_loop_enabled,
             get_settings().lab_execution_enabled)
    assert after == (False, False)
    # and the record says so instead of staying silent about it
    text = lab_experiment.export_markdown(
        sim, lab_tasks.TaskResult(task="anomaly", status="ok", message="m"),
        meta=lab_experiment.build_meta(sim, lab_tasks.TaskResult(
            task="anomaly", status="ok", message="m"), params={},
            runtime_ms=0, seed=None, refs=refs))
    assert "both disabled by default" in text


def test_text_that_looks_like_a_device_command_stays_inert_data(session, sim):
    """§7: an uploaded value or name that reads like config is never executed.

    It is either kept as inert text (a series name) or rejected as a
    non-numeric value, and the run still cannot reach an executor.
    """
    rows = [{"collected_at": BASE + dt.timedelta(minutes=i), "device": "R1",
             "interface": "eth0", "metric": "shutdown "
             "interface ethernet 1", "value": float(i % 4), "unit": "count"}
            for i in range(40)]
    rows.append({"collected_at": BASE + dt.timedelta(minutes=40),
                 "device": "R1", "interface": "eth0",
                 "metric": "shutdown interface ethernet 1",
                 "value": "no ip route 0.0.0.0 0.0.0.0 10.0.0.1", "unit": ""})
    dataset = lab_dataset.canonicalize(rows,
                                      source_type=lab_dataset.SOURCE_UPLOADED,
                                      source_id="hostile-values")
    # the non-numeric cell never becomes a measured value
    assert dataset.profile["n_missing_values"] >= 1
    out = run(session, dataset, lab_dataset.TASK_STATISTICS,
              refs=dataset.series_refs())
    text = lab_experiment.export_markdown(dataset, out["result"],
                                         meta=out["meta"])
    assert "no ip route" not in text
    assert out["meta"]["status"] in ("ok", "insufficient_data")
    assert get_settings().lab_execution_enabled is False
    assert get_settings().change_loop_enabled is False
