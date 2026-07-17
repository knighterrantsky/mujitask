from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "skills" / "mujitask-amazon-feishu-sync" / "run_skill_step.py"
SPEC = importlib.util.spec_from_file_location("amazon_openclaw_skill", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_amazon_table_refs_are_composed_from_skill_env() -> None:
    table_refs = MODULE._amazon_table_refs(
        {
            "MUJITASK_FEISHU_AMAZON_PRODUCTS_BASE_URL": (
                "https://client.feishu.cn/base/appAmazon?from=skill"
            ),
            "MUJITASK_FEISHU_AMAZON_PRODUCTS_TABLE_ID": "tblAmazon",
            "MUJITASK_FEISHU_AMAZON_PRODUCTS_VIEW_ID": "vewAmazon",
        }
    )

    assert table_refs == {
        "AMAZON_PRODUCTS": (
            "https://client.feishu.cn/base/appAmazon?from=skill&table=tblAmazon&view=vewAmazon"
        )
    }


def test_amazon_table_refs_do_not_fall_back_to_generic_project_base() -> None:
    with pytest.raises(ValueError, match="MUJITASK_FEISHU_AMAZON_PRODUCTS_BASE_URL"):
        MODULE._amazon_table_refs(
            {
                "MUJITASK_FEISHU_BASE_URL": "https://project.feishu.cn/base/appProject",
                "MUJITASK_FEISHU_AMAZON_PRODUCTS_TABLE_ID": "tblAmazon",
            }
        )


def test_submit_includes_amazon_table_route_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "mujitask"
    python_bin = install_dir / ".venv" / "bin" / "python"
    python_bin.parent.mkdir(parents=True)
    python_bin.touch()
    env_file = tmp_path / "skill.local.env"
    env_file.write_text(
        f'INSTALL_DIR="{install_dir}"\n'
        'OPENCLAW_DELIVERY_ACCOUNT_ID="client-amazon-bot"\n'
        'MUJITASK_FEISHU_AMAZON_PRODUCTS_BASE_URL="https://client.feishu.cn/base/appAmazon"\n'
        'MUJITASK_FEISHU_AMAZON_PRODUCTS_TABLE_ID="tblAmazon"\n'
        'MUJITASK_FEISHU_AMAZON_PRODUCTS_VIEW_ID="vewAmazon"\n',
        encoding="utf-8",
    )
    submitted_params: list[dict[str, object]] = []

    def fake_run(command, **_kwargs):
        params_index = command.index("--params-json") + 1
        submitted_params.append(json.loads(command[params_index]))
        result_index = command.index("--result-file") + 1
        Path(command[result_index]).write_text(
            json.dumps({"request_id": "req-amazon", "request_status": "pending"}),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(MODULE, "ENV_FILE", env_file)
    monkeypatch.setattr(
        MODULE,
        "_amazon_delivery_context",
        lambda _env: {
            "channel": "feishu",
            "to": "chat:oc_amazon",
            "accountId": "client-amazon-bot",
        },
    )
    monkeypatch.setattr(MODULE.subprocess, "run", fake_run)

    result = MODULE._submit(task_code=MODULE.BATCH_TASK_CODE)

    assert result["request_id"] == "req-amazon"
    assert submitted_params[0]["table_refs"] == {
        "AMAZON_PRODUCTS": (
            "https://client.feishu.cn/base/appAmazon?table=tblAmazon&view=vewAmazon"
        )
    }


def test_explicit_delivery_context_requires_bound_amazon_group() -> None:
    delivery = MODULE._amazon_delivery_context(
        {
            "OPENCLAW_DELIVERY_ACCOUNT_ID": "client-amazon-bot",
            "OPENCLAW_DELIVERY_CHANNEL": "feishu",
            "OPENCLAW_DELIVERY_TO": "chat:oc_amazon",
        }
    )

    assert delivery == {
        "channel": "feishu",
        "to": "chat:oc_amazon",
        "accountId": "client-amazon-bot",
    }


def test_delivery_context_rejects_direct_chat() -> None:
    with pytest.raises(ValueError, match="Amazon Feishu group session"):
        MODULE._amazon_delivery_context(
            {
                "OPENCLAW_DELIVERY_ACCOUNT_ID": "client-amazon-bot",
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
                        "accountId": "client-amazon-bot",
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
        "accountId": "client-amazon-bot",
        "sessionId": "session-amazon",
    }


def test_delivery_context_rejects_session_from_another_local_account() -> None:
    with pytest.raises(ValueError, match="Amazon Feishu group session"):
        MODULE._amazon_delivery_context(
            {
                "OPENCLAW_DELIVERY_ACCOUNT_ID": "client-amazon-bot",
                "OPENCLAW_DELIVERY_CONTEXT_JSON": json.dumps(
                    {
                        "channel": "feishu",
                        "to": "chat:oc_amazon",
                        "accountId": "another-local-account",
                    }
                ),
            }
        )


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
