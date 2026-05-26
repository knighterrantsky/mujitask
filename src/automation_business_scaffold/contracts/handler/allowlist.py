from __future__ import annotations

import re
from types import MappingProxyType

from .contract import HandlerContract

_STANDARD_RESULT_STATUSES = (
    "success",
    "skipped",
    "partial_success",
    "failed",
    "fallback_required",
)

PROHIBITED_HANDLER_CODES = MappingProxyType(
    {
        "orchestrate_sync_tk_influencer_pool": (
            "workflow orchestrator names cannot enter the handler registry."
        ),
        "feishu_single_row_update": (
            "legacy Feishu single-row writebacks must migrate to feishu_table_write."
        ),
        "feishu_seed_row_insert": (
            "legacy Feishu seed-row insertion must migrate to feishu_table_write."
        ),
        "feishu_tk_selection_table_read": (
            "table-specific Feishu reads must live in adapters, not runtime handlers."
        ),
        "feishu_tk_selection_table_writeback": (
            "table-specific Feishu writebacks must live in projection mappers, not runtime handlers."
        ),
        "influencer_pool_product": (
            "influencer pool workflow cannot register business-specific runtime handlers."
        ),
        "influencer_pool_author": (
            "author terminology is not admitted; use fastmoss_creator_fetch instead."
        ),
        "influencer_pool_finalizer": (
            "workflow finalization stays in executor/reconciler, not the handler registry."
        ),
        "fastmoss_author_fetch": (
            "creator is the admitted FastMoss entity name; use fastmoss_creator_fetch."
        ),
        "fastmoss_product_search_v1": (
            "handler codes must stay stable and cannot evolve with version suffixes."
        ),
        "fastmoss_product_search_v2": (
            "handler codes must stay stable and cannot evolve with version suffixes."
        ),
        "selection_table_source_adapter": (
            "adapters are internal workflow components, not registry keys."
        ),
        "competitor_table_projection_mapper": (
            "mappers are internal workflow components, not registry keys."
        ),
    }
)

PROHIBITED_HANDLER_CODE_PATTERNS = (
    (re.compile(r"^orchestrate_"), "workflow orchestrator names cannot enter the handler registry."),
    (re.compile(r"^run_.*_workflow$"), "workflow entrypoints cannot enter the handler registry."),
    (re.compile(r"^run_sync_"), "workflow sync entrypoints cannot enter the handler registry."),
    (re.compile(r".*_orchestrator$"), "orchestrator names cannot enter the handler registry."),
    (
        re.compile(r".*_(adapter|mapper|policy|renderer)$"),
        "adapters, mappers, policies, and renderers are not runtime handler codes.",
    ),
)


def prohibited_handler_code_reason(handler_code: str) -> str | None:
    exact_reason = PROHIBITED_HANDLER_CODES.get(handler_code)
    if exact_reason is not None:
        return exact_reason
    for pattern, reason in PROHIBITED_HANDLER_CODE_PATTERNS:
        if pattern.match(handler_code):
            return reason
    return None


def _contract(
    *,
    handler_code: str,
    worker_type: str,
    runtime_table: str,
    purpose: str,
    contract_reference: str,
    side_effects: tuple[str, ...] = (),
) -> HandlerContract:
    return HandlerContract(
        handler_code=handler_code,
        worker_type=worker_type,
        runtime_table=runtime_table,
        purpose=purpose,
        payload_schema={
            "required": [],
            "optional": [],
            "forbidden": [],
            "notes": (
                "Baseline skeleton. Concrete payload fields should be filled in the "
                "per-handler module before the runtime starts executing it."
            ),
        },
        result_schema={
            "statuses": list(_STANDARD_RESULT_STATUSES),
            "notes": "Handlers return the standard HandlerResult envelope.",
        },
        error_schema={
            "shape": "HandlerError",
            "notes": "Handlers return the standard HandlerError envelope for failed statuses.",
        },
        retry_policy={
            "mode": "per-handler",
            "notes": "Worker/reconciler should read retryability from HandlerError plus handler policy.",
        },
        timeout_policy={
            "mode": "per-handler",
            "notes": "Worker supervisor owns wall-clock enforcement; handler documents its expectation here.",
        },
        idempotency_policy={
            "mode": "per-handler",
            "notes": "Each handler must define its own dedupe key and side-effect boundary.",
        },
        side_effects=side_effects,
        progress_policy={
            "mode": "worker_supervised",
            "notes": "Supervisor manages lease, heartbeat, and progress shell; handler fills summary/result.",
        },
        reconciler_contract={
            "notes": "Reconciler consumes status, summary, result, warnings, next_action, and error.",
        },
        contract_reference=contract_reference,
    )


API_HANDLER_CONTRACTS = MappingProxyType(
    {
        "feishu_table_read": _contract(
            handler_code="feishu_table_read",
            worker_type="api_worker",
            runtime_table="api_worker_job",
            purpose="Read Feishu table data; business semantics stay in source adapters.",
            contract_reference="docs/arch/handler-contract-design.md#51-feishu_table_read",
            side_effects=("feishu.read",),
        ),
        "feishu_table_write": _contract(
            handler_code="feishu_table_write",
            worker_type="api_worker",
            runtime_table="api_worker_job",
            purpose="Insert or update Feishu table rows; field projection stays in mappers.",
            contract_reference="docs/arch/handler-contract-design.md#53-feishu_table_write",
            side_effects=("feishu.write",),
        ),
        "competitor_row_refresh": _contract(
            handler_code="competitor_row_refresh",
            worker_type="api_worker",
            runtime_table="api_worker_job",
            purpose="Refresh one competitor row as a serial pipeline that reuses existing step handlers.",
            contract_reference="docs/arch/handler-contract-design.md#68-competitor_row_refresh",
            side_effects=("tiktok.request", "fastmoss.request", "artifact.write", "fact_db.write", "feishu.write"),
        ),
        "keyword_seed_import": _contract(
            handler_code="keyword_seed_import",
            worker_type="api_worker",
            runtime_table="api_worker_job",
            purpose="Search FastMoss products and write keyword competitor seed rows as one business job.",
            contract_reference="docs/arch/workflow-competitor-table-design.md#73-关键词搜索竞品写入-keyword_seed_import",
            side_effects=("fastmoss.request", "feishu.write"),
        ),
        "product_creator_discovery": _contract(
            handler_code="product_creator_discovery",
            worker_type="api_worker",
            runtime_table="api_worker_job",
            purpose="Discover related creators for one competitor product as one business job.",
            contract_reference="docs/arch/workflow-influencer-pool-sync-design.md#112-商品达人发现-product_creator_discovery",
            side_effects=("fastmoss.request",),
        ),
        "influencer_creator_sync": _contract(
            handler_code="influencer_creator_sync",
            worker_type="api_worker",
            runtime_table="api_worker_job",
            purpose="Sync one unique creator into TK influencer pool and reconcile touched product statuses.",
            contract_reference="docs/arch/workflow-influencer-pool-sync-design.md#113-达人同步业务-job-influencer_creator_sync",
            side_effects=("fastmoss.request", "fact_db.write", "artifact.write", "feishu.write"),
        ),
        "tiktok_product_request_fetch": _contract(
            handler_code="tiktok_product_request_fetch",
            worker_type="api_worker",
            runtime_table="api_worker_job",
            purpose="Fetch TikTok product data through the request-first path.",
            contract_reference="docs/arch/handler-contract-design.md#61-tiktok_product_request_fetch",
            side_effects=("tiktok.request",),
        ),
        "fastmoss_product_search": _contract(
            handler_code="fastmoss_product_search",
            worker_type="api_worker",
            runtime_table="api_worker_job",
            purpose="Search FastMoss products with keyword, filter, and condition inputs.",
            contract_reference="docs/arch/handler-contract-design.md#63-fastmoss_product_search",
            side_effects=("fastmoss.request",),
        ),
        "fastmoss_product_fetch": _contract(
            handler_code="fastmoss_product_fetch",
            worker_type="api_worker",
            runtime_table="api_worker_job",
            purpose="Fetch FastMoss product facts, shop facts, and product metrics.",
            contract_reference="docs/arch/handler-contract-design.md#64-fastmoss_product_fetch",
            side_effects=("fastmoss.request",),
        ),
        "fastmoss_creator_fetch": _contract(
            handler_code="fastmoss_creator_fetch",
            worker_type="api_worker",
            runtime_table="api_worker_job",
            purpose="Fetch FastMoss creator facts and creator metrics.",
            contract_reference="docs/arch/handler-contract-design.md#65-fastmoss_creator_fetch--fastmoss_shop_fetch--fastmoss_video_fetch",
            side_effects=("fastmoss.request",),
        ),
        "fastmoss_shop_fetch": _contract(
            handler_code="fastmoss_shop_fetch",
            worker_type="api_worker",
            runtime_table="api_worker_job",
            purpose="Fetch FastMoss shop facts and shop metrics.",
            contract_reference="docs/arch/handler-contract-design.md#65-fastmoss_creator_fetch--fastmoss_shop_fetch--fastmoss_video_fetch",
            side_effects=("fastmoss.request",),
        ),
        "fastmoss_video_fetch": _contract(
            handler_code="fastmoss_video_fetch",
            worker_type="api_worker",
            runtime_table="api_worker_job",
            purpose="Fetch FastMoss video facts and video metrics.",
            contract_reference="docs/arch/handler-contract-design.md#65-fastmoss_creator_fetch--fastmoss_shop_fetch--fastmoss_video_fetch",
            side_effects=("fastmoss.request",),
        ),
        "product_video_outreach_check": _contract(
            handler_code="product_video_outreach_check",
            worker_type="api_worker",
            runtime_table="api_worker_job",
            purpose="Collect FastMoss product-associated videos through HTTP for outreach creator matches.",
            contract_reference="docs/arch/workflow-influencer-outreach-design.md#62-product_video_outreach_check-payload",
            side_effects=("fastmoss.request", "artifact.write"),
        ),
        "media_asset_sync": _contract(
            handler_code="media_asset_sync",
            worker_type="api_worker",
            runtime_table="api_worker_job",
            purpose="Sync media assets into object storage and fact indexes.",
            contract_reference="docs/arch/handler-contract-design.md#66-media_asset_sync",
            side_effects=("artifact.write", "fact.index"),
        ),
        "fact_bundle_upsert": _contract(
            handler_code="fact_bundle_upsert",
            worker_type="api_worker",
            runtime_table="api_worker_job",
            purpose="Upsert normalized entities, relations, and observations into Fact DB.",
            contract_reference="docs/arch/handler-contract-design.md#67-fact_bundle_upsert",
            side_effects=("fact_db.write",),
        ),
        "selection_row_refresh": _contract(
            handler_code="selection_row_refresh",
            worker_type="api_worker",
            runtime_table="api_worker_job",
            purpose="Refresh one selection row as a serial pipeline that reuses existing step handlers.",
            contract_reference="docs/arch/workflow-selection-table-design.md#4-job-设计",
            side_effects=("runtime_db", "feishu.write", "fact_db.write", "artifact.write", "fastmoss.request", "tiktok.request"),
        ),
    }
)

BROWSER_HANDLER_CONTRACTS = MappingProxyType(
    {
        "tiktok_product_browser_fetch": _contract(
            handler_code="tiktok_product_browser_fetch",
            worker_type="browser_worker",
            runtime_table="task_execution",
            purpose="Fetch TikTok product data via browser fallback after request path failure.",
            contract_reference="docs/arch/handler-contract-design.md#62-tiktok_product_browser_fetch",
            side_effects=("browser.fetch", "artifact.write"),
        ),
        "fastmoss_security_browser_resolve": _contract(
            handler_code="fastmoss_security_browser_resolve",
            worker_type="browser_worker",
            runtime_table="task_execution",
            purpose="Resolve FastMoss search security verification in a real browser and refresh the cookie cache.",
            contract_reference="docs/arch/workflow-competitor-table-design.md#73-关键词搜索竞品写入-keyword_seed_import",
            side_effects=("browser.security_resolve", "fastmoss.cookie_cache.write"),
        ),
    }
)

OUTBOX_HANDLER_CONTRACTS = MappingProxyType(
    {
        "outbox_dispatch": _contract(
            handler_code="outbox_dispatch",
            worker_type="outbox_dispatcher",
            runtime_table="notification_outbox",
            purpose="Dispatch final notifications and task summaries from the notification outbox.",
            contract_reference="docs/arch/handler-contract-design.md#91-准入清单",
            side_effects=("notification.send",),
        )
    }
)

API_HANDLER_CODES = frozenset(API_HANDLER_CONTRACTS)
BROWSER_HANDLER_CODES = frozenset(BROWSER_HANDLER_CONTRACTS)
OUTBOX_HANDLER_CODES = frozenset(OUTBOX_HANDLER_CONTRACTS)

HANDLER_CONTRACTS_BY_REGISTRY = MappingProxyType(
    {
        "api": API_HANDLER_CONTRACTS,
        "browser": BROWSER_HANDLER_CONTRACTS,
        "outbox": OUTBOX_HANDLER_CONTRACTS,
    }
)

ALLOWED_HANDLER_CODES = frozenset(
    API_HANDLER_CODES | BROWSER_HANDLER_CODES | OUTBOX_HANDLER_CODES
)
