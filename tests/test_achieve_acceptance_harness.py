from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from automation_business_scaffold.acceptance import (
    AchieveComparator,
    JsonRefResolver,
    compare_achieve_payload,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
BUSINESS_ROOT = REPO_ROOT / "src" / "automation_business_scaffold" / "business"
ACCEPTANCE_ROOT = REPO_ROOT / "src" / "automation_business_scaffold" / "acceptance"
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "achieve_acceptance"
RUNTIME_SCOPES = ("flows", "tasks", "workflows", "handlers", "workflow_defs")


PASSING_FIXTURES = (
    ("refresh_current_competitor_table", "competitor_row_refresh_minimal"),
    ("search_keyword_competitor_products", "keyword_halloween_min_day7_sales"),
    ("sync_tk_influencer_pool", "influencer_pool_basic_upsert"),
)


def _load_payload(workflow_code: str, scenario_id: str) -> tuple[dict, Path]:
    payload_path = FIXTURE_ROOT / workflow_code / scenario_id / "payload.json"
    return json.loads(payload_path.read_text(encoding="utf-8")), payload_path.parent


def _imports_forbidden_module(path: Path, forbidden_segments: set[str]) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if forbidden_segments.intersection(str(alias.name).split(".")):
                    return True
        elif isinstance(node, ast.ImportFrom):
            module = str(node.module or "")
            if forbidden_segments.intersection(module.split(".")):
                return True
    return False


@pytest.mark.parametrize(("workflow_code", "scenario_id"), PASSING_FIXTURES)
def test_achieve_acceptance_fixtures_pass_and_write_report(
    workflow_code: str,
    scenario_id: str,
    tmp_path: Path,
) -> None:
    payload, base_dir = _load_payload(workflow_code, scenario_id)

    result = compare_achieve_payload(payload, base_dir=base_dir, artifact_dir=tmp_path)

    assert result["status"] == "pass"
    assert result["summary"]["allowed_difference_count"] == 1
    assert result["summary"]["unexpected_difference_count"] == 0
    assert result["summary"]["missing_required_count"] == 0
    assert all(check["status"] == "pass" for check in result["required_projection_checks"])
    assert result["artifact_refs"]["diff_report"].endswith(
        f"/{workflow_code}/{scenario_id}/diff-report.json"
    )

    report_path = tmp_path / workflow_code / scenario_id / "diff-report.json"
    normalized_baseline_path = tmp_path / workflow_code / scenario_id / "baseline-normalized.json"
    normalized_candidate_path = tmp_path / workflow_code / scenario_id / "candidate-normalized.json"
    assert report_path.exists()
    assert normalized_baseline_path.exists()
    assert normalized_candidate_path.exists()
    written_report = json.loads(report_path.read_text(encoding="utf-8"))
    assert written_report["status"] == "pass"


def test_comparator_reports_unexpected_business_differences() -> None:
    payload, base_dir = _load_payload(
        "search_keyword_competitor_products",
        "keyword_halloween_min_day7_sales",
    )
    base_resolver = JsonRefResolver(base_dir=base_dir)
    bad_ref = "artifact://runtime/keyword/bad-feishu-projection.json"
    bad_feishu_projection = base_resolver.load_json_ref(payload["candidate"]["feishu_projection_ref"])
    bad_feishu_projection["records"][0]["fields"]["商品状态"] = "待人工确认"
    payload["candidate"]["feishu_projection_ref"] = bad_ref

    comparator = AchieveComparator(
        resolver=JsonRefResolver(base_dir=base_dir, artifact_values={bad_ref: bad_feishu_projection})
    )
    result = comparator.compare_payload(payload)

    assert result["status"] == "fail"
    assert result["summary"]["unexpected_difference_count"] == 1
    unexpected = [diff for diff in result["diffs"] if diff["severity"] == "unexpected"]
    assert unexpected[0]["path"] == "feishu_projection.records[0].fields.商品状态"
    assert unexpected[0]["baseline"] == "已入库"
    assert unexpected[0]["candidate"] == "待人工确认"


def test_comparator_fails_when_required_projection_is_missing() -> None:
    payload, base_dir = _load_payload(
        "sync_tk_influencer_pool",
        "influencer_pool_basic_upsert",
    )
    bad_ref = "artifact://runtime/influencer/missing-fact-projection.json"
    payload["candidate"]["fact_projection_ref"] = bad_ref

    comparator = AchieveComparator(
        resolver=JsonRefResolver(
            base_dir=base_dir,
            artifact_values={bad_ref: {"persisted_entity_count": 1}},
        )
    )
    result = comparator.compare_payload(payload)

    assert result["status"] == "fail"
    assert result["summary"]["missing_required_count"] == 1
    missing_checks = [
        check for check in result["required_projection_checks"] if check["status"] == "fail"
    ]
    assert missing_checks == [{"path": "fact_projection.persisted_entities", "status": "fail"}]


def test_acceptance_comparator_does_not_import_achieve() -> None:
    violating = [
        path
        for path in sorted(ACCEPTANCE_ROOT.rglob("*.py"))
        if _imports_forbidden_module(path, {"achieve"})
    ]
    assert violating == []


def test_business_runtime_code_must_not_import_acceptance_harness() -> None:
    runtime_files: list[Path] = []
    for scope in RUNTIME_SCOPES:
        scope_root = BUSINESS_ROOT / scope
        if scope_root.exists():
            runtime_files.extend(
                path for path in scope_root.rglob("*.py") if "achieve" not in path.parts
            )

    violating = [
        path
        for path in sorted(runtime_files)
        if _imports_forbidden_module(path, {"acceptance"})
    ]
    assert violating == [], "runtime business code must not import acceptance harness:\n" + "\n".join(
        str(path.relative_to(REPO_ROOT)) for path in violating
    )
