from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ROADMAP = REPO_ROOT / "contracts" / "harness" / "code-roadmap.yaml"
DEFAULT_ARCHITECTURE_DELTA_GATE = REPO_ROOT / "scripts" / "harness" / "validate_architecture_delta.py"


def _result(
    *,
    feature_code: str,
    claim: str,
    passed_checks: list[dict[str, str]],
    failed_checks: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        "feature_code": feature_code,
        "claim": claim,
        "passed_checks": passed_checks,
        "failed_checks": failed_checks,
    }


def _record(target: list[dict[str, str]], check: str, detail: str) -> None:
    target.append({"check": check, "detail": detail})


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _resolve_path(value: str) -> Path:
    path = value.strip().rstrip("/")
    if path.endswith("/**"):
        path = path.removesuffix("/**")
    return REPO_ROOT / path


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _path_exists(value: str) -> bool:
    return _resolve_path(value).exists()


def _done_gate_items(done_gate: Any) -> tuple[list[str], list[str]]:
    if isinstance(done_gate, dict):
        return _as_list(done_gate.get("tests")), _as_list(done_gate.get("commands"))
    return [], _as_list(done_gate)


def _command_exists(command: str) -> bool:
    parts = shlex.split(command)
    if not parts:
        return False
    executable_exists = shutil.which(parts[0]) is not None or _path_exists(parts[0])
    referenced_paths = [
        part for part in parts if part.endswith(".py") or part.endswith(".yaml") or part.endswith(".md")
    ]
    return executable_exists and all(_path_exists(part) for part in referenced_paths)


def _tail(text: str, limit: int = 2000) -> str:
    cleaned = text.strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[-limit:]


def _contains_any_runtime_signal(feature_code: str, feature: dict[str, Any]) -> bool:
    area = str(feature.get("feature_area", ""))
    text = " ".join([feature_code, area]).lower()
    return any(token in text for token in ("runtime", "watchdog", "supervisor"))


def _requires_architecture_delta_gate(feature: dict[str, Any]) -> bool:
    if bool(feature.get("requires_architecture_delta_gate")):
        return True
    change_type = str(feature.get("change_type") or feature.get("feature_type") or "").lower()
    if any(token in change_type for token in ("implementation", "refactor")):
        return True
    implementation_prefixes = (
        "src/automation_business_scaffold/domains/",
        "src/automation_business_scaffold/capabilities/",
        "src/automation_business_scaffold/control_plane/",
        "src/automation_business_scaffold/business/",
    )
    allowed_paths = _as_list(feature.get("allowed_paths"))
    return any(path.startswith(implementation_prefixes) for path in allowed_paths)


def _run_architecture_delta_gate(
    passed: list[dict[str, str]],
    failed: list[dict[str, str]],
) -> None:
    command = [sys.executable, str(DEFAULT_ARCHITECTURE_DELTA_GATE)]
    architecture_repo_root = os.environ.get("HARNESS_ARCHITECTURE_DELTA_REPO_ROOT")
    if architecture_repo_root:
        command.extend(["--repo-root", architecture_repo_root])

    result = subprocess.run(
        command,
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    detail = _tail(result.stdout or result.stderr)
    if result.returncode == 0:
        _record(passed, "architecture_delta_gate", detail or "passed")
        return
    _record(failed, "architecture_delta_gate", detail or f"exit {result.returncode}")


def _run_done_gate_commands(
    commands: list[str],
    passed: list[dict[str, str]],
    failed: list[dict[str, str]],
) -> None:
    if not commands:
        _record(failed, "done_gate_commands_present_for_run", "done_gate.commands is required")
        return

    for command in commands:
        try:
            result = subprocess.run(
                shlex.split(command),
                cwd=REPO_ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:
            _record(failed, "done_gate_command_failed", f"{command}: {exc}")
            continue

        if result.returncode == 0:
            _record(passed, "done_gate_command_passed", command)
            continue

        output = "\n".join(part for part in (result.stdout, result.stderr) if part)
        _record(
            failed,
            "done_gate_command_failed",
            f"{command}: exit {result.returncode}\n{_tail(output)}",
        )


def _validate_feature(
    feature_code: str,
    feature: dict[str, Any],
    passed: list[dict[str, str]],
    failed: list[dict[str, str]],
    *,
    run_gates: bool,
) -> str:
    default_context = _as_list(feature.get("default_context"))
    missing_context = [path for path in default_context if not _path_exists(path)]
    if missing_context:
        _record(failed, "default_context_paths_exist", ", ".join(missing_context))
    else:
        _record(passed, "default_context_paths_exist", f"{len(default_context)} paths")

    source_contracts = _as_list(feature.get("source_contracts"))
    missing_contracts = [path for path in source_contracts if not _path_exists(path)]
    if missing_contracts:
        _record(failed, "source_contracts_exist", ", ".join(missing_contracts))
    else:
        _record(passed, "source_contracts_exist", f"{len(source_contracts)} paths")

    done_gate = feature.get("done_gate")
    tests, commands = _done_gate_items(done_gate)
    if not tests and not commands:
        _record(failed, "done_gate_present", "done_gate must list tests or commands")
    else:
        _record(passed, "done_gate_present", f"{len(tests)} tests, {len(commands)} commands")

    missing_tests = [path for path in tests if not _path_exists(path)]
    invalid_commands = [command for command in commands if not _command_exists(command)]
    if missing_tests or invalid_commands:
        details = []
        if missing_tests:
            details.append("missing tests: " + ", ".join(missing_tests))
        if invalid_commands:
            details.append("invalid commands: " + " | ".join(invalid_commands))
        _record(failed, "done_gate_entries_exist", "; ".join(details))
    else:
        _record(passed, "done_gate_entries_exist", f"{len(tests) + len(commands)} entries")

    allowed_paths = set(_as_list(feature.get("allowed_paths")))
    forbidden_paths = set(_as_list(feature.get("forbidden_paths")))
    overlap = sorted(allowed_paths & forbidden_paths)
    if overlap:
        _record(failed, "forbidden_paths_not_allowed", ", ".join(overlap))
    else:
        _record(passed, "forbidden_paths_not_allowed", "no overlap")

    if _contains_any_runtime_signal(feature_code, feature):
        contract_text = " ".join(source_contracts).lower()
        gate_text = " ".join(tests + commands).lower()
        has_runtime_contract = "runtime" in contract_text or "control-plane" in contract_text
        has_runtime_gate = any(token in gate_text for token in ("runtime", "watchdog", "supervisor"))
        if not has_runtime_contract or not has_runtime_gate:
            _record(
                failed,
                "runtime_features_reference_runtime_contract_and_tests",
                "runtime/watchdog/supervisor features need runtime contracts and tests",
            )
        else:
            _record(
                passed,
                "runtime_features_reference_runtime_contract_and_tests",
                "runtime contract and tests referenced",
            )

    if str(feature.get("status")) == "complete":
        if failed:
            _record(failed, "complete_requires_gate_pass", "feature is marked complete but checks failed")
            return "not_complete"
        if not run_gates:
            _record(
                failed,
                "done_gate_run_required",
                "complete claim requires --run-gates so done_gate commands actually run",
            )
            return "not_complete"
        if _requires_architecture_delta_gate(feature):
            _run_architecture_delta_gate(passed, failed)
        else:
            _record(
                passed,
                "architecture_delta_gate_not_required",
                "feature does not touch implementation/refactor owner paths",
            )
        _run_done_gate_commands(commands, passed, failed)
        if failed:
            _record(
                failed,
                "complete_requires_gate_pass",
                "feature is marked complete but gate execution failed",
            )
            return "not_complete"
        _record(passed, "complete_requires_gate_pass", "all checks passed")
        return "complete"

    return "not_complete"


def check_claim(feature_code: str, roadmap_path: Path, *, run_gates: bool = False) -> tuple[dict[str, Any], int]:
    passed: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []

    if not roadmap_path.exists():
        _record(failed, "roadmap_exists", str(roadmap_path))
        return _result(
            feature_code=feature_code,
            claim="not_complete",
            passed_checks=passed,
            failed_checks=failed,
        ), 1
    _record(passed, "roadmap_exists", _display_path(roadmap_path))

    try:
        roadmap = yaml.safe_load(roadmap_path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - exact parser errors are not important here.
        _record(failed, "roadmap_parses", f"{type(exc).__name__}: {exc}")
        return _result(
            feature_code=feature_code,
            claim="not_complete",
            passed_checks=passed,
            failed_checks=failed,
        ), 1

    if not isinstance(roadmap, dict):
        _record(failed, "roadmap_parses", "top-level YAML must be a mapping")
        return _result(
            feature_code=feature_code,
            claim="not_complete",
            passed_checks=passed,
            failed_checks=failed,
        ), 1
    _record(passed, "roadmap_parses", "yaml mapping")

    features = roadmap.get("features", [])
    if not isinstance(features, list):
        _record(failed, "features_shape", "features must be a list")
        return _result(
            feature_code=feature_code,
            claim="not_complete",
            passed_checks=passed,
            failed_checks=failed,
        ), 1

    by_code = {
        str(feature.get("feature_code")): feature
        for feature in features
        if isinstance(feature, dict) and feature.get("feature_code")
    }
    feature = by_code.get(feature_code)
    if feature is None:
        _record(failed, "feature_code_exists", feature_code)
        return _result(
            feature_code=feature_code,
            claim="not_complete",
            passed_checks=passed,
            failed_checks=failed,
        ), 1
    _record(passed, "feature_code_exists", feature_code)

    claim = _validate_feature(feature_code, feature, passed, failed, run_gates=run_gates)
    exit_code = 0 if not failed else 1
    return _result(
        feature_code=feature_code,
        claim=claim,
        passed_checks=passed,
        failed_checks=failed,
    ), exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate whether a feature can be claimed done.")
    parser.add_argument("feature_code")
    parser.add_argument(
        "--roadmap",
        default=os.environ.get("HARNESS_CODE_ROADMAP_PATH", str(DEFAULT_ROADMAP)),
        help="Path to code-roadmap.yaml. Defaults to contracts/harness/code-roadmap.yaml.",
    )
    parser.add_argument(
        "--run-gates",
        action="store_true",
        help="Actually run architecture and done_gate commands before allowing complete.",
    )
    args = parser.parse_args(argv)

    result, exit_code = check_claim(args.feature_code, Path(args.roadmap), run_gates=args.run_gates)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
