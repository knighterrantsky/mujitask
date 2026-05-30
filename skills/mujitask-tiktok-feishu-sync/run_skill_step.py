#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


SCRIPT_DIR = Path(__file__).resolve().parent
ENV_FILE = SCRIPT_DIR / "skill.local.env"
RESOLVE_BROWSER_TARGET = SCRIPT_DIR / "resolve_browser_target.py"
LIGHTWEIGHT_SUBMIT_HELPER = SCRIPT_DIR / "lightweight_submit.py"

DEFAULT_OPENCLAW_AGENT_ID = "tiktok-ops"
FEISHU_ACCESS_TOKEN_ENV = "MUJITASK_FEISHU_ACCESS_TOKEN"
FEISHU_TABLE_REF_PREFIX = "feishu://mujitask/"
DEFAULT_URL_FIELD_NAME = "产品链接"

TK_SELECTION_TABLE_ALIAS = "tk_selection"
TK_COMPETITOR_TABLE_ALIAS = "tk_competitor"
TK_INFLUENCER_POOL_TABLE_ALIAS = "tk_influencer_pool"
TK_INFLUENCER_OUTREACH_TABLE_ALIAS = "tk_influencer_outreach"
TK_HOT_VIDEO_TABLE_ALIAS = "tk_hot_video"

TK_FEISHU_TABLE_ENV_SLUGS = {
    TK_SELECTION_TABLE_ALIAS: "TK_SELECTION",
    TK_COMPETITOR_TABLE_ALIAS: "TK_COMPETITOR",
    TK_INFLUENCER_POOL_TABLE_ALIAS: "TK_INFLUENCER_POOL",
    TK_INFLUENCER_OUTREACH_TABLE_ALIAS: "TK_INFLUENCER_OUTREACH",
    TK_HOT_VIDEO_TABLE_ALIAS: "TK_HOT_VIDEO",
}

KEYWORD_COMPETITOR_SEARCH_INTENT = "keyword_competitor_search"
KEYWORD_SELECTION_SEARCH_INTENT = "keyword_selection_search"
BATCH_KEYWORD_MAX_ITEMS = 50
BATCH_KEYWORD_ALLOWED_ITEM_KEYS = {"search_keyword", "threshold_type", "threshold_value"}
BATCH_KEYWORD_ALLOWED_THRESHOLD_TYPES = {"", "sales_7d", "total_sales"}


def _normalize_env_entry(value: str) -> str:
    normalized = value.strip().lstrip("\ufeff")
    if normalized.startswith("export "):
        normalized = normalized[len("export ") :].strip()
    if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in {'"', "'"}:
        normalized = normalized[1:-1]
    return normalized


def _load_skill_env(path: Path) -> dict[str, str]:
    if not path.exists():
        raise ValueError(f"Missing {path}. Copy skill.local.env.example and fill it first.")

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in raw_line:
            continue
        key, value = raw_line.split("=", 1)
        normalized_key = _normalize_env_entry(key)
        if normalized_key:
            values[normalized_key] = _normalize_env_entry(value)
    return values


def _require_env_value(env: dict[str, str], key: str) -> str:
    value = str(env.get(key, "") or "").strip()
    if not value:
        raise ValueError(f"{key} is required in {ENV_FILE}.")
    return value


def _optional_env_value(env: dict[str, str], key: str) -> str:
    return str(env.get(key, "") or "").strip()


def _json_compact(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _feishu_table_ref(table_alias: str) -> str:
    return f"{FEISHU_TABLE_REF_PREFIX}{table_alias}"


def _compose_feishu_table_url(base_url: str, table_id: str, view_id: str = "") -> str:
    base = str(base_url or "").strip()
    table = str(table_id or "").strip()
    view = str(view_id or "").strip()
    if not base or not table:
        return ""
    parsed = urlparse(base)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["table"] = table
    if view:
        query["view"] = view
    else:
        query.pop("view", None)
    path = parsed.path.rstrip("/") or parsed.path
    return urlunparse((parsed.scheme, parsed.netloc, path, parsed.params, urlencode(query), parsed.fragment))


def _feishu_table_env_key(table_alias: str, suffix: str) -> str:
    return f"MUJITASK_FEISHU_{TK_FEISHU_TABLE_ENV_SLUGS[table_alias]}_{suffix}"


def _configured_feishu_table_url(*, skill_env: dict[str, str], table_alias: str) -> str:
    table_id = _optional_env_value(skill_env, _feishu_table_env_key(table_alias, "TABLE_ID"))
    view_id = _optional_env_value(skill_env, _feishu_table_env_key(table_alias, "VIEW_ID"))
    base_url = _optional_env_value(skill_env, "MUJITASK_FEISHU_BASE_URL")
    return _compose_feishu_table_url(base_url, table_id, view_id)


def _load_feishu_table_refs(skill_env: dict[str, str]) -> dict[str, Any]:
    table_refs: dict[str, Any] = {}
    for table_alias in TK_FEISHU_TABLE_ENV_SLUGS:
        table_url = _configured_feishu_table_url(skill_env=skill_env, table_alias=table_alias)
        if not table_url:
            continue
        table_refs[table_alias] = table_url
        table_refs[_feishu_table_ref(table_alias)] = table_url
    return table_refs


def _table_url_from_ref(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return str(value.get("table_url") or "").strip()
    return ""


def _resolve_table_url(
    skill_env: dict[str, str],
    table_refs: dict[str, Any],
    table_alias: str,
) -> str:
    for key in (table_alias, _feishu_table_ref(table_alias)):
        table_url = _table_url_from_ref(table_refs.get(key))
        if table_url:
            return table_url
    raise ValueError(
        f"{table_alias} table route is required. Configure MUJITASK_FEISHU_BASE_URL with "
        f"{_feishu_table_env_key(table_alias, 'TABLE_ID')} and {_feishu_table_env_key(table_alias, 'VIEW_ID')}."
    )


def _append_feishu_table_refs(params: list[str], table_refs: dict[str, Any]) -> list[str]:
    if table_refs:
        params.append(f"table_refs={_json_compact(table_refs)}")
    return params


def _load_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _openclaw_state_dir(skill_env: dict[str, str]) -> Path:
    configured = (
        _optional_env_value(skill_env, "OPENCLAW_STATE_DIR")
        or str(os.environ.get("OPENCLAW_STATE_DIR", "")).strip()
    )
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".openclaw"


def _discover_openclaw_delivery_context(skill_env: dict[str, str]) -> dict[str, Any]:
    raw_json = (
        _optional_env_value(skill_env, "OPENCLAW_DELIVERY_CONTEXT_JSON")
        or str(os.environ.get("OPENCLAW_DELIVERY_CONTEXT_JSON", "")).strip()
    )
    if raw_json:
        try:
            payload = json.loads(raw_json)
        except Exception as exc:
            raise ValueError("OPENCLAW_DELIVERY_CONTEXT_JSON is not valid JSON.") from exc
        if not isinstance(payload, dict):
            raise ValueError("OPENCLAW_DELIVERY_CONTEXT_JSON must be a JSON object.")
        return payload

    explicit_channel = (
        _optional_env_value(skill_env, "OPENCLAW_DELIVERY_CHANNEL")
        or str(os.environ.get("OPENCLAW_DELIVERY_CHANNEL", "")).strip()
    )
    explicit_to = (
        _optional_env_value(skill_env, "OPENCLAW_DELIVERY_TO")
        or str(os.environ.get("OPENCLAW_DELIVERY_TO", "")).strip()
    )
    explicit_account = (
        _optional_env_value(skill_env, "OPENCLAW_DELIVERY_ACCOUNT_ID")
        or str(os.environ.get("OPENCLAW_DELIVERY_ACCOUNT_ID", "")).strip()
    )
    explicit_session_id = (
        _optional_env_value(skill_env, "OPENCLAW_DELIVERY_SESSION_ID")
        or str(os.environ.get("OPENCLAW_DELIVERY_SESSION_ID", "")).strip()
    )
    if explicit_channel and explicit_to:
        payload: dict[str, Any] = {"channel": explicit_channel, "to": explicit_to}
        if explicit_account:
            payload["accountId"] = explicit_account
        if explicit_session_id:
            payload["sessionId"] = explicit_session_id
        return payload

    agent_id = (
        _optional_env_value(skill_env, "OPENCLAW_AGENT_ID")
        or str(os.environ.get("OPENCLAW_AGENT_ID", "")).strip()
        or DEFAULT_OPENCLAW_AGENT_ID
    )
    sessions_dir = _openclaw_state_dir(skill_env) / "agents" / agent_id / "sessions"
    candidate_files = [sessions_dir / "sessions.json"]
    backups_dir = sessions_dir / "backups"
    if backups_dir.exists():
        candidate_files.extend(
            sorted(backups_dir.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)[:10]
        )

    best_candidate: dict[str, Any] = {}
    best_updated_at = -1.0
    for path in candidate_files:
        sessions_payload = _load_json_object(path)
        for session_key, session_payload in sessions_payload.items():
            if not isinstance(session_payload, dict):
                continue
            delivery = session_payload.get("deliveryContext")
            if not isinstance(delivery, dict):
                delivery = {}
            channel = str(delivery.get("channel") or session_payload.get("lastChannel") or "").strip()
            target = str(delivery.get("to") or session_payload.get("lastTo") or "").strip()
            account_id = str(delivery.get("accountId") or session_payload.get("lastAccountId") or "").strip()
            session_id = str(session_payload.get("sessionId") or "").strip()
            if not channel or not target:
                continue
            try:
                updated_at = float(session_payload.get("updatedAt") or 0.0)
            except (TypeError, ValueError):
                updated_at = 0.0
            if updated_at < best_updated_at:
                continue
            best_updated_at = updated_at
            best_candidate = {
                "channel": channel,
                "to": target,
                "accountId": account_id,
                "sessionId": session_id,
                "sessionKey": str(session_key),
                "source": "openclaw_session_store",
            }
    return best_candidate


def _resolve_browser_target(
    *,
    python_bin: Path,
    install_dir: Path,
    requested_profile_ref: str,
    fallback_profile_ref: str = "",
) -> dict[str, Any]:
    command = [
        str(python_bin),
        str(RESOLVE_BROWSER_TARGET),
        "resolve",
        "--install-dir",
        str(install_dir),
    ]
    if requested_profile_ref:
        command.extend(["--profile-ref", requested_profile_ref])
    if fallback_profile_ref:
        command.extend(["--fallback-profile-ref", fallback_profile_ref])

    result = subprocess.run(command, check=True, capture_output=True, text=True)
    payload = json.loads(result.stdout)
    return payload if isinstance(payload, dict) else {}


def _resolve_profile_ref_for_task(
    *,
    python_bin: Path,
    install_dir: Path,
    requested_profile_ref: str,
    fallback_profile_ref: str = "",
    ensure_ready: bool = False,
) -> str:
    del ensure_ready
    browser_target = _resolve_browser_target(
        python_bin=python_bin,
        install_dir=install_dir,
        requested_profile_ref=requested_profile_ref,
        fallback_profile_ref=fallback_profile_ref,
    )
    return str(browser_target["profile_ref"])


def _refresh_competitor_submit_params(
    *,
    python_bin: Path,
    install_dir: Path,
    requested_profile_ref: str,
    fallback_profile_ref: str = "",
    ensure_ready: bool = False,
) -> list[str]:
    resolved_profile_ref = _resolve_profile_ref_for_task(
        python_bin=python_bin,
        install_dir=install_dir,
        requested_profile_ref=requested_profile_ref,
        fallback_profile_ref=fallback_profile_ref,
        ensure_ready=ensure_ready,
    )
    return [
        f"profile_ref={resolved_profile_ref}",
        "verify_fastmoss_login=false",
        "fastmoss_phone_env=FASTMOSS_PHONE",
        "fastmoss_password_env=FASTMOSS_PASSWORD",
        "fastmoss_window_days=90",
    ]


def _product_url_complete_submit_params(
    *,
    python_bin: Path,
    install_dir: Path,
    requested_profile_ref: str,
    fallback_profile_ref: str = "",
    ensure_ready: bool = False,
) -> list[str]:
    resolved_profile_ref = _resolve_profile_ref_for_task(
        python_bin=python_bin,
        install_dir=install_dir,
        requested_profile_ref=requested_profile_ref,
        fallback_profile_ref=fallback_profile_ref,
        ensure_ready=ensure_ready,
    )
    return [
        f"profile_ref={resolved_profile_ref}",
        "verify_fastmoss_login=false",
        "fastmoss_phone_env=FASTMOSS_PHONE",
        "fastmoss_password_env=FASTMOSS_PASSWORD",
        "fastmoss_window_days=90",
        "fallback_allowed=true",
    ]


def _keyword_search_submit_params(
    *,
    python_bin: Path,
    install_dir: Path,
    requested_profile_ref: str,
    fallback_profile_ref: str = "",
    search_keyword: str,
    sales_7d_threshold: str,
    total_sales_threshold: str = "",
    max_candidates: str = "",
    product_price_threshold: str = "",
    keyword_workflow_mode: str = "",
    skip_fastmoss_login_validation: bool = False,
    ensure_ready: bool = False,
) -> list[str]:
    resolved_profile_ref = _resolve_profile_ref_for_task(
        python_bin=python_bin,
        install_dir=install_dir,
        requested_profile_ref=requested_profile_ref,
        fallback_profile_ref=fallback_profile_ref,
        ensure_ready=ensure_ready,
    )
    params = [
        f"profile_ref={resolved_profile_ref}",
        f"search_keyword={search_keyword}",
    ]
    if total_sales_threshold != "":
        params.append(f"total_sales_threshold={total_sales_threshold}")
        if sales_7d_threshold != "":
            params.append(f"sales_7d_threshold={sales_7d_threshold}")
    else:
        params.append(f"sales_7d_threshold={sales_7d_threshold}")
    params.extend(
        [
            "fastmoss_phone_env=FASTMOSS_PHONE",
            "fastmoss_password_env=FASTMOSS_PASSWORD",
        ]
    )
    if max_candidates != "":
        params.append(f"max_candidates={max_candidates}")
    if product_price_threshold != "":
        params.append(f"product_price_threshold={product_price_threshold}")
    if keyword_workflow_mode:
        params.append(f"keyword_workflow_mode={keyword_workflow_mode}")
    if skip_fastmoss_login_validation:
        params.append("verify_fastmoss_login=false")
    return params


def _influencer_pool_sync_submit_params(
    *,
    skill_env: dict[str, str],
    include_submit_control_action: bool = True,
    max_source_rows: int = 0,
    max_author_pages: int = 0,
    max_author_detail_jobs_per_source_row: int = 0,
    queue_mode: str = "inline",
    worker_kinds: str = "",
    worker_max_iterations: int = 1,
    worker_stop_when_idle: bool | None = None,
    include_contact: bool = False,
    request_delay_min_seconds: float = 1.0,
    request_delay_max_seconds: float = 3.0,
) -> tuple[list[str], dict[str, str]]:
    table_refs = _load_feishu_table_refs(skill_env)
    source_table_url = _resolve_table_url(skill_env, table_refs, TK_COMPETITOR_TABLE_ALIAS)
    target_table_url = _resolve_table_url(skill_env, table_refs, TK_INFLUENCER_POOL_TABLE_ALIAS)
    outreach_table_url = _resolve_table_url(skill_env, table_refs, TK_INFLUENCER_OUTREACH_TABLE_ALIAS)
    hot_video_table_url = _resolve_table_url(skill_env, table_refs, TK_HOT_VIDEO_TABLE_ALIAS)
    config = {
        "feishu_access_token_env": _optional_env_value(skill_env, "INFLUENCER_POOL_FEISHU_ACCESS_TOKEN_ENV")
        or FEISHU_ACCESS_TOKEN_ENV,
        "feishu_access_token": _require_env_value(skill_env, FEISHU_ACCESS_TOKEN_ENV),
        "fastmoss_phone_env": _optional_env_value(skill_env, "INFLUENCER_POOL_FASTMOSS_PHONE_ENV")
        or "FASTMOSS_PHONE",
        "fastmoss_password_env": _optional_env_value(skill_env, "INFLUENCER_POOL_FASTMOSS_PASSWORD_ENV")
        or "FASTMOSS_PASSWORD",
        "fastmoss_phone": _optional_env_value(skill_env, "FASTMOSS_PHONE"),
        "fastmoss_password": _optional_env_value(skill_env, "FASTMOSS_PASSWORD"),
    }
    source_table_ref = _feishu_table_ref(TK_COMPETITOR_TABLE_ALIAS)
    target_table_ref = _feishu_table_ref(TK_INFLUENCER_POOL_TABLE_ALIAS)
    outreach_table_ref = _feishu_table_ref(TK_INFLUENCER_OUTREACH_TABLE_ALIAS)
    hot_video_table_ref = _feishu_table_ref(TK_HOT_VIDEO_TABLE_ALIAS)
    params = _append_feishu_table_refs(
        [
            f"source_table_ref={source_table_ref}",
            f"target_table_ref={target_table_ref}",
            f"outreach_table_ref={outreach_table_ref}",
            f"hot_video_table_ref={hot_video_table_ref}",
            f"table_url={source_table_url}",
            f"target_table_url={target_table_url}",
            f"outreach_table_url={outreach_table_url}",
            f"hot_video_table_url={hot_video_table_url}",
            f"access_token_env={config['feishu_access_token_env']}",
            f"fastmoss_phone_env={config['fastmoss_phone_env']}",
            f"fastmoss_password_env={config['fastmoss_password_env']}",
            f"max_source_rows={max(max_source_rows, 0)}",
            f"max_author_pages={max(max_author_pages, 0)}",
            "request_filter_status=已完成",
            "status_field_name=状态",
            "url_field_name=产品链接",
            f"max_author_detail_jobs_per_source_row={max(max_author_detail_jobs_per_source_row, 0)}",
            f"queue_mode={queue_mode}",
            f"worker_kinds={worker_kinds}",
            f"worker_max_iterations={max(worker_max_iterations, 0)}",
            f"request_delay_min_seconds={max(request_delay_min_seconds, 0.0)}",
            f"request_delay_max_seconds={max(request_delay_max_seconds, 0.0)}",
        ],
        table_refs,
    )
    if worker_stop_when_idle is not None:
        params.append(f"worker_stop_when_idle={str(bool(worker_stop_when_idle)).lower()}")
    if include_contact:
        params.append("include_contact=true")
    if include_submit_control_action:
        params.append("control_action=submit")
    extra_env = {
        config["feishu_access_token_env"]: config["feishu_access_token"],
        config["fastmoss_phone_env"]: config["fastmoss_phone"],
        config["fastmoss_password_env"]: config["fastmoss_password"],
    }
    return _append_runtime_params(params, skill_env), extra_env


def _influencer_outreach_sync_submit_params(
    *,
    skill_env: dict[str, str],
    include_submit_control_action: bool = True,
) -> tuple[list[str], dict[str, str]]:
    table_refs = _load_feishu_table_refs(skill_env)
    outreach_table_url = _resolve_table_url(skill_env, table_refs, TK_INFLUENCER_OUTREACH_TABLE_ALIAS)
    outreach_table_ref = _feishu_table_ref(TK_INFLUENCER_OUTREACH_TABLE_ALIAS)
    feishu_access_token_env = (
        _optional_env_value(skill_env, "INFLUENCER_POOL_FEISHU_ACCESS_TOKEN_ENV")
        or FEISHU_ACCESS_TOKEN_ENV
    )
    fastmoss_phone_env = (
        _optional_env_value(skill_env, "INFLUENCER_POOL_FASTMOSS_PHONE_ENV")
        or "FASTMOSS_PHONE"
    )
    fastmoss_password_env = (
        _optional_env_value(skill_env, "INFLUENCER_POOL_FASTMOSS_PASSWORD_ENV")
        or "FASTMOSS_PASSWORD"
    )
    params = _append_feishu_table_refs(
        [
            f"source_table_ref={outreach_table_ref}",
            f"target_table_ref={outreach_table_ref}",
            f"table_url={outreach_table_url}",
            f"target_table_url={outreach_table_url}",
            f"access_token_env={feishu_access_token_env}",
            f"fastmoss_phone_env={fastmoss_phone_env}",
            f"fastmoss_password_env={fastmoss_password_env}",
            "writeback_enabled=true",
        ],
        table_refs,
    )
    if include_submit_control_action:
        params.append("control_action=submit")
    extra_env = {
        feishu_access_token_env: _require_env_value(skill_env, FEISHU_ACCESS_TOKEN_ENV),
        fastmoss_phone_env: _optional_env_value(skill_env, "FASTMOSS_PHONE"),
        fastmoss_password_env: _optional_env_value(skill_env, "FASTMOSS_PASSWORD"),
    }
    return _append_runtime_params(params, skill_env), extra_env


def _append_influencer_pool_browser_params(
    *,
    params: list[str],
    skill_env: dict[str, str],
    python_bin: Path,
    install_dir: Path,
    requested_profile_ref: str,
    fallback_profile_ref: str,
) -> list[str]:
    del skill_env
    try:
        browser_target = _resolve_browser_target(
            python_bin=python_bin,
            install_dir=install_dir,
            requested_profile_ref=requested_profile_ref,
            fallback_profile_ref=fallback_profile_ref,
        )
    except Exception as exc:
        print(
            "[skill-step] Browser target resolution skipped for influencer-pool-sync; "
            f"HTTP login fallback remains available. detail={exc}"
        )
        return params
    params.append(f"profile_ref={browser_target['profile_ref']}")
    return params


def _append_runtime_params(params: list[str], skill_env: dict[str, str]) -> list[str]:
    notification_channel_code = _optional_env_value(skill_env, "NOTIFICATION_CHANNEL_CODE")
    delivery_context = _discover_openclaw_delivery_context(skill_env)

    if notification_channel_code:
        params.append(f"notification_channel_code={notification_channel_code}")
    elif delivery_context:
        params.append("notification_channel_code=openclaw_message")
    if delivery_context:
        session_id = str(delivery_context.get("sessionId", "") or "").strip()
        if session_id:
            params.append(f"source_session_id={session_id}")
        params.append(f"reply_target={_json_compact(delivery_context)}")
    return params


def _parse_param_value(raw_value: str) -> Any:
    try:
        return json.loads(raw_value)
    except json.JSONDecodeError:
        return raw_value


def _parse_param_items(items: list[str]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"Invalid param value '{item}'. Expected KEY=VALUE.")
        key, raw_value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"Invalid param value '{item}'. KEY cannot be empty.")
        params[key] = _parse_param_value(raw_value)
    return params


def _batch_keyword_search_table_alias(target_intent: str) -> str:
    if target_intent == KEYWORD_COMPETITOR_SEARCH_INTENT:
        return TK_COMPETITOR_TABLE_ALIAS
    if target_intent == KEYWORD_SELECTION_SEARCH_INTENT:
        return TK_SELECTION_TABLE_ALIAS
    raise ValueError("target_intent must be keyword_competitor_search or keyword_selection_search.")


def _batch_keyword_search_task_name(target_intent: str) -> str:
    if target_intent == KEYWORD_COMPETITOR_SEARCH_INTENT:
        return "search_keyword_competitor_products"
    if target_intent == KEYWORD_SELECTION_SEARCH_INTENT:
        return "search_keyword_selection_products"
    raise ValueError("target_intent must be keyword_competitor_search or keyword_selection_search.")


def _string_field(payload: dict[str, Any], key: str) -> str:
    return str(payload.get(key, "") or "").strip()


def _normalize_batch_keyword_item(raw: Any, *, target_intent: str, row_index: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"Batch keyword row {row_index} must be a JSON object.")
    unsupported_keys = sorted(set(raw) - BATCH_KEYWORD_ALLOWED_ITEM_KEYS)
    if unsupported_keys:
        raise ValueError(
            f"Batch keyword row {row_index} contains unsupported fields: {', '.join(unsupported_keys)}."
        )

    search_keyword = _string_field(raw, "search_keyword")
    if not search_keyword:
        raise ValueError(f"Batch keyword row {row_index} requires search_keyword.")

    threshold_type = _string_field(raw, "threshold_type")
    threshold_value = _string_field(raw, "threshold_value")
    if threshold_type not in BATCH_KEYWORD_ALLOWED_THRESHOLD_TYPES:
        raise ValueError(f"Batch keyword row {row_index} has unsupported threshold_type: {threshold_type}.")
    if threshold_type and not threshold_value:
        raise ValueError(f"Batch keyword row {row_index} requires threshold_value when threshold_type is set.")
    if not threshold_type and threshold_value:
        raise ValueError(f"Batch keyword row {row_index} requires threshold_type when threshold_value is set.")
    if target_intent == KEYWORD_SELECTION_SEARCH_INTENT and threshold_type == "total_sales":
        raise ValueError("Selection keyword batch rows do not support total_sales threshold.")

    return {
        "row_index": row_index,
        "search_keyword": search_keyword,
        "threshold_type": threshold_type,
        "threshold_value": threshold_value,
    }


def _parse_batch_keyword_items(raw_json: str, *, target_intent: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ValueError("--items-json must be a valid JSON array.") from exc
    if not isinstance(payload, list):
        raise ValueError("--items-json must be a JSON array.")
    if not payload:
        raise ValueError("--items-json must contain at least one keyword row.")
    if len(payload) > BATCH_KEYWORD_MAX_ITEMS:
        raise ValueError(f"Batch keyword search supports at most {BATCH_KEYWORD_MAX_ITEMS} rows.")
    return [
        _normalize_batch_keyword_item(item, target_intent=target_intent, row_index=index)
        for index, item in enumerate(payload, start=1)
    ]


def _batch_keyword_idempotency_key(*, target_intent: str, row: dict[str, Any]) -> str:
    payload = _json_compact(
        {
            "target_intent": target_intent,
            "row_index": row["row_index"],
            "search_keyword": row["search_keyword"],
            "threshold_type": row["threshold_type"],
            "threshold_value": row["threshold_value"],
        }
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"openclaw_batch_keyword:{target_intent}:{row['row_index']}:{digest}"


def _batch_keyword_submit_params(
    *,
    target_intent: str,
    row: dict[str, Any],
    skill_env: dict[str, str],
    table_refs: dict[str, Any],
    competitor_table_ref: str,
    competitor_table_url: str,
    selection_table_ref: str,
    selection_table_url: str,
    python_bin: Path,
    install_dir: Path,
    requested_profile_ref: str,
) -> list[str]:
    if target_intent == KEYWORD_COMPETITOR_SEARCH_INTENT:
        params = _append_runtime_params(
            _append_feishu_table_refs(
                [
                    f"seed_table_ref={competitor_table_ref}",
                    f"target_table_ref={competitor_table_ref}",
                    f"table_url={competitor_table_url}",
                    "access_token_env=MUJITASK_FEISHU_ACCESS_TOKEN",
                    f"url_field_name={DEFAULT_URL_FIELD_NAME}",
                    "control_action=submit",
                ],
                table_refs,
            ),
            skill_env,
        )
        sales_7d_threshold = ""
        total_sales_threshold = ""
        if row["threshold_type"] == "total_sales":
            total_sales_threshold = row["threshold_value"]
        elif row["threshold_type"] == "sales_7d":
            sales_7d_threshold = row["threshold_value"]
        else:
            sales_7d_threshold = "200"
        params.extend(
            _keyword_search_submit_params(
                python_bin=python_bin,
                install_dir=install_dir,
                requested_profile_ref=requested_profile_ref,
                search_keyword=row["search_keyword"],
                sales_7d_threshold=sales_7d_threshold,
                total_sales_threshold=total_sales_threshold,
                max_candidates="20",
            )
        )
    elif target_intent == KEYWORD_SELECTION_SEARCH_INTENT:
        params = _append_runtime_params(
            _append_feishu_table_refs(
                [
                    f"selection_table_ref={selection_table_ref}",
                    f"seed_table_ref={selection_table_ref}",
                    f"target_table_ref={selection_table_ref}",
                    f"table_url={selection_table_url}",
                    "access_token_env=MUJITASK_FEISHU_ACCESS_TOKEN",
                    "control_action=submit",
                ],
                table_refs,
            ),
            skill_env,
        )
        params.extend(
            _keyword_search_submit_params(
                python_bin=python_bin,
                install_dir=install_dir,
                requested_profile_ref=requested_profile_ref,
                search_keyword=row["search_keyword"],
                sales_7d_threshold=row["threshold_value"] if row["threshold_type"] == "sales_7d" else "500",
                product_price_threshold="10.99",
                keyword_workflow_mode="selection",
            )
        )
    else:
        raise ValueError("target_intent must be keyword_competitor_search or keyword_selection_search.")
    params.append(f"idempotency_key={_batch_keyword_idempotency_key(target_intent=target_intent, row=row)}")
    return params


def _batch_keyword_message(items: list[dict[str, Any]], submitted_count: int, failed_count: int) -> str:
    lines = [f"已提交批量关键词搜索任务：成功 {submitted_count} 个，失败 {failed_count} 个。"]
    for item in items:
        request_id = str(item.get("request_id") or "").strip()
        error = str(item.get("error") or "").strip()
        if request_id:
            lines.append(f"{item['row_index']}. {item['search_keyword']} request_id: {request_id}")
        else:
            lines.append(f"{item['row_index']}. {item['search_keyword']} 提交失败：{error or 'unknown error'}")
    return "\n".join(lines)


def _submit_batch_keyword_items(
    *,
    target_intent: str,
    items: list[dict[str, Any]],
    skill_env: dict[str, str],
    table_refs: dict[str, Any],
    competitor_table_ref: str,
    competitor_table_url: str,
    selection_table_ref: str,
    selection_table_url: str,
    install_dir: Path,
    python_bin: Path,
    requested_profile_ref: str,
    extra_env: dict[str, str],
) -> dict[str, Any]:
    task_name = _batch_keyword_search_task_name(target_intent)
    result_items: list[dict[str, Any]] = []
    request_ids: list[str] = []
    for row in items:
        params = _batch_keyword_submit_params(
            target_intent=target_intent,
            row=row,
            skill_env=skill_env,
            table_refs=table_refs,
            competitor_table_ref=competitor_table_ref,
            competitor_table_url=competitor_table_url,
            selection_table_ref=selection_table_ref,
            selection_table_url=selection_table_url,
            python_bin=python_bin,
            install_dir=install_dir,
            requested_profile_ref=requested_profile_ref,
        )
        status, payload = _run_lightweight_submit_capture_payload(
            install_dir=install_dir,
            python_bin=python_bin,
            task_name=task_name,
            params=params,
            stdout_prefix="batch-keyword-search-submit-step",
            extra_env=extra_env,
            accepted_message="Batch keyword search row accepted for asynchronous execution.",
        )
        result_item = {
            "row_index": row["row_index"],
            "search_keyword": row["search_keyword"],
            "threshold_type": row["threshold_type"],
            "threshold_value": row["threshold_value"],
            "task_name": task_name,
            "status": "success" if status == 0 else "failed",
            "request_id": str(payload.get("request_id", "") or "") if isinstance(payload, dict) else "",
            "request_status": str(payload.get("request_status", "") or "") if isinstance(payload, dict) else "",
            "error": str(payload.get("error", "") or "") if isinstance(payload, dict) else "",
        }
        if result_item["request_id"]:
            request_ids.append(result_item["request_id"])
        result_items.append(result_item)

    submitted_count = sum(1 for item in result_items if item["status"] == "success")
    failed_count = len(result_items) - submitted_count
    if submitted_count == len(result_items):
        status = "success"
    elif submitted_count:
        status = "partial_success"
    else:
        status = "failed"
    summary = {"total": len(result_items), "counts": {"submitted": submitted_count, "failed": failed_count}}
    return {
        "status": status,
        "task_name": "batch_keyword_search",
        "target_intent": target_intent,
        "target_table": _batch_keyword_search_table_alias(target_intent),
        "control_action": "submit",
        "summary": summary,
        "summary_text": _build_summary_text(summary),
        "items": result_items,
        "request_ids": request_ids,
        "failed_item_count": failed_count,
        "message": _batch_keyword_message(result_items, submitted_count, failed_count),
    }


def _build_summary_text(summary: dict[str, Any]) -> str:
    counts = summary.get("counts", {})
    if not isinstance(counts, dict) or not counts:
        total = summary.get("total")
        return f"total={total}" if total is not None else ""
    parts = [f"{key}={counts[key]}" for key in sorted(counts)]
    total = summary.get("total")
    if total is not None:
        parts.append(f"total={total}")
    return ", ".join(parts)


def _augment_message_with_request_id(payload: dict[str, Any]) -> None:
    request_id = str(payload.get("request_id", "") or "").strip()
    if not request_id:
        return
    message = str(payload.get("message", "") or "").strip()
    if request_id in message:
        return
    payload["message"] = (
        f"已成功提交任务，request_id: {request_id}；{message}"
        if message
        else f"已成功提交任务，request_id: {request_id}"
    )


def _normalize_lightweight_submit_payload(
    *,
    task_name: str,
    payload: dict[str, Any],
    accepted_message: str,
) -> dict[str, Any]:
    normalized = dict(payload)
    normalized["status"] = "success"
    normalized["task_name"] = task_name
    normalized["control_action"] = str(normalized.get("control_action", "") or "submit")
    normalized["message"] = str(normalized.get("message", "") or accepted_message)
    summary = normalized.get("summary")
    if not isinstance(summary, dict) or not summary:
        summary = {"total": 1, "counts": {"queued": 1}}
    normalized["summary"] = summary
    normalized["summary_text"] = _build_summary_text(summary)
    normalized.setdefault("request_id", "")
    normalized.setdefault("request_status", "")
    normalized.setdefault("failed_item_count", 0)
    normalized.setdefault("error", "")
    normalized.setdefault("artifacts", [])
    _augment_message_with_request_id(normalized)
    return normalized


def _read_json_file(path: Path) -> Any:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _run_lightweight_submit_capture_payload(
    *,
    install_dir: Path,
    python_bin: Path,
    task_name: str,
    params: list[str],
    stdout_prefix: str,
    extra_env: dict[str, str],
    accepted_message: str,
) -> tuple[int, dict[str, Any]]:
    parsed_params = _parse_param_items(params)
    env = os.environ.copy()
    env.update(extra_env)

    with tempfile.TemporaryDirectory(prefix="mujitask-lightweight-submit-") as temp_dir:
        result_file = Path(temp_dir) / "result.json"
        command = [
            str(python_bin),
            str(LIGHTWEIGHT_SUBMIT_HELPER),
            "--install-dir",
            str(install_dir),
            "--task-name",
            task_name,
            "--params-json",
            json.dumps(parsed_params, ensure_ascii=False),
            "--result-file",
            str(result_file),
        ]
        print(f"[{stdout_prefix}] Running lightweight submit for {task_name}")
        result = subprocess.run(
            command,
            cwd=str(install_dir),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        payload = _read_json_file(result_file)
        if result.returncode != 0:
            error_message = str(result.stderr or result.stdout or "").strip()
            return result.returncode, {
                "status": "failed",
                "task_name": task_name,
                "message": f"{task_name} submit failed.",
                "error": error_message or f"lightweight submit exited with code {result.returncode}",
            }
        if not isinstance(payload, dict):
            return 1, {
                "status": "failed",
                "task_name": task_name,
                "message": f"{task_name} submit failed.",
                "error": "lightweight submit did not return a JSON object payload",
            }
        normalized = _normalize_lightweight_submit_payload(
            task_name=task_name,
            payload=payload,
            accepted_message=accepted_message,
        )
        print(
            f"[{stdout_prefix}] Submitted request_id={normalized.get('request_id', '')} "
            f"request_status={normalized.get('request_status', '') or 'pending'}"
        )
        return 0, normalized


def _emit_final_result(payload: dict[str, Any]) -> int:
    result_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if os.getenv("MUJITASK_RESULT_FILE"):
        Path(os.environ["MUJITASK_RESULT_FILE"]).write_text(f"{result_json}\n", encoding="utf-8")
    if os.getenv("MUJITASK_SUPPRESS_RESULT_MARKER", "0") != "1":
        print(f"__OPENCLAW_RESULT__ {result_json}")
    status = str(payload.get("status", "") or payload.get("execution_status", "") or "").strip().lower()
    return 1 if status in {"failed", "error"} else 0


def _add_profile_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile-ref", default="")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Submit Mujitask OpenClaw skill tasks.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    refresh_parser = subparsers.add_parser("refresh-current-competitor-table-submit")
    _add_profile_arg(refresh_parser)

    selection_parser = subparsers.add_parser("selection-table-complete-submit")
    _add_profile_arg(selection_parser)

    product_parser = subparsers.add_parser("product-url-complete-submit")
    _add_profile_arg(product_parser)
    product_parser.add_argument("--product-url", required=True)

    competitor_parser = subparsers.add_parser("competitor-row-by-url-submit")
    _add_profile_arg(competitor_parser)
    competitor_parser.add_argument("--product-url", required=True)

    keyword_parser = subparsers.add_parser("keyword-search-submit")
    _add_profile_arg(keyword_parser)
    keyword_parser.add_argument("--search-keyword", required=True)
    keyword_parser.add_argument("--sales-7d-threshold", default="")
    keyword_parser.add_argument("--total-sales-threshold", default="")
    keyword_parser.add_argument("--max-candidates", default="20")
    keyword_parser.add_argument("--skip-fastmoss-login-validation", action="store_true")

    selection_keyword_parser = subparsers.add_parser("selection-keyword-search-submit")
    _add_profile_arg(selection_keyword_parser)
    selection_keyword_parser.add_argument("--search-keyword", required=True)
    selection_keyword_parser.add_argument("--sales-7d-threshold", default="500")
    selection_keyword_parser.add_argument("--price-range-max-threshold", default="10.99")
    selection_keyword_parser.add_argument("--skip-fastmoss-login-validation", action="store_true")

    batch_keyword_parser = subparsers.add_parser("batch-keyword-search-submit")
    _add_profile_arg(batch_keyword_parser)
    batch_keyword_parser.add_argument(
        "--target-intent",
        required=True,
        choices=[KEYWORD_COMPETITOR_SEARCH_INTENT, KEYWORD_SELECTION_SEARCH_INTENT],
    )
    batch_keyword_parser.add_argument("--items-json", required=True)

    influencer_parser = subparsers.add_parser("influencer-pool-sync-submit")
    influencer_parser.add_argument("--max-source-rows", type=int, default=0)
    influencer_parser.add_argument("--max-author-pages", type=int, default=0)
    influencer_parser.add_argument("--max-author-detail-jobs-per-source-row", type=int, default=0)
    influencer_parser.add_argument("--queue-mode", default="inline")
    influencer_parser.add_argument("--worker-kinds", default="")
    influencer_parser.add_argument("--worker-max-iterations", type=int, default=1)
    influencer_parser.add_argument("--worker-stop-when-idle", action="store_true")
    influencer_parser.add_argument("--include-contact", action="store_true")
    influencer_parser.add_argument("--request-delay-min-seconds", type=float, default=1.0)
    influencer_parser.add_argument("--request-delay-max-seconds", type=float, default=3.0)

    subparsers.add_parser("influencer-outreach-sync-submit")

    return parser


def _runtime_paths(skill_env: dict[str, str]) -> tuple[Path, Path]:
    install_dir = Path(_require_env_value(skill_env, "INSTALL_DIR")).expanduser().resolve()
    python_bin = install_dir / ".venv" / "bin" / "python"
    if not python_bin.exists():
        raise ValueError(f"Cannot find Python at {python_bin}. Re-run the deployment script.")
    return install_dir, python_bin


def _base_extra_env(skill_env: dict[str, str]) -> dict[str, str]:
    extra_env = {FEISHU_ACCESS_TOKEN_ENV: _require_env_value(skill_env, FEISHU_ACCESS_TOKEN_ENV)}
    fastmoss_phone = _optional_env_value(skill_env, "FASTMOSS_PHONE")
    fastmoss_password = _optional_env_value(skill_env, "FASTMOSS_PASSWORD")
    if fastmoss_phone:
        extra_env["FASTMOSS_PHONE"] = fastmoss_phone
    if fastmoss_password:
        extra_env["FASTMOSS_PASSWORD"] = fastmoss_password
    return extra_env


def _submit(
    *,
    install_dir: Path,
    python_bin: Path,
    task_name: str,
    params: list[str],
    stdout_prefix: str,
    extra_env: dict[str, str],
    accepted_message: str,
) -> int:
    status, payload = _run_lightweight_submit_capture_payload(
        install_dir=install_dir,
        python_bin=python_bin,
        task_name=task_name,
        params=params,
        stdout_prefix=stdout_prefix,
        extra_env=extra_env,
        accepted_message=accepted_message,
    )
    if status != 0:
        return _emit_final_result(payload or {"status": "failed", "error": "submit failed"})
    return _emit_final_result(payload)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    skill_env = _load_skill_env(ENV_FILE)
    install_dir, python_bin = _runtime_paths(skill_env)
    extra_env = _base_extra_env(skill_env)

    table_refs = _load_feishu_table_refs(skill_env)
    competitor_table_url = _resolve_table_url(skill_env, table_refs, TK_COMPETITOR_TABLE_ALIAS)
    selection_table_url = _resolve_table_url(skill_env, table_refs, TK_SELECTION_TABLE_ALIAS)
    competitor_table_ref = _feishu_table_ref(TK_COMPETITOR_TABLE_ALIAS)
    selection_table_ref = _feishu_table_ref(TK_SELECTION_TABLE_ALIAS)

    if args.command == "refresh-current-competitor-table-submit":
        params = _append_runtime_params(
            _append_feishu_table_refs(
                [
                    f"source_table_ref={competitor_table_ref}",
                    f"table_url={competitor_table_url}",
                    "access_token_env=MUJITASK_FEISHU_ACCESS_TOKEN",
                    f"url_field_name={DEFAULT_URL_FIELD_NAME}",
                    "control_action=submit",
                ],
                table_refs,
            ),
            skill_env,
        )
        params.extend(
            _refresh_competitor_submit_params(
                python_bin=python_bin,
                install_dir=install_dir,
                requested_profile_ref=args.profile_ref,
            )
        )
        return _submit(
            install_dir=install_dir,
            python_bin=python_bin,
            task_name="refresh_current_competitor_table",
            params=params,
            stdout_prefix="refresh-current-competitor-table-submit-step",
            extra_env=extra_env,
            accepted_message="Refresh task accepted for asynchronous execution.",
        )

    if args.command == "selection-table-complete-submit":
        params = _append_runtime_params(
            _append_feishu_table_refs(
                [
                    f"source_table_ref={selection_table_ref}",
                    f"selection_table_ref={selection_table_ref}",
                    f"table_url={selection_table_url}",
                    "access_token_env=MUJITASK_FEISHU_ACCESS_TOKEN",
                    "control_action=submit",
                ],
                table_refs,
            ),
            skill_env,
        )
        params.extend(
            _product_url_complete_submit_params(
                python_bin=python_bin,
                install_dir=install_dir,
                requested_profile_ref=args.profile_ref,
            )
        )
        return _submit(
            install_dir=install_dir,
            python_bin=python_bin,
            task_name="tiktok_fastmoss_product_ingest",
            params=params,
            stdout_prefix="selection-table-complete-submit-step",
            extra_env=extra_env,
            accepted_message="Selection table completion task accepted for asynchronous execution.",
        )

    if args.command == "product-url-complete-submit":
        params = _append_runtime_params(
            _append_feishu_table_refs(
                [
                    f"source_table_ref={selection_table_ref}",
                    f"selection_table_ref={selection_table_ref}",
                    f"table_url={selection_table_url}",
                    f"product_url={args.product_url}",
                    "access_token_env=MUJITASK_FEISHU_ACCESS_TOKEN",
                    "control_action=submit",
                ],
                table_refs,
            ),
            skill_env,
        )
        params.extend(
            _product_url_complete_submit_params(
                python_bin=python_bin,
                install_dir=install_dir,
                requested_profile_ref=args.profile_ref,
            )
        )
        return _submit(
            install_dir=install_dir,
            python_bin=python_bin,
            task_name="tiktok_fastmoss_product_ingest",
            params=params,
            stdout_prefix="product-url-complete-submit-step",
            extra_env=extra_env,
            accepted_message="Product URL completion task accepted for asynchronous execution.",
        )

    if args.command == "competitor-row-by-url-submit":
        params = _append_runtime_params(
            _append_feishu_table_refs(
                [
                    f"source_table_ref={competitor_table_ref}",
                    f"table_url={competitor_table_url}",
                    f"product_url={args.product_url}",
                    "access_token_env=MUJITASK_FEISHU_ACCESS_TOKEN",
                    "control_action=submit",
                ],
                table_refs,
            ),
            skill_env,
        )
        params.extend(
            _product_url_complete_submit_params(
                python_bin=python_bin,
                install_dir=install_dir,
                requested_profile_ref=args.profile_ref,
            )
        )
        return _submit(
            install_dir=install_dir,
            python_bin=python_bin,
            task_name="refresh_competitor_row_by_url",
            params=params,
            stdout_prefix="competitor-row-by-url-submit-step",
            extra_env=extra_env,
            accepted_message="Competitor row refresh by URL task accepted for asynchronous execution.",
        )

    if args.command == "keyword-search-submit":
        params = _append_runtime_params(
            _append_feishu_table_refs(
                [
                    f"seed_table_ref={competitor_table_ref}",
                    f"target_table_ref={competitor_table_ref}",
                    f"table_url={competitor_table_url}",
                    "access_token_env=MUJITASK_FEISHU_ACCESS_TOKEN",
                    f"url_field_name={DEFAULT_URL_FIELD_NAME}",
                    "control_action=submit",
                ],
                table_refs,
            ),
            skill_env,
        )
        params.extend(
            _keyword_search_submit_params(
                python_bin=python_bin,
                install_dir=install_dir,
                requested_profile_ref=args.profile_ref,
                search_keyword=args.search_keyword,
                sales_7d_threshold=args.sales_7d_threshold or ("" if args.total_sales_threshold else "200"),
                total_sales_threshold=args.total_sales_threshold,
                max_candidates=args.max_candidates,
                skip_fastmoss_login_validation=args.skip_fastmoss_login_validation,
            )
        )
        return _submit(
            install_dir=install_dir,
            python_bin=python_bin,
            task_name="search_keyword_competitor_products",
            params=params,
            stdout_prefix="keyword-search-submit-step",
            extra_env=extra_env,
            accepted_message="Keyword search task accepted for asynchronous execution.",
        )

    if args.command == "selection-keyword-search-submit":
        params = _append_runtime_params(
            _append_feishu_table_refs(
                [
                    f"selection_table_ref={selection_table_ref}",
                    f"seed_table_ref={selection_table_ref}",
                    f"target_table_ref={selection_table_ref}",
                    f"table_url={selection_table_url}",
                    "access_token_env=MUJITASK_FEISHU_ACCESS_TOKEN",
                    "control_action=submit",
                ],
                table_refs,
            ),
            skill_env,
        )
        params.extend(
            _keyword_search_submit_params(
                python_bin=python_bin,
                install_dir=install_dir,
                requested_profile_ref=args.profile_ref,
                search_keyword=args.search_keyword,
                sales_7d_threshold=args.sales_7d_threshold,
                product_price_threshold=args.price_range_max_threshold,
                keyword_workflow_mode="selection",
                skip_fastmoss_login_validation=args.skip_fastmoss_login_validation,
            )
        )
        return _submit(
            install_dir=install_dir,
            python_bin=python_bin,
            task_name="search_keyword_selection_products",
            params=params,
            stdout_prefix="selection-keyword-search-submit-step",
            extra_env=extra_env,
            accepted_message="Selection keyword search task accepted for asynchronous execution.",
        )

    if args.command == "batch-keyword-search-submit":
        items = _parse_batch_keyword_items(args.items_json, target_intent=args.target_intent)
        payload = _submit_batch_keyword_items(
            target_intent=args.target_intent,
            items=items,
            skill_env=skill_env,
            table_refs=table_refs,
            competitor_table_ref=competitor_table_ref,
            competitor_table_url=competitor_table_url,
            selection_table_ref=selection_table_ref,
            selection_table_url=selection_table_url,
            install_dir=install_dir,
            python_bin=python_bin,
            requested_profile_ref=args.profile_ref,
            extra_env=extra_env,
        )
        return _emit_final_result(payload)

    if args.command == "influencer-pool-sync-submit":
        params, influencer_pool_env = _influencer_pool_sync_submit_params(
            skill_env=skill_env,
            include_submit_control_action=True,
            max_source_rows=max(args.max_source_rows, 0),
            max_author_pages=max(args.max_author_pages, 0),
            max_author_detail_jobs_per_source_row=max(args.max_author_detail_jobs_per_source_row, 0),
            queue_mode=str(args.queue_mode or "inline"),
            worker_kinds=str(args.worker_kinds or ""),
            worker_max_iterations=max(args.worker_max_iterations, 0),
            worker_stop_when_idle=bool(args.worker_stop_when_idle),
            include_contact=bool(args.include_contact),
            request_delay_min_seconds=float(args.request_delay_min_seconds),
            request_delay_max_seconds=float(args.request_delay_max_seconds),
        )
        return _submit(
            install_dir=install_dir,
            python_bin=python_bin,
            task_name="sync_tk_influencer_pool",
            params=params,
            stdout_prefix="influencer-pool-sync-submit-step",
            extra_env={**extra_env, **influencer_pool_env},
            accepted_message="Influencer pool sync task accepted for asynchronous execution.",
        )

    if args.command == "influencer-outreach-sync-submit":
        params, outreach_env = _influencer_outreach_sync_submit_params(
            skill_env=skill_env,
            include_submit_control_action=True,
        )
        return _submit(
            install_dir=install_dir,
            python_bin=python_bin,
            task_name="tiktok_influencer_outreach_sync",
            params=params,
            stdout_prefix="influencer-outreach-sync-submit-step",
            extra_env={**extra_env, **outreach_env},
            accepted_message="Influencer outreach sync task accepted for asynchronous execution.",
        )

    raise ValueError(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
