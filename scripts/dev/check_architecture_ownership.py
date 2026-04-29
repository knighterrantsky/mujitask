#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
import sys


PROJECT_PACKAGE = "automation_business_scaffold"


@dataclass(frozen=True, slots=True)
class Finding:
    check: str
    path: Path
    message: str
    line: int | None = None

    def sort_key(self, root: Path) -> tuple[str, str, int, str]:
        return (self.check, _display_path(self.path, root), self.line or 0, self.message)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    repo_root = args.repo_root.resolve()

    findings = check_repository(repo_root)
    findings.sort(key=lambda finding: finding.sort_key(repo_root))

    if args.json:
        _print_json(findings, repo_root)
    else:
        _print_text(findings, repo_root)
    return 1 if findings else 0


def check_repository(repo_root: Path) -> list[Finding]:
    src_root = repo_root / "src" / PROJECT_PACKAGE
    findings: list[Finding] = []
    findings.extend(_check_capabilities_implementations(src_root))
    findings.extend(_check_capability_handler_ownership(src_root))
    findings.extend(_check_reexports_and_thin_wrappers(src_root))
    findings.extend(_check_domain_owner_files_are_not_reexport_only(src_root))
    findings.extend(_check_no_domain_aggregate_owner_files(src_root))
    return findings


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Statically check architecture ownership rules without importing runtime code."
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="Repository root. Defaults to this script's repository.",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    return parser


def _check_capabilities_implementations(src_root: Path) -> list[Finding]:
    implementations_dir = src_root / "capabilities" / "_implementations"
    if not implementations_dir.exists():
        return []
    file_count = sum(1 for path in implementations_dir.rglob("*") if path.is_file())
    suffix = f" ({file_count} file(s) present)" if file_count else ""
    return [
        Finding(
            check="no_capabilities_implementations",
            path=implementations_dir,
            message=f"capabilities/_implementations must not exist; put owned code in capability modules{suffix}.",
        )
    ]


def _check_capability_handler_ownership(src_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    capabilities_root = src_root / "capabilities"
    for path in _iter_python_files(capabilities_root):
        tree = _parse_python(path)
        if tree is None:
            continue
        if not _is_capability_handler_module(path, tree):
            continue
        handler_functions = [
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and _is_public(node.name)
            and node.name.endswith("_handler")
        ]
        handler_code_assignments = [
            node
            for node in tree.body
            if _is_assignment_to_name(node, "HANDLER_CODE")
        ]
        if len(handler_functions) > 1:
            names = ", ".join(node.name for node in handler_functions)
            findings.append(
                Finding(
                    check="one_public_handler_per_file",
                    path=path,
                    line=handler_functions[1].lineno,
                    message=f"handler file defines multiple public handler functions: {names}.",
                )
            )
        if len(handler_code_assignments) > 1:
            findings.append(
                Finding(
                    check="one_handler_code_per_file",
                    path=path,
                    line=getattr(handler_code_assignments[1], "lineno", None),
                    message="handler file defines HANDLER_CODE more than once.",
                )
            )
        findings.extend(_check_handler_imports_handler(path, src_root, tree))
    return findings


def _is_capability_handler_module(path: Path, tree: ast.Module) -> bool:
    if path.name.endswith("_handler.py"):
        return True
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.endswith("_handler"):
            return True
        if _is_assignment_to_name(node, "HANDLER_CODE"):
            return True
    return False


def _check_handler_imports_handler(path: Path, src_root: Path, tree: ast.Module) -> list[Finding]:
    findings: list[Finding] = []
    current_module = _module_name_for_path(path, src_root)
    for node in ast.walk(tree):
        imported_modules: list[str] = []
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module_name = _resolve_import_from(current_module, node.module, node.level)
            if module_name:
                imported_modules.append(module_name)
            imported_modules.extend(
                f"{module_name}.{alias.name}"
                for alias in node.names
                if module_name and alias.name != "*"
            )
        for imported in imported_modules:
            if imported != current_module and imported.endswith("_handler"):
                findings.append(
                    Finding(
                        check="capability_handlers_do_not_import_handlers",
                        path=path,
                        line=getattr(node, "lineno", None),
                        message=f"capability handler imports another handler module {imported!r}.",
                    )
                )
                break
    return findings


def _check_reexports_and_thin_wrappers(src_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in _iter_python_files(src_root):
        tree = _parse_python(path)
        if tree is None:
            continue
        is_owner_file = _is_ownership_checked_path(path, src_root)
        if not is_owner_file:
            continue
        imported_names = _top_level_imported_names(tree)
        all_names = _literal_all_names(tree)
        reexported_names = sorted(name for name in all_names if name in imported_names and _is_public(name))
        if reexported_names:
            findings.append(
                Finding(
                    check="no_explicit_reexports",
                    path=path,
                    line=_line_for_all(tree),
                    message=f"__all__ explicitly re-exports imported owner names: {', '.join(reexported_names)}.",
                )
            )
        if _is_reexport_only_module(tree):
            findings.append(
                Finding(
                    check="no_reexport_only_modules",
                    path=path,
                    message="module contains only imports, __all__, constants, and/or a docstring; move callers to the owner module.",
                )
            )
        if is_owner_file:
            findings.extend(_check_thin_wrappers(path, tree, imported_names))
    return findings


def _check_domain_owner_files_are_not_reexport_only(src_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    domains_root = src_root / "domains"
    for path in _iter_python_files(domains_root):
        if not _is_domain_custom_owner_path(path, src_root):
            continue
        tree = _parse_python(path)
        if tree is None or not _is_reexport_only_module(tree):
            continue
        findings.append(
            Finding(
                check="domain_owner_files_are_not_reexport_only",
                path=path,
                message="domain mapper/projection owner files must contain owned implementation, not only re-exports.",
            )
        )
    return findings


def _check_no_domain_aggregate_owner_files(src_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in _iter_python_files(src_root / "domains"):
        if not _is_domain_custom_owner_path(path, src_root):
            continue
        if path.name.endswith(("_adapters.py", "_mappers.py")):
            findings.append(
                Finding(
                    check="no_domain_aggregate_owner_files",
                    path=path,
                    message=(
                        "domain mapper/projection owner files must be business-object specific; "
                        "use registry.py for registration and dedicated owner files for implementation."
                    ),
                )
            )
    return findings


def _check_thin_wrappers(path: Path, tree: ast.Module, imported_names: set[str]) -> list[Finding]:
    findings: list[Finding] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not _is_public(node.name):
            continue
        target = _thin_wrapper_target(node, imported_names)
        if target:
            findings.append(
                Finding(
                    check="no_thin_wrappers",
                    path=path,
                    line=node.lineno,
                    message=f"public function {node.name!r} only forwards to imported owner {target!r}.",
                )
            )
    return findings


def _thin_wrapper_target(node: ast.FunctionDef | ast.AsyncFunctionDef, imported_names: set[str]) -> str:
    body = [statement for statement in node.body if not _is_docstring_expr(statement)]
    if len(body) != 1:
        return ""
    statement = body[0]
    call: ast.Call | None = None
    if isinstance(statement, ast.Return) and isinstance(statement.value, ast.Call):
        call = statement.value
    elif isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
        call = statement.value
    if call is None:
        return ""
    root_name = _call_root_name(call.func)
    if root_name in imported_names and _call_only_forwards_parameters(node, call):
        return ast.unparse(call.func)
    return ""


def _call_only_forwards_parameters(node: ast.FunctionDef | ast.AsyncFunctionDef, call: ast.Call) -> bool:
    positional_names = {arg.arg for arg in node.args.posonlyargs + node.args.args}
    keyword_names = {arg.arg for arg in node.args.kwonlyargs}
    all_names = positional_names | keyword_names
    vararg_name = node.args.vararg.arg if node.args.vararg else ""
    kwarg_name = node.args.kwarg.arg if node.args.kwarg else ""
    if node.args.defaults or node.args.kw_defaults:
        return False
    if not call.args and not call.keywords:
        return False
    for arg in call.args:
        if isinstance(arg, ast.Name) and arg.id in all_names:
            continue
        if isinstance(arg, ast.Starred) and isinstance(arg.value, ast.Name) and arg.value.id == vararg_name:
            continue
        return False
    for keyword in call.keywords:
        if keyword.arg is None:
            if isinstance(keyword.value, ast.Name) and keyword.value.id == kwarg_name:
                continue
            return False
        if isinstance(keyword.value, ast.Name) and keyword.arg == keyword.value.id and keyword.value.id in all_names:
            continue
        return False
    return True


def _call_root_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _call_root_name(node.value)
    return ""


def _is_reexport_only_module(tree: ast.Module) -> bool:
    has_import = False
    for node in tree.body:
        if _is_docstring_expr(node):
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            has_import = True
            continue
        if _is_all_assignment(node):
            continue
        if _is_simple_constant_assignment(node):
            continue
        return False
    return has_import


def _top_level_imported_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".", maxsplit=1)[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    names.add("*")
                else:
                    names.add(alias.asname or alias.name)
    return names


def _literal_all_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if not _is_all_assignment(node):
            continue
        value = node.value if isinstance(node, ast.Assign) else node.value
        if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
            names.update(
                element.value
                for element in value.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            )
    return names


def _line_for_all(tree: ast.Module) -> int | None:
    for node in tree.body:
        if _is_all_assignment(node):
            return getattr(node, "lineno", None)
    return None


def _is_assignment_to_name(node: ast.stmt, name: str) -> bool:
    if isinstance(node, ast.Assign):
        return any(isinstance(target, ast.Name) and target.id == name for target in node.targets)
    return isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name


def _is_all_assignment(node: ast.stmt) -> bool:
    return _is_assignment_to_name(node, "__all__")


def _is_simple_constant_assignment(node: ast.stmt) -> bool:
    if isinstance(node, ast.Assign):
        return all(isinstance(target, ast.Name) for target in node.targets) and _is_literalish(node.value)
    if isinstance(node, ast.AnnAssign):
        return isinstance(node.target, ast.Name) and _is_literalish(node.value)
    return False


def _is_literalish(node: ast.AST | None) -> bool:
    if node is None:
        return True
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return all(_is_literalish(element) for element in node.elts)
    if isinstance(node, ast.Dict):
        return all(_is_literalish(key) and _is_literalish(value) for key, value in zip(node.keys, node.values))
    return False


def _is_docstring_expr(node: ast.stmt) -> bool:
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)


def _iter_python_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(
        path
        for path in root.rglob("*.py")
        if path.is_file() and "__pycache__" not in path.parts
    )


def _parse_python(path: Path) -> ast.Module | None:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
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
    if isinstance(tree, ast.Module):
        return tree
    return None


def _module_name_for_path(path: Path, src_root: Path) -> str:
    return ".".join([PROJECT_PACKAGE, *path.relative_to(src_root).with_suffix("").parts])


def _resolve_import_from(current_module: str, module: str | None, level: int) -> str:
    if level == 0:
        return module or ""
    package_parts = current_module.split(".")[:-1]
    base_parts = package_parts[: max(len(package_parts) - level + 1, 0)]
    if module:
        base_parts.extend(module.split("."))
    return ".".join(part for part in base_parts if part)


def _is_public(name: str) -> bool:
    return not name.startswith("_")


def _is_ownership_checked_path(path: Path, src_root: Path) -> bool:
    return _is_domain_custom_owner_path(path, src_root) or _is_capability_handler_path(path, src_root)


def _is_domain_custom_owner_path(path: Path, src_root: Path) -> bool:
    try:
        relative = path.relative_to(src_root)
    except ValueError:
        return False
    if not relative.parts or relative.parts[0] != "domains":
        return False
    if path.name in {"__init__.py", "registry.py"}:
        return False
    return "mappers" in relative.parts or "projections" in relative.parts


def _is_capability_handler_path(path: Path, src_root: Path) -> bool:
    try:
        relative = path.relative_to(src_root)
    except ValueError:
        return False
    return bool(relative.parts) and relative.parts[0] == "capabilities" and path.name.endswith("_handler.py")


def _display_path(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _print_text(findings: list[Finding], repo_root: Path) -> None:
    if not findings:
        print("architecture ownership checks passed.")
        return

    grouped: dict[str, list[Finding]] = defaultdict(list)
    for finding in findings:
        grouped[finding.check].append(finding)

    print("architecture ownership checks failed:")
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

    print(
        json.dumps(
            {
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
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
