from __future__ import annotations

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = REPO_ROOT / "contracts" / "agents" / "business-agent-bindings.yaml"


def test_amazon_agent_binding_is_physically_isolated_from_tiktok() -> None:
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    bindings = contract["business_domains"]
    amazon = bindings["amazon"]
    tiktok = bindings["tiktok"]

    assert amazon["skill_code"] == "mujitask-amazon-feishu-sync"
    assert amazon["openclaw_agent_id"] == "amazon-ops"
    assert amazon["workspace_name"] == "workspace-amazon"
    assert amazon["feishu_account_id"] == "default"
    assert amazon["routing_scope"] == "exact_peer"
    assert amazon["required_peer_kind"] == "group"
    assert amazon["peer_id_source"] == "openclaw_deployment_binding"
    assert amazon["required_channel"] == "feishu"
    assert amazon["shared_runtime"] is True
    assert amazon["allowed_task_domains"] == ["amazon"]
    assert amazon["forbidden_skill_codes"] == ["mujitask-tiktok-feishu-sync"]

    assert amazon["skill_code"] != tiktok["skill_code"]
    assert amazon["openclaw_agent_id"] != tiktok["openclaw_agent_id"]
    assert amazon["workspace_name"] != tiktok["workspace_name"]
    assert amazon["feishu_account_id"] == tiktok["feishu_account_id"]
    assert amazon["routing_scope"] != tiktok["routing_scope"]


def test_amazon_workflow_contract_routes_through_amazon_agent_artifact() -> None:
    workflow = yaml.safe_load(
        (REPO_ROOT / "contracts" / "workflow" / "refresh_amazon_product_row_by_asin.yaml").read_text(
            encoding="utf-8"
        )
    )

    assert workflow["agent_artifact"] == {
        "skill_code": "mujitask-amazon-feishu-sync",
        "path": "skills/mujitask-amazon-feishu-sync",
        "openclaw_agent_id": "amazon-ops",
        "workspace_name": "workspace-amazon",
        "feishu_account_id": "default",
        "feishu_peer_kind": "group",
        "peer_id_source": "openclaw_deployment_binding",
    }
    assert workflow["notification_routing"]["required_reply_channel"] == "feishu"
    assert workflow["notification_routing"]["required_account_id"] == "default"
    assert workflow["notification_routing"]["required_target_prefix"] == "chat:oc_"


def test_amazon_batch_workflow_uses_the_same_isolated_agent_route() -> None:
    workflow = yaml.safe_load(
        (
            REPO_ROOT
            / "contracts"
            / "workflow"
            / "refresh_current_amazon_product_table.yaml"
        ).read_text(encoding="utf-8")
    )

    assert workflow["agent_artifact"]["skill_code"] == "mujitask-amazon-feishu-sync"
    assert workflow["agent_artifact"]["openclaw_agent_id"] == "amazon-ops"
    assert workflow["agent_artifact"]["feishu_account_id"] == "default"
    assert workflow["selection"] == {
        "table_display_name": "Amazon竞品表",
        "field_name": "采集标签",
        "comparison": "exact_text",
        "include_value": "T",
        "missing_or_other_value": "exclude",
        "asin_validation": "trim_uppercase_then_match_10_alphanumeric",
    }
