from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any

import requests
from automation_business_scaffold.infrastructure.rate_limit import RequestPacer, resolve_api_request_pacer_config

from automation_business_scaffold.contracts.handler.shared import (
    build_error,
    coerce_bool,
    coerce_mapping,
    coerce_str,
    failed_result,
    first_non_empty,
    success_result,
)
from automation_business_scaffold.contracts.handler.contract import HandlerContext, HandlerResult
from automation_business_scaffold.contracts.handler.allowlist import OUTBOX_HANDLER_CONTRACTS

RETRYABLE_WEBHOOK_STATUS_CODES = {408, 409, 425, 429}
RETRYABLE_FEISHU_CODES = {99991400, 99991663, 99991664}


HANDLER_CODE = "outbox_dispatch"
CONTRACT = OUTBOX_HANDLER_CONTRACTS[HANDLER_CODE]


class OutboxDispatchError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        retryable: bool,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable
        self.details = details or {}


def outbox_dispatch_handler(context: HandlerContext) -> HandlerResult:
    payload = dict(context.payload)
    channel_code = first_non_empty(
        payload.get("channel_code"),
        context.metadata.get("channel_code"),
        "stdout",
    )
    reply_target = first_non_empty(payload.get("reply_target"), context.metadata.get("reply_target"))
    message = _render_message(payload)
    dry_run = coerce_bool(payload.get("dry_run"), default=False)
    _report_progress(
        context,
        "dispatching",
        message=f"Dispatching outbox message through {channel_code}.",
        details={"channel_code": channel_code, "reply_target": reply_target},
    )

    if channel_code in {"noop", "disabled"}:
        _report_progress(
            context,
            "dispatch_skipped",
            message=f"Outbox channel {channel_code} does not emit notifications.",
            details={"channel_code": channel_code},
        )
        return success_result(
            context,
            summary={"channel_code": channel_code, "delivery_state": "skipped", "message_length": len(message)},
            result={
                "channel_code": channel_code,
                "reply_target": reply_target,
                "message": message,
                "delivery_state": "skipped",
            },
        )

    if channel_code in {"stdout", "console"}:
        if not dry_run and coerce_bool(payload.get("emit_to_stdout"), default=True):
            print(message)
        delivery_state = "sent" if not dry_run else "simulated"
        _report_progress(
            context,
            f"dispatch_{delivery_state}",
            message=f"Outbox message {delivery_state} through {channel_code}.",
            details={"channel_code": channel_code, "delivery_state": delivery_state},
        )
        return success_result(
            context,
            summary={"channel_code": channel_code, "delivery_state": delivery_state, "message_length": len(message)},
            result={
                "channel_code": channel_code,
                "reply_target": reply_target,
                "message": message,
                "delivery_state": delivery_state,
            },
        )

    if channel_code == "webhook":
        webhook_url = coerce_str(payload.get("webhook_url"))
        if not webhook_url:
            _report_progress(
                context,
                "dispatch_failed",
                message="Webhook dispatch is missing its URL.",
                details={"channel_code": channel_code, "retryable": False},
            )
            error = build_error(
                error_type="dispatch_failure",
                error_code="outbox_webhook_missing_url",
                message="Webhook dispatch requires payload.webhook_url.",
                retryable=False,
                details={"channel_code": channel_code},
            )
            return failed_result(context, error=error, summary={"channel_code": channel_code})
        if dry_run:
            _report_progress(
                context,
                "dispatch_simulated",
                message="Webhook dispatch simulated in dry-run mode.",
                details={"channel_code": channel_code, "webhook_url": webhook_url},
            )
            return success_result(
                context,
                summary={"channel_code": channel_code, "delivery_state": "simulated", "message_length": len(message)},
                result={
                    "channel_code": channel_code,
                    "reply_target": reply_target,
                    "message": message,
                    "delivery_state": "simulated",
                    "webhook_url": webhook_url,
                },
            )
        try:
            request_pacer = RequestPacer(resolve_api_request_pacer_config(payload, provider="outbox"))
            request_pacer.wait_before_request("outbox:webhook")
            response = requests.post(
                webhook_url,
                json={"message": message, "payload": coerce_mapping(payload.get("body")) or payload},
                timeout=float(payload.get("timeout_seconds", 10.0) or 10.0),
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            retryable = _is_retryable_webhook_error(exc)
            status_code = _request_status_code(exc)
            _report_progress(
                context,
                "dispatch_retryable_failure" if retryable else "dispatch_terminal_failure",
                message=str(exc),
                details=_compact_details(
                    {
                        "channel_code": channel_code,
                        "webhook_url": webhook_url,
                        "status_code": status_code,
                        "retryable": retryable,
                    }
                ),
            )
            error = build_error(
                error_type="dispatch_failure",
                error_code="outbox_webhook_request_failed",
                message=str(exc),
                retryable=retryable,
                details=_compact_details(
                    {"channel_code": channel_code, "webhook_url": webhook_url, "status_code": status_code}
                ),
            )
            return failed_result(context, error=error, summary={"channel_code": channel_code})
        finally:
            request_pacer.mark_request_finished("outbox:webhook")
        _report_progress(
            context,
            "dispatch_sent",
            message="Webhook dispatch completed.",
            details={"channel_code": channel_code, "webhook_url": webhook_url, "status_code": response.status_code},
        )
        return success_result(
            context,
            summary={"channel_code": channel_code, "delivery_state": "sent", "message_length": len(message)},
            result={
                "channel_code": channel_code,
                "reply_target": reply_target,
                "message": message,
                "delivery_state": "sent",
                "webhook_url": webhook_url,
                "status_code": response.status_code,
            },
        )

    if channel_code in {"feishu_bot_api", "feishu_direct_api"}:
        if dry_run:
            return _simulated_success(
                context,
                channel_code=channel_code,
                reply_target=reply_target,
                message=message,
                progress_message="Feishu bot API dispatch simulated in dry-run mode.",
            )
        try:
            dispatch_result = _dispatch_via_feishu_bot_api(
                message_text=message,
                reply_target=reply_target,
                request_pacer=RequestPacer(resolve_api_request_pacer_config(payload, provider="feishu")),
            )
        except OutboxDispatchError as exc:
            return _dispatch_failed_result(
                context,
                channel_code=channel_code,
                reply_target=reply_target,
                error_code=exc.error_code,
                message=str(exc),
                retryable=exc.retryable,
                details=exc.details,
            )
        _report_progress(
            context,
            "dispatch_sent",
            message="Feishu bot API dispatch completed.",
            details=_compact_details(
                {
                    "channel_code": channel_code,
                    "reply_target": reply_target,
                    "account_id": dispatch_result.get("account_id"),
                    "receive_id_type": dispatch_result.get("receive_id_type"),
                    "feishu_code": dispatch_result.get("feishu_code"),
                }
            ),
        )
        return success_result(
            context,
            summary={"channel_code": channel_code, "delivery_state": "sent", "message_length": len(message)},
            result={
                "channel_code": channel_code,
                "reply_target": reply_target,
                "message": message,
                "delivery_state": "sent",
                "transport": dispatch_result,
            },
        )

    if channel_code in {"openclaw_message", "feishu_openclaw"}:
        if dry_run:
            return _simulated_success(
                context,
                channel_code=channel_code,
                reply_target=reply_target,
                message=message,
                progress_message="OpenClaw message dispatch simulated in dry-run mode.",
            )
        try:
            dispatch_result = _dispatch_via_openclaw_message(message_text=message, reply_target=reply_target)
        except OutboxDispatchError as exc:
            return _dispatch_failed_result(
                context,
                channel_code=channel_code,
                reply_target=reply_target,
                error_code=exc.error_code,
                message=str(exc),
                retryable=exc.retryable,
                details=exc.details,
            )
        _report_progress(
            context,
            "dispatch_sent",
            message="OpenClaw message dispatch completed.",
            details=_compact_details(
                {
                    "channel_code": channel_code,
                    "reply_target": reply_target,
                    "account_id": dispatch_result.get("account_id"),
                }
            ),
        )
        return success_result(
            context,
            summary={"channel_code": channel_code, "delivery_state": "sent", "message_length": len(message)},
            result={
                "channel_code": channel_code,
                "reply_target": reply_target,
                "message": message,
                "delivery_state": "sent",
                "transport": dispatch_result,
            },
        )

    _report_progress(
        context,
        "dispatch_failed",
        message=f"Unsupported outbox channel {channel_code}.",
        details={"channel_code": channel_code, "retryable": False},
    )
    error = build_error(
        error_type="dispatch_failure",
        error_code="outbox_channel_unsupported",
        message=f"Unsupported outbox channel '{channel_code}'.",
        retryable=False,
        details={"channel_code": channel_code},
    )
    return failed_result(context, error=error, summary={"channel_code": channel_code})


def _simulated_success(
    context: HandlerContext,
    *,
    channel_code: str,
    reply_target: str,
    message: str,
    progress_message: str,
) -> HandlerResult:
    _report_progress(
        context,
        "dispatch_simulated",
        message=progress_message,
        details={"channel_code": channel_code},
    )
    return success_result(
        context,
        summary={"channel_code": channel_code, "delivery_state": "simulated", "message_length": len(message)},
        result={
            "channel_code": channel_code,
            "reply_target": reply_target,
            "message": message,
            "delivery_state": "simulated",
        },
    )


def _dispatch_failed_result(
    context: HandlerContext,
    *,
    channel_code: str,
    reply_target: str,
    error_code: str,
    message: str,
    retryable: bool,
    details: dict[str, Any],
) -> HandlerResult:
    progress_stage = "dispatch_retryable_failure" if retryable else "dispatch_terminal_failure"
    _report_progress(
        context,
        progress_stage,
        message=message,
        details=_compact_details(
            {"channel_code": channel_code, "reply_target": reply_target, "retryable": retryable} | details
        ),
    )
    error = build_error(
        error_type="dispatch_failure",
        error_code=error_code,
        message=message,
        retryable=retryable,
        details=_compact_details({"channel_code": channel_code} | details),
    )
    return failed_result(context, error=error, summary={"channel_code": channel_code})


def _render_message(payload: dict[str, Any]) -> str:
    for key in ("message", "message_text", "text"):
        text = coerce_str(payload.get(key))
        if text:
            return text
    summary = coerce_mapping(payload.get("summary"))
    result = coerce_mapping(payload.get("result"))
    if summary or result:
        return json.dumps(
            {"summary": summary, "result": result},
            ensure_ascii=False,
            sort_keys=True,
        )
    event_type = first_non_empty(payload.get("event_type"), "task_notification")
    ref_type = first_non_empty(payload.get("ref_type"), "task_request")
    ref_id = first_non_empty(payload.get("ref_id"), payload.get("request_id"))
    return f"{event_type} {ref_type} {ref_id}".strip()


def _parse_reply_target(reply_target: str) -> dict[str, Any]:
    raw_value = coerce_str(reply_target).strip()
    if not raw_value:
        return {}
    try:
        payload = json.loads(raw_value)
    except Exception:
        try:
            payload = ast.literal_eval(raw_value)
        except Exception:
            return {"to": raw_value}
    return payload if isinstance(payload, dict) else {"to": raw_value}


def _dispatch_via_feishu_bot_api(
    *,
    message_text: str,
    reply_target: str,
    request_pacer: RequestPacer | None = None,
) -> dict[str, Any]:
    delivery_context = _parse_reply_target(reply_target)
    channel = coerce_str(delivery_context.get("channel")).strip().lower()
    if channel and channel != "feishu":
        raise OutboxDispatchError(
            f"feishu_bot_api only supports Feishu reply targets, got '{channel}'.",
            error_code="outbox_feishu_target_channel_invalid",
            retryable=False,
            details={"reply_channel": channel},
        )
    raw_target = coerce_str(delivery_context.get("to") or delivery_context.get("target")).strip()
    account_id = coerce_str(delivery_context.get("accountId") or delivery_context.get("account_id")).strip()
    receive_id_type, receive_id = _normalize_feishu_receive_target(raw_target)
    feishu_account = _load_feishu_account_config(account_id)
    base_url = _feishu_base_url(feishu_account["domain"])

    token_payload = _post_feishu_json(
        f"{base_url}/open-apis/auth/v3/tenant_access_token/internal",
        {
            "app_id": feishu_account["app_id"],
            "app_secret": feishu_account["app_secret"],
        },
        error_code="outbox_feishu_token_request_failed",
        request_pacer=request_pacer,
    )
    token_code = _read_response_code(token_payload)
    if token_code != 0:
        raise OutboxDispatchError(
            f"Feishu tenant_access_token request failed with code {token_code}: {token_payload.get('msg') or token_payload.get('message') or ''}",
            error_code="outbox_feishu_token_request_failed",
            retryable=_is_retryable_feishu_code(token_code),
            details={"feishu_code": token_code, "feishu_msg": token_payload.get("msg") or token_payload.get("message")},
        )
    tenant_access_token = coerce_str(token_payload.get("tenant_access_token")).strip()
    if not tenant_access_token:
        raise OutboxDispatchError(
            "Feishu tenant_access_token response did not include tenant_access_token.",
            error_code="outbox_feishu_token_missing",
            retryable=True,
            details={"feishu_code": token_code},
        )

    message_payload = _post_feishu_json(
        f"{base_url}/open-apis/im/v1/messages?receive_id_type={receive_id_type}",
        {
            "receive_id": receive_id,
            "msg_type": "text",
            "content": json.dumps({"text": message_text}, ensure_ascii=False),
        },
        headers={"Authorization": f"Bearer {tenant_access_token}"},
        error_code="outbox_feishu_message_request_failed",
        request_pacer=request_pacer,
    )
    message_code = _read_response_code(message_payload)
    if message_code != 0:
        raise OutboxDispatchError(
            f"Feishu send message request failed with code {message_code}: {message_payload.get('msg') or message_payload.get('message') or ''}",
            error_code="outbox_feishu_message_request_failed",
            retryable=_is_retryable_feishu_code(message_code),
            details={"feishu_code": message_code, "feishu_msg": message_payload.get("msg") or message_payload.get("message")},
        )
    return {
        "channel_code": "feishu_bot_api",
        "account_id": feishu_account["account_id"],
        "domain": feishu_account["domain"],
        "receive_id_type": receive_id_type,
        "feishu_code": message_code,
        "message_id": _extract_feishu_message_id(message_payload),
    }


def _dispatch_via_openclaw_message(*, message_text: str, reply_target: str) -> dict[str, Any]:
    delivery_context = _parse_reply_target(reply_target)
    channel = coerce_str(delivery_context.get("channel")).strip() or "feishu"
    target = coerce_str(
        delivery_context.get("to")
        or delivery_context.get("target")
        or delivery_context.get("reply_to")
    ).strip()
    account_id = coerce_str(delivery_context.get("accountId") or delivery_context.get("account_id")).strip()
    if not channel or not target:
        raise OutboxDispatchError(
            "OpenClaw message dispatch is missing reply_target.channel or reply_target.to.",
            error_code="outbox_openclaw_target_missing",
            retryable=False,
            details={"reply_channel": channel},
        )
    command = [
        _resolve_openclaw_cli(),
        "message",
        "send",
        "--channel",
        channel,
        "--target",
        target,
        "--message",
        message_text,
        "--json",
    ]
    if account_id:
        command.extend(["--account", account_id])
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=max(float(os.environ.get("OPENCLAW_MESSAGE_SEND_TIMEOUT_SECONDS", "20") or 20.0), 1.0),
        )
    except subprocess.TimeoutExpired as exc:
        raise OutboxDispatchError(
            f"OpenClaw message send timed out: {exc.cmd}",
            error_code="outbox_openclaw_message_timeout",
            retryable=True,
            details={"reply_channel": channel, "account_id": account_id},
        ) from exc
    if completed.returncode != 0:
        details = coerce_str(completed.stderr).strip() or coerce_str(completed.stdout).strip()
        raise OutboxDispatchError(
            f"OpenClaw message send failed: {details or f'exit code {completed.returncode}'}",
            error_code="outbox_openclaw_message_failed",
            retryable=False,
            details={"reply_channel": channel, "account_id": account_id, "exit_code": completed.returncode},
        )
    stdout = coerce_str(completed.stdout).strip()
    transport = _safe_json_object(stdout)
    return {
        "channel_code": "openclaw_message",
        "reply_channel": channel,
        "account_id": account_id,
        "openclaw": transport,
    }


def _load_feishu_account_config(account_id: str) -> dict[str, str]:
    accounts, default_account = _load_feishu_accounts_from_env_json()
    if not accounts:
        accounts, default_account = _load_feishu_accounts_from_file()
    if not accounts:
        accounts, default_account = _load_feishu_accounts_from_openclaw()
    if not accounts:
        raise OutboxDispatchError(
            "Feishu account config is missing. Set MUJITASK_FEISHU_ACCOUNTS_JSON, MUJITASK_FEISHU_ACCOUNTS_FILE, or OpenClaw channels.feishu config.",
            error_code="outbox_feishu_config_missing",
            retryable=False,
        )
    effective_account_id = account_id or default_account or "default"
    account = accounts.get(effective_account_id)
    if account is None and effective_account_id == "default" and len(accounts) == 1:
        effective_account_id, account = next(iter(accounts.items()))
    if not isinstance(account, dict):
        raise OutboxDispatchError(
            f"Feishu account '{effective_account_id}' is not configured.",
            error_code="outbox_feishu_account_missing",
            retryable=False,
            details={"account_id": effective_account_id},
        )
    app_id = coerce_str(account.get("appId") or account.get("app_id")).strip()
    app_secret = coerce_str(account.get("appSecret") or account.get("app_secret")).strip()
    domain = coerce_str(account.get("domain") or "feishu").strip().lower() or "feishu"
    if not app_id or not app_secret:
        raise OutboxDispatchError(
            f"Feishu account '{effective_account_id}' is missing appId/appSecret.",
            error_code="outbox_feishu_account_incomplete",
            retryable=False,
            details={"account_id": effective_account_id},
        )
    return {
        "account_id": effective_account_id,
        "app_id": app_id,
        "app_secret": app_secret,
        "domain": domain,
    }


def _load_feishu_accounts_from_env_json() -> tuple[dict[str, dict[str, Any]], str]:
    raw_value = coerce_str(os.environ.get("MUJITASK_FEISHU_ACCOUNTS_JSON")).strip()
    if not raw_value:
        return {}, ""
    payload = _safe_json_object(raw_value)
    return _normalize_feishu_accounts_payload(payload)


def _load_feishu_accounts_from_file() -> tuple[dict[str, dict[str, Any]], str]:
    raw_path = coerce_str(os.environ.get("MUJITASK_FEISHU_ACCOUNTS_FILE")).strip()
    if not raw_path:
        return {}, ""
    path = Path(raw_path).expanduser()
    if not path.exists():
        raise OutboxDispatchError(
            f"Feishu account config file not found: {path}",
            error_code="outbox_feishu_config_file_missing",
            retryable=False,
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise OutboxDispatchError(
            f"Feishu account config file is not valid JSON: {path}",
            error_code="outbox_feishu_config_file_invalid",
            retryable=False,
        ) from exc
    return _normalize_feishu_accounts_payload(payload if isinstance(payload, dict) else {})


def _load_feishu_accounts_from_openclaw() -> tuple[dict[str, dict[str, Any]], str]:
    config_path = _openclaw_config_path()
    if not config_path.exists():
        return {}, ""
    try:
        config_payload = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise OutboxDispatchError(
            f"OpenClaw config file is not valid JSON: {config_path}",
            error_code="outbox_openclaw_config_invalid",
            retryable=False,
        ) from exc
    channels = config_payload.get("channels") if isinstance(config_payload, dict) else None
    feishu_config = channels.get("feishu") if isinstance(channels, dict) else None
    if not isinstance(feishu_config, dict):
        return {}, ""
    default_account = coerce_str(feishu_config.get("defaultAccount") or "default").strip() or "default"
    accounts_payload = feishu_config.get("accounts")
    if isinstance(accounts_payload, dict) and accounts_payload:
        accounts = {
            str(key): _inherit_feishu_account_defaults(value, feishu_config)
            for key, value in accounts_payload.items()
            if isinstance(value, dict)
        }
    else:
        accounts = {default_account: feishu_config}
    return accounts, default_account


def _normalize_feishu_accounts_payload(payload: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], str]:
    raw_default = payload.get("defaultAccount") or payload.get("default_account")
    if raw_default is None and isinstance(payload.get("default"), str):
        raw_default = payload.get("default")
    default_account = coerce_str(
        raw_default
        or "default"
    ).strip() or "default"
    accounts_payload = payload.get("accounts")
    if isinstance(accounts_payload, dict):
        accounts = {str(key): value for key, value in accounts_payload.items() if isinstance(value, dict)}
        return accounts, default_account
    if any(key in payload for key in ("appId", "app_id", "appSecret", "app_secret")):
        return {default_account: payload}, default_account
    accounts = {str(key): value for key, value in payload.items() if isinstance(value, dict)}
    return accounts, default_account


def _inherit_feishu_account_defaults(account: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    merged = dict(account)
    for key in ("appId", "appSecret", "domain"):
        if not merged.get(key) and root.get(key):
            merged[key] = root[key]
    return merged


def _openclaw_config_path() -> Path:
    configured = coerce_str(os.environ.get("OPENCLAW_CONFIG_PATH")).strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".openclaw" / "openclaw.json"


def _resolve_openclaw_cli() -> str:
    configured = coerce_str(os.environ.get("OPENCLAW_CLI_BIN")).strip()
    if configured:
        return configured
    resolved = shutil.which("openclaw")
    if resolved:
        return resolved
    for candidate in ("/opt/homebrew/bin/openclaw", "/usr/local/bin/openclaw"):
        if Path(candidate).exists():
            return candidate
    raise OutboxDispatchError(
        "Cannot find the openclaw CLI. Set OPENCLAW_CLI_BIN or add openclaw to PATH.",
        error_code="outbox_openclaw_cli_missing",
        retryable=False,
    )


def _normalize_feishu_receive_target(raw_target: str) -> tuple[str, str]:
    normalized = coerce_str(raw_target).strip()
    if not normalized:
        raise OutboxDispatchError(
            "Feishu reply target is empty.",
            error_code="outbox_feishu_target_missing",
            retryable=False,
        )
    lowered = normalized.lower()
    if lowered.startswith(("user:", "dm:", "open_id:")):
        return "open_id", normalized.split(":", 1)[1]
    if lowered.startswith(("chat:", "group:", "channel:")):
        return "chat_id", normalized.split(":", 1)[1]
    if normalized.startswith("oc_"):
        return "chat_id", normalized
    return "open_id", normalized


def _feishu_base_url(domain: str) -> str:
    normalized = coerce_str(domain or "feishu").strip().lower()
    if normalized == "lark":
        return "https://open.larksuite.com"
    return "https://open.feishu.cn"


def _post_feishu_json(
    url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    error_code: str,
    request_pacer: RequestPacer | None = None,
) -> dict[str, Any]:
    request_headers = {"Content-Type": "application/json; charset=utf-8"}
    if headers:
        request_headers.update(headers)
    try:
        if request_pacer is not None:
            request_pacer.wait_before_request("feishu:bot")
        try:
            response = requests.post(
                url,
                json=payload,
                headers=request_headers,
                timeout=max(float(os.environ.get("FEISHU_BOT_API_TIMEOUT_SECONDS", "15") or 15.0), 1.0),
            )
        finally:
            if request_pacer is not None:
                request_pacer.mark_request_finished("feishu:bot")
        response.raise_for_status()
    except requests.RequestException as exc:
        status_code = _request_status_code(exc)
        raise OutboxDispatchError(
            str(exc),
            error_code=error_code,
            retryable=_is_retryable_http_status(status_code),
            details=_compact_details({"status_code": status_code}),
        ) from exc
    try:
        response_payload = response.json()
    except ValueError as exc:
        raise OutboxDispatchError(
            "Feishu response is not valid JSON.",
            error_code=error_code,
            retryable=True,
            details={"status_code": response.status_code},
        ) from exc
    if not isinstance(response_payload, dict):
        raise OutboxDispatchError(
            "Feishu response is not a JSON object.",
            error_code=error_code,
            retryable=True,
            details={"status_code": response.status_code},
        )
    return response_payload


def _read_response_code(payload: dict[str, Any]) -> int:
    raw_code = payload.get("code", -1)
    try:
        return int(raw_code)
    except (TypeError, ValueError):
        return -1


def _is_retryable_feishu_code(code: int) -> bool:
    return code in RETRYABLE_FEISHU_CODES or code >= 50000000


def _is_retryable_http_status(status_code: int | None) -> bool:
    if status_code is None:
        return True
    if status_code >= 500:
        return True
    return status_code in RETRYABLE_WEBHOOK_STATUS_CODES


def _extract_feishu_message_id(payload: dict[str, Any]) -> str:
    data = payload.get("data")
    if isinstance(data, dict):
        return coerce_str(data.get("message_id")).strip()
    return ""


def _safe_json_object(raw_value: str) -> dict[str, Any]:
    if not raw_value:
        return {}
    try:
        payload = json.loads(raw_value)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _report_progress(
    context: HandlerContext,
    progress_stage: str,
    *,
    message: str = "",
    details: dict[str, Any] | None = None,
) -> None:
    progress_callback = context.metadata.get("progress_callback")
    if callable(progress_callback):
        progress_callback(progress_stage, message=message, details=details or {})


def _request_status_code(exc: requests.RequestException) -> int | None:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code is None:
        return None
    try:
        return int(status_code)
    except (TypeError, ValueError):
        return None


def _is_retryable_webhook_error(exc: requests.RequestException) -> bool:
    status_code = _request_status_code(exc)
    if status_code is None:
        return True
    if status_code >= 500:
        return True
    return status_code in RETRYABLE_WEBHOOK_STATUS_CODES


def _compact_details(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value not in (None, "")}


__all__ = ["CONTRACT", "HANDLER_CODE", "outbox_dispatch_handler"]
