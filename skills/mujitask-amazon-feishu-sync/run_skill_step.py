#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


SCRIPT_DIR = Path(__file__).resolve().parent
ENV_FILE = SCRIPT_DIR / "skill.local.env"
LIGHTWEIGHT_SUBMIT = SCRIPT_DIR / "lightweight_submit.py"
ROW_TASK_CODE = "refresh_amazon_product_row_by_asin"
BATCH_TASK_CODE = "refresh_current_amazon_product_table"
EXPECTED_AGENT_ID = "amazon-ops"
AMAZON_TABLE_REF = "AMAZON_PRODUCTS"


def _normalize_env_value(value: str) -> str:
    normalized = value.strip().lstrip("\ufeff")
    if normalized.startswith("export "):
        normalized = normalized[len("export ") :].strip()
    if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in {'"', "'"}:
        normalized = normalized[1:-1]
    return normalized


def _load_skill_env(path: Path) -> dict[str, str]:
    if not path.exists():
        raise ValueError("Amazon skill is not configured on this workspace.")
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in raw_line:
            continue
        key, value = raw_line.split("=", 1)
        normalized_key = _normalize_env_value(key)
        if normalized_key:
            values[normalized_key] = _normalize_env_value(value)
    return values


def _required(env: dict[str, str], key: str) -> str:
    value = str(env.get(key) or "").strip()
    if not value:
        raise ValueError(f"Amazon skill configuration is missing {key}.")
    return value


def _amazon_table_refs(env: dict[str, str]) -> dict[str, str]:
    base_url = str(env.get("MUJITASK_FEISHU_AMAZON_PRODUCTS_BASE_URL") or "").strip()
    if not base_url:
        raise ValueError(
            "Amazon skill configuration is missing "
            "MUJITASK_FEISHU_AMAZON_PRODUCTS_BASE_URL."
        )
    table_id = _required(env, "MUJITASK_FEISHU_AMAZON_PRODUCTS_TABLE_ID")
    view_id = str(env.get("MUJITASK_FEISHU_AMAZON_PRODUCTS_VIEW_ID") or "").strip()
    parsed = urlparse(base_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["table"] = table_id
    if view_id:
        query["view"] = view_id
    else:
        query.pop("view", None)
    path = parsed.path.rstrip("/") or parsed.path
    table_url = urlunparse(
        (parsed.scheme, parsed.netloc, path, parsed.params, urlencode(query), parsed.fragment)
    )
    return {AMAZON_TABLE_REF: table_url}


def _load_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _explicit_delivery_context(env: dict[str, str]) -> dict[str, Any]:
    raw_json = str(
        env.get("OPENCLAW_DELIVERY_CONTEXT_JSON")
        or os.environ.get("OPENCLAW_DELIVERY_CONTEXT_JSON")
        or ""
    ).strip()
    if raw_json:
        payload = json.loads(raw_json)
        if not isinstance(payload, dict):
            raise ValueError("Amazon delivery context must be a JSON object.")
        return payload

    channel = str(
        env.get("OPENCLAW_DELIVERY_CHANNEL")
        or os.environ.get("OPENCLAW_DELIVERY_CHANNEL")
        or ""
    ).strip()
    target = str(
        env.get("OPENCLAW_DELIVERY_TO")
        or os.environ.get("OPENCLAW_DELIVERY_TO")
        or ""
    ).strip()
    account_id = str(
        env.get("OPENCLAW_DELIVERY_ACCOUNT_ID")
        or os.environ.get("OPENCLAW_DELIVERY_ACCOUNT_ID")
        or ""
    ).strip()
    if not channel and not target:
        return {}
    if not channel or not target:
        raise ValueError("Amazon delivery context requires both channel and target.")
    return {"channel": channel, "to": target, "accountId": account_id}


def _latest_agent_delivery_context(env: dict[str, str]) -> dict[str, Any]:
    agent_id = str(env.get("OPENCLAW_AGENT_ID") or EXPECTED_AGENT_ID).strip()
    if agent_id != EXPECTED_AGENT_ID:
        raise ValueError("Amazon skill must run in the amazon-ops OpenClaw agent.")
    raw_state_dir = str(env.get("OPENCLAW_STATE_DIR") or "~/.openclaw").strip()
    state_dir = Path(os.path.expandvars(raw_state_dir)).expanduser()
    sessions_dir = state_dir / "agents" / agent_id / "sessions"
    candidates = [sessions_dir / "sessions.json"]
    backups_dir = sessions_dir / "backups"
    if backups_dir.exists():
        candidates.extend(
            sorted(backups_dir.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)[:10]
        )

    latest: dict[str, Any] = {}
    latest_updated_at = -1.0
    for path in candidates:
        for session in _load_json_object(path).values():
            if not isinstance(session, dict):
                continue
            delivery = session.get("deliveryContext")
            if not isinstance(delivery, dict):
                delivery = {}
            channel = str(delivery.get("channel") or session.get("lastChannel") or "").strip()
            target = str(delivery.get("to") or session.get("lastTo") or "").strip()
            account_id = str(
                delivery.get("accountId") or session.get("lastAccountId") or ""
            ).strip()
            if not channel or not target:
                continue
            try:
                updated_at = float(session.get("updatedAt") or 0.0)
            except (TypeError, ValueError):
                updated_at = 0.0
            if updated_at < latest_updated_at:
                continue
            latest_updated_at = updated_at
            latest = {
                "channel": channel,
                "to": target,
                "accountId": account_id,
                "sessionId": str(session.get("sessionId") or "").strip(),
            }
    return latest


def _amazon_delivery_context(env: dict[str, str]) -> dict[str, Any]:
    configured_account_id = _required(env, "OPENCLAW_DELIVERY_ACCOUNT_ID")
    delivery = _explicit_delivery_context(env) or _latest_agent_delivery_context(env)
    channel = str(delivery.get("channel") or "").strip().lower()
    target = str(delivery.get("to") or delivery.get("target") or "").strip()
    account_id = str(delivery.get("accountId") or delivery.get("account_id") or "").strip()
    if (
        channel != "feishu"
        or not target.startswith("chat:oc_")
        or account_id != configured_account_id
    ):
        raise ValueError("Amazon task delivery must come from the bound Amazon Feishu group session.")
    return {
        "channel": "feishu",
        "to": target,
        "accountId": configured_account_id,
        **({"sessionId": str(delivery["sessionId"])} if delivery.get("sessionId") else {}),
    }


def _submit(*, task_code: str, source_record_id: str = "") -> dict[str, Any]:
    record_id = str(source_record_id or "").strip()
    if task_code == ROW_TASK_CODE and not record_id:
        raise ValueError("source_record_id is required.")
    env = _load_skill_env(ENV_FILE)
    install_dir = Path(os.path.expandvars(_required(env, "INSTALL_DIR"))).expanduser().resolve()
    python_bin = install_dir / ".venv" / "bin" / "python"
    if not python_bin.is_file():
        raise ValueError("Amazon task runtime is not installed.")

    delivery = _amazon_delivery_context(env)
    params: dict[str, Any] = {
        "control_action": "submit",
        "table_ref": AMAZON_TABLE_REF,
        "table_refs": _amazon_table_refs(env),
        "notification_channel_code": str(env.get("NOTIFICATION_CHANNEL_CODE") or "feishu_bot_api"),
        "source_channel_code": "feishu",
        "reply_target": json.dumps(delivery, ensure_ascii=False, separators=(",", ":")),
        "requested_by": "amazon-openclaw-skill",
    }
    if task_code == ROW_TASK_CODE:
        params["source_record_id"] = record_id
    if delivery.get("sessionId"):
        params["source_session_id"] = delivery["sessionId"]

    with tempfile.TemporaryDirectory(prefix="mujitask-amazon-submit-") as temp_dir:
        result_file = Path(temp_dir) / "result.json"
        completed = subprocess.run(
            [
                str(python_bin),
                str(LIGHTWEIGHT_SUBMIT),
                "--install-dir",
                str(install_dir),
                "--task-code",
                task_code,
                "--params-json",
                json.dumps(params, ensure_ascii=False),
                "--result-file",
                str(result_file),
            ],
            cwd=str(install_dir),
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            check=False,
        )
        result = _load_json_object(result_file)
        if completed.returncode != 0:
            raise ValueError("Amazon task submission process failed.")

    request_id = str(result.get("request_id") or "").strip()
    if not request_id:
        message = str(result.get("message") or "Amazon task submission was rejected.").strip()
        return {"status": "failed", "task_name": task_code, "message": message}
    return {
        "status": "success",
        "task_name": task_code,
        "request_id": request_id,
        "request_status": str(result.get("request_status") or "pending"),
        "message": f"request_id: {request_id}",
    }


def _emit(payload: dict[str, Any]) -> int:
    print(f"__OPENCLAW_RESULT__ {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}")
    return 1 if payload.get("status") == "failed" else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Submit Amazon OpenClaw skill tasks.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    submit_parser = subparsers.add_parser("amazon-product-row-submit")
    submit_parser.add_argument("--source-record-id", required=True)
    subparsers.add_parser("amazon-product-table-submit")
    args = parser.parse_args(argv)
    try:
        if args.command == "amazon-product-table-submit":
            return _emit(_submit(task_code=BATCH_TASK_CODE))
        return _emit(
            _submit(
                task_code=ROW_TASK_CODE,
                source_record_id=args.source_record_id,
            )
        )
    except Exception as exc:
        task_code = BATCH_TASK_CODE if args.command == "amazon-product-table-submit" else ROW_TASK_CODE
        return _emit(
            {
                "status": "failed",
                "task_name": task_code,
                "message": f"任务提交失败：{str(exc)}",
            }
        )


if __name__ == "__main__":
    raise SystemExit(main())
