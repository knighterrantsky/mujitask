from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ROW_FLOW_FILES = (
    REPO_ROOT
    / "src"
    / "automation_business_scaffold"
    / "domains"
    / "tiktok"
    / "flows"
    / "selection_row_refresh.py",
    REPO_ROOT
    / "src"
    / "automation_business_scaffold"
    / "domains"
    / "tiktok"
    / "flows"
    / "competitor_row_refresh.py",
)
BROWSER_RUNLOOP_PLIST = (
    REPO_ROOT / "config" / "deployment" / "launchd" / "com.happyzhao.mujitask.browser-runloop.plist.template"
)
EXECUTOR_LOOPING = (
    REPO_ROOT
    / "src"
    / "automation_business_scaffold"
    / "control_plane"
    / "executor"
    / "looping.py"
)


def test_row_refresh_flows_do_not_inline_browser_fallback() -> None:
    forbidden_tokens = (
        "capabilities.browser",
        "tiktok_product_browser_fetch_handler",
        "fastmoss_security_browser_resolve_handler",
        "run_supervised_handler",
        "ChildRunnerConfig",
        "worker_type=\"browser_worker\"",
        "worker_type = \"browser_worker\"",
    )

    violations: list[str] = []
    for path in ROW_FLOW_FILES:
        source = path.read_text(encoding="utf-8")
        for token in forbidden_tokens:
            if token in source:
                violations.append(f"{path.relative_to(REPO_ROOT)} contains {token}")

    assert violations == [], "row refresh flows must not inline browser fallback:\n" + "\n".join(violations)


def test_selection_keyword_workflow_owns_row_browser_fallback_stage() -> None:
    workflow_source = (
        REPO_ROOT
        / "src"
        / "automation_business_scaffold"
        / "domains"
        / "tiktok"
        / "workflows"
        / "search_keyword_selection_products.py"
    ).read_text(encoding="utf-8")
    runtime_source = (
        REPO_ROOT
        / "src"
        / "automation_business_scaffold"
        / "domains"
        / "tiktok"
        / "flows"
        / "search_keyword_selection_products.py"
    ).read_text(encoding="utf-8")
    contract_source = (
        REPO_ROOT / "contracts" / "workflow" / "search_keyword_selection_products.yaml"
    ).read_text(encoding="utf-8")

    required_tokens = (
        "selection_row_browser_fallback",
        "resume_selection_rows_after_browser_fallback",
        "enqueue_task_executions",
        "Waiting for selection row browser fallback executions to finish.",
        "Row-level browser fallback is owned by task_execution/browser-runloop",
    )
    combined = "\n".join((workflow_source, runtime_source, contract_source))
    missing = [token for token in required_tokens if token not in combined]

    assert missing == [], "selection row browser fallback boundary is missing:\n" + "\n".join(missing)


def test_product_ingest_workflow_owns_row_browser_fallback_stage() -> None:
    workflow_source = (
        REPO_ROOT
        / "src"
        / "automation_business_scaffold"
        / "domains"
        / "tiktok"
        / "workflows"
        / "tiktok_fastmoss_product_ingest.py"
    ).read_text(encoding="utf-8")
    runtime_source = (
        REPO_ROOT
        / "src"
        / "automation_business_scaffold"
        / "domains"
        / "tiktok"
        / "flows"
        / "tiktok_fastmoss_product_ingest.py"
    ).read_text(encoding="utf-8")
    contract_source = (
        REPO_ROOT / "contracts" / "workflow" / "tiktok_fastmoss_product_ingest.yaml"
    ).read_text(encoding="utf-8")

    required_tokens = (
        "selection_row_browser_fallback",
        "resume_selection_rows_after_browser_fallback",
        "enqueue_task_executions",
        "Waiting for selection row browser fallback executions to finish.",
        "Row-level browser fallback is owned by task_execution/browser-runloop",
    )
    combined = "\n".join((workflow_source, runtime_source, contract_source))
    missing = [token for token in required_tokens if token not in combined]

    assert missing == [], "product ingest row browser fallback boundary is missing:\n" + "\n".join(missing)


def test_refresh_competitor_workflow_owns_row_browser_fallback_stage() -> None:
    workflow_source = (
        REPO_ROOT
        / "src"
        / "automation_business_scaffold"
        / "domains"
        / "tiktok"
        / "workflows"
        / "refresh_current_competitor_table.py"
    ).read_text(encoding="utf-8")
    row_by_url_workflow_source = (
        REPO_ROOT
        / "src"
        / "automation_business_scaffold"
        / "domains"
        / "tiktok"
        / "workflows"
        / "refresh_competitor_row_by_url.py"
    ).read_text(encoding="utf-8")
    runtime_source = (
        REPO_ROOT
        / "src"
        / "automation_business_scaffold"
        / "domains"
        / "tiktok"
        / "flows"
        / "refresh_current_competitor_table.py"
    ).read_text(encoding="utf-8")
    contract_source = (
        REPO_ROOT / "contracts" / "workflow" / "refresh_current_competitor_table.yaml"
    ).read_text(encoding="utf-8")

    required_tokens = (
        "browser_fallback",
        "resume_competitor_rows_after_browser_fallback",
        "enqueue_task_executions",
        "Waiting for browser fallback executions to finish.",
        "Row-level browser fallback is owned by task_execution/browser-runloop",
    )
    combined = "\n".join((workflow_source, row_by_url_workflow_source, runtime_source, contract_source))
    missing = [token for token in required_tokens if token not in combined]

    assert missing == [], "competitor row browser fallback boundary is missing:\n" + "\n".join(missing)


def test_browser_runloop_never_defaults_to_inline_supervision() -> None:
    plist_source = BROWSER_RUNLOOP_PLIST.read_text(encoding="utf-8")
    looping_source = EXECUTOR_LOOPING.read_text(encoding="utf-8")

    assert "<string>--supervisor-mode</string>" in plist_source
    assert "<string>child_process</string>" in plist_source
    assert "<string>inline</string>" not in plist_source
    assert 'worker_type == "browser_worker"' in looping_source
    assert 'return "child_process"' in looping_source
