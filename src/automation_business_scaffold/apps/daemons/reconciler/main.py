from __future__ import annotations

import argparse
import json

from automation_business_scaffold.control_plane.reconciler.reconciler import (
    reconcile_parent_after_child_completion,
)
from automation_business_scaffold.control_plane.runtime_config.settings import (
    build_runtime_settings,
    create_runtime_store,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="automation-business-scaffold-reconciler",
        description="Repair a parent task_request after child runtime records finish.",
    )
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--db-url")
    args = parser.parse_args(argv)
    params = {"execution_control_db_url": args.db_url} if args.db_url else {}
    store = create_runtime_store(build_runtime_settings(params))
    payload = reconcile_parent_after_child_completion(store=store, request_id=args.request_id)
    print(json.dumps({"updates": payload}, ensure_ascii=False, indent=2))
    return 0


__all__ = ["main"]
