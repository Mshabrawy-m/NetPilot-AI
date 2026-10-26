"""Offline AI-quality evaluation CLI (B4).

Runs the deterministic, version-controlled evaluation set against the existing
NetPilot AI workflows. No API key, no network access, no real model call and no
paid service is used: every assistant response is fixed in the dataset and each
case runs against its own temporary SQLite database.

Usage
-----
    python tools/ai_quality_eval.py
    python tools/ai_quality_eval.py --dataset evaluation/datasets/ai_quality_v1.json
    python tools/ai_quality_eval.py --report evaluation/reports/ai_quality_report.json

Exit codes
----------
    0  every case passed and every safety gate passed
    1  one or more cases failed
    2  the dataset or harness itself errored
    3  a safety gate failed (takes precedence over all other statuses)

This command does **not** test the live network, Streamlit rendering, a Groq
provider, Gate 3 or Gate D. Those results are explicitly ``not_run`` in the
report and are excluded from every metric.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation.runner import (DatasetError, exit_code_for,  # noqa: E402
                               run_evaluation)

DEFAULT_DATASET = "evaluation/datasets/ai_quality_v1.json"
DEFAULT_REPORT = "evaluation/reports/ai_quality_report.json"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ai_quality_eval",
        description=("Offline, deterministic AI-quality/groundedness "
                     "evaluation for NetPilot AI (no provider, no network)."))
    parser.add_argument("--dataset", default=DEFAULT_DATASET,
                        help=f"evaluation dataset (default: {DEFAULT_DATASET})")
    parser.add_argument("--report", default=DEFAULT_REPORT,
                        help=f"machine-readable report path "
                             f"(default: {DEFAULT_REPORT})")
    parser.add_argument("--work-dir", default=None,
                        help="optional scratch directory for per-case databases")
    parser.add_argument("--quiet", action="store_true",
                        help="only print the final status line")
    return parser.parse_args(argv)


def _write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(path)


def _print_summary(report: dict, report_path: Path) -> None:
    counts = report["counts"]
    print(f"Evaluation set : {report['evaluation_set']['id']} "
          f"v{report['evaluation_set']['version']} "
          f"({report['evaluation_set']['case_count']} cases, "
          f"sha256 {report['evaluation_set']['sha256'][:12]}...)")
    print(f"Status         : {report['status']}")
    print(f"Cases          : {counts['passed']} passed, "
          f"{counts['failed']} failed, {counts['skipped']} skipped, "
          f"{counts['errors']} errors (of {counts['cases_total']})")
    print("Metrics        :")
    for name, metric in report["metrics"].items():
        if metric["status"] == "available":
            print(f"  {name:<26} {metric['numerator']}/"
                  f"{metric['denominator']} = {metric['percent']}%")
        else:
            print(f"  {name:<26} unavailable "
                  f"(0 scorable, not reported as 100%)")
    print("Safety gates   :")
    for gate in report["safety_gates"]:
        mark = "pass" if gate["passed"] else "FAIL"
        print(f"  [{mark}] {gate['id']}")
    if report["failures"]:
        print("Case failures  :")
        for failure in report["failures"]:
            print(f"  - {failure['case_id']}: {failure['assertion']} "
                  f"({failure['detail']})")
    if report["harness_errors"]:
        print("Harness errors :")
        for error in report["harness_errors"]:
            print(f"  - {error['case_id']}: {error['error']}")
    scope = report["scope"]
    print(f"Scope          : live_network_tested="
          f"{scope['live_network_tested']}, streamlit_tested="
          f"{scope['streamlit_tested']}, provider_tested="
          f"{scope['provider_tested']}")
    print(f"Report         : {report_path}")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    dataset_path = Path(args.dataset)
    if not dataset_path.is_absolute():
        dataset_path = ROOT / dataset_path
    report_path = Path(args.report)
    if not report_path.is_absolute():
        report_path = ROOT / report_path

    try:
        report = run_evaluation(dataset_path, repo_root=ROOT,
                                work_root=args.work_dir)
    except DatasetError as exc:
        print(f"dataset error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # harness failure is never a pass
        print(f"harness error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    try:
        _write_report(report_path, report)
    except OSError as exc:
        print(f"could not write report: {exc}", file=sys.stderr)
        return 2

    if not args.quiet:
        _print_summary(report, report_path)
    else:
        print(f"{report['status']} -> {report_path}")

    return exit_code_for(report)


if __name__ == "__main__":
    raise SystemExit(main())
