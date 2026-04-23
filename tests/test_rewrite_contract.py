from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BUSINESS_ROOT = REPO_ROOT / "src" / "automation_business_scaffold" / "business"


def _business_runtime_files() -> list[Path]:
    targets: list[Path] = []
    for scope in ("flows", "tasks", "workflows"):
        scope_root = BUSINESS_ROOT / scope
        if not scope_root.exists():
            continue
        for path in scope_root.rglob("*.py"):
            if "achieve" in path.parts:
                continue
            targets.append(path)
    return sorted(targets)


def _imports_achieve(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = str(node.module or "")
            if "achieve" in module.split("."):
                return True
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if "achieve" in str(alias.name or "").split("."):
                    return True
    return False


def test_business_runtime_code_must_not_import_achieve() -> None:
    violating = [path for path in _business_runtime_files() if _imports_achieve(path)]
    assert violating == [], "runtime business code must not import achieve:\n" + "\n".join(
        str(path.relative_to(REPO_ROOT)) for path in violating
    )
