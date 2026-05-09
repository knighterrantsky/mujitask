from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "src" / "automation_business_scaffold"

RUNTIME_STORE = PACKAGE_ROOT / "infrastructure" / "runtime" / "runtime_store.py"
TK_FACT_STORE = PACKAGE_ROOT / "infrastructure" / "facts" / "tk_fact_store.py"
RUNTIME_SCHEMA = PACKAGE_ROOT / "infrastructure" / "schemas" / "runtime_schema.py"
FACT_SCHEMA = PACKAGE_ROOT / "infrastructure" / "schemas" / "fact_schema.py"
FEISHU_INPUT_SOURCE = PACKAGE_ROOT / "capabilities" / "input_sources" / "feishu"
TIKTOK_INFRASTRUCTURE = PACKAGE_ROOT / "infrastructure" / "tiktok"
TIKTOK_BROWSER_PRODUCT_PAGE = PACKAGE_ROOT / "capabilities" / "browser" / "tiktok" / "product_page.py"
OLD_FASTMOSS_MAPPER = PACKAGE_ROOT / "infrastructure" / "fastmoss" / "fact_mappers.py"
FASTMOSS_FACT_BUNDLE_MAPPER = (
    PACKAGE_ROOT
    / "capabilities"
    / "fact_sources"
    / "fastmoss"
    / "mappers"
    / "fact_bundle_mapper.py"
)
LAUNCH_AGENT_INSTALL_SCRIPT = REPO_ROOT / "scripts" / "execution_control" / "install_launch_agents.sh"

DDL_TOKENS = ("CREATE TABLE", "ALTER TABLE", "DROP TABLE", "CREATE INDEX")
FEISHU_BUSINESS_FIELD_TOKENS = (
    "产品链接",
    "SKU-ID",
    "商品状态",
    "达人查找状态",
    "商品ID",
    "店铺名称",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _python_sources(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def _method_call_names(path: Path, *, class_name: str, method_name: str) -> set[str]:
    tree = ast.parse(_read(path), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for item in node.body:
            if not isinstance(item, ast.FunctionDef) or item.name != method_name:
                continue
            calls: set[str] = set()
            for child in ast.walk(item):
                if not isinstance(child, ast.Call):
                    continue
                if isinstance(child.func, ast.Name):
                    calls.add(child.func.id)
                elif isinstance(child.func, ast.Attribute):
                    calls.add(child.func.attr)
            return calls
    raise AssertionError(f"{class_name}.{method_name} not found in {path}")


def _assert_tokens_absent(path: Path, tokens: tuple[str, ...]) -> None:
    source = _read(path)
    found = [token for token in tokens if token in source]
    assert found == [], f"{path.relative_to(REPO_ROOT)} contains forbidden tokens: {found}"


def test_runtime_store_constructor_does_not_execute_schema_ddl() -> None:
    calls = _method_call_names(RUNTIME_STORE, class_name="RuntimeStore", method_name="__init__")
    forbidden_calls = {"ensure_runtime_schema", "ensure_tk_fact_schema", "bootstrap_schema", "_ensure_schema"}

    assert calls.isdisjoint(forbidden_calls)
    _assert_tokens_absent(RUNTIME_STORE, DDL_TOKENS + ("_ensure_schema",))
    assert "def bootstrap_schema" in _read(RUNTIME_STORE)


def test_tk_fact_store_constructor_does_not_execute_schema_ddl() -> None:
    calls = _method_call_names(TK_FACT_STORE, class_name="TKFactStore", method_name="__init__")

    assert "ensure_tk_fact_schema" not in calls
    assert "bootstrap_schema" not in calls
    _assert_tokens_absent(TK_FACT_STORE, DDL_TOKENS + ("TK_FACT_SCHEMA_STATEMENTS",))
    assert "def bootstrap_schema" in _read(TK_FACT_STORE)


def test_schema_bootstrap_owner_modules_hold_explicit_ddl_entrypoints() -> None:
    runtime_schema = _read(RUNTIME_SCHEMA)
    fact_schema = _read(FACT_SCHEMA)

    assert "def ensure_runtime_schema" in runtime_schema
    assert "def ensure_tk_fact_schema" in fact_schema
    assert "ensure_tk_fact_schema" not in runtime_schema
    assert any(token in runtime_schema for token in DDL_TOKENS)
    assert any(token in fact_schema for token in DDL_TOKENS)


def test_local_bootstrap_script_invokes_runtime_and_fact_schema_explicitly() -> None:
    script = _read(LAUNCH_AGENT_INSTALL_SCRIPT)

    assert "runtime_store.bootstrap_schema()" in script
    assert "TKFactStore(runtime_store=runtime_store).bootstrap_schema()" in script


def test_feishu_input_source_capability_has_no_tiktok_business_fields() -> None:
    violations: list[str] = []
    for path in _python_sources(FEISHU_INPUT_SOURCE):
        source = _read(path)
        for token in FEISHU_BUSINESS_FIELD_TOKENS:
            if token in source:
                violations.append(f"{path.relative_to(REPO_ROOT)}: {token}")

    assert violations == [], "Feishu input source capability owns TikTok business fields:\n" + "\n".join(
        violations
    )


def test_tiktok_browser_logic_is_not_owned_by_tiktok_infrastructure() -> None:
    assert TIKTOK_BROWSER_PRODUCT_PAGE.is_file()
    assert "automation_framework.browser" in _read(TIKTOK_BROWSER_PRODUCT_PAGE)

    violations: list[str] = []
    for path in _python_sources(TIKTOK_INFRASTRUCTURE):
        source = _read(path)
        forbidden_tokens = (
            "automation_framework.browser",
            "DEFAULT_FEISHU_FIELD_MAPPING",
        ) + FEISHU_BUSINESS_FIELD_TOKENS
        for token in forbidden_tokens:
            if token in source:
                violations.append(f"{path.relative_to(REPO_ROOT)}: {token}")

    assert violations == [], "TikTok infrastructure owns browser or Feishu field logic:\n" + "\n".join(
        violations
    )


def test_fastmoss_fact_bundle_mapper_owner_moved_to_capability() -> None:
    assert not OLD_FASTMOSS_MAPPER.exists()
    assert FASTMOSS_FACT_BUNDLE_MAPPER.is_file()

    forbidden_imports = (
        "automation_business_scaffold.infrastructure.fastmoss.fact_mappers",
        "automation_business_scaffold.infrastructure.fastmoss import fact_mappers",
        "automation_business_scaffold.infrastructure.tiktok.product_page",
        "automation_business_scaffold.infrastructure.tiktok import product_page",
    )
    violations: list[str] = []
    for root in (PACKAGE_ROOT, REPO_ROOT / "tests"):
        for path in _python_sources(root):
            if path == Path(__file__).resolve():
                continue
            source = _read(path)
            for token in forbidden_imports:
                if token in source:
                    violations.append(f"{path.relative_to(REPO_ROOT)}: {token}")

    assert violations == [], "old owner import paths remain:\n" + "\n".join(violations)


def test_moved_owner_packages_do_not_reexport_implementations() -> None:
    package_inits = (
        PACKAGE_ROOT / "capabilities" / "browser" / "tiktok" / "__init__.py",
        PACKAGE_ROOT / "capabilities" / "fact_sources" / "fastmoss" / "mappers" / "__init__.py",
    )
    violations: list[str] = []
    for path in package_inits:
        source = _read(path)
        if "from .product_page import" in source or "from .fact_bundle_mapper import" in source:
            violations.append(str(path.relative_to(REPO_ROOT)))

    assert violations == [], "moved owner packages must not re-export implementations:\n" + "\n".join(
        violations
    )
