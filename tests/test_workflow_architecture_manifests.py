from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "src" / "automation_business_scaffold"
MANIFEST_ROOT = PACKAGE_ROOT / "contracts" / "workflow"
DOMAIN_ROOT = PACKAGE_ROOT / "domains"
WORKFLOW_PATTERN_DOC = REPO_ROOT / "docs" / "arch" / "workflow-implementation-patterns.md"
TARGET_ARCH_DOC = REPO_ROOT / "docs" / "arch" / "target-project-architecture-contract.md"

SNAKE_CASE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
LEGACY_WORKFLOW_CODES = {
    "refresh_current_competitor_table",
    "search_keyword_competitor_products",
    "sync_tk_influencer_pool",
    "tiktok_fastmoss_product_ingest",
}
ORIGIN_VALUES = {"migrated_existing", "new_workflow"}
CUSTOM_LOGIC_KINDS = ("mappers", "policies", "projections")
FORBIDDEN_REAL_IMPLEMENTATION_FRAGMENTS = (
    "from .implementations import",
    "from ..implementations import",
    "from automation_business_scaffold.business.",
    "import automation_business_scaffold.business.",
    "sys.modules[__name__]",
    "capabilities/_implementations",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _manifest_paths() -> list[Path]:
    return sorted(path for path in MANIFEST_ROOT.glob("*.yaml") if path.is_file())


def _load_manifest(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(_read(path))
    assert isinstance(loaded, dict), f"{path.relative_to(REPO_ROOT)} must contain a YAML mapping"
    loaded["_manifest_path"] = path
    return loaded


def _manifests() -> list[dict[str, Any]]:
    return [_load_manifest(path) for path in _manifest_paths()]


def _manifest_by_code() -> dict[str, dict[str, Any]]:
    manifests = _manifests()
    by_code = {str(item.get("workflow_code")): item for item in manifests}
    assert len(by_code) == len(manifests), "workflow manifest workflow_code values must be unique"
    return by_code


def _workflow_files_by_code() -> dict[str, Path]:
    workflow_files: dict[str, Path] = {}
    for path in sorted(DOMAIN_ROOT.glob("*/workflows/*.py")):
        if path.name == "__init__.py":
            continue
        workflow_files[path.stem] = path
    return workflow_files


def _import_module(module_name: str) -> ModuleType:
    return importlib.import_module(module_name)


def _module_path(module_name: str) -> Path:
    prefix = "automation_business_scaffold."
    assert module_name.startswith(prefix), module_name
    return PACKAGE_ROOT / (module_name.removeprefix(prefix).replace(".", "/") + ".py")


def _exports(entry: dict[str, Any]) -> list[str]:
    values: list[str] = []
    if entry.get("export"):
        values.append(str(entry["export"]))
    values.extend(str(value) for value in entry.get("exports", []))
    return values


def _assert_exports_exist(module: ModuleType, entry: dict[str, Any]) -> None:
    missing = [export for export in _exports(entry) if not hasattr(module, export)]
    assert not missing, f"{module.__name__} is missing exports: {missing}"


def _custom_logic_entries(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    custom_logic = manifest.get("custom_logic")
    assert isinstance(custom_logic, dict), "custom_logic must be a mapping"
    entries: list[dict[str, Any]] = []
    for kind in CUSTOM_LOGIC_KINDS:
        values = custom_logic.get(kind, [])
        assert isinstance(values, list), f"custom_logic.{kind} must be a list"
        for value in values:
            assert isinstance(value, dict), f"custom_logic.{kind} entries must be mappings"
            entries.append(value)
    return entries


def _custom_logic_codes(manifest: dict[str, Any]) -> set[str]:
    return {str(entry.get("code")) for entry in _custom_logic_entries(manifest)}


def _known_gap_codes(manifest: dict[str, Any]) -> set[str]:
    gaps = manifest.get("known_architecture_gaps", [])
    assert isinstance(gaps, list), "known_architecture_gaps must be a list"
    return {str(gap.get("code")) for gap in gaps if isinstance(gap, dict)}


def _stage_custom_logic_codes(definition: Any) -> set[str]:
    codes: set[str] = set()
    for stage in definition.stages:
        for binding in stage.job_bindings:
            if binding.adapter_code:
                codes.add(binding.adapter_code)
            if binding.mapper_code:
                codes.add(binding.mapper_code)
    return codes


def _module_defines_export(module_name: str, export: str) -> bool:
    path = _module_path(module_name)
    tree = ast.parse(_read(path), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            if node.name == export:
                return True
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == export for target in node.targets):
                return True
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == export:
                return True
    return False


def _imported_local_modules(module_name: str) -> set[str]:
    path = _module_path(module_name)
    tree = ast.parse(_read(path), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return {
        module
        for module in modules
        if module.startswith("automation_business_scaffold.")
    }


def _assert_source_has_no_forbidden_legacy_patterns(module_name: str) -> None:
    path = _module_path(module_name)
    source = _read(path)
    found = [fragment for fragment in FORBIDDEN_REAL_IMPLEMENTATION_FRAGMENTS if fragment in source]
    assert not found, f"{path.relative_to(REPO_ROOT)} contains forbidden migration patterns: {found}"


def test_workflow_architecture_manifests_cover_domain_workflow_modules() -> None:
    workflow_files = _workflow_files_by_code()
    manifest_codes = set(_manifest_by_code())

    assert workflow_files, "expected at least one domain workflow module"
    assert manifest_codes == set(workflow_files), (
        "workflow architecture manifests must cover every domain workflow module exactly:\n"
        f"missing={sorted(set(workflow_files) - manifest_codes)}\n"
        f"stale={sorted(manifest_codes - set(workflow_files))}"
    )


def test_workflow_architecture_manifests_have_required_shape() -> None:
    required_top_level = {
        "schema_version",
        "workflow_origin",
        "workflow_code",
        "domain",
        "agent_artifact",
        "task",
        "workflow",
        "custom_logic",
        "outbox",
        "jobs",
    }
    for manifest in _manifests():
        missing = required_top_level - set(manifest)
        assert not missing, f"{manifest['_manifest_path'].name} is missing keys: {sorted(missing)}"
        assert manifest["schema_version"] == 1
        assert manifest["workflow_origin"] in ORIGIN_VALUES
        assert SNAKE_CASE_RE.match(manifest["workflow_code"])
        assert SNAKE_CASE_RE.match(manifest["domain"])

        if manifest["workflow_code"] in LEGACY_WORKFLOW_CODES:
            assert manifest["workflow_origin"] == "migrated_existing"
        else:
            assert manifest["workflow_origin"] == "new_workflow"

        for section_name in ("task", "workflow", "outbox"):
            assert isinstance(manifest[section_name], dict), section_name

        jobs = manifest["jobs"]
        assert isinstance(jobs, list) and jobs, "jobs must be a non-empty list"
        job_codes = [job.get("code") for job in jobs]
        assert len(job_codes) == len(set(job_codes)), f"duplicate job codes in {manifest['workflow_code']}"

        for job in jobs:
            assert {"code", "module", "handler_code", "capability"} <= set(job), job
            assert SNAKE_CASE_RE.match(str(job["code"]))
            capability = job["capability"]
            assert isinstance(capability, dict), job
            assert {"role", "system", "module", "export"} <= set(capability), capability

        for entry in _custom_logic_entries(manifest):
            assert {"code", "module"} <= set(entry), entry
            assert _exports(entry), entry


def test_manifest_modules_exports_and_job_bindings_exist() -> None:
    for manifest in _manifests():
        task_module = _import_module(manifest["task"]["module"])
        _assert_exports_exist(task_module, manifest["task"])

        workflow_module = _import_module(manifest["workflow"]["module"])
        _assert_exports_exist(workflow_module, manifest["workflow"])
        for export_name in ("definition_export", "runtime_export"):
            assert hasattr(workflow_module, manifest["workflow"][export_name])

        definition_builder = getattr(workflow_module, manifest["workflow"]["definition_export"])
        definition = definition_builder()
        assert definition.workflow_code == manifest["workflow_code"]
        assert definition.task_code == manifest["task"]["code"]

        declared_job_codes = {job["code"] for job in manifest["jobs"]}
        definition_job_codes = {job_def.job_code for job_def in definition.job_defs}
        stage_job_codes = {
            binding.job_code
            for stage in definition.stages
            for binding in stage.job_bindings
        }
        assert definition_job_codes == declared_job_codes
        assert stage_job_codes <= declared_job_codes
        assert definition.summary_policy.outbox_job_code == manifest["outbox"]["job_code"]

        for job in manifest["jobs"]:
            job_module = _import_module(job["module"])
            assert job_module.JOB_CODE == job["code"]
            assert job_module.HANDLER_CODE == job["handler_code"]
            assert job_module.JOB_DEFINITION.job_code == job["code"]
            assert job_module.JOB_DEFINITION.handler_code == job["handler_code"]

            capability_module = _import_module(job["capability"]["module"])
            assert hasattr(capability_module, job["capability"]["export"])
            if hasattr(capability_module, "HANDLER_CODE"):
                assert capability_module.HANDLER_CODE == job["handler_code"]

        outbox_capability = manifest["outbox"]["capability"]
        outbox_module = _import_module(outbox_capability["module"])
        assert hasattr(outbox_module, outbox_capability["export"])
        if hasattr(outbox_module, "HANDLER_CODE"):
            assert outbox_module.HANDLER_CODE == manifest["outbox"]["handler_code"]

        for entry in _custom_logic_entries(manifest):
            module = _import_module(entry["module"])
            _assert_exports_exist(module, entry)


def test_workflow_custom_logic_references_are_declared_or_explicit_gap() -> None:
    for manifest in _manifests():
        workflow_module = _import_module(manifest["workflow"]["module"])
        definition_builder = getattr(workflow_module, manifest["workflow"]["definition_export"])
        definition = definition_builder()

        referenced_codes = _stage_custom_logic_codes(definition)
        declared_codes = _custom_logic_codes(manifest)
        known_gap_codes = _known_gap_codes(manifest)

        missing = referenced_codes - declared_codes - known_gap_codes
        assert not missing, (
            f"{manifest['workflow_code']} stage bindings reference custom logic not declared "
            f"in manifest custom_logic or known_architecture_gaps: {sorted(missing)}"
        )

        unused_gaps = known_gap_codes - referenced_codes
        assert not unused_gaps, (
            f"{manifest['workflow_code']} declares stale known_architecture_gaps: "
            f"{sorted(unused_gaps)}"
        )

        if manifest["workflow_origin"] == "new_workflow":
            assert not known_gap_codes, "new_workflow manifests cannot carry known architecture gaps"
            assert referenced_codes <= declared_codes


def test_new_workflow_manifests_use_strict_target_shape() -> None:
    for manifest in _manifests():
        if manifest["workflow_origin"] != "new_workflow":
            continue

        task_module = _import_module(manifest["task"]["module"])
        workflow_module = _import_module(manifest["workflow"]["module"])
        assert getattr(task_module, "TASK_CODE") == manifest["task"]["code"]
        assert getattr(workflow_module, "WORKFLOW_CODE") == manifest["workflow_code"]

        agent_path = REPO_ROOT / manifest["agent_artifact"]["path"]
        assert agent_path.is_dir(), f"new workflow agent artifact path is missing: {agent_path}"

        modules_to_check = [
            manifest["task"]["module"],
            manifest["workflow"]["module"],
            *(job["module"] for job in manifest["jobs"]),
            *(entry["module"] for entry in _custom_logic_entries(manifest)),
            *(job["capability"]["module"] for job in manifest["jobs"]),
        ]
        for module_name in modules_to_check:
            _assert_source_has_no_forbidden_legacy_patterns(module_name)

        for entry in _custom_logic_entries(manifest):
            for export in _exports(entry):
                assert _module_defines_export(entry["module"], export), (
                    f"{manifest['workflow_code']} custom logic export {export} must be "
                    f"defined in {entry['module']}, not re-exported from another module"
                )

        for job in manifest["jobs"]:
            capability_module = job["capability"]["module"]
            assert _module_defines_export(capability_module, job["capability"]["export"])
            imported_modules = _imported_local_modules(capability_module)
            forbidden_domain_imports = sorted(
                module
                for module in imported_modules
                if module.startswith("automation_business_scaffold.domains.")
            )
            assert not forbidden_domain_imports, (
                f"{capability_module} imports domain modules directly: {forbidden_domain_imports}"
            )


def test_workflow_manifest_contract_is_documented() -> None:
    workflow_pattern_doc = _read(WORKFLOW_PATTERN_DOC)
    target_arch_doc = _read(TARGET_ARCH_DOC)
    required_tokens = (
        "Workflow Architecture Manifest",
        "contracts/workflow/{workflow_code}.yaml",
        "workflow_origin",
        "new_workflow",
        "migrated_existing",
        "known_architecture_gaps",
        "test_workflow_architecture_manifests",
    )
    missing_from_pattern = [token for token in required_tokens if token not in workflow_pattern_doc]
    missing_from_target = [token for token in required_tokens[:3] if token not in target_arch_doc]

    assert not missing_from_pattern, (
        "workflow implementation pattern doc is missing manifest tokens:\n"
        + "\n".join(missing_from_pattern)
    )
    assert not missing_from_target, (
        "target architecture contract is missing manifest tokens:\n"
        + "\n".join(missing_from_target)
    )
