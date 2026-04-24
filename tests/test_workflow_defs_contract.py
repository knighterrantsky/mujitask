from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DEFS_ROOT = REPO_ROOT / "src" / "automation_business_scaffold" / "business" / "workflow_defs"
OFFICIAL_WORKFLOW_CODES = (
    "refresh_current_competitor_table",
    "search_keyword_competitor_products",
    "sync_tk_influencer_pool",
    "tiktok_fastmoss_product_ingest",
)
WORKFLOW_DEF_REQUIRED_TOKENS = (
    "task_code",
    "workflow_code",
    "stages",
    "job_defs",
    "transitions",
    "summary_policy",
    "idempotency_policy",
    "timeout_policy",
    "watchdog_policy",
)
WORKFLOW_DEF_CORE_TYPES = ("WorkflowDefinition", "StageDefinition", "JobDefinition")


def _module_entry(root: Path, name: str) -> Path | None:
    module_file = root / f"{name}.py"
    if module_file.exists():
        return module_file

    package_init = root / name / "__init__.py"
    if package_init.exists():
        return package_init

    return None


def test_workflow_defs_registry_and_core_types_exist() -> None:
    assert WORKFLOW_DEFS_ROOT.exists(), "business.workflow_defs package must exist during the rewrite."
    assert (WORKFLOW_DEFS_ROOT / "registry.py").exists(), (
        "workflow_defs must provide a registry entrypoint for executor/reconciler discovery."
    )

    package_sources = []
    for path in sorted(WORKFLOW_DEFS_ROOT.glob("*.py")):
        package_sources.append(path.read_text(encoding="utf-8"))
    combined_source = "\n".join(package_sources)

    missing = [name for name in WORKFLOW_DEF_CORE_TYPES if name not in combined_source]
    assert missing == [], (
        "workflow_defs foundation layer should define the core workflow shape types: " + ", ".join(missing)
    )


def test_official_workflow_definition_modules_exist() -> None:
    missing = [
        workflow_code
        for workflow_code in OFFICIAL_WORKFLOW_CODES
        if _module_entry(WORKFLOW_DEFS_ROOT, workflow_code) is None
    ]
    assert missing == [], (
        "workflow_defs must provide one definition module per formal workflow code:\n" + "\n".join(missing)
    )


def test_workflow_definition_modules_expose_minimum_contract_fields() -> None:
    missing_fields_by_workflow: list[str] = []

    for workflow_code in OFFICIAL_WORKFLOW_CODES:
        module_path = _module_entry(WORKFLOW_DEFS_ROOT, workflow_code)
        if module_path is None:
            continue
        source = module_path.read_text(encoding="utf-8")
        missing = [token for token in WORKFLOW_DEF_REQUIRED_TOKENS if token not in source]
        if missing:
            missing_fields_by_workflow.append(f"{workflow_code}: {', '.join(missing)}")

    assert missing_fields_by_workflow == [], (
        "each workflow definition should spell out the minimum runtime contract fields:\n"
        + "\n".join(missing_fields_by_workflow)
    )
