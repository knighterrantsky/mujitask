from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROJECT_ENV_FILES = (
    Path("scripts/execution_control/executor.local.env"),
    Path("skills/mujitask-tiktok-feishu-sync/skill.local.env"),
    Path(".env"),
)

_BOOTSTRAPPED = False


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def load_project_env_files(
    *,
    root_dir: Path | None = None,
    env_files: Sequence[str | Path] | None = None,
    override: bool = False,
) -> dict[str, list[str]]:
    root = Path(root_dir or PROJECT_ROOT).resolve()
    candidates: Iterable[str | Path] = env_files or DEFAULT_PROJECT_ENV_FILES
    loaded: dict[str, list[str]] = {}

    for candidate in candidates:
        path = Path(candidate)
        if not path.is_absolute():
            path = root / path
        path = path.resolve()
        if not path.exists():
            continue

        applied_keys: list[str] = []
        for key, value in parse_env_file(path).items():
            if override or key not in os.environ:
                os.environ[key] = value
                applied_keys.append(key)

        if applied_keys:
            label = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
            loaded[label] = applied_keys

    return loaded


def bootstrap_project_env(*, force: bool = False) -> dict[str, list[str]]:
    global _BOOTSTRAPPED

    if _BOOTSTRAPPED and not force:
        return {}

    loaded = load_project_env_files()
    _BOOTSTRAPPED = True
    return loaded


__all__ = [
    "DEFAULT_PROJECT_ENV_FILES",
    "PROJECT_ROOT",
    "bootstrap_project_env",
    "load_project_env_files",
    "parse_env_file",
]
