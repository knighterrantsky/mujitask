from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "skills" / "mujitask-tiktok-feishu-sync"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_skill_spec_schema_parses() -> None:
    schema = json.loads((REPO_ROOT / "contracts" / "skill_spec.schema.json").read_text(encoding="utf-8"))

    assert schema["title"] == "Mujitask Skill Spec"
    assert "metadata" in schema["required"]
    assert "source_of_truth" in schema["required"]
    assert "formal_task_codes" in schema["required"]
    assert "inputs" in schema["required"]
    assert "intents" in schema["required"]


def test_rendered_skill_is_up_to_date() -> None:
    result = _run("tools/render_skill.py", "--check")

    assert result.returncode == 0, result.stdout + result.stderr


def test_skill_contract_validator_passes() -> None:
    result = _run("tools/validate_skill.py", "--json")

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["errors"] == []
    assert "skills/mujitask-tiktok-feishu-sync" in payload["passed"]


def test_side_effect_skill_examples_cover_all_submit_intents() -> None:
    spec = yaml.safe_load((SKILL_DIR / "skill.spec.yaml").read_text(encoding="utf-8"))
    examples = yaml.safe_load((SKILL_DIR / "examples.eval.yaml").read_text(encoding="utf-8"))

    intent_ids = {intent["id"] for intent in spec["intents"]}
    covered_ids = {case["expected_intent"] for case in examples["cases"]}

    assert spec["metadata"]["side_effects"] is True
    assert intent_ids <= covered_ids
    assert spec["formal_task_codes"] == [
        "refresh_current_competitor_table",
        "search_keyword_competitor_products",
        "sync_tk_influencer_pool",
        "tiktok_fastmoss_product_ingest",
        "search_keyword_selection_products",
    ]
    assert "keyword_search" not in intent_ids
    assert {"keyword_competitor_search", "keyword_selection_search"} <= intent_ids
    assert any(case["expected_intent"] == "ask_target_table" for case in examples["cases"])
