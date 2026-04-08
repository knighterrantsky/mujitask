#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
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
    if os.getenv("MUJITASK_RESULT_FILE"):
        Path(os.environ["MUJITASK_RESULT_FILE"]).write_text(f"{result_json}\n", encoding="utf-8")

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

    if os.getenv("MUJITASK_SUPPRESS_RESULT_MARKER", "0") != "1":
        print(f"__OPENCLAW_RESULT__ {result_json}")
    return cli_status


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run deterministic OpenClaw skill steps.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    cleanup_parser = subparsers.add_parser("cleanup")
    cleanup_parser.add_argument("--run-mode", default="draft")

    pending_parser = subparsers.add_parser("pending-rows")
    pending_parser.add_argument("--run-mode", default="draft")

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
        params.append(f"record_id={args.record_id}")
        if args.product_url:
            params.append(f"product_url={args.product_url}")
        if args.sku_id:
            params.append(f"sku_id={args.sku_id}")
        browser_target = _resolve_browser_target(
            python_bin=python_bin,
            install_dir=install_dir,
            requested_profile_ref=args.profile_ref,
            fallback_profile_ref=browser_profile_ref,
        )
        _ensure_browser_ready(python_bin=python_bin, script_dir=SCRIPT_DIR, browser_target=browser_target)
        params.append(f"profile_ref={browser_target['profile_ref']}")
        params.extend(
            [
                "fastmoss_phone_env=FASTMOSS_PHONE",
                "fastmoss_password_env=FASTMOSS_PASSWORD",
            ]
        )
        if args.skip_fastmoss_login_validation:
            params.append("verify_fastmoss_login=false")
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
