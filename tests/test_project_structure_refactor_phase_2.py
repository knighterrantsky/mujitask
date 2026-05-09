from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "src" / "automation_business_scaffold"
FLOWS = PACKAGE_ROOT / "domains" / "tiktok" / "flows"
RUNTIME = PACKAGE_ROOT / "infrastructure" / "runtime"

SELECTION_FLOW = FLOWS / "search_keyword_selection_products"
COMPETITOR_FLOW = FLOWS / "search_keyword_competitor_products"

SELECTION_STAGE_CODES = {
    "keyword_seed_import",
    "fastmoss_security_browser_fallback",
    "dispatch_selection_row_refresh_jobs",
    "refresh_selection_rows",
    "selection_row_browser_fallback",
    "resume_selection_rows_after_browser_fallback",
    "ready_for_summary",
}
COMPETITOR_STAGE_CODES = {
    "keyword_seed_import",
    "fastmoss_security_browser_fallback",
    "dispatch_row_refresh_jobs",
    "refresh_competitor_rows",
    "browser_fallback",
    "resume_competitor_rows_after_browser_fallback",
    "ready_for_summary",
}


def _stage_modules(flow_package: Path) -> set[str]:
    return {
        path.stem
        for path in (flow_package / "stages").glob("*.py")
        if path.name != "__init__.py"
    }


def test_selection_and_competitor_exemplars_are_package_structured() -> None:
    assert SELECTION_FLOW.is_dir()
    assert COMPETITOR_FLOW.is_dir()
    assert not SELECTION_FLOW.with_suffix(".py").exists()
    assert not COMPETITOR_FLOW.with_suffix(".py").exists()

    assert _stage_modules(SELECTION_FLOW) == SELECTION_STAGE_CODES
    assert _stage_modules(COMPETITOR_FLOW) == COMPETITOR_STAGE_CODES

    for flow_package in (SELECTION_FLOW, COMPETITOR_FLOW):
        assert (flow_package / "orchestrator.py").is_file()
        assert (flow_package / "context").is_dir()
        assert not (flow_package / "context.py").exists()
        assert (flow_package / "summary.py").is_file()


def test_runtime_decomposition_phase_2_files_exist() -> None:
    expected = (
        "queries/db_health_query.py",
        "queries/request_status_query.py",
        "queries/watchdog_query.py",
        "repositories/api_worker_job_repo.py",
        "repositories/artifact_object_repo.py",
        "repositories/influencer_pool_job_repo.py",
        "repositories/notification_outbox_repo.py",
        "repositories/resource_lease_repo.py",
        "repositories/task_execution_repo.py",
        "repositories/task_request_repo.py",
        "bootstrap.py",
        "schema_version.py",
        "runtime_store.py",
    )
    missing = [path for path in expected if not (RUNTIME / path).is_file()]
    assert missing == []
