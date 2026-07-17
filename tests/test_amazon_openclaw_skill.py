from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "skills" / "mujitask-amazon-feishu-sync" / "run_skill_step.py"
SPEC = importlib.util.spec_from_file_location("amazon_openclaw_skill", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_explicit_delivery_context_requires_bound_amazon_group() -> None:
    delivery = MODULE._amazon_delivery_context(
        {
            "OPENCLAW_DELIVERY_ACCOUNT_ID": "default",
            "OPENCLAW_DELIVERY_CHANNEL": "feishu",
            "OPENCLAW_DELIVERY_TO": "chat:oc_amazon",
        }
    )

    assert delivery == {
        "channel": "feishu",
        "to": "chat:oc_amazon",
        "accountId": "default",
    }


def test_delivery_context_rejects_direct_chat() -> None:
    with pytest.raises(ValueError, match="Amazon Feishu group session"):
        MODULE._amazon_delivery_context(
            {
                "OPENCLAW_DELIVERY_ACCOUNT_ID": "default",
                "OPENCLAW_DELIVERY_CHANNEL": "feishu",
                "OPENCLAW_DELIVERY_TO": "user:ou_amazon",
            }
        )


def test_session_discovery_reads_only_amazon_agent_workspace_state(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "agents" / "amazon-ops" / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "sessions.json").write_text(
        json.dumps(
            {
                "agent:amazon-ops:feishu:group:oc_amazon": {
                    "sessionId": "session-amazon",
                    "updatedAt": 100,
                    "deliveryContext": {
                        "channel": "feishu",
                        "to": "chat:oc_amazon",
                        "accountId": "default",
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    delivery = MODULE._latest_agent_delivery_context(
        {"OPENCLAW_AGENT_ID": "amazon-ops", "OPENCLAW_STATE_DIR": str(tmp_path)}
    )

    assert delivery == {
        "channel": "feishu",
        "to": "chat:oc_amazon",
        "accountId": "default",
        "sessionId": "session-amazon",
    }


def test_session_discovery_rejects_non_amazon_agent_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="amazon-ops"):
        MODULE._latest_agent_delivery_context(
            {"OPENCLAW_AGENT_ID": "tiktok-ops", "OPENCLAW_STATE_DIR": str(tmp_path)}
        )


def test_batch_command_submits_the_fixed_amazon_table_task(monkeypatch, capsys) -> None:
    calls = []

    def fake_submit(**kwargs):
        calls.append(kwargs)
        return {
            "status": "success",
            "task_name": MODULE.BATCH_TASK_CODE,
            "request_id": "req-batch",
        }

    monkeypatch.setattr(MODULE, "_submit", fake_submit)

    assert MODULE.main(["amazon-product-table-submit"]) == 0
    assert calls == [{"task_code": "refresh_current_amazon_product_table"}]
    assert "req-batch" in capsys.readouterr().out
