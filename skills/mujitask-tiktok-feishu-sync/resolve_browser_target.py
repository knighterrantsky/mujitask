#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _normalize_env_entry(value: str) -> str:
    normalized = value.strip().lstrip("\ufeff")
    if normalized.startswith("export "):
        normalized = normalized[len("export ") :].strip()
    if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in {'"', "'"}:
        normalized = normalized[1:-1]
    return normalized


def _load_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in raw_line:
            continue
        key, value = raw_line.split("=", 1)
        normalized_key = _normalize_env_entry(key)
        if not normalized_key:
            continue
        env[normalized_key] = _normalize_env_entry(value)
    return env


def _resolve_path(install_dir: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return install_dir / path


def resolve_browser_target(
    *,
    install_dir: Path,
    profile_ref: str | None,
    fallback_profile_ref: str | None,
) -> dict[str, object]:
    install_dir = install_dir.resolve()
    env = _load_env_file(install_dir / ".env")
    browser_profiles_file = env.get("BROWSER_PROFILES_FILE", "config/browser_profiles.json")
    browser_profiles_path = _resolve_path(install_dir, browser_profiles_file)
    if not browser_profiles_path.exists():
        raise ValueError(f"Browser profiles file not found: {browser_profiles_path}")

    data = json.loads(browser_profiles_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Browser profiles file must contain a JSON object: {browser_profiles_path}")

    resolved_profile_ref = (
        (profile_ref or "").strip()
        or (fallback_profile_ref or "").strip()
        or env.get("DEFAULT_PROFILE_REF", "").strip()
    )
    if not resolved_profile_ref:
        raise ValueError(
            "No browser profile_ref provided. Pass one explicitly, set BROWSER_PROFILE_REF "
            "in skill.local.env, or configure DEFAULT_PROFILE_REF in the project .env."
        )

    payload = data.get(resolved_profile_ref)
    if not isinstance(payload, dict):
        raise ValueError(
            f"Unknown browser profile_ref '{resolved_profile_ref}' in {browser_profiles_path}"
        )

    provider = str(payload.get("provider", "") or "").strip()
    profile_id = str(payload.get("profile_id", "") or "").strip()
    workspace_id = payload.get("workspace_id")
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}

    if not provider:
        raise ValueError(f"Browser profile '{resolved_profile_ref}' is missing provider")
    if not profile_id:
        raise ValueError(f"Browser profile '{resolved_profile_ref}' is missing profile_id")

    return {
        "profile_ref": resolved_profile_ref,
        "provider": provider,
        "profile_id": profile_id,
        "workspace_id": workspace_id,
        "metadata": metadata,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="resolve_browser_target.py",
        description="Resolve one browser profile from the installed project config.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    resolve_parser = subparsers.add_parser("resolve", help="Resolve one browser target.")
    resolve_parser.add_argument("--install-dir", required=True, help="Installed project directory.")
    resolve_parser.add_argument("--profile-ref", help="Explicit browser profile ref override.")
    resolve_parser.add_argument("--fallback-profile-ref", help="Fallback browser profile ref.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "resolve":
            payload = resolve_browser_target(
                install_dir=Path(args.install_dir),
                profile_ref=args.profile_ref,
                fallback_profile_ref=args.fallback_profile_ref,
            )
            print(json.dumps(payload, ensure_ascii=False))
            return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    parser.error(f"Unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
