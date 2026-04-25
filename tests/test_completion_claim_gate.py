from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
CLAIM_DONE = REPO_ROOT / "scripts" / "harness" / "claim_done.py"


def _run_claim(
    feature_code: str,
    *,
    roadmap: Path | None = None,
    extra_args: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if roadmap is not None:
        env["HARNESS_CODE_ROADMAP_PATH"] = str(roadmap)
    command = [sys.executable, str(CLAIM_DONE), feature_code]
    if extra_args:
        command.extend(extra_args)
    return subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _payload(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    return json.loads(result.stdout)


def test_unknown_feature_code_fails() -> None:
    result = _run_claim("unknown_feature_code")
    payload = _payload(result)

    assert result.returncode != 0
    assert payload["claim"] == "not_complete"
    assert payload["feature_code"] == "unknown_feature_code"
    assert payload["failed_checks"]


def test_harness_completion_claim_gate_returns_structured_result() -> None:
    result = _run_claim("harness_completion_claim_gate")
    payload = _payload(result)

    assert result.returncode in {0, 1}
    assert payload["claim"] in {"complete", "not_complete"}
    assert isinstance(payload["passed_checks"], list)
    assert isinstance(payload["failed_checks"], list)


def test_missing_context_feature_fails(tmp_path: Path) -> None:
    roadmap = {
        "schema_version": 1,
        "current_phase": "test",
        "features": [
            {
                "feature_code": "missing_context",
                "status": "complete",
                "default_context": ["missing/context.md"],
                "source_contracts": ["AGENTS.md"],
                "allowed_paths": ["tests/**"],
                "forbidden_paths": [],
                "done_gate": {"tests": ["tests/test_completion_claim_gate.py"]},
            }
        ],
    }
    path = tmp_path / "code-roadmap.yaml"
    path.write_text(yaml.safe_dump(roadmap, allow_unicode=True), encoding="utf-8")

    result = _run_claim("missing_context", roadmap=path)
    payload = _payload(result)

    assert result.returncode != 0
    assert payload["claim"] == "not_complete"
    assert any(
        item["check"] == "default_context_paths_exist"
        for item in payload["failed_checks"]
    )


def test_complete_claim_requires_done_gate(tmp_path: Path) -> None:
    roadmap = {
        "schema_version": 1,
        "current_phase": "test",
        "features": [
            {
                "feature_code": "complete_without_gate",
                "status": "complete",
                "default_context": ["AGENTS.md"],
                "source_contracts": ["AGENTS.md"],
                "allowed_paths": ["tests/**"],
                "forbidden_paths": [],
            }
        ],
    }
    path = tmp_path / "code-roadmap.yaml"
    path.write_text(yaml.safe_dump(roadmap, allow_unicode=True), encoding="utf-8")

    result = _run_claim("complete_without_gate", roadmap=path)
    payload = _payload(result)

    assert result.returncode != 0
    assert payload["claim"] == "not_complete"
    assert any(item["check"] == "done_gate_present" for item in payload["failed_checks"])


def test_complete_claim_requires_run_gates(tmp_path: Path) -> None:
    roadmap = {
        "schema_version": 1,
        "current_phase": "test",
        "features": [
            {
                "feature_code": "complete_without_running_gates",
                "status": "complete",
                "default_context": ["AGENTS.md"],
                "source_contracts": ["AGENTS.md"],
                "allowed_paths": ["tests/**"],
                "forbidden_paths": [],
                "done_gate": {
                    "tests": ["tests/test_completion_claim_gate.py"],
                    "commands": [f"{sys.executable} -c \"print('ok')\""],
                },
            }
        ],
    }
    path = tmp_path / "code-roadmap.yaml"
    path.write_text(yaml.safe_dump(roadmap, allow_unicode=True), encoding="utf-8")

    result = _run_claim("complete_without_running_gates", roadmap=path)
    payload = _payload(result)

    assert result.returncode != 0
    assert payload["claim"] == "not_complete"
    assert any(item["check"] == "done_gate_run_required" for item in payload["failed_checks"])


def test_run_gates_executes_done_gate_commands(tmp_path: Path) -> None:
    roadmap = {
        "schema_version": 1,
        "current_phase": "test",
        "features": [
            {
                "feature_code": "command_failure",
                "status": "complete",
                "feature_type": "repository_governance",
                "default_context": ["AGENTS.md"],
                "source_contracts": ["AGENTS.md"],
                "allowed_paths": ["tests/**"],
                "forbidden_paths": [],
                "done_gate": {
                    "tests": ["tests/test_completion_claim_gate.py"],
                    "commands": [f"{sys.executable} -c \"import sys; sys.exit(3)\""],
                },
            }
        ],
    }
    path = tmp_path / "code-roadmap.yaml"
    path.write_text(yaml.safe_dump(roadmap, allow_unicode=True), encoding="utf-8")

    result = _run_claim("command_failure", roadmap=path, extra_args=["--run-gates"])
    payload = _payload(result)

    assert result.returncode != 0
    assert payload["claim"] == "not_complete"
    assert any(item["check"] == "done_gate_command_failed" for item in payload["failed_checks"])
