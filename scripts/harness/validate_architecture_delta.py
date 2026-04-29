from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
ARCHITECTURE_OWNERSHIP = "contracts/harness/architecture-ownership.yaml"
PRODUCT_FACT_COLLECTION = "contracts/facts/product-fact-collection.yaml"
IMPLEMENTATION_PREFIX = "src/automation_business_scaffold/"

HELPER_LIKE_TOKENS = (
    "helper",
    "helpers",
    "service",
    "manager",
    "coordinator",
    "collection",
    "collector",
    "orchestrator",
)

PRODUCT_FACT_SIGNALS = (
    "product media",
    "product_media",
    "media_asset",
    "media assets",
    "minio",
    "fact_bundle",
    "fact db",
    "fact_db",
    "fact media",
    "fact_media",
    "tk_media_assets",
    "object_key",
    "remote_uri",
    "product_main_image",
    "product_gallery_image",
    "product_sku_image",
)

DEFAULT_FORBIDDEN_NEW_MODULE_PATTERNS = (
    "src/automation_business_scaffold/domains/**/facts/*collection*.py",
    "src/automation_business_scaffold/domains/**/*helper*.py",
    "src/automation_business_scaffold/domains/**/*service*.py",
    "src/automation_business_scaffold/domains/**/*manager*.py",
    "src/automation_business_scaffold/domains/**/*coordinator*.py",
)

ALLOW_PATTERN_KEYS = {
    "allowed_helper_like_modules",
    "allowed_helper_like_paths",
    "allowed_new_modules",
    "allowed_abstraction_modules",
}


@dataclass(frozen=True)
class ChangedPath:
    path: str
    status: str
    added: bool


def _record(target: list[dict[str, str]], check: str, detail: str) -> None:
    target.append({"check": check, "detail": detail})


def _result(
    *,
    claim: str,
    passed_checks: list[dict[str, str]],
    failed_checks: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        "claim": claim,
        "passed_checks": passed_checks,
        "failed_checks": failed_checks,
    }


def _run_git(repo_root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _parse_status_line(line: str) -> ChangedPath | None:
    if len(line) < 4:
        return None
    code = line[:2]
    path = line[3:]
    if " -> " in path:
        path = path.rsplit(" -> ", 1)[1]
    path = path.strip().strip('"')
    if not path:
        return None
    added = code == "??" or "A" in code
    return ChangedPath(path=path, status=code.strip() or "M", added=added)


def _changed_paths(repo_root: Path) -> tuple[list[ChangedPath], str | None]:
    result = _run_git(repo_root, ["status", "--porcelain=v1", "--untracked-files=all"])
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "git status failed").strip()
        return [], detail
    changes = [
        parsed
        for parsed in (_parse_status_line(line) for line in result.stdout.splitlines())
        if parsed is not None
    ]
    return changes, None


def _added_lines_from_patch(patch: str) -> str:
    lines: list[str] = []
    for line in patch.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            lines.append(line[1:])
    return "\n".join(lines)


def _changed_text(repo_root: Path, change: ChangedPath) -> str:
    parts: list[str] = []
    for args in (["diff", "--", change.path], ["diff", "--cached", "--", change.path]):
        result = _run_git(repo_root, args)
        if result.returncode == 0 and result.stdout:
            parts.append(_added_lines_from_patch(result.stdout))

    target = repo_root / change.path
    if change.added and target.is_file():
        try:
            if target.stat().st_size <= 1_000_000:
                parts.append(target.read_text(encoding="utf-8"))
        except UnicodeDecodeError:
            return "\n".join(parts)
    return "\n".join(part for part in parts if part)


def _load_yaml_contract(repo_root: Path, rel_path: str) -> tuple[Any | None, str | None]:
    path = repo_root / rel_path
    if not path.exists():
        return None, "missing"
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - exact parser wording is not stable.
        return None, f"{type(exc).__name__}: {exc}"
    if not isinstance(loaded, dict):
        return None, "top-level YAML must be a mapping"
    return loaded, None


def _is_implementation_path(path: str) -> bool:
    return path.startswith(IMPLEMENTATION_PREFIX) and path.endswith(".py")


def _is_helper_like_path(path: str) -> bool:
    filename = Path(path).name.lower()
    return any(token in filename for token in HELPER_LIKE_TOKENS)


def _matches_any(path: str, patterns: list[str] | tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def _as_patterns(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        patterns: list[str] = []
        for item in value:
            if isinstance(item, str):
                patterns.append(item)
            elif isinstance(item, dict):
                for key in ("path", "pattern", "module"):
                    if item.get(key):
                        patterns.append(str(item[key]))
                        break
        return patterns
    if isinstance(value, dict):
        return _as_patterns([value])
    return []


def _collect_allow_patterns_from_value(
    value: Any,
    *,
    source_path: str,
) -> list[tuple[str, str]]:
    patterns: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key) in ALLOW_PATTERN_KEYS:
                patterns.extend((pattern, source_path) for pattern in _as_patterns(child))
            else:
                patterns.extend(_collect_allow_patterns_from_value(child, source_path=source_path))
    elif isinstance(value, list):
        for item in value:
            patterns.extend(_collect_allow_patterns_from_value(item, source_path=source_path))
    return patterns


def _collect_allow_patterns(repo_root: Path) -> list[tuple[str, str]]:
    patterns: list[tuple[str, str]] = []
    contracts_root = repo_root / "contracts"
    if not contracts_root.exists():
        return patterns
    for path in sorted(contracts_root.rglob("*.yaml")):
        rel_path = path.relative_to(repo_root).as_posix()
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        patterns.extend(_collect_allow_patterns_from_value(loaded, source_path=rel_path))
    return patterns


def _allow_sources_for(path: str, allow_patterns: list[tuple[str, str]]) -> list[str]:
    return [source for pattern, source in allow_patterns if fnmatch.fnmatchcase(path, pattern)]


def _product_fact_contract_referenced(
    *,
    changed_paths: list[ChangedPath],
    changed_texts: dict[str, str],
) -> bool:
    path_set = {change.path for change in changed_paths}
    if PRODUCT_FACT_COLLECTION in path_set:
        return True
    needles = (PRODUCT_FACT_COLLECTION, "product-fact-collection.yaml")
    return any(any(needle in text for needle in needles) for text in changed_texts.values())


def _has_product_fact_signal(path: str, text: str) -> bool:
    if not path.startswith(IMPLEMENTATION_PREFIX):
        return False
    haystack = f"{path}\n{text}".lower()
    return any(signal in haystack for signal in PRODUCT_FACT_SIGNALS)


def validate(repo_root: Path) -> tuple[dict[str, Any], int]:
    repo_root = repo_root.resolve()
    passed: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []

    ownership, ownership_error = _load_yaml_contract(repo_root, ARCHITECTURE_OWNERSHIP)
    if ownership_error:
        _record(failed, "architecture_ownership_parses", ownership_error)
    else:
        _record(passed, "architecture_ownership_parses", ARCHITECTURE_OWNERSHIP)

    product_contract, product_error = _load_yaml_contract(repo_root, PRODUCT_FACT_COLLECTION)
    if product_error:
        _record(failed, "product_fact_collection_contract_parses", product_error)
    else:
        _record(passed, "product_fact_collection_contract_parses", PRODUCT_FACT_COLLECTION)

    changes, status_error = _changed_paths(repo_root)
    if status_error:
        _record(failed, "git_delta_available", status_error)
        return _result(claim="not_complete", passed_checks=passed, failed_checks=failed), 1
    _record(passed, "git_delta_available", f"{len(changes)} changed paths")

    changed_texts = {change.path: _changed_text(repo_root, change) for change in changes}
    changed_contracts = {
        change.path
        for change in changes
        if change.path.startswith("contracts/")
        and (change.path.endswith(".yaml") or change.path.endswith(".md"))
    }

    allow_patterns = _collect_allow_patterns(repo_root)
    helper_like_added = [
        change.path
        for change in changes
        if change.added and _is_implementation_path(change.path) and _is_helper_like_path(change.path)
    ]
    helper_like_without_allow: list[str] = []
    helper_like_without_changed_contract: list[str] = []
    for path in helper_like_added:
        sources = _allow_sources_for(path, allow_patterns)
        if not sources:
            helper_like_without_allow.append(path)
        elif not any(source in changed_contracts for source in sources):
            helper_like_without_changed_contract.append(path)

    if helper_like_without_allow:
        _record(
            failed,
            "helper_like_added_files_require_contract_allow",
            ", ".join(helper_like_without_allow),
        )
    elif helper_like_without_changed_contract:
        _record(
            failed,
            "helper_like_added_files_require_changed_contract",
            ", ".join(helper_like_without_changed_contract),
        )
    else:
        _record(
            passed,
            "helper_like_added_files_require_contract_allow",
            f"{len(helper_like_added)} helper-like implementation files",
        )

    forbidden_patterns = list(DEFAULT_FORBIDDEN_NEW_MODULE_PATTERNS)
    if isinstance(product_contract, dict):
        configured = product_contract.get("forbidden_new_modules_by_default")
        configured_patterns = _as_patterns(configured)
        if configured_patterns:
            forbidden_patterns = configured_patterns

    forbidden_new_modules = [
        change.path
        for change in changes
        if change.added
        and _is_implementation_path(change.path)
        and _matches_any(change.path, forbidden_patterns)
        and not _allow_sources_for(change.path, allow_patterns)
    ]
    if forbidden_new_modules:
        _record(failed, "forbidden_new_module_patterns", ", ".join(forbidden_new_modules))
    else:
        _record(passed, "forbidden_new_module_patterns", f"{len(forbidden_patterns)} patterns")

    product_fact_signal_paths = [
        path for path, text in changed_texts.items() if _has_product_fact_signal(path, text)
    ]
    if product_fact_signal_paths and not _product_fact_contract_referenced(
        changed_paths=changes,
        changed_texts=changed_texts,
    ):
        _record(
            failed,
            "product_fact_collection_contract_required_for_media_delta",
            ", ".join(product_fact_signal_paths),
        )
    else:
        _record(
            passed,
            "product_fact_collection_contract_required_for_media_delta",
            f"{len(product_fact_signal_paths)} implementation paths with product fact/media signals",
        )

    claim = "complete" if not failed else "not_complete"
    return _result(claim=claim, passed_checks=passed, failed_checks=failed), 0 if not failed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate architecture drift in the current git delta.")
    parser.add_argument(
        "--repo-root",
        default=str(REPO_ROOT),
        help="Repository root to inspect. Defaults to the current Mujitask checkout.",
    )
    args = parser.parse_args(argv)

    result, exit_code = validate(Path(args.repo_root))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
