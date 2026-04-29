#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
import sys
import tomllib


PROJECT_PACKAGE = "automation_business_scaffold"
MAIN_PATH_DIRS = ("apps", "control_plane", "domains", "capabilities", "contracts")
LEGACY_ROOT_ENTRYPOINT_MODULES = frozenset(
    {
        f"{PROJECT_PACKAGE}.agent",
        f"{PROJECT_PACKAGE}.api_worker_daemon",
        f"{PROJECT_PACKAGE}.browser_runloop",
        f"{PROJECT_PACKAGE}.cli",
        f"{PROJECT_PACKAGE}.executor_daemon",
        f"{PROJECT_PACKAGE}.outbox_dispatcher",
        f"{PROJECT_PACKAGE}.watchdog_scanner",
    }
)
LEGACY_DOMAIN_AGGREGATE_FILES = frozenset(
    {
        Path("domains/tiktok/mappers/feishu_source_adapters.py"),
        Path("domains/tiktok/projections/feishu_projection_mappers.py"),
    }
)


@dataclass(frozen=True, slots=True)
class Finding:
    check: str
    path: Path
    message: str
    line: int | None = None

    def sort_key(self, root: Path) -> tuple[str, str, int, str]:
        return (
            self.check,
            _display_path(self.path, root),
            self.line or 0,
            self.message,
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Check real_migration ownership rules without importing runtime code "
            "or running pytest."
        )
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="Repository root. Defaults to this script's repository.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of grouped text.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    src_root = repo_root / "src" / PROJECT_PACKAGE
    pyproject_path = repo_root / "pyproject.toml"

    findings: list[Finding] = []
    findings.extend(_check_no_sys_modules_alias(src_root))
    findings.extend(_check_no_wildcard_reexports(src_root))
    findings.extend(_check_no_thin_reexport_modules(src_root))
    findings.extend(_check_no_empty_non_init_main_modules(src_root))
    findings.extend(_check_no_capabilities_implementations(src_root))
    findings.extend(_check_no_legacy_domain_aggregates(src_root))
    findings.extend(_check_legacy_root_entrypoints(repo_root, pyproject_path))

    findings.sort(key=lambda finding: finding.sort_key(repo_root))
    if args.json:
        _print_json(findings, repo_root)
    else:
        _print_text(findings, repo_root)
    return 1 if findings else 0


def _iter_python_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(
        path
        for path in root.rglob("*.py")
        if "__pycache__" not in path.parts and path.is_file()
    )


def _parse_python(path: Path) -> ast.AST | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        return ast.Module(
            body=[
                ast.Expr(
                    value=ast.Constant(
                        f"unparseable python at line {exc.lineno or 0}: {exc.msg}"
                    )
                )
            ],
            type_ignores=[],
        )


def _check_no_sys_modules_alias(src_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in _iter_python_files(src_root):
        tree = _parse_python(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Subscript) and _is_sys_modules_dunder_name(node):
                findings.append(
                    Finding(
                        check="no_sys_modules_alias",
                        path=path,
                        line=getattr(node, "lineno", None),
                        message="sys.modules[__name__] aliases are forbidden in src.",
                    )
                )
    return findings


def _is_sys_modules_dunder_name(node: ast.Subscript) -> bool:
    value = node.value
    if not (
        isinstance(value, ast.Attribute)
        and value.attr == "modules"
        and isinstance(value.value, ast.Name)
        and value.value.id == "sys"
    ):
        return False
    return isinstance(node.slice, ast.Name) and node.slice.id == "__name__"


def _check_no_wildcard_reexports(src_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in _iter_python_files(src_root):
        text = path.read_text(encoding="utf-8")
        tree = _parse_python(path)
        if tree is not None:
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and any(
                    alias.name == "*" for alias in node.names
                ):
                    imported_from = _format_import_from(path, src_root, node)
                    findings.append(
                        Finding(
                            check="no_wildcard_reexports",
                            path=path,
                            line=node.lineno,
                            message=f"wildcard import from {imported_from!r} is forbidden.",
                        )
                    )
        for line_number, line in enumerate(text.splitlines(), start=1):
            if "noqa" in line and "F401" in line and "F403" in line:
                findings.append(
                    Finding(
                        check="no_wildcard_reexports",
                        path=path,
                        line=line_number,
                        message="noqa: F401,F403 is forbidden; do not preserve re-export shims.",
                    )
                )
    return findings


def _check_no_thin_reexport_modules(src_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in _iter_main_path_files(src_root):
        if path.name == "__init__.py":
            continue
        tree = _parse_python(path)
        if tree is None:
            continue
        imports: list[ast.AST] = []
        all_assigns: list[ast.AST] = []
        substantive: list[ast.AST] = []
        for node in tree.body:
            if _is_docstring_expr(node):
                continue
            if isinstance(node, ast.ImportFrom) and node.module == "__future__":
                continue
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                imports.append(node)
                continue
            if _is_all_assignment(node):
                all_assigns.append(node)
                continue
            substantive.append(node)
        if imports and all_assigns and not substantive:
            findings.append(
                Finding(
                    check="no_thin_reexport_modules",
                    path=path,
                    line=getattr(imports[0], "lineno", None),
                    message=(
                        "thin import + __all__ re-export modules are forbidden; "
                        "move the real implementation into this module or delete it."
                    ),
                )
            )
    return findings


def _check_no_empty_non_init_main_modules(src_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in _iter_main_path_files(src_root):
        if path.name == "__init__.py":
            continue
        tree = _parse_python(path)
        if tree is None:
            continue
        substantive = [
            node
            for node in tree.body
            if not _is_docstring_expr(node)
            and not (isinstance(node, ast.ImportFrom) and node.module == "__future__")
            and not _is_all_assignment(node)
        ]
        if not substantive:
            findings.append(
                Finding(
                    check="no_empty_non_init_main_modules",
                    path=path,
                    message=(
                        "empty migration-note modules are forbidden in main paths; "
                        "delete the file or move a real implementation into it."
                    ),
                )
            )
    return findings


def _is_docstring_expr(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _is_all_assignment(node: ast.AST) -> bool:
    if isinstance(node, ast.Assign):
        return any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets)
    return isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "__all__"


def _check_no_capabilities_implementations(src_root: Path) -> list[Finding]:
    implementations_dir = src_root / "capabilities" / "_implementations"
    if not implementations_dir.exists():
        return []

    files = [path for path in implementations_dir.rglob("*") if path.is_file()]
    suffix = f" ({len(files)} file(s) present)" if files else ""
    return [
        Finding(
            check="no_capabilities_implementations",
            path=implementations_dir,
            message=(
                "capabilities/_implementations must not exist in real_migration mode"
                f"{suffix}."
            ),
        )
    ]


def _check_no_legacy_domain_aggregates(src_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for relative_path in LEGACY_DOMAIN_AGGREGATE_FILES:
        path = src_root / relative_path
        if path.exists():
            findings.append(
                Finding(
                    check="no_legacy_domain_aggregates",
                    path=path,
                    message=(
                        "legacy domain aggregate modules must not exist after real_migration; "
                        "use specific mapper/projection modules plus registry.py."
                    ),
                )
            )
    return findings


def _iter_main_path_files(src_root: Path) -> list[Path]:
    files: list[Path] = []
    for dirname in MAIN_PATH_DIRS:
        files.extend(_iter_python_files(src_root / dirname))
    return sorted(set(files))


def _module_name_for_path(path: Path, src_root: Path) -> str:
    relative = path.relative_to(src_root)
    parts = [PROJECT_PACKAGE, *relative.with_suffix("").parts]
    return ".".join(parts)


def _resolve_import_from(current_module: str, module: str | None, level: int) -> str:
    if level == 0:
        return module or ""

    package_parts = current_module.split(".")[:-1]
    if level > len(package_parts):
        base_parts: list[str] = []
    else:
        base_parts = package_parts[: len(package_parts) - level + 1]
    if module:
        base_parts.extend(module.split("."))
    return ".".join(part for part in base_parts if part)


def _format_import_from(path: Path, src_root: Path, node: ast.ImportFrom) -> str:
    return _resolve_import_from(_module_name_for_path(path, src_root), node.module, node.level)


def _check_legacy_root_entrypoints(repo_root: Path, pyproject_path: Path) -> list[Finding]:
    if not pyproject_path.exists():
        return []

    with pyproject_path.open("rb") as handle:
        pyproject = tomllib.load(handle)
    scripts = pyproject.get("project", {}).get("scripts", {})
    if not isinstance(scripts, dict):
        return []

    line_lookup = _script_line_lookup(pyproject_path)
    findings: list[Finding] = []
    for script_name, target in sorted(scripts.items()):
        target_text = str(target)
        module_name = target_text.split(":", maxsplit=1)[0]
        if module_name not in LEGACY_ROOT_ENTRYPOINT_MODULES:
            continue

        module_path = repo_root / "src" / Path(*module_name.split(".")).with_suffix(".py")
        if module_path.exists():
            findings.append(
                Finding(
                    check="legacy_root_entrypoints",
                    path=pyproject_path,
                    line=line_lookup.get(script_name),
                    message=(
                        f"console script {script_name!r} targets legacy root module "
                        f"{target_text!r}; point it at apps/** or remove the shim file."
                    ),
                )
            )
    return findings


def _script_line_lookup(pyproject_path: Path) -> dict[str, int]:
    lines = pyproject_path.read_text(encoding="utf-8").splitlines()
    lookup: dict[str, int] = {}
    in_project_scripts = False
    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_project_scripts = stripped == "[project.scripts]"
            continue
        if not in_project_scripts or "=" not in stripped:
            continue
        key = stripped.split("=", maxsplit=1)[0].strip()
        lookup[key] = line_number
    return lookup


def _display_path(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _print_text(findings: list[Finding], repo_root: Path) -> None:
    if not findings:
        print("real_migration ownership checks passed.")
        return

    print("real_migration ownership checks failed:")
    grouped: dict[str, list[Finding]] = defaultdict(list)
    for finding in findings:
        grouped[finding.check].append(finding)

    for check in sorted(grouped):
        print(f"\n[{check}]")
        for finding in grouped[check]:
            location = _display_path(finding.path, repo_root)
            if finding.line is not None:
                location = f"{location}:{finding.line}"
            print(f"  - {location}: {finding.message}")

    print(f"\nSummary: {len(findings)} violation(s) across {len(grouped)} check(s).")


def _print_json(findings: list[Finding], repo_root: Path) -> None:
    import json

    payload = {
        "status": "fail" if findings else "pass",
        "violation_count": len(findings),
        "violations": [
            {
                "check": finding.check,
                "path": _display_path(finding.path, repo_root),
                "line": finding.line,
                "message": finding.message,
            }
            for finding in findings
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    sys.exit(main())
