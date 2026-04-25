from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
TASK_ROUTING = REPO_ROOT / "contracts" / "codex" / "task-routing.yaml"
REWRITE_STATE = REPO_ROOT / "docs" / "dev" / "rewrite-state.yaml"
MODEL_RULES = REPO_ROOT / ".platform" / "model-rules.yaml"


def _load_yaml(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), f"{path.relative_to(REPO_ROOT)} must be a YAML mapping"
    return loaded


def _context_path_exists(value: str) -> bool:
    path = value.rstrip("/")
    if path.endswith("/**"):
        path = path.removesuffix("/**")
    return (REPO_ROOT / path).exists()


def test_agents_md_declares_root_conversational_mode_and_stop_protocol() -> None:
    doc = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    required_tokens = (
        "## Root Conversational Mode",
        "contracts/codex/task-routing.yaml",
        "## Rewrite Source Of Truth",
        "src/automation_business_scaffold/domains/**",
        "src/automation_business_scaffold/business/**",
        "## Stop Protocol",
        "默认所有实现、修复、重构和治理任务都是 bounded task",
    )
    missing = [token for token in required_tokens if token not in doc]
    assert missing == [], "AGENTS.md is missing root conversational routing tokens:\n" + "\n".join(missing)


def test_rewrite_state_declares_current_runtime_owners() -> None:
    state = _load_yaml(REWRITE_STATE)

    assert state["status"] == "active_rewrite"
    assert state["current_phase"] == "domains_runtime_rewrite"
    owners = state["canonical_runtime_owners"]
    assert owners["domain_business_logic"] == "src/automation_business_scaffold/domains/**"
    assert owners["reusable_capabilities"] == "src/automation_business_scaffold/capabilities/**"
    assert owners["runtime_control_plane"] == "src/automation_business_scaffold/control_plane/**"
    assert "src/automation_business_scaffold/business/**" in state["legacy_reference"]["paths"]
    assert "no new business runtime owner files" in state["must_remain_green"]


def test_codex_task_routing_has_minimal_context_routes() -> None:
    routing = _load_yaml(TASK_ROUTING)

    assert routing["schema_version"] == 1
    assert routing["default_mode"] == "bounded_task"
    assert "stop_protocol" in routing
    route_ids = {route["route_id"] for route in routing["routing"]}
    assert {
        "tk_influencer_pool_runtime",
        "tk_competitor_runtime",
        "product_fact_ingest_runtime",
        "architecture_governance",
    } <= route_ids

    all_keywords = {
        keyword
        for route in routing["routing"]
        for keyword in route.get("match", {}).get("keywords", [])
    }
    assert {"达人池", "达人查找", "竞品", "商品状态", "选品"} <= all_keywords


def test_codex_task_routing_context_paths_exist() -> None:
    routing = _load_yaml(TASK_ROUTING)

    values: list[str] = []
    values.extend(routing.get("default_context", []))
    for route in routing["routing"]:
        values.extend(route.get("default_context", []))
        values.extend(route.get("avoid_by_default", []))

    missing = [value for value in values if not _context_path_exists(value)]
    assert missing == [], "task routing references missing paths:\n" + "\n".join(missing)


def test_platform_rules_prefer_domains_and_treat_business_as_legacy_reference() -> None:
    rules = _load_yaml(MODEL_RULES)
    business_feature = rules["change_modes"]["business_feature"]

    may_edit = set(business_feature["may_edit"])
    must_not_edit = set(business_feature["must_not_edit"])
    assert "src/automation_business_scaffold/domains/**" in may_edit
    assert "src/automation_business_scaffold/capabilities/**" in may_edit
    assert "src/automation_business_scaffold/control_plane/**" in may_edit
    assert "contracts/**" in may_edit
    assert "src/automation_business_scaffold/business/**" in must_not_edit

    legacy_reference = rules["change_modes"]["legacy_reference"]
    assert "src/automation_business_scaffold/business/**" in legacy_reference["readable"]
    assert "docs/business/**" in legacy_reference["readable"]
