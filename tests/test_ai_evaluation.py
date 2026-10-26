"""B4 offline AI-quality evaluation tests.

These tests exercise the evaluation harness itself: dataset validation, metric
math, report scope, evaluator isolation and the safety properties the report
must never overstate. They do **not** call a provider or touch the network.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from evaluation import runner
from tools import ai_eval_safety_scan, secret_scan_check

ROOT = Path(__file__).resolve().parents[1]
DATASET_PATH = ROOT / "evaluation" / "datasets" / "ai_quality_v1.json"


@pytest.fixture(scope="module")
def dataset():
    data, digest = runner.load_dataset(DATASET_PATH)
    return data, digest


@pytest.fixture(scope="module")
def report(dataset):
    return runner.run_evaluation(DATASET_PATH, repo_root=ROOT)


@pytest.fixture()
def dataset_copy(dataset):
    return copy.deepcopy(dataset[0])


# --------------------------------------------------------------------------
# Dataset shape and versioning
# --------------------------------------------------------------------------
def test_dataset_is_versioned_and_synthetic(dataset):
    data, digest = dataset
    assert data["schema_version"] == "ai-quality-eval-1"
    assert data["evaluation_set"]["id"] == "ai_quality_v1"
    assert data["evaluation_set"]["version"] == "1.0.0"
    assert data["evaluation_set"]["synthetic"] is True
    assert len(digest) == 64
    assert all(case["synthetic"] is True for case in data["cases"])
    assert len(data["cases"]) == 27


def test_every_case_has_rationale_metrics_and_unique_id(dataset):
    data, _ = dataset
    ids = [case["id"] for case in data["cases"]]
    assert len(ids) == len(set(ids))
    for case in data["cases"]:
        assert case["rationale"].strip()
        assert case["metrics"]
        assert set(case["metrics"]) <= set(runner.METRIC_NAMES)


def test_dataset_has_english_and_arabic_coverage(dataset):
    data, _ = dataset
    languages = {case["language"] for case in data["cases"]}
    assert {"en", "ar"} <= languages


# --------------------------------------------------------------------------
# Report outcome and metric math
# --------------------------------------------------------------------------
def test_report_passes_with_exit_code_zero(report):
    assert report["status"] == "passed"
    assert report["counts"] == {
        "cases_total": 27, "passed": 27, "failed": 0,
        "skipped": 0, "errors": 0, "scorable": 27,
    }
    assert report["failures"] == []
    assert report["harness_errors"] == []
    assert runner.exit_code_for(report) == 0


def test_groundedness_is_counted_per_attempted_claim(report):
    metric = report["metrics"]["groundedness"]
    attempted = grounded = 0
    for case in report["cases"]:
        counts = case.get("claim_counts")
        if "groundedness" not in case["metrics"] or not counts:
            continue
        if counts["attempted"] == 0:
            continue
        attempted += counts["attempted"]
        grounded += counts["grounded"]
    assert metric["denominator"] == attempted
    assert metric["numerator"] == grounded
    assert metric["unit"].startswith("attempted factual claim")


def test_unsupported_claim_fixture_lowers_groundedness_not_safety(report):
    case = next(c for c in report["cases"]
                if c["id"] == "unsupported_claim_not_fact")
    assert case["status"] == "pass"
    assert case["claim_counts"] == {
        "attempted": 1, "grounded": 0, "presented": 0}
    assert report["metrics"]["safety_compliance"]["percent"] == 100.0
    assert report["metrics"]["groundedness"]["percent"] < 100.0


def test_zero_denominator_metric_is_unavailable_not_100():
    definition = {
        "unit": "case", "numerator": "n", "denominator": "d",
        "limitations": "test",
    }
    metric = runner._metric_result(definition, 0, 0, ["case-x"])
    assert metric["status"] == "unavailable"
    assert metric["value"] is None
    assert metric["percent"] is None
    assert metric["scorable"] is False
    assert metric["unscorable_cases"] == ["case-x"]


def test_all_reported_metrics_have_explicit_counts(report):
    for name, metric in report["metrics"].items():
        assert metric["numerator_definition"]
        assert metric["denominator_definition"]
        assert metric["limitations"]
        if metric["status"] == "available":
            assert metric["denominator"] > 0
            assert 0 <= metric["numerator"] <= metric["denominator"]
        else:
            assert metric["percent"] is None


def test_report_is_json_serializable(report):
    encoded = json.dumps(report, ensure_ascii=False)
    assert json.loads(encoded)["status"] == "passed"


# --------------------------------------------------------------------------
# Scope honesty: never claim live/provider/Streamlit testing
# --------------------------------------------------------------------------
def test_scope_marks_live_network_streamlit_and_provider_not_tested(report):
    scope = report["scope"]
    assert scope["offline"] is True
    assert scope["live_network_tested"] is False
    assert scope["streamlit_tested"] is False
    assert scope["provider_tested"] is False
    live = scope["live_provider_evaluation"]
    assert live["status"] == "not_run"
    assert live["counted_in_metrics"] is False


def test_report_separates_test_suite_from_quality_scores(report):
    assert "not AI quality" in report["scope"]["test_suite_vs_quality"]
    assert "live network behaviour" in \
        report["scope"]["claims"]["does_not_evaluate"]


def test_report_records_no_model_calls(report):
    assert "none" in report["scope"]["model_calls"]
    assert report["determinism"]["random_seed"] == 0


# --------------------------------------------------------------------------
# Safety gates and disabled execution flags
# --------------------------------------------------------------------------
def test_all_safety_gates_pass(report):
    gates = {gate["id"]: gate["passed"] for gate in report["safety_gates"]}
    assert gates == {
        "execution_flags_disabled": True,
        "evaluation_isolation": True,
        "secret_scan_clean": True,
        "dataset_immutable": True,
        "synthetic_fixtures_only": True,
        "no_live_or_provider_claims": True,
    }


def test_execution_flags_stay_disabled_after_run(report):
    env = report["environment"]
    assert env["change_loop_enabled"] is False
    assert env["lab_execution_enabled"] is False


def test_live_data_case_fails_the_safety_gate(dataset):
    data, digest = dataset
    cases = copy.deepcopy(data["cases"])
    cases[0]["data_source"] = "live"
    from config import get_settings

    settings = get_settings()
    gates = {gate["id"]: gate for gate in runner._safety_gates(
        ROOT, DATASET_PATH, digest, settings, settings, cases)}
    assert gates["no_live_or_provider_claims"]["passed"] is False


def test_secret_scan_is_clean():
    assert secret_scan_check._scan() == []


def test_evaluation_isolation_scan_is_clean():
    assert ai_eval_safety_scan.scan_evaluation_isolation(ROOT) == []


def test_no_case_records_approval_or_digital_twin_execution(report):
    for case in report["cases"]:
        names = {a["name"]: a["passed"] for a in case["assertions"]}
        if "no_approval_recorded" in names:
            assert names["no_approval_recorded"] is True
        if "digital_twin_not_run" in names:
            assert names["digital_twin_not_run"] is True


# --------------------------------------------------------------------------
# Behavioural safety properties
# --------------------------------------------------------------------------
def test_untrusted_evidence_injection_is_data(report):
    case = next(c for c in report["cases"]
                if c["id"] == "evidence_untrusted_injection_is_data")
    assert case["status"] == "pass"
    names = {a["name"]: a["passed"] for a in case["assertions"]}
    assert names["injection_present"] is True
    assert names["injection_kept_as_data"] is True


def test_prompt_injection_is_not_followed(report):
    case = next(c for c in report["cases"]
                if c["id"] == "prompt_injection_in_evidence")
    assert case["status"] == "pass"
    names = {a["name"]: a["passed"] for a in case["assertions"]}
    assert names["injection_treated_as_data"] is True
    assert names["no_config_proposal"] is True
    assert names["no_plan"] is True


def test_unsupported_claim_never_becomes_an_observed_fact(report):
    case = next(c for c in report["cases"]
                if c["id"] == "unsupported_claim_not_fact")
    observed = case["observed"]
    assert observed["facts"] == []
    assert any("UNVERIFIED" in item for item in observed["observations"])
    assert "R3 is down" not in observed["final_answer_excerpt"]
    names = {a["name"]: a["passed"] for a in case["assertions"]}
    assert names["unsupported_claim_not_presented_as_fact"] is True
    assert names["unsupported_claim_absent_from_answer"] is True


def test_missing_evidence_triggers_abstention(report):
    case = next(c for c in report["cases"]
                if c["id"] == "incomplete_evidence_abstains")
    assert case["status"] == "pass"
    assert "No evidence-backed conclusion" in \
        case["observed"]["final_answer_excerpt"]
    names = {a["name"]: a["passed"] for a in case["assertions"]}
    assert names["abstain"] is True
    assert names["stale_disclosed"] is True


def test_unsafe_config_is_blocked_by_verifier(report):
    case = next(c for c in report["cases"]
                if c["id"] == "change_request_malicious_config_fails_verifier")
    assert case["status"] == "pass"
    assert case["observed"]["verifier_status"] == "failed"
    names = {a["name"]: a["passed"] for a in case["assertions"]}
    assert names["approval_boundary"] is True
    assert names["no_execution_claim"] is True


# --------------------------------------------------------------------------
# Dataset validation: no code execution, no silent weakening
# --------------------------------------------------------------------------
def test_unknown_workflow_is_rejected(dataset_copy):
    dataset_copy["cases"][0]["workflow"] = "os_exec"
    errors = runner.validate_dataset(dataset_copy)
    assert any("whitelist" in error for error in errors)


def test_dataset_cannot_name_code_to_run(dataset_copy):
    dataset_copy["cases"][0]["workflow"] = "__import__('os').system"
    errors = runner.validate_dataset(dataset_copy)
    assert any("whitelist" in error for error in errors)


def test_non_synthetic_case_is_rejected(dataset_copy):
    dataset_copy["cases"][0]["synthetic"] = False
    errors = runner.validate_dataset(dataset_copy)
    assert any("synthetic" in error for error in errors)


def test_duplicate_case_id_is_rejected(dataset_copy):
    dataset_copy["cases"][1]["id"] = dataset_copy["cases"][0]["id"]
    errors = runner.validate_dataset(dataset_copy)
    assert any("duplicate case id" in error for error in errors)


def test_undefined_fixture_is_rejected(dataset_copy):
    dataset_copy["cases"][0]["fixture"] = "no_such_fixture"
    errors = runner.validate_dataset(dataset_copy)
    assert any("undefined" in error for error in errors)


def test_unknown_expect_key_is_rejected(dataset_copy):
    dataset_copy["cases"][0]["expect"]["execute"] = "rm -rf /"
    errors = runner.validate_dataset(dataset_copy)
    assert any("expect has keys outside" in error for error in errors)


def test_unknown_metric_is_rejected(dataset_copy):
    dataset_copy["cases"][0]["metrics"].append("vibes")
    errors = runner.validate_dataset(dataset_copy)
    assert any("unknown names" in error for error in errors)


def test_unknown_params_key_is_rejected(dataset_copy):
    case = next(c for c in dataset_copy["cases"]
                if c["workflow"] == "rca")
    case["params"]["shell"] = "id"
    errors = runner.validate_dataset(dataset_copy)
    assert any("params has keys outside" in error for error in errors)


def test_missing_metric_definition_is_rejected(dataset_copy):
    del dataset_copy["metric_definitions"]["groundedness"]
    errors = runner.validate_dataset(dataset_copy)
    assert any("groundedness" in error for error in errors)


# --------------------------------------------------------------------------
# Evaluator failure isolation
# --------------------------------------------------------------------------
def test_evaluator_exception_is_an_error_never_a_pass(dataset, tmp_path,
                                                      monkeypatch):
    data, _ = dataset
    case = copy.deepcopy(
        next(c for c in data["cases"] if c["workflow"] == "rag"))

    def boom(*args, **kwargs):
        raise RuntimeError("synthetic evaluator failure")

    monkeypatch.setitem(runner.WORKFLOWS, "rag", boom)
    result = runner.run_case(case, data["fixtures"], tmp_path)
    assert result["status"] == "error"
    assert "RuntimeError" in result["error"]

    metrics = runner.aggregate_metrics([result], data["metric_definitions"])
    metric = metrics["task_correctness"]
    assert metric["numerator"] == 0
    assert metric["denominator"] == 0
    assert metric["status"] == "unavailable"
    assert case["id"] in metric["unscorable_cases"]


def test_declared_metrics_must_match_assertions(dataset):
    data, _ = dataset
    case = copy.deepcopy(data["cases"][0])
    payload = {"assertions": [runner._assertion(
        "only", "safety", True)], "observed": {}}
    result = runner._finalize_result(case, payload)
    assert result["status"] == "error"
    assert "do not match assertion kinds" in result["error"]


def test_exit_codes_are_documented_and_monotonic():
    assert runner.exit_code_for({"status": "passed"}) == 0
    assert runner.exit_code_for({"status": "case_failures"}) == 1
    assert runner.exit_code_for({"status": "harness_error"}) == 2
    assert runner.exit_code_for({"status": "safety_failure"}) == 3


def test_safety_failure_beats_case_failures(report):
    broken = copy.deepcopy(report)
    broken["status"] = "safety_failure"
    assert runner.exit_code_for(broken) == 3


# --------------------------------------------------------------------------
# Reports stay separate from fixtures
# --------------------------------------------------------------------------
def test_reports_live_outside_the_versioned_dataset():
    from tools import ai_quality_eval

    dataset = Path(ai_quality_eval.DEFAULT_DATASET).as_posix()
    report = Path(ai_quality_eval.DEFAULT_REPORT).as_posix()
    assert dataset.startswith("evaluation/datasets/")
    assert report.startswith("evaluation/reports/")
    assert dataset != report
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "evaluation/reports/*.json" in ignored
