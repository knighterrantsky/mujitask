from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BUSINESS_ROOT = REPO_ROOT / "src" / "automation_business_scaffold" / "business"
DOMAIN_ROOT = REPO_ROOT / "src" / "automation_business_scaffold" / "domains"
RUNTIME_SCOPES = ("flows", "tasks", "workflows", "jobs", "handlers", "workflow_defs", "feishu")
DOMAIN_RUNTIME_SCOPES = ("flows", "tasks", "workflows", "jobs", "mappers", "policies", "projections")


def _business_runtime_files() -> list[Path]:
    targets: list[Path] = []
    for scope in RUNTIME_SCOPES:
        scope_root = BUSINESS_ROOT / scope
        if not scope_root.exists():
            continue
        for path in scope_root.rglob("*.py"):
            if "achieve" in path.parts:
                continue
            targets.append(path)
    return sorted(targets)


def _domain_runtime_files() -> list[Path]:
    targets: list[Path] = []
    for domain_root in sorted(path for path in DOMAIN_ROOT.iterdir() if path.is_dir()):
        for scope in DOMAIN_RUNTIME_SCOPES:
            scope_root = domain_root / scope
            if not scope_root.exists():
                continue
            targets.extend(sorted(scope_root.rglob("*.py")))
    return sorted(targets)


def _business_non_reference_runtime_files() -> list[Path]:
    targets: list[Path] = []
    for scope in RUNTIME_SCOPES:
        scope_root = BUSINESS_ROOT / scope
        if not scope_root.exists():
            continue
        for path in scope_root.rglob("*.py"):
            if "achieve" in path.parts:
                continue
            if path.name == "__init__.py":
                continue
            targets.append(path)
    return sorted(targets)


def _import_module_aliases(tree: ast.AST) -> tuple[set[str], set[str]]:
    importlib_aliases: set[str] = {"importlib"}
    import_module_aliases: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "importlib":
                    importlib_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "importlib":
            for alias in node.names:
                if alias.name == "import_module":
                    import_module_aliases.add(alias.asname or alias.name)

    return importlib_aliases, import_module_aliases


def _literal_string(node: ast.expr | None) -> str | None:
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                return None
            parts.append(value.value)
        return "".join(parts)
    return None


def _contains_achieve_segment(module_name: str) -> bool:
    return "achieve" in module_name.split(".")


def _is_dynamic_import_call(
    node: ast.Call,
    *,
    importlib_aliases: set[str],
    import_module_aliases: set[str],
) -> bool:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "__import__" or func.id in import_module_aliases
    if isinstance(func, ast.Attribute) and func.attr == "import_module" and isinstance(func.value, ast.Name):
        return func.value.id in importlib_aliases
    return False


def _imports_achieve(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    importlib_aliases, import_module_aliases = _import_module_aliases(tree)

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = str(node.module or "")
            if _contains_achieve_segment(module):
                return True
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if _contains_achieve_segment(str(alias.name or "")):
                    return True
        elif isinstance(node, ast.Call) and _is_dynamic_import_call(
            node,
            importlib_aliases=importlib_aliases,
            import_module_aliases=import_module_aliases,
        ):
            module_name = _literal_string(node.args[0]) if node.args else None
            if module_name is None:
                for keyword in node.keywords:
                    if keyword.arg in {"name", "module"}:
                        module_name = _literal_string(keyword.value)
                        if module_name is not None:
                            break
            if module_name and _contains_achieve_segment(module_name):
                return True
    return False


def test_business_runtime_code_must_not_import_achieve() -> None:
    violating = [path for path in _business_runtime_files() if _imports_achieve(path)]
    assert violating == [], "runtime business code must not import achieve:\n" + "\n".join(
        str(path.relative_to(REPO_ROOT)) for path in violating
    )


def test_domain_runtime_code_must_not_import_achieve() -> None:
    violating = [path for path in _domain_runtime_files() if _imports_achieve(path)]
    assert violating == [], "domain runtime code must not import achieve:\n" + "\n".join(
        str(path.relative_to(REPO_ROOT)) for path in violating
    )


def test_no_new_business_runtime_owner_files() -> None:
    violating = _business_non_reference_runtime_files()
    message = "business/** is legacy reference; new runtime owner files belong in domains/**:\n"
    assert violating == [], message + "\n".join(str(path.relative_to(REPO_ROOT)) for path in violating)
