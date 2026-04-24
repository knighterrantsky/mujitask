#!/usr/bin/env python3
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
DEFAULT_FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "achieve_acceptance"

BASELINE_REF_OVERRIDES = {
    "baseline_input": "input_ref",
    "baseline_output": "output_ref",
    "baseline_trace": "trace_ref",
}
CANDIDATE_REF_OVERRIDES = {
    "candidate_request_payload": "request_payload_ref",
    "candidate_runtime_trace": "runtime_trace_ref",
    "candidate_fact_projection": "fact_projection_ref",
    "candidate_feishu_projection": "feishu_projection_ref",
    "candidate_outbox": "outbox_ref",
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare achieve baseline fixtures with candidate runtime artifacts "
            "without invoking pytest."
        )
    )
    parser.add_argument(
        "--fixture-root",
        type=Path,
        default=DEFAULT_FIXTURE_ROOT,
        help="Root containing tests/fixtures/achieve_acceptance payloads.",
    )
    parser.add_argument(
        "--payload",
        action="append",
        type=Path,
        default=[],
        help="Specific payload.json to compare. Can be repeated.",
    )
    parser.add_argument("--workflow-code", help="Workflow fixture directory to compare.")
    parser.add_argument("--scenario-id", help="Scenario fixture directory to compare.")
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        help="Optional directory for normalized baseline/candidate and diff-report outputs.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON summary.",
    )
    parser.add_argument(
        "--max-diffs",
        type=int,
        default=10,
        help="Maximum non-pass diffs to print in text mode.",
    )

    parser.add_argument("--baseline-input", type=Path)
    parser.add_argument("--baseline-output", type=Path)
    parser.add_argument("--baseline-trace", type=Path)
    parser.add_argument("--candidate-request-payload", type=Path)
    parser.add_argument("--candidate-runtime-trace", type=Path)
    parser.add_argument("--candidate-fact-projection", type=Path)
    parser.add_argument("--candidate-feishu-projection", type=Path)
    parser.add_argument("--candidate-outbox", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    payload_paths = _resolve_payload_paths(args, parser)
    if _has_artifact_overrides(args) and len(payload_paths) != 1:
        parser.error("artifact override arguments require exactly one payload.")

    reports = [_compare_payload(path, args) for path in payload_paths]
    status = "pass" if all(report["status"] == "pass" for report in reports) else "fail"
    if args.json:
        _print_json(status, reports)
    else:
        _print_text(status, reports, args.max_diffs)
    return 0 if status == "pass" else 1


def _resolve_payload_paths(args: argparse.Namespace, parser: argparse.ArgumentParser) -> list[Path]:
    if args.payload:
        paths = [path.expanduser().resolve() for path in args.payload]
    elif args.workflow_code or args.scenario_id:
        if not args.workflow_code or not args.scenario_id:
            parser.error("--workflow-code and --scenario-id must be provided together.")
        paths = [
            (args.fixture_root / args.workflow_code / args.scenario_id / "payload.json")
            .expanduser()
            .resolve()
        ]
    else:
        paths = sorted(args.fixture_root.expanduser().resolve().glob("*/*/payload.json"))

    missing = [path for path in paths if not path.exists()]
    if missing:
        parser.error("payload file(s) not found: " + ", ".join(str(path) for path in missing))
    if not paths:
        parser.error(f"no payload.json files found under {args.fixture_root}")
    return paths


def _has_artifact_overrides(args: argparse.Namespace) -> bool:
    return any(
        getattr(args, name) is not None
        for name in (*BASELINE_REF_OVERRIDES.keys(), *CANDIDATE_REF_OVERRIDES.keys())
    )


def _compare_payload(payload_path: Path, args: argparse.Namespace) -> dict[str, Any]:
    if str(SRC_ROOT) not in sys.path:
        sys.path.insert(0, str(SRC_ROOT))
    from automation_business_scaffold.acceptance.comparator import compare_achieve_payload

    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    payload = _apply_artifact_overrides(payload, args)
    report = compare_achieve_payload(
        payload,
        base_dir=payload_path.parent,
        artifact_dir=args.artifact_dir.expanduser().resolve() if args.artifact_dir else None,
    )
    report["payload_path"] = str(payload_path)
    return report


def _apply_artifact_overrides(payload: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    updated = deepcopy(payload)
    baseline = updated.setdefault("baseline", {})
    candidate = updated.setdefault("candidate", {})

    for arg_name, ref_key in BASELINE_REF_OVERRIDES.items():
        path = getattr(args, arg_name)
        if path is not None:
            baseline[ref_key] = _artifact_path_ref(path)

    for arg_name, ref_key in CANDIDATE_REF_OVERRIDES.items():
        path = getattr(args, arg_name)
        if path is None:
            continue
        if arg_name == "candidate_request_payload":
            candidate.pop("request_payload", None)
        candidate[ref_key] = _artifact_path_ref(path)

    return updated


def _artifact_path_ref(path: Path) -> str:
    return str(path.expanduser().resolve())


def _print_text(status: str, reports: list[dict[str, Any]], max_diffs: int) -> None:
    for report in reports:
        summary = report.get("summary") or {}
        print(
            "{workflow_code}/{scenario_id}: {status} "
            "(matched={matched}, allowed={allowed}, unexpected={unexpected}, "
            "missing_required={missing})".format(
                workflow_code=report.get("workflow_code"),
                scenario_id=report.get("scenario_id"),
                status=report.get("status"),
                matched=summary.get("matched_count", 0),
                allowed=summary.get("allowed_difference_count", 0),
                unexpected=summary.get("unexpected_difference_count", 0),
                missing=summary.get("missing_required_count", 0),
            )
        )
        if report.get("status") != "pass":
            _print_report_details(report, max_diffs)
    print(f"Overall status: {status}")


def _print_report_details(report: dict[str, Any], max_diffs: int) -> None:
    failing_checks = [
        check for check in report.get("required_projection_checks", []) if check.get("status") != "pass"
    ]
    for check in failing_checks:
        print(f"  missing required projection: {check.get('path')}")

    non_pass_diffs = [
        diff for diff in report.get("diffs", []) if diff.get("severity") != "allowed"
    ]
    for diff in non_pass_diffs[:max_diffs]:
        reason = f" ({diff.get('reason')})" if diff.get("reason") else ""
        print(f"  {diff.get('severity')}: {diff.get('path')}{reason}")
        print(f"    baseline: {_compact_json(diff.get('baseline'))}")
        print(f"    candidate: {_compact_json(diff.get('candidate'))}")
    if len(non_pass_diffs) > max_diffs:
        print(f"  ... {len(non_pass_diffs) - max_diffs} more non-pass diff(s)")


def _compact_json(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if len(text) <= 240:
        return text
    return text[:237] + "..."


def _print_json(status: str, reports: list[dict[str, Any]]) -> None:
    print(
        json.dumps(
            {
                "status": status,
                "comparison_count": len(reports),
                "reports": reports,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
