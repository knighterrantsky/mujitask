#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

SCRIPT_DIR = Path(__file__).resolve().parent
ENV_FILE = SCRIPT_DIR / "skill.local.env"
RESULT_HELPER = SCRIPT_DIR / "openclaw_result.py"
RESOLVE_BROWSER_TARGET = SCRIPT_DIR / "resolve_browser_target.py"
LIGHTWEIGHT_SUBMIT_HELPER = SCRIPT_DIR / "lightweight_submit.py"
DEFAULT_OPENCLAW_AGENT_ID = "tiktok-ops"


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
        if not normalized_key:
            continue
        values[normalized_key] = _normalize_env_entry(value)
    return values


def _require_env_value(env: dict[str, str], key: str) -> str:
    value = str(env.get(key, "")).strip()
    if not value:
        raise ValueError(f"{key} is required in {ENV_FILE}.")
    return value


def _optional_env_value(env: dict[str, str], key: str) -> str:
    return str(env.get(key, "") or "").strip()


def _json_compact(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


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
        payload = {"channel": explicit_channel, "to": explicit_to}
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
        backup_files = sorted(
            backups_dir.glob("*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        candidate_files.extend(backup_files[:10])

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


def _resolve_browser_target(*, python_bin: Path, install_dir: Path, requested_profile_ref: str, fallback_profile_ref: str) -> dict[str, Any]:
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
    return json.loads(result.stdout)


def _probe_cdp_status(debug_http: str) -> tuple[bool, str]:
    version_url = f"{debug_http.rstrip('/')}/json/version"
    try:
        with urllib.request.urlopen(version_url, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
        browser = str(payload.get("Browser", "") or "").strip()
        if browser:
            return True, f"Browser={browser}"
        return False, "missing Browser field in /json/version response"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code} from {version_url}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _ensure_browser_ready(*, python_bin: Path, script_dir: Path, browser_target: dict[str, Any]) -> None:
    provider = str(browser_target.get("provider", "")).strip()
    profile_ref = str(browser_target.get("profile_ref", "")).strip()
    metadata = browser_target.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    debug_http = str(metadata.get("debug_http", "") or "http://127.0.0.1:9222").strip()

    if provider == "roxy":
        print(f"[skill-step] Using browser profile_ref={profile_ref} provider=roxy. Skipping local CDP checks.")
        return
    if provider != "chrome_cdp":
        raise ValueError(f"Unsupported browser provider '{provider}' for profile_ref={profile_ref}.")
    ready, detail = _probe_cdp_status(debug_http)
    if ready:
        return
    parsed_debug = urlparse(debug_http)
    debug_host = (parsed_debug.hostname or "").strip().lower()
    debug_port = parsed_debug.port or (443 if parsed_debug.scheme == "https" else 80)
    if debug_host not in {"127.0.0.1", "localhost"}:
        raise ValueError(
            f"Chrome CDP is not ready at {debug_http} for profile_ref={profile_ref}. "
            f"Probe detail: {detail}."
        )

    print(
        f"[skill-step] Chrome CDP is not ready at {debug_http} "
        f"(probe={detail}). Trying to start Chrome on port {debug_port}."
    )
    startup_env = os.environ.copy()
    startup_env["MUJITASK_CHROME_CDP_PORT"] = str(debug_port)
    subprocess.run(["bash", str(script_dir / "start_browser_cdp.sh")], check=True, env=startup_env)
    last_detail = detail
    for _ in range(30):
        ready, last_detail = _probe_cdp_status(debug_http)
        if ready:
            return
        time.sleep(1)
    raise ValueError(f"Chrome CDP did not become ready on {debug_http}. Last probe detail: {last_detail}.")


def _generate_run_id(task_name: str) -> str:
    return f"openclaw-{task_name}-{time.strftime('%Y%m%d%H%M%S')}-{os.getpid()}"


def _read_json_file(path: Path) -> Any:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _progress_snapshot(*, run_file: Path, steps_file: Path) -> tuple[int, str, str, str]:
    step_count = 0
    last_step = ""
    last_status = ""
    run_status = ""

    steps_payload = _read_json_file(steps_file)
    if isinstance(steps_payload, list):
        step_count = len(steps_payload)
        if steps_payload and isinstance(steps_payload[-1], dict):
            last_step = str(steps_payload[-1].get("step_id", "") or "")
            last_status = str(steps_payload[-1].get("status", "") or "")

    run_payload = _read_json_file(run_file)
    if isinstance(run_payload, dict):
        run_status = str(run_payload.get("status", "") or "")

    return step_count, last_step, last_status, run_status


def _monitor_process(*, process: subprocess.Popen[str], run_file: Path, steps_file: Path, prefix: str) -> None:
    last_snapshot: tuple[int, str, str, str] | None = None
    heartbeat_counter = 0
    while process.poll() is None:
        snapshot = _progress_snapshot(run_file=run_file, steps_file=steps_file)
        if snapshot != last_snapshot:
            step_count, last_step, last_status, run_status = snapshot
            if step_count > 0:
                print(
                    f"[{prefix}] Progress: run_status={run_status or 'running'} "
                    f"completed_steps={step_count} last_step={last_step or 'unknown'} "
                    f"last_status={last_status or 'unknown'}"
                )
            elif run_status:
                print(f"[{prefix}] Progress: run_status={run_status} waiting for workflow steps")
            last_snapshot = snapshot
            heartbeat_counter = 0
        else:
            heartbeat_counter += 1
            if heartbeat_counter % 3 == 0:
                if run_file.exists() or steps_file.exists():
                    print(f"[{prefix}] Heartbeat: run is still active; waiting for the next workflow update")
                else:
                    print(f"[{prefix}] Heartbeat: run is still active; waiting for runtime files to appear")
        time.sleep(5)


def _build_result_json(
    *,
    python_bin: Path,
    run_file: Path,
    steps_file: Path,
    signals_file: Path,
    stdout_file: Path,
    run_id: str,
    task_name: str,
    cli_status: int,
) -> str:
    command = [
        str(python_bin),
        str(RESULT_HELPER),
        "run-summary",
        "--run-file",
        str(run_file),
        "--steps-file",
        str(steps_file),
        "--signals-file",
        str(signals_file),
        "--stdout-file",
        str(stdout_file),
        "--run-id",
        run_id,
        "--fallback-task",
        task_name,
        "--status",
        "success" if cli_status == 0 else "failed",
        "--error-message",
        "" if cli_status == 0 else f"{task_name} exited with code {cli_status}",
    ]
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _resolve_profile_ref_for_task(
    *,
    python_bin: Path,
    install_dir: Path,
    requested_profile_ref: str,
    fallback_profile_ref: str,
    ensure_ready: bool,
) -> str:
    browser_target = _resolve_browser_target(
        python_bin=python_bin,
        install_dir=install_dir,
        requested_profile_ref=requested_profile_ref,
        fallback_profile_ref=fallback_profile_ref,
    )
    if ensure_ready:
        _ensure_browser_ready(
            python_bin=python_bin,
            script_dir=SCRIPT_DIR,
            browser_target=browser_target,
        )
    return str(browser_target["profile_ref"])


def _single_row_submit_params(
    *,
    python_bin: Path,
    install_dir: Path,
    requested_profile_ref: str,
    fallback_profile_ref: str,
    record_id: str,
    product_url: str,
    sku_id: str,
    skip_fastmoss_login_validation: bool,
    ensure_ready: bool,
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
        f"record_id={record_id}",
        "fastmoss_phone_env=FASTMOSS_PHONE",
        "fastmoss_password_env=FASTMOSS_PASSWORD",
    ]
    if product_url:
        params.append(f"product_url={product_url}")
    if sku_id:
        params.append(f"sku_id={sku_id}")
    if skip_fastmoss_login_validation:
        params.append("verify_fastmoss_login=false")
    return params


def _refresh_competitor_submit_params(
    *,
    python_bin: Path,
    install_dir: Path,
    requested_profile_ref: str,
    fallback_profile_ref: str,
    ensure_ready: bool,
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
    ]


def _keyword_search_submit_params(
    *,
    python_bin: Path,
    install_dir: Path,
    requested_profile_ref: str,
    fallback_profile_ref: str,
    search_keyword: str,
    sales_7d_threshold: str,
    skip_fastmoss_login_validation: bool,
    ensure_ready: bool,
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
        f"sales_7d_threshold={sales_7d_threshold}",
        "fastmoss_phone_env=FASTMOSS_PHONE",
        "fastmoss_password_env=FASTMOSS_PASSWORD",
    ]
    if skip_fastmoss_login_validation:
        params.append("verify_fastmoss_login=false")
    return params


def _influencer_pool_sync_env(skill_env: dict[str, str]) -> dict[str, str]:
    source_table_url = _require_env_value(skill_env, "INFLUENCER_POOL_SOURCE_TABLE_URL")
    target_table_url = _require_env_value(skill_env, "INFLUENCER_POOL_TARGET_TABLE_URL")
    feishu_access_token_env = _require_env_value(skill_env, "INFLUENCER_POOL_FEISHU_ACCESS_TOKEN_ENV")
    fastmoss_phone_env = _require_env_value(skill_env, "INFLUENCER_POOL_FASTMOSS_PHONE_ENV")
    fastmoss_password_env = _require_env_value(skill_env, "INFLUENCER_POOL_FASTMOSS_PASSWORD_ENV")

    feishu_access_token = _require_env_value(skill_env, feishu_access_token_env)
    fastmoss_phone = _require_env_value(skill_env, fastmoss_phone_env)
    fastmoss_password = _require_env_value(skill_env, fastmoss_password_env)

    return {
        "source_table_url": source_table_url,
        "target_table_url": target_table_url,
        "feishu_access_token_env": feishu_access_token_env,
        "feishu_access_token": feishu_access_token,
        "fastmoss_phone_env": fastmoss_phone_env,
        "fastmoss_phone": fastmoss_phone,
        "fastmoss_password_env": fastmoss_password_env,
        "fastmoss_password": fastmoss_password,
    }


def _influencer_pool_sync_submit_params(
    *,
    skill_env: dict[str, str],
    include_submit_control_action: bool = True,
    max_source_rows: int = 0,
    max_author_pages: int = 0,
    max_author_detail_jobs_per_source_row: int = 0,
    queue_mode: str = "",
    worker_kinds: str = "",
    worker_max_iterations: int = 0,
    worker_stop_when_idle: bool | None = None,
    include_contact: bool = False,
    request_delay_min_seconds: float = 1.0,
    request_delay_max_seconds: float = 3.0,
) -> tuple[list[str], dict[str, str]]:
    config = _influencer_pool_sync_env(skill_env)
    base_params = [
        f"table_url={config['source_table_url']}",
        f"target_table_url={config['target_table_url']}",
        f"access_token_env={config['feishu_access_token_env']}",
        f"fastmoss_phone_env={config['fastmoss_phone_env']}",
        f"fastmoss_password_env={config['fastmoss_password_env']}",
    ]
    if max_source_rows > 0:
        base_params.append(f"max_source_rows={max_source_rows}")
    if max_author_pages > 0:
        base_params.append(f"max_author_pages={max_author_pages}")
    if max_author_detail_jobs_per_source_row > 0:
        base_params.append(
            f"max_author_detail_jobs_per_source_row={max_author_detail_jobs_per_source_row}"
        )
    if queue_mode:
        base_params.append(f"queue_mode={queue_mode}")
    if worker_kinds:
        base_params.append(f"worker_kinds={worker_kinds}")
    if worker_max_iterations >= 0:
        base_params.append(f"worker_max_iterations={worker_max_iterations}")
    if worker_stop_when_idle is not None:
        base_params.append(f"worker_stop_when_idle={str(bool(worker_stop_when_idle)).lower()}")
    if include_contact:
        base_params.append("include_contact=true")
    base_params.append(f"request_delay_min_seconds={max(request_delay_min_seconds, 0.0)}")
    base_params.append(f"request_delay_max_seconds={max(request_delay_max_seconds, 0.0)}")
    if include_submit_control_action:
        base_params.append("control_action=submit")
    params = _append_runtime_params(base_params, skill_env)
    extra_env = {
        config["feishu_access_token_env"]: config["feishu_access_token"],
        config["fastmoss_phone_env"]: config["fastmoss_phone"],
        config["fastmoss_password_env"]: config["fastmoss_password"],
    }
    return params, extra_env


def _append_influencer_pool_browser_params(
    *,
    params: list[str],
    skill_env: dict[str, str],
    python_bin: Path,
    install_dir: Path,
    requested_profile_ref: str,
    fallback_profile_ref: str,
) -> list[str]:
    explicit_provider = _optional_env_value(skill_env, "BROWSER_PROVIDER_NAME")
    explicit_profile_id = _optional_env_value(skill_env, "BROWSER_PROFILE_ID")
    explicit_workspace_id = _optional_env_value(skill_env, "BROWSER_WORKSPACE_ID")
    explicit_profile_ref = requested_profile_ref or fallback_profile_ref

    if explicit_provider and explicit_profile_id and explicit_workspace_id:
        if explicit_profile_ref:
            params.append(f"profile_ref={explicit_profile_ref}")
        params.extend(
            [
                f"browser_provider_name={explicit_provider}",
                f"browser_profile_id={explicit_profile_id}",
                f"browser_workspace_id={explicit_workspace_id}",
            ]
        )
        return params

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
    provider = str(browser_target.get("provider", "") or "").strip()
    profile_id = str(browser_target.get("profile_id", "") or "").strip()
    workspace_id = str(browser_target.get("workspace_id", "") or "").strip()
    if provider:
        params.append(f"browser_provider_name={provider}")
    if profile_id:
        params.append(f"browser_profile_id={profile_id}")
    if workspace_id:
        params.append(f"browser_workspace_id={workspace_id}")
    return params


def _append_runtime_params(params: list[str], skill_env: dict[str, str]) -> list[str]:
    db_url = _optional_env_value(skill_env, "EXECUTION_CONTROL_DB_URL")
    artifact_root = _optional_env_value(skill_env, "EXECUTION_CONTROL_ARTIFACT_ROOT")
    artifact_bucket = _optional_env_value(skill_env, "EXECUTION_CONTROL_ARTIFACT_BUCKET")
    requested_by = _optional_env_value(skill_env, "EXECUTION_CONTROL_REQUESTED_BY")
    notification_channel_code = _optional_env_value(skill_env, "NOTIFICATION_CHANNEL_CODE")
    delivery_context = _discover_openclaw_delivery_context(skill_env)

    if db_url:
        params.append(f"execution_control_db_url={db_url}")
    if artifact_root:
        params.append(f"execution_control_artifact_root={artifact_root}")
    if artifact_bucket:
        params.append(f"execution_control_artifact_bucket={artifact_bucket}")
    if requested_by:
        params.append(f"requested_by={requested_by}")
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


def _build_summary_text(summary: dict[str, Any]) -> str:
    if not isinstance(summary, dict):
        return ""
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
    normalized["failed_item_count"] = int(normalized.get("failed_item_count", 0) or 0)
    normalized.setdefault("error", "")
    normalized.setdefault("run_id", "")
    normalized.setdefault("run_file", "")
    normalized.setdefault("steps_file", "")
    normalized.setdefault("signals_file", "")
    normalized.setdefault("stdout_file", "")
    normalized.setdefault("execution_id", "")
    normalized.setdefault("execution_status", "")
    normalized.setdefault("resource_code", "")
    normalized.setdefault("queue_position", 0)
    normalized.setdefault("wait_timed_out", False)
    normalized.setdefault("daemon_status", "")
    normalized.setdefault("processed_count", 0)
    normalized.setdefault("success_count", 0)
    normalized.setdefault("failed_count", 0)
    normalized.setdefault("artifact_count", 0)
    normalized.setdefault("artifact_uri_prefix", "")
    normalized.setdefault("run_object_key", "")
    normalized.setdefault("steps_object_key", "")
    normalized.setdefault("signals_object_key", "")
    normalized.setdefault("stdout_object_key", "")
    normalized.setdefault("artifacts_dir", "")
    normalized.setdefault("worker_id", "")
    normalized.setdefault("artifacts", [])
    _augment_message_with_request_id(normalized)
    return normalized


def _run_lightweight_submit_capture_payload(
    *,
    install_dir: Path,
    python_bin: Path,
    task_name: str,
    run_mode: str,
    params: list[str],
    stdout_prefix: str,
    extra_env: dict[str, str],
    accepted_message: str,
) -> tuple[int, dict[str, Any]]:
    parsed_params = _parse_param_items(params)
    parsed_params["run_mode"] = run_mode

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
            failed_payload = {
                "status": "failed",
                "task_name": task_name,
                "message": f"{task_name} submit failed.",
                "error": error_message or f"lightweight submit exited with code {result.returncode}",
            }
            return result.returncode, failed_payload
        if not isinstance(payload, dict):
            failed_payload = {
                "status": "failed",
                "task_name": task_name,
                "message": f"{task_name} submit failed.",
                "error": "lightweight submit did not return a JSON object payload",
            }
            return 1, failed_payload
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


def _run_cli_task(
    *,
    install_dir: Path,
    python_bin: Path,
    cli_bin: Path,
    task_name: str,
    run_mode: str,
    params: list[str],
    stdout_prefix: str,
    extra_env: dict[str, str],
) -> int:
    run_dir = install_dir / "runtime" / "cli_runs"
    stdout_dir = run_dir / "stdout"
    stdout_dir.mkdir(parents=True, exist_ok=True)

    run_id = _generate_run_id(task_name)
    run_file = run_dir / f"{run_id}.json"
    steps_file = run_dir / "steps" / f"{run_id}.json"
    signals_file = run_dir / "signals" / f"{run_id}.json"
    stdout_file = stdout_dir / f"{run_id}.log"

    env = os.environ.copy()
    env.update(extra_env)

    command = [
        str(cli_bin),
        "run",
        "--task",
        task_name,
        "--run-mode",
        run_mode,
        "--run-id",
        run_id,
    ]
    for item in params:
        command.extend(["--param", item])

    print(f"[{stdout_prefix}] Running {task_name} with run_mode={run_mode} run_id={run_id}")
    print(f"[{stdout_prefix}] Progress files: run_file={run_file} steps_file={steps_file}")
    print(f"[{stdout_prefix}] CLI output: stdout_file={stdout_file}")

    with stdout_file.open("w", encoding="utf-8") as output_handle:
        process = subprocess.Popen(
            command,
            cwd=str(install_dir),
            env=env,
            stdout=output_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
        _monitor_process(process=process, run_file=run_file, steps_file=steps_file, prefix=stdout_prefix)
        cli_status = process.wait()

    result_json = _build_result_json(
        python_bin=python_bin,
        run_file=run_file,
        steps_file=steps_file,
        signals_file=signals_file,
        stdout_file=stdout_file,
        run_id=run_id,
        task_name=task_name,
        cli_status=cli_status,
    )
    result_file_path = str(env.get("MUJITASK_RESULT_FILE", "") or "").strip()
    if result_file_path:
        Path(result_file_path).write_text(f"{result_json}\n", encoding="utf-8")

    try:
        payload = json.loads(result_json)
    except Exception:
        payload = {}
    summary_text = str(payload.get("summary_text", "") or "").strip()
    if summary_text:
        print(f"[{stdout_prefix}] Summary: {summary_text}")
    if cli_status == 0:
        print(f"[{stdout_prefix}] Completed run_id={run_id}")
    else:
        print(
            f"[{stdout_prefix}] Failed run_id={run_id}. "
            f"Inspect {run_file}, {steps_file}, {signals_file}, and {stdout_file} for details."
        )

    if str(env.get("MUJITASK_SUPPRESS_RESULT_MARKER", "0")) != "1":
        print(f"__OPENCLAW_RESULT__ {result_json}")
    return cli_status


def _run_cli_task_capture_payload(
    *,
    install_dir: Path,
    python_bin: Path,
    cli_bin: Path,
    task_name: str,
    run_mode: str,
    params: list[str],
    stdout_prefix: str,
    extra_env: dict[str, str],
) -> tuple[int, dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="mujitask-skill-result-") as temp_dir:
        result_file = Path(temp_dir) / "result.json"
        env = dict(extra_env)
        env["MUJITASK_RESULT_FILE"] = str(result_file)
        env["MUJITASK_SUPPRESS_RESULT_MARKER"] = "1"
        status = _run_cli_task(
            install_dir=install_dir,
            python_bin=python_bin,
            cli_bin=cli_bin,
            task_name=task_name,
            run_mode=run_mode,
            params=params,
            stdout_prefix=stdout_prefix,
            extra_env=env,
        )
        payload = _read_json_file(result_file)
        return status, payload if isinstance(payload, dict) else {}


def _emit_final_result(payload: dict[str, Any]) -> int:
    result_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if os.getenv("MUJITASK_RESULT_FILE"):
        Path(os.environ["MUJITASK_RESULT_FILE"]).write_text(f"{result_json}\n", encoding="utf-8")
    if os.getenv("MUJITASK_SUPPRESS_RESULT_MARKER", "0") != "1":
        print(f"__OPENCLAW_RESULT__ {result_json}")
    status = str(payload.get("status", "") or payload.get("execution_status", "") or "").strip().lower()
    if status in {"failed", "error"}:
        return 1
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run deterministic OpenClaw skill steps.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    cleanup_parser = subparsers.add_parser("cleanup")
    cleanup_parser.add_argument("--run-mode", default="draft")

    pending_parser = subparsers.add_parser("pending-rows")
    pending_parser.add_argument("--run-mode", default="draft")

    clear_row_parser = subparsers.add_parser("clear-row-by-url")
    clear_row_parser.add_argument("--run-mode", default="canary")
    clear_row_parser.add_argument("--url", required=True)

    login_parser = subparsers.add_parser("fastmoss-login-check")
    login_parser.add_argument("--run-mode", default="draft")
    login_parser.add_argument("--profile-ref", default="")

    update_parser = subparsers.add_parser("single-row-update")
    update_parser.add_argument("--run-mode", default="canary")
    update_parser.add_argument("--record-id", required=True)
    update_parser.add_argument("--profile-ref", default="")
    update_parser.add_argument("--product-url", default="")
    update_parser.add_argument("--sku-id", default="")
    update_parser.add_argument("--skip-fastmoss-login-validation", action="store_true")

    refresh_parser = subparsers.add_parser("refresh-current-competitor-table")
    refresh_parser.add_argument("--run-mode", default="canary")
    refresh_parser.add_argument("--profile-ref", default="")
    refresh_parser.add_argument("--max-idle-cycles", type=int, default=1)

    refresh_submit_parser = subparsers.add_parser("refresh-current-competitor-table-submit")
    refresh_submit_parser.add_argument("--run-mode", default="canary")
    refresh_submit_parser.add_argument("--profile-ref", default="")

    refresh_status_parser = subparsers.add_parser("refresh-current-competitor-table-status")
    refresh_status_parser.add_argument("--run-mode", default="canary")
    refresh_status_parser.add_argument("--request-id", required=True)

    refresh_result_parser = subparsers.add_parser("refresh-current-competitor-table-result")
    refresh_result_parser.add_argument("--run-mode", default="canary")
    refresh_result_parser.add_argument("--request-id", required=True)

    keyword_search_parser = subparsers.add_parser("keyword-search")
    keyword_search_parser.add_argument("--run-mode", default="canary")
    keyword_search_parser.add_argument("--profile-ref", default="")
    keyword_search_parser.add_argument("--search-keyword", required=True)
    keyword_search_parser.add_argument("--sales-7d-threshold", default="200")
    keyword_search_parser.add_argument("--skip-fastmoss-login-validation", action="store_true")

    keyword_search_submit_parser = subparsers.add_parser("keyword-search-submit")
    keyword_search_submit_parser.add_argument("--run-mode", default="canary")
    keyword_search_submit_parser.add_argument("--profile-ref", default="")
    keyword_search_submit_parser.add_argument("--search-keyword", required=True)
    keyword_search_submit_parser.add_argument("--sales-7d-threshold", default="200")
    keyword_search_submit_parser.add_argument("--skip-fastmoss-login-validation", action="store_true")

    keyword_search_status_parser = subparsers.add_parser("keyword-search-status")
    keyword_search_status_parser.add_argument("--run-mode", default="canary")
    keyword_search_status_parser.add_argument("--request-id", required=True)

    keyword_search_result_parser = subparsers.add_parser("keyword-search-result")
    keyword_search_result_parser.add_argument("--run-mode", default="canary")
    keyword_search_result_parser.add_argument("--request-id", required=True)

    influencer_pool_sync_parser = subparsers.add_parser("influencer-pool-sync")
    influencer_pool_sync_parser.add_argument("--run-mode", default="canary")
    influencer_pool_sync_parser.add_argument("--max-source-rows", type=int, default=0)
    influencer_pool_sync_parser.add_argument("--max-author-pages", type=int, default=0)
    influencer_pool_sync_parser.add_argument("--max-author-detail-jobs-per-source-row", type=int, default=0)
    influencer_pool_sync_parser.add_argument("--queue-mode", default="inline")
    influencer_pool_sync_parser.add_argument("--worker-kinds", default="")
    influencer_pool_sync_parser.add_argument("--worker-max-iterations", type=int, default=1)
    influencer_pool_sync_parser.add_argument("--worker-stop-when-idle", action="store_true")
    influencer_pool_sync_parser.add_argument("--include-contact", action="store_true")
    influencer_pool_sync_parser.add_argument("--request-delay-min-seconds", type=float, default=1.0)
    influencer_pool_sync_parser.add_argument("--request-delay-max-seconds", type=float, default=3.0)

    influencer_pool_sync_submit_parser = subparsers.add_parser("influencer-pool-sync-submit")
    influencer_pool_sync_submit_parser.add_argument("--run-mode", default="canary")
    influencer_pool_sync_submit_parser.add_argument("--max-source-rows", type=int, default=0)
    influencer_pool_sync_submit_parser.add_argument("--max-author-pages", type=int, default=0)
    influencer_pool_sync_submit_parser.add_argument("--max-author-detail-jobs-per-source-row", type=int, default=0)
    influencer_pool_sync_submit_parser.add_argument("--queue-mode", default="inline")
    influencer_pool_sync_submit_parser.add_argument("--worker-kinds", default="")
    influencer_pool_sync_submit_parser.add_argument("--worker-max-iterations", type=int, default=1)
    influencer_pool_sync_submit_parser.add_argument("--worker-stop-when-idle", action="store_true")
    influencer_pool_sync_submit_parser.add_argument("--include-contact", action="store_true")
    influencer_pool_sync_submit_parser.add_argument("--request-delay-min-seconds", type=float, default=1.0)
    influencer_pool_sync_submit_parser.add_argument("--request-delay-max-seconds", type=float, default=3.0)

    influencer_pool_sync_status_parser = subparsers.add_parser("influencer-pool-sync-status")
    influencer_pool_sync_status_parser.add_argument("--run-mode", default="canary")
    influencer_pool_sync_status_parser.add_argument("--request-id", required=True)

    influencer_pool_sync_result_parser = subparsers.add_parser("influencer-pool-sync-result")
    influencer_pool_sync_result_parser.add_argument("--run-mode", default="canary")
    influencer_pool_sync_result_parser.add_argument("--request-id", required=True)

    influencer_pool_worker_parser = subparsers.add_parser("influencer-pool-worker")
    influencer_pool_worker_parser.add_argument("--run-mode", default="canary")
    influencer_pool_worker_parser.add_argument("--worker-kinds", default="product,author,finalizer")
    influencer_pool_worker_parser.add_argument("--worker-max-iterations", type=int, default=1)
    influencer_pool_worker_parser.add_argument("--worker-stop-when-idle", action="store_true")
    influencer_pool_worker_parser.add_argument("--include-contact", action="store_true")
    influencer_pool_worker_parser.add_argument("--request-delay-min-seconds", type=float, default=1.0)
    influencer_pool_worker_parser.add_argument("--request-delay-max-seconds", type=float, default=3.0)

    keyword_parser = subparsers.add_parser("keyword-candidates")
    keyword_parser.add_argument("--run-mode", default="draft")
    keyword_parser.add_argument("--profile-ref", default="")
    keyword_parser.add_argument("--search-keyword", required=True)
    keyword_parser.add_argument("--sales-7d-threshold", default="200")
    keyword_parser.add_argument("--skip-fastmoss-login-validation", action="store_true")

    seed_parser = subparsers.add_parser("insert-seed-row")
    seed_parser.add_argument("--run-mode", default="canary")
    seed_parser.add_argument("--sku-id", required=True)
    seed_parser.add_argument("--search-keyword", required=True)
    seed_parser.add_argument("--product-url", default="")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    skill_env = _load_skill_env(ENV_FILE)

    install_dir = Path(_require_env_value(skill_env, "INSTALL_DIR")).expanduser().resolve()
    table_url = _require_env_value(skill_env, "TABLE_URL")
    feishu_access_token = _require_env_value(skill_env, "FEISHU_ACCESS_TOKEN")
    browser_profile_ref = str(skill_env.get("BROWSER_PROFILE_REF", "")).strip()
    fastmoss_phone = str(skill_env.get("FASTMOSS_PHONE", "")).strip()
    fastmoss_password = str(skill_env.get("FASTMOSS_PASSWORD", "")).strip()

    cli_bin = install_dir / ".venv" / "bin" / "automation-business-scaffold-run"
    python_bin = install_dir / ".venv" / "bin" / "python"
    if not cli_bin.exists():
        raise ValueError(f"Cannot find CLI at {cli_bin}. Re-run the deployment script.")
    if not python_bin.exists():
        raise ValueError(f"Cannot find Python at {python_bin}. Re-run the deployment script.")

    extra_env = {
        "FEISHU_ACCESS_TOKEN": feishu_access_token,
    }
    if fastmoss_phone:
        extra_env["FASTMOSS_PHONE"] = fastmoss_phone
    if fastmoss_password:
        extra_env["FASTMOSS_PASSWORD"] = fastmoss_password

    params = [
        f"table_url={table_url}",
        "access_token_env=FEISHU_ACCESS_TOKEN",
        f"url_field_name={DEFAULT_URL_FIELD_NAME}",
    ]
    task_name = ""
    prefix = "skill-step"

    if args.command == "cleanup":
        task_name = "tiktok_product_link_cleanup"
        prefix = "cleanup-step"
    elif args.command == "pending-rows":
        task_name = "feishu_pending_rows_scan"
        prefix = "pending-rows-step"
    elif args.command == "clear-row-by-url":
        task_name = "feishu_clear_row_by_url"
        prefix = "clear-row-by-url-step"
        params.append(f"url={args.url}")
    elif args.command == "fastmoss-login-check":
        task_name = "fastmoss_login_check"
        prefix = "fastmoss-login-check-step"
        browser_target = _resolve_browser_target(
            python_bin=python_bin,
            install_dir=install_dir,
            requested_profile_ref=args.profile_ref,
            fallback_profile_ref=browser_profile_ref,
        )
        _ensure_browser_ready(python_bin=python_bin, script_dir=SCRIPT_DIR, browser_target=browser_target)
        params = [
            f"profile_ref={browser_target['profile_ref']}",
            "fastmoss_phone_env=FASTMOSS_PHONE",
            "fastmoss_password_env=FASTMOSS_PASSWORD",
        ]
    elif args.command == "single-row-update":
        task_name = "feishu_single_row_update"
        prefix = "single-row-update-step"
        params.extend(
            _single_row_submit_params(
                python_bin=python_bin,
                install_dir=install_dir,
                requested_profile_ref=args.profile_ref,
                fallback_profile_ref=browser_profile_ref,
                record_id=args.record_id,
                product_url=args.product_url,
                sku_id=args.sku_id,
                skip_fastmoss_login_validation=args.skip_fastmoss_login_validation,
                ensure_ready=True,
            )
        )
    elif args.command == "refresh-current-competitor-table-submit":
        submit_params = _append_runtime_params(
            [
                f"table_url={table_url}",
                "access_token_env=FEISHU_ACCESS_TOKEN",
                f"url_field_name={DEFAULT_URL_FIELD_NAME}",
                "control_action=submit",
            ],
            skill_env,
        )
        submit_params.extend(
            _refresh_competitor_submit_params(
                python_bin=python_bin,
                install_dir=install_dir,
                requested_profile_ref=args.profile_ref,
                fallback_profile_ref=browser_profile_ref,
                ensure_ready=False,
            )
        )
        submit_status, submit_payload = _run_lightweight_submit_capture_payload(
            install_dir=install_dir,
            python_bin=python_bin,
            task_name="refresh_current_competitor_table",
            run_mode=args.run_mode,
            params=submit_params,
            stdout_prefix="refresh-current-competitor-table-submit-step",
            extra_env=extra_env,
            accepted_message="Refresh task accepted for asynchronous execution.",
        )
        if submit_status != 0:
            return _emit_final_result(submit_payload)
        return _emit_final_result(submit_payload)
    elif args.command == "refresh-current-competitor-table-status":
        task_name = "refresh_current_competitor_table"
        prefix = "refresh-current-competitor-table-status-step"
        params = _append_runtime_params(
            [
                "control_action=status",
                f"request_id={args.request_id}",
            ],
            skill_env,
        )
    elif args.command == "refresh-current-competitor-table-result":
        task_name = "refresh_current_competitor_table"
        prefix = "refresh-current-competitor-table-result-step"
        params = _append_runtime_params(
            [
                "control_action=result",
                f"request_id={args.request_id}",
            ],
            skill_env,
        )
    elif args.command == "refresh-current-competitor-table":
        submit_params = _append_runtime_params(
            [
                f"table_url={table_url}",
                "access_token_env=FEISHU_ACCESS_TOKEN",
                f"url_field_name={DEFAULT_URL_FIELD_NAME}",
                "control_action=submit",
            ],
            skill_env,
        )
        submit_params.extend(
            _refresh_competitor_submit_params(
                python_bin=python_bin,
                install_dir=install_dir,
                requested_profile_ref=args.profile_ref,
                fallback_profile_ref=browser_profile_ref,
                ensure_ready=False,
            )
        )
        submit_status, submit_payload = _run_lightweight_submit_capture_payload(
            install_dir=install_dir,
            python_bin=python_bin,
            task_name="refresh_current_competitor_table",
            run_mode=args.run_mode,
            params=submit_params,
            stdout_prefix="refresh-current-competitor-table-submit-step",
            extra_env=extra_env,
            accepted_message="Refresh task accepted for asynchronous execution.",
        )
        if submit_status != 0:
            return _emit_final_result(submit_payload or {"status": "failed", "error": "submit failed"})
        return _emit_final_result(submit_payload)
    elif args.command == "keyword-search-submit":
        submit_params = _append_runtime_params(
            [
                f"table_url={table_url}",
                "access_token_env=FEISHU_ACCESS_TOKEN",
                f"url_field_name={DEFAULT_URL_FIELD_NAME}",
                "control_action=submit",
            ],
            skill_env,
        )
        submit_params.extend(
            _keyword_search_submit_params(
                python_bin=python_bin,
                install_dir=install_dir,
                requested_profile_ref=args.profile_ref,
                fallback_profile_ref=browser_profile_ref,
                search_keyword=args.search_keyword,
                sales_7d_threshold=args.sales_7d_threshold,
                skip_fastmoss_login_validation=args.skip_fastmoss_login_validation,
                ensure_ready=False,
            )
        )
        submit_status, submit_payload = _run_lightweight_submit_capture_payload(
            install_dir=install_dir,
            python_bin=python_bin,
            task_name="search_keyword_competitor_products",
            run_mode=args.run_mode,
            params=submit_params,
            stdout_prefix="keyword-search-submit-step",
            extra_env=extra_env,
            accepted_message="Keyword search task accepted for asynchronous execution.",
        )
        if submit_status != 0:
            return _emit_final_result(submit_payload)
        return _emit_final_result(submit_payload)
    elif args.command == "keyword-search-status":
        task_name = "search_keyword_competitor_products"
        prefix = "keyword-search-status-step"
        params = _append_runtime_params(
            [
                "control_action=status",
                f"request_id={args.request_id}",
            ],
            skill_env,
        )
    elif args.command == "keyword-search-result":
        task_name = "search_keyword_competitor_products"
        prefix = "keyword-search-result-step"
        params = _append_runtime_params(
            [
                "control_action=result",
                f"request_id={args.request_id}",
            ],
            skill_env,
        )
    elif args.command == "keyword-search":
        submit_params = _append_runtime_params(
            [
                f"table_url={table_url}",
                "access_token_env=FEISHU_ACCESS_TOKEN",
                f"url_field_name={DEFAULT_URL_FIELD_NAME}",
                "control_action=submit",
            ],
            skill_env,
        )
        submit_params.extend(
            _keyword_search_submit_params(
                python_bin=python_bin,
                install_dir=install_dir,
                requested_profile_ref=args.profile_ref,
                fallback_profile_ref=browser_profile_ref,
                search_keyword=args.search_keyword,
                sales_7d_threshold=args.sales_7d_threshold,
                skip_fastmoss_login_validation=args.skip_fastmoss_login_validation,
                ensure_ready=False,
            )
        )
        submit_status, submit_payload = _run_lightweight_submit_capture_payload(
            install_dir=install_dir,
            python_bin=python_bin,
            task_name="search_keyword_competitor_products",
            run_mode=args.run_mode,
            params=submit_params,
            stdout_prefix="keyword-search-submit-step",
            extra_env=extra_env,
            accepted_message="Keyword search task accepted for asynchronous execution.",
        )
        if submit_status != 0:
            return _emit_final_result(submit_payload or {"status": "failed", "error": "submit failed"})
        return _emit_final_result(submit_payload)
    elif args.command == "influencer-pool-sync-submit":
        task_name = "sync_tk_influencer_pool"
        prefix = "influencer-pool-sync-submit-step"
        submit_params, influencer_pool_env = _influencer_pool_sync_submit_params(
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
        submit_status, submit_payload = _run_lightweight_submit_capture_payload(
            install_dir=install_dir,
            python_bin=python_bin,
            task_name=task_name,
            run_mode=args.run_mode,
            params=submit_params,
            stdout_prefix=prefix,
            extra_env={**extra_env, **influencer_pool_env},
            accepted_message="Influencer pool sync task accepted for asynchronous execution.",
        )
        if submit_status != 0:
            return _emit_final_result(submit_payload or {"status": "failed", "error": "submit failed"})
        return _emit_final_result(submit_payload)
    elif args.command == "influencer-pool-sync-status":
        task_name = "sync_tk_influencer_pool"
        prefix = "influencer-pool-sync-status-step"
        params = _append_runtime_params(
            [
                "control_action=status",
                f"request_id={args.request_id}",
            ],
            skill_env,
        )
    elif args.command == "influencer-pool-sync-result":
        task_name = "sync_tk_influencer_pool"
        prefix = "influencer-pool-sync-result-step"
        params = _append_runtime_params(
            [
                "control_action=result",
                f"request_id={args.request_id}",
            ],
            skill_env,
        )
    elif args.command == "influencer-pool-sync":
        task_name = "sync_tk_influencer_pool"
        prefix = "influencer-pool-sync-step"
        submit_params, influencer_pool_env = _influencer_pool_sync_submit_params(
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
        submit_status, submit_payload = _run_lightweight_submit_capture_payload(
            install_dir=install_dir,
            python_bin=python_bin,
            task_name=task_name,
            run_mode=args.run_mode,
            params=submit_params,
            stdout_prefix=prefix,
            extra_env={**extra_env, **influencer_pool_env},
            accepted_message="Influencer pool sync task accepted for asynchronous execution.",
        )
        if submit_status != 0:
            return _emit_final_result(submit_payload or {"status": "failed", "error": "submit failed"})
        return _emit_final_result(submit_payload)
    elif args.command == "influencer-pool-worker":
        task_name = "sync_tk_influencer_pool"
        prefix = "influencer-pool-worker-step"
        params, influencer_pool_env = _influencer_pool_sync_submit_params(
            skill_env=skill_env,
            include_submit_control_action=False,
            queue_mode="worker",
            worker_kinds=str(args.worker_kinds or "product,author,finalizer"),
            worker_max_iterations=max(args.worker_max_iterations, 0),
            worker_stop_when_idle=bool(args.worker_stop_when_idle),
            include_contact=bool(args.include_contact),
            request_delay_min_seconds=float(args.request_delay_min_seconds),
            request_delay_max_seconds=float(args.request_delay_max_seconds),
        )
        cli_status, cli_payload = _run_cli_task_capture_payload(
            install_dir=install_dir,
            python_bin=python_bin,
            cli_bin=cli_bin,
            task_name=task_name,
            run_mode=args.run_mode,
            params=params,
            stdout_prefix=prefix,
            extra_env={**extra_env, **influencer_pool_env},
        )
        if cli_status != 0:
            return _emit_final_result(cli_payload or {"status": "failed", "error": "run failed"})
        return _emit_final_result(cli_payload)
    elif args.command == "keyword-candidates":
        task_name = "fastmoss_keyword_candidate_discovery"
        prefix = "keyword-candidates-step"
        params.extend(
            [
                f"search_keyword={args.search_keyword}",
                f"sales_7d_threshold={args.sales_7d_threshold}",
                "fastmoss_phone_env=FASTMOSS_PHONE",
                "fastmoss_password_env=FASTMOSS_PASSWORD",
            ]
        )
        browser_target = _resolve_browser_target(
            python_bin=python_bin,
            install_dir=install_dir,
            requested_profile_ref=args.profile_ref,
            fallback_profile_ref=browser_profile_ref,
        )
        _ensure_browser_ready(python_bin=python_bin, script_dir=SCRIPT_DIR, browser_target=browser_target)
        params.append(f"profile_ref={browser_target['profile_ref']}")
        if args.skip_fastmoss_login_validation:
            params.append("verify_fastmoss_login=false")
    elif args.command == "insert-seed-row":
        task_name = "feishu_seed_row_insert"
        prefix = "insert-seed-row-step"
        params.extend(
            [
                f"sku_id={args.sku_id}",
                f"search_keyword={args.search_keyword}",
            ]
        )
        if args.product_url:
            params.append(f"product_url={args.product_url}")
    else:
        raise ValueError(f"Unsupported command: {args.command}")

    return _run_cli_task(
        install_dir=install_dir,
        python_bin=python_bin,
        cli_bin=cli_bin,
        task_name=task_name,
        run_mode=args.run_mode,
        params=params,
        stdout_prefix=prefix,
        extra_env=extra_env,
    )


DEFAULT_URL_FIELD_NAME = "产品链接"


if __name__ == "__main__":
    raise SystemExit(main())
