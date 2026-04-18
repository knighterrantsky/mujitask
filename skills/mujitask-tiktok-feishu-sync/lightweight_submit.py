#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import sys
import os
import time
from pathlib import Path
from typing import Any, Callable


def _load_submitter(install_dir: Path, task_name: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    if task_name == "sync_tk_influencer_pool":
        return _submit_sync_tk_influencer_pool

    src_dir = install_dir / "src"
    if not src_dir.exists():
        raise ValueError(f"Cannot find project source directory at {src_dir}.")
    src_path = str(src_dir)
    if src_path not in sys.path:
        sys.path.insert(0, src_path)

    from automation_business_scaffold.flows import (  # pylint: disable=import-outside-toplevel
        submit_refresh_current_competitor_table,
        submit_search_keyword_competitor_products,
    )

    submitters: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
        "refresh_current_competitor_table": submit_refresh_current_competitor_table,
        "search_keyword_competitor_products": submit_search_keyword_competitor_products,
        "sync_tk_influencer_pool": _submit_sync_tk_influencer_pool,
    }
    submitter = submitters.get(task_name)
    if submitter is None:
        available = ", ".join(sorted(submitters))
        raise ValueError(f"Unsupported lightweight submit task '{task_name}'. Available: {available}")
    return submitter


def _build_submit_request_id(task_name: str) -> str:
    return f"openclaw-{task_name}-{time.strftime('%Y%m%d%H%M%S')}-{os.getpid()}"


def _submit_sync_tk_influencer_pool(params: dict[str, Any]) -> dict[str, Any]:
    source_table_url = str(params.get("table_url", "") or "").strip()
    target_table_url = str(params.get("target_table_url", "") or "").strip()
    access_token_env = str(params.get("access_token_env", "") or "").strip()
    fastmoss_phone_env = str(params.get("fastmoss_phone_env", "") or "").strip()
    fastmoss_password_env = str(params.get("fastmoss_password_env", "") or "").strip()
    if not source_table_url:
        raise ValueError("sync_tk_influencer_pool requires table_url.")
    if not target_table_url:
        raise ValueError("sync_tk_influencer_pool requires target_table_url.")
    if not access_token_env:
        raise ValueError("sync_tk_influencer_pool requires access_token_env.")
    if not fastmoss_phone_env:
        raise ValueError("sync_tk_influencer_pool requires fastmoss_phone_env.")
    if not fastmoss_password_env:
        raise ValueError("sync_tk_influencer_pool requires fastmoss_password_env.")

    request_id = _build_submit_request_id("sync_tk_influencer_pool")
    return {
        "status": "success",
        "task_name": "sync_tk_influencer_pool",
        "control_action": str(params.get("control_action", "") or "submit"),
        "request_id": request_id,
        "message": "Influencer pool sync submit placeholder accepted. Use influencer-pool-sync to execute immediately.",
        "summary": {"total": 1, "counts": {"queued": 1}},
        "failed_item_count": 0,
        "request_status": "pending",
        "execution_status": "",
        "queue_position": 0,
        "wait_timed_out": False,
        "daemon_status": "",
        "processed_count": 0,
        "success_count": 0,
        "failed_count": 0,
        "artifact_count": 0,
        "artifacts": [],
        "submit_params": {
            "table_url": source_table_url,
            "target_table_url": target_table_url,
            "access_token_env": access_token_env,
            "fastmoss_phone_env": fastmoss_phone_env,
            "fastmoss_password_env": fastmoss_password_env,
            "run_mode": str(params.get("run_mode", "") or ""),
        },
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run lightweight Phase 1 submit helpers.")
    parser.add_argument("--install-dir", required=True)
    parser.add_argument("--task-name", required=True)
    parser.add_argument("--params-json", required=True)
    parser.add_argument("--result-file", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    install_dir = Path(args.install_dir).expanduser().resolve()
    result_file = Path(args.result_file).expanduser().resolve()

    params = json.loads(args.params_json)
    if not isinstance(params, dict):
        raise ValueError("--params-json must decode to a JSON object.")

    submitter = _load_submitter(install_dir, args.task_name)
    payload = submitter(params)
    if not isinstance(payload, dict):
        raise TypeError(f"Lightweight submitter for {args.task_name} must return a dict payload.")
    result_file.parent.mkdir(parents=True, exist_ok=True)
    result_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
