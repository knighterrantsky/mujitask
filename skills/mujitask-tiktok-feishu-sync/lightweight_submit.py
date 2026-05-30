#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable


def _load_submitter(install_dir: Path, task_name: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    src_dir = install_dir / "src"
    if not src_dir.exists():
        raise ValueError(f"Cannot find project source directory at {src_dir}.")
    src_path = str(src_dir)
    if src_path not in sys.path:
        sys.path.insert(0, src_path)

    from automation_business_scaffold.control_plane.executor.runner import (  # pylint: disable=import-outside-toplevel
        run_refresh_competitor_row_by_url_request,
        run_refresh_current_competitor_table_request,
        run_search_keyword_competitor_products_request,
        run_search_keyword_selection_products_request,
        run_sync_tk_influencer_pool_request,
        run_tiktok_influencer_outreach_sync_request,
        run_tiktok_fastmoss_product_ingest_request,
    )

    submitters: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
        "refresh_competitor_row_by_url": run_refresh_competitor_row_by_url_request,
        "refresh_current_competitor_table": run_refresh_current_competitor_table_request,
        "search_keyword_competitor_products": run_search_keyword_competitor_products_request,
        "search_keyword_selection_products": run_search_keyword_selection_products_request,
        "sync_tk_influencer_pool": run_sync_tk_influencer_pool_request,
        "tiktok_influencer_outreach_sync": run_tiktok_influencer_outreach_sync_request,
        "tiktok_fastmoss_product_ingest": run_tiktok_fastmoss_product_ingest_request,
    }
    submitter = submitters.get(task_name)
    if submitter is None:
        available = ", ".join(sorted(submitters))
        raise ValueError(f"Unsupported lightweight submit task '{task_name}'. Available: {available}")
    return submitter


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run lightweight browser runtime submit helpers.")
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
    control_action = str(params.get("control_action") or "submit").strip()
    if control_action != "submit":
        raise ValueError("Lightweight skill submit only supports control_action=submit.")

    submitter = _load_submitter(install_dir, args.task_name)
    payload = submitter(params)
    if not isinstance(payload, dict):
        raise TypeError(f"Lightweight submitter for {args.task_name} must return a dict payload.")
    result_file.parent.mkdir(parents=True, exist_ok=True)
    result_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
