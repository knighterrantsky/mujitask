#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import shutil
import tomllib
from pathlib import Path


def _normalize_env_entry(value: str) -> str:
    normalized = value.strip().lstrip("\ufeff")
    if normalized.startswith("export "):
        normalized = normalized[len("export ") :].strip()
    if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in {'"', "'"}:
        normalized = normalized[1:-1]
    return normalized


def _parse_key_value_line(raw_line: str) -> tuple[str, str] | None:
    stripped = raw_line.strip()
    if not stripped or stripped.startswith("#") or "=" not in raw_line:
        return None
    key, value = raw_line.split("=", 1)
    normalized_key = _normalize_env_entry(key)
    if not normalized_key:
        return None
    return normalized_key, _normalize_env_entry(value)


def merge_key_value_file(path: Path, managed: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    seen_keys: set[str] = set()
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)

    output_lines: list[str] = []
    for raw_line in lines:
        parsed = _parse_key_value_line(raw_line)
        if parsed is None:
            output_lines.append(raw_line)
            continue

        key, _ = parsed
        if key in managed and key not in seen_keys:
            output_lines.append(f"{key}={managed[key]}\n")
            seen_keys.add(key)
            continue

        output_lines.append(raw_line)

    for key, value in managed.items():
        if key in seen_keys:
            continue
        output_lines.append(f"{key}={value}\n")

    text = "".join(output_lines)
    if text and not text.endswith("\n"):
        text += "\n"
    path.write_text(text, encoding="utf-8")


def remove_key_value_file(path: Path, keys: list[str]) -> None:
    if not path.exists():
        return

    removed = set(keys)
    output_lines: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines(keepends=True):
        parsed = _parse_key_value_line(raw_line)
        if parsed is not None and parsed[0] in removed:
            continue
        output_lines.append(raw_line)

    text = "".join(output_lines)
    if text and not text.endswith("\n"):
        text += "\n"
    path.write_text(text, encoding="utf-8")


def write_deploy_state_file(
    path: Path,
    *,
    repo_url: str,
    resolved_ref: str,
    repo_archive_url: str,
    framework_archive_url: str,
    install_layout_version: str,
    update_supported: str,
) -> None:
    managed = {
        "REPO_URL": repo_url,
        "LAST_RESOLVED_REF": resolved_ref,
        "REPO_ARCHIVE_URL": repo_archive_url,
        "FRAMEWORK_ARCHIVE_URL": framework_archive_url,
        "INSTALL_LAYOUT_VERSION": install_layout_version,
        "UPDATE_SUPPORTED": update_supported,
    }
    merge_key_value_file(path, managed)


def read_framework_dependency(path: Path) -> dict[str, str]:
    with open(path, "rb") as handle:
        data = tomllib.load(handle)

    dependencies = data.get("project", {}).get("dependencies", [])
    matches = [dep for dep in dependencies if dep.startswith("automation-framework @ ")]
    if len(matches) != 1:
        raise ValueError(
            "pyproject.toml must declare exactly one 'automation-framework @ ...' dependency."
        )

    dependency = matches[0]
    source = dependency.split(" @ ", 1)[1].strip()
    result = {
        "dependency": dependency,
        "source": source,
        "kind": "direct",
    }
    if source.startswith("git+"):
        git_source = source[len("git+") :]
        base, _, fragment = git_source.partition("#")
        repo_url, sep, ref = base.rpartition("@")
        if sep and repo_url and ref:
            result["kind"] = "git"
            result["repo_url"] = repo_url
            result["ref"] = ref
        if fragment:
            result["fragment"] = fragment
    return result


def deploy_state_supports_update(path: Path) -> bool:
    if not path.exists():
        return False
    data = load_key_value_file(path)
    return (
        data.get("INSTALL_LAYOUT_VERSION", "").strip() == "1"
        and data.get("UPDATE_SUPPORTED", "").strip() == "1"
    )


def load_key_value_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_key_value_line(raw_line)
        if parsed is None:
            continue
        key, value = parsed
        if key not in data:
            data[key] = value
    return data


def _is_preserved_path(rel_path: Path, preserved: set[Path]) -> bool:
    return any(rel_path == candidate or candidate in rel_path.parents for candidate in preserved)


def _is_preserved_ancestor(rel_path: Path, preserved: set[Path]) -> bool:
    return any(rel_path in candidate.parents for candidate in preserved)


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _clean_target_dir(target_root: Path, current_dir: Path, preserved: set[Path]) -> None:
    for child in current_dir.iterdir():
        rel_path = child.relative_to(target_root)
        if _is_preserved_path(rel_path, preserved):
            continue
        if _is_preserved_ancestor(rel_path, preserved) and child.is_dir():
            _clean_target_dir(target_root, child, preserved)
            continue
        _remove_path(child)


def _copy_tree(source_root: Path, current_source: Path, target_root: Path, preserved: set[Path]) -> None:
    for child in current_source.iterdir():
        rel_path = child.relative_to(source_root)
        if _is_preserved_path(rel_path, preserved):
            continue

        target_path = target_root / rel_path
        if child.is_dir():
            target_path.mkdir(parents=True, exist_ok=True)
            _copy_tree(source_root, child, target_root, preserved)
            continue

        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(child, target_path)


def sync_install_tree(source_dir: Path, target_dir: Path, preserve_paths: list[str]) -> None:
    source_dir = source_dir.resolve()
    target_dir = target_dir.resolve()
    preserved = {Path(item) for item in preserve_paths}

    target_dir.mkdir(parents=True, exist_ok=True)
    _clean_target_dir(target_dir, target_dir, preserved)
    _copy_tree(source_dir, source_dir, target_dir, preserved)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Helpers for OpenClaw deploy/update scripts.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    merge_parser = subparsers.add_parser("merge-key-value-file")
    merge_parser.add_argument("--path", required=True)
    merge_parser.add_argument("--managed", action="append", default=[], metavar="KEY=VALUE")

    remove_parser = subparsers.add_parser("remove-key-value-file")
    remove_parser.add_argument("--path", required=True)
    remove_parser.add_argument("--key", action="append", default=[], required=True)

    deploy_state_parser = subparsers.add_parser("write-deploy-state")
    deploy_state_parser.add_argument("--path", required=True)
    deploy_state_parser.add_argument("--repo-url", required=True)
    deploy_state_parser.add_argument("--resolved-ref", required=True)
    deploy_state_parser.add_argument("--repo-archive-url", default="")
    deploy_state_parser.add_argument("--framework-archive-url", default="")
    deploy_state_parser.add_argument("--install-layout-version", default="1")
    deploy_state_parser.add_argument("--update-supported", default="1")

    framework_parser = subparsers.add_parser("read-framework-dependency")
    framework_parser.add_argument("--path", required=True)

    support_parser = subparsers.add_parser("check-update-support")
    support_parser.add_argument("--path", required=True)

    sync_parser = subparsers.add_parser("sync-install-tree")
    sync_parser.add_argument("--source", required=True)
    sync_parser.add_argument("--target", required=True)
    sync_parser.add_argument("--preserve", action="append", default=[])
    return parser


def _parse_managed_pairs(items: list[str]) -> dict[str, str]:
    managed: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"Invalid managed entry: {item}")
        key, value = item.split("=", 1)
        key = _normalize_env_entry(key)
        if not key:
            raise ValueError(f"Managed entry key cannot be empty: {item}")
        managed[key] = value
    return managed


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.command == "merge-key-value-file":
        merge_key_value_file(Path(args.path), _parse_managed_pairs(args.managed))
        return 0
    if args.command == "remove-key-value-file":
        remove_key_value_file(Path(args.path), list(args.key))
        return 0

    if args.command == "write-deploy-state":
        write_deploy_state_file(
            Path(args.path),
            repo_url=args.repo_url,
            resolved_ref=args.resolved_ref,
            repo_archive_url=args.repo_archive_url,
            framework_archive_url=args.framework_archive_url,
            install_layout_version=args.install_layout_version,
            update_supported=args.update_supported,
        )
        return 0

    if args.command == "read-framework-dependency":
        print(json.dumps(read_framework_dependency(Path(args.path)), ensure_ascii=False))
        return 0

    if args.command == "check-update-support":
        return 0 if deploy_state_supports_update(Path(args.path)) else 1

    if args.command == "sync-install-tree":
        sync_install_tree(Path(args.source), Path(args.target), args.preserve)
        return 0

    raise ValueError(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
