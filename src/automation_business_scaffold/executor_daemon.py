from __future__ import annotations

import argparse
import contextlib
import json
import sys
from typing import Any

from automation_business_scaffold.flows import (
    execute_next_controlled_feishu_single_row_update,
    run_controlled_executor_daemon,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="automation-business-scaffold-executor",
        description="Run the Phase 1 controlled executor for feishu_single_row_update.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process at most one queued execution and exit.",
    )
    parser.add_argument(
        "--db-url",
        help="Override BUSINESS_EXECUTION_CONTROL_DB_URL, usually a postgresql+psycopg URL.",
    )
    parser.add_argument(
        "--db-path",
        help="Fallback SQLite database path when --db-url is not set.",
    )
    parser.add_argument(
        "--artifact-root",
        help="Override BUSINESS_EXECUTION_CONTROL_ARTIFACT_ROOT.",
    )
    parser.add_argument(
        "--artifact-bucket",
        help="Override BUSINESS_EXECUTION_CONTROL_ARTIFACT_BUCKET.",
    )
    parser.add_argument(
        "--worker-id",
        help="Override BUSINESS_EXECUTION_CONTROL_WORKER_ID.",
    )
    parser.add_argument(
        "--lease-seconds",
        type=float,
        help="Override BUSINESS_EXECUTION_CONTROL_LEASE_SECONDS.",
    )
    parser.add_argument(
        "--poll-interval-seconds",
        type=float,
        help="Override BUSINESS_EXECUTION_CONTROL_POLL_INTERVAL_SECONDS.",
    )
    parser.add_argument(
        "--stop-when-idle",
        action="store_true",
        help="Exit once the queue stays idle for max idle cycles.",
    )
    parser.add_argument(
        "--max-idle-cycles",
        type=int,
        default=1,
        help="Idle loop count before exit when --stop-when-idle is enabled.",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=0,
        help="Maximum daemon loop iterations. 0 means unlimited.",
    )
    return parser


def _build_params(args: argparse.Namespace) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if args.db_url:
        params["execution_control_db_url"] = args.db_url
    if args.db_path:
        params["execution_control_db_path"] = args.db_path
    if args.artifact_root:
        params["execution_control_artifact_root"] = args.artifact_root
    if args.artifact_bucket:
        params["execution_control_artifact_bucket"] = args.artifact_bucket
    if args.worker_id:
        params["execution_worker_id"] = args.worker_id
    if args.lease_seconds is not None:
        params["execution_lease_seconds"] = args.lease_seconds
    if args.poll_interval_seconds is not None:
        params["execution_poll_interval_seconds"] = args.poll_interval_seconds
    if not args.once:
        params["execution_control_stop_when_idle"] = args.stop_when_idle
        params["execution_control_max_idle_cycles"] = max(args.max_idle_cycles, 1)
        params["execution_control_max_iterations"] = max(args.max_iterations, 0)
    return params


def _exit_code(payload: dict[str, Any]) -> int:
    if payload.get("execution_status") == "failed":
        return 1
    if int(payload.get("failed_count", 0) or 0) > 0:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    params = _build_params(args)
    try:
        with contextlib.redirect_stdout(sys.stderr):
            if args.once:
                payload = execute_next_controlled_feishu_single_row_update(params)
            else:
                payload = run_controlled_executor_daemon(params)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": str(exc),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return _exit_code(payload)


if __name__ == "__main__":
    raise SystemExit(main())
