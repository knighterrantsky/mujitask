from __future__ import annotations

import re
from pathlib import Path

from automation_business_scaffold.contracts.handler.allowlist import (
    API_HANDLER_CONTRACTS,
    BROWSER_HANDLER_CONTRACTS,
    OUTBOX_HANDLER_CONTRACTS,
    PROHIBITED_HANDLER_CODES,
)
from automation_business_scaffold.contracts.handler.api import BOUND_API_HANDLERS
from automation_business_scaffold.contracts.handler.browser import BOUND_BROWSER_HANDLERS
from automation_business_scaffold.contracts.handler.outbox import BOUND_OUTBOX_HANDLERS


REPO_ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST_BY_WORKER = {
    "api": {
        "competitor_row_refresh",
        "selection_row_refresh",
        "keyword_seed_import",
        "feishu_table_read",
        "feishu_table_write",
        "tiktok_product_request_fetch",
        "fastmoss_product_search",
        "fastmoss_product_fetch",
        "fastmoss_creator_fetch",
        "fastmoss_shop_fetch",
        "fastmoss_video_fetch",
        "product_video_outreach_check",
        "outreach_creator_video_metric_refresh",
        "product_creator_discovery",
        "influencer_creator_sync",
        "media_asset_sync",
        "fact_bundle_upsert",
    },
    "browser": {"fastmoss_security_browser_resolve", "tiktok_product_browser_fetch"},
    "outbox": {"outbox_dispatch"},
}
FORBIDDEN_EXACT_NAMES = {
    "orchestrate_sync_tk_influencer_pool",
    "feishu_single_row_update",
    "feishu_seed_row_insert",
    "feishu_tk_selection_table_read",
    "feishu_tk_selection_table_writeback",
    "influencer_pool_product",
    "influencer_pool_author",
    "influencer_pool_finalizer",
    "fastmoss_author_fetch",
    "fastmoss_product_search_v1",
    "fastmoss_product_search_v2",
    "selection_table_source_adapter",
    "competitor_table_projection_mapper",
}
FORBIDDEN_PATTERNS = (
    re.compile(r"^orchestrate_"),
    re.compile(r"^run_.*_workflow$"),
    re.compile(r"^run_sync_"),
    re.compile(r".*_orchestrator$"),
    re.compile(r".*_(adapter|mapper|policy|renderer)$"),
)

BOUND_HANDLERS_BY_WORKER = {
    "api": BOUND_API_HANDLERS,
    "browser": BOUND_BROWSER_HANDLERS,
    "outbox": BOUND_OUTBOX_HANDLERS,
}
CONTRACTS_BY_WORKER = {
    "api": API_HANDLER_CONTRACTS,
    "browser": BROWSER_HANDLER_CONTRACTS,
    "outbox": OUTBOX_HANDLER_CONTRACTS,
}


def test_legacy_business_package_is_not_part_of_runtime_source() -> None:
    legacy_root = REPO_ROOT / "src" / "automation_business_scaffold" / "business"
    assert not legacy_root.exists(), "legacy business package must stay outside the runtime source tree."


def test_handler_allowlist_matches_documented_contract() -> None:
    for worker, expected in ALLOWLIST_BY_WORKER.items():
        actual = set(CONTRACTS_BY_WORKER[worker])
        assert actual == expected, (
            f"{worker} handler allowlist drifted from docs/arch/handler-contract-design.md: "
            f"expected {sorted(expected)}, got {sorted(actual)}"
        )

    missing_prohibited = sorted(name for name in FORBIDDEN_EXACT_NAMES if name not in PROHIBITED_HANDLER_CODES)
    assert missing_prohibited == [], (
        "handlers.allowlist should explicitly reject the documented legacy/disallowed names:\n"
        + "\n".join(missing_prohibited)
    )


def test_handler_modules_must_stay_within_the_documented_allowlist() -> None:
    problems: list[str] = []

    for worker, allowlist in ALLOWLIST_BY_WORKER.items():
        implemented = set(BOUND_HANDLERS_BY_WORKER[worker])

        unexpected = sorted(name for name in implemented if name not in allowlist)
        if unexpected:
            problems.append(f"{worker}: unexpected handler modules {unexpected}")

        forbidden = sorted(
            name
            for name in implemented
            if name in FORBIDDEN_EXACT_NAMES or any(pattern.match(name) for pattern in FORBIDDEN_PATTERNS)
        )
        if forbidden:
            problems.append(f"{worker}: forbidden handler names {forbidden}")

    assert problems == [], (
        "handler registry may only route allowlisted handler codes from docs/arch/handler-contract-design.md:\n"
        + "\n".join(problems)
    )


def test_each_allowlisted_handler_has_a_named_module_with_contract() -> None:
    problems: list[str] = []

    for worker, allowlist in ALLOWLIST_BY_WORKER.items():
        for handler_code in sorted(allowlist):
            contract = CONTRACTS_BY_WORKER[worker].get(handler_code)
            if getattr(contract, "handler_code", "") != handler_code:
                problems.append(f"{worker}: contract does not match {handler_code}")
                continue
            handler = BOUND_HANDLERS_BY_WORKER[worker].get(handler_code)
            if handler is None:
                problems.append(f"{worker}: missing bound handler {handler_code}")

    assert problems == [], (
        "each admitted handler must have a contract and bound capability implementation:\n"
        + "\n".join(problems)
    )
