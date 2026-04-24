from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HANDLERS_ROOT = REPO_ROOT / "src" / "automation_business_scaffold" / "business" / "handlers"
ALLOWLIST_MODULE = HANDLERS_ROOT / "allowlist.py"
ALLOWLIST_BY_WORKER = {
    "api": {
        "feishu_table_read",
        "feishu_table_write",
        "tiktok_product_request_fetch",
        "fastmoss_product_search",
        "fastmoss_product_fetch",
        "fastmoss_creator_fetch",
        "fastmoss_shop_fetch",
        "fastmoss_video_fetch",
        "media_asset_sync",
        "fact_bundle_upsert",
    },
    "browser": {"tiktok_product_browser_fetch"},
    "outbox": {"outbox_dispatch"},
}
FORBIDDEN_EXACT_NAMES = {
    "orchestrate_sync_tk_influencer_pool",
    "feishu_single_row_update",
    "feishu_seed_row_insert",
    "feishu_tk_selection_table_read",
    "feishu_tk_selection_table_writeback",
    "influencer_pool_product",
    "influencer_pool_author",
    "influencer_pool_finalizer",
    "fastmoss_author_fetch",
    "fastmoss_product_search_v1",
    "fastmoss_product_search_v2",
    "selection_table_source_adapter",
    "competitor_table_projection_mapper",
}
FORBIDDEN_PATTERNS = (
    re.compile(r"^orchestrate_"),
    re.compile(r"^run_.*_workflow$"),
    re.compile(r"^run_sync_"),
    re.compile(r".*_orchestrator$"),
    re.compile(r".*_(adapter|mapper|policy|renderer)$"),
)


def _assigned_expression(tree: ast.Module, name: str) -> ast.expr | None:
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            return node.value
    return None


def _string_keys(expression: ast.expr | None) -> set[str]:
    if expression is None:
        return set()
    if isinstance(expression, ast.Dict):
        return {
            key.value
            for key in expression.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
    if isinstance(expression, ast.Call) and expression.args:
        return _string_keys(expression.args[0])
    return set()


def _implemented_module_names(root: Path) -> set[str]:
    names: set[str] = set()
    if not root.exists():
        return names

    for path in root.iterdir():
        if path.name == "__pycache__":
            continue
        if path.is_file() and path.suffix == ".py":
            names.add(path.stem)
        elif path.is_dir() and (path / "__init__.py").exists():
            names.add(path.name)

    return names


def test_handler_registry_worker_packages_exist() -> None:
    missing = [
        str((HANDLERS_ROOT / worker / "registry.py").relative_to(REPO_ROOT))
        for worker in ALLOWLIST_BY_WORKER
        if not (HANDLERS_ROOT / worker / "registry.py").exists()
    ]
    assert missing == [], (
        "foundation handler registry must provide api/browser/outbox registry shells:\n" + "\n".join(missing)
    )


def test_handler_allowlist_matches_documented_contract() -> None:
    assert ALLOWLIST_MODULE.exists(), "handlers.allowlist must exist as the single runtime admission table."

    tree = ast.parse(ALLOWLIST_MODULE.read_text(encoding="utf-8"), filename=str(ALLOWLIST_MODULE))
    actual_allowlists = {
        "api": _string_keys(_assigned_expression(tree, "API_HANDLER_CONTRACTS")),
        "browser": _string_keys(_assigned_expression(tree, "BROWSER_HANDLER_CONTRACTS")),
        "outbox": _string_keys(_assigned_expression(tree, "OUTBOX_HANDLER_CONTRACTS")),
    }
    actual_prohibited_names = _string_keys(_assigned_expression(tree, "PROHIBITED_HANDLER_CODES"))

    for worker, expected in ALLOWLIST_BY_WORKER.items():
        assert actual_allowlists[worker] == expected, (
            f"{worker} handler allowlist drifted from docs/arch/handler-contract-design.md: "
            f"expected {sorted(expected)}, got {sorted(actual_allowlists[worker])}"
        )

    missing_prohibited = sorted(name for name in FORBIDDEN_EXACT_NAMES if name not in actual_prohibited_names)
    assert missing_prohibited == [], (
        "handlers.allowlist should explicitly reject the documented legacy/disallowed names:\n"
        + "\n".join(missing_prohibited)
    )


def test_handler_modules_must_stay_within_the_documented_allowlist() -> None:
    problems: list[str] = []

    for worker, allowlist in ALLOWLIST_BY_WORKER.items():
        worker_root = HANDLERS_ROOT / worker
        implemented = _implemented_module_names(worker_root) - {"__init__", "registry", "implementations"}

        unexpected = sorted(name for name in implemented if name not in allowlist)
        if unexpected:
            problems.append(f"{worker}: unexpected handler modules {unexpected}")

        forbidden = sorted(
            name
            for name in implemented
            if name in FORBIDDEN_EXACT_NAMES or any(pattern.match(name) for pattern in FORBIDDEN_PATTERNS)
        )
        if forbidden:
            problems.append(f"{worker}: forbidden handler names {forbidden}")

    assert problems == [], (
        "handler registry may only route allowlisted handler codes from docs/arch/handler-contract-design.md:\n"
        + "\n".join(problems)
    )


def test_each_allowlisted_handler_has_a_named_module_with_contract() -> None:
    problems: list[str] = []

    for worker, allowlist in ALLOWLIST_BY_WORKER.items():
        for handler_code in sorted(allowlist):
            module_name = (
                "automation_business_scaffold.business.handlers."
                f"{worker}.{handler_code}"
            )
            try:
                module = importlib.import_module(module_name)
            except ModuleNotFoundError as exc:
                problems.append(f"{worker}: missing module {module_name}: {exc}")
                continue

            contract = getattr(module, "CONTRACT", None)
            if getattr(contract, "handler_code", "") != handler_code:
                problems.append(f"{worker}: {module_name}.CONTRACT does not match {handler_code}")
            if getattr(module, "HANDLER_CODE", "") != handler_code:
                problems.append(f"{worker}: {module_name}.HANDLER_CODE does not match {handler_code}")

    assert problems == [], (
        "each admitted handler must have a discoverable same-name module:\n"
        + "\n".join(problems)
    )
