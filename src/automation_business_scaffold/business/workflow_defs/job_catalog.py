from __future__ import annotations

from .models import (
    IdempotencyRule,
    JobDefinition,
    SummaryPolicy,
    SummaryStatusRule,
    TimeoutRule,
    WatchdogRule,
    contract,
    optional_field,
    required_field,
)

DEFAULT_CONTRACT_REVISION = "2026-04-23"

STANDARD_ERROR_CONTRACT = contract(
    "standard_error",
    required_field("error_type", "Stable error family for executor/reconciler decisions.", type_hint="str"),
    required_field("error_code", "Stable machine-readable error code.", type_hint="str"),
    required_field("message", "Human-readable failure detail.", type_hint="str"),
    optional_field("retryable", "Whether the failing job can be retried.", type_hint="bool"),
    optional_field("fallback_allowed", "Whether the workflow may dispatch a fallback stage.", type_hint="bool"),
    optional_field("fallback_reason", "Reason for fallback routing.", type_hint="str"),
)

STANDARD_SUMMARY_CONTRACT = contract(
    "workflow_summary",
    required_field("final_status", "Workflow terminal status.", type_hint="str"),
    optional_field("child_total_count", "Total child jobs observed by reconciler.", type_hint="int"),
    optional_field("child_success_count", "Child jobs completed with success.", type_hint="int"),
    optional_field("child_failed_count", "Child jobs completed with failed.", type_hint="int"),
    optional_field("child_skipped_count", "Child jobs completed with skipped.", type_hint="int"),
    optional_field("warnings", "Business or retry warnings.", type_hint="list[str]"),
)

COMMON_TASK_COMPLETED_NOTIFICATION_JOB = JobDefinition(
    job_code="task_completed_notification",
    handler_code="outbox_dispatch",
    worker_type="outbox_dispatcher",
    runtime_table="notification_outbox",
    purpose="Deliver the finalized workflow summary to reply targets via the notification outbox.",
    payload_contract=contract(
        "task_completed_notification_payload",
        required_field("request_id", "Top-level task request identifier.", type_hint="str"),
        required_field("summary_payload", "Normalized summary prepared by executor.", type_hint="dict[str, Any]"),
        optional_field("reply_target", "Outbound reply target.", type_hint="str"),
        optional_field("channel_code", "Destination channel code.", type_hint="str"),
    ),
    result_contract=contract(
        "task_completed_notification_result",
        required_field("event_type", "Dispatched event type.", type_hint="str"),
        optional_field("delivery_targets", "Resolved delivery targets.", type_hint="list[str]"),
    ),
    business_key_template="{request_id}",
    dedupe_key_template="task_request.completed:{request_id}",
    side_effects=("notification_outbox",),
)

FEISHU_TABLE_READ_JOB = JobDefinition(
    job_code="feishu_table_read",
    handler_code="feishu_table_read",
    worker_type="api_worker",
    runtime_table="api_worker_job",
    purpose="Read source rows or source snapshots from a Feishu table and normalize source context.",
    payload_contract=contract(
        "feishu_table_read_payload",
        required_field("source_table_ref", "Stable identifier for the source table or app table.", type_hint="str"),
        optional_field("view_ref", "Optional view identifier.", type_hint="str"),
        optional_field("filter_spec", "Normalized filter settings for the read.", type_hint="dict[str, Any]"),
        optional_field("adapter_code", "Source adapter used after transport-level read.", type_hint="str"),
        optional_field("cursor_context", "Existing stage cursor data for incremental reads.", type_hint="dict[str, Any]"),
    ),
    result_contract=contract(
        "feishu_table_read_result",
        required_field("source_rows", "Normalized source rows for executor fan-out.", type_hint="list[dict[str, Any]]"),
        optional_field("source_snapshot", "Source snapshot or metadata extracted from Feishu.", type_hint="dict[str, Any]"),
        optional_field("candidate_keys", "Candidate entity keys discovered from the rows.", type_hint="list[str]"),
        optional_field("writeback_context", "Context later reused by Feishu writeback stages.", type_hint="dict[str, Any]"),
    ),
    business_key_template="{source_table_ref}",
    dedupe_key_template="{request_id}:{job_code}:{source_table_ref}:{view_ref_or_default}",
    side_effects=("feishu", "runtime_db"),
)

FEISHU_TABLE_WRITE_JOB = JobDefinition(
    job_code="feishu_table_write",
    handler_code="feishu_table_write",
    worker_type="api_worker",
    runtime_table="api_worker_job",
    purpose="Write business projections back to a Feishu table through a projection mapper.",
    payload_contract=contract(
        "feishu_table_write_payload",
        required_field("target_table_ref", "Stable identifier for the target Feishu table.", type_hint="str"),
        required_field("records", "Normalized rows to insert or update.", type_hint="list[dict[str, Any]]"),
        optional_field("mapper_code", "Projection mapper applied before write.", type_hint="str"),
        optional_field("write_mode", "Insert, update, or upsert mode.", type_hint="str"),
        optional_field("idempotency_context", "Stable business identity for dedupe and checkpointing.", type_hint="dict[str, Any]"),
    ),
    result_contract=contract(
        "feishu_table_write_result",
        required_field("written_count", "Number of records written.", type_hint="int"),
        optional_field("target_record_ids", "Feishu record ids created or updated.", type_hint="list[str]"),
        optional_field("skipped_count", "Number of skipped rows.", type_hint="int"),
        optional_field("writeback_context", "Projection context for later summary or follow-up.", type_hint="dict[str, Any]"),
    ),
    business_key_template="{target_table_ref}:{business_entity_key}",
    dedupe_key_template="{request_id}:{job_code}:{target_table_ref}:{business_entity_key}",
    side_effects=("feishu", "runtime_db"),
)

TIKTOK_PRODUCT_REQUEST_FETCH_JOB = JobDefinition(
    job_code="tiktok_product_request_fetch",
    handler_code="tiktok_product_request_fetch",
    worker_type="api_worker",
    runtime_table="api_worker_job",
    purpose="Fetch and normalize TikTok product facts through request/API paths before any browser fallback.",
    payload_contract=contract(
        "tiktok_product_request_fetch_payload",
        required_field("product_identity", "Normalized TikTok product key or URL bundle.", type_hint="dict[str, Any]"),
        optional_field("normalized_product_url", "Canonical product URL for dedupe and artifact naming.", type_hint="str"),
        optional_field("source_context", "Business source row or source request context.", type_hint="dict[str, Any]"),
        optional_field("fallback_allowed", "Whether browser fallback is permitted for this request.", type_hint="bool"),
    ),
    result_contract=contract(
        "tiktok_product_request_fetch_result",
        optional_field("normalized_product_result", "Normalized TikTok product result contract.", type_hint="dict[str, Any]"),
        optional_field("fallback_required", "Whether browser fallback should be dispatched.", type_hint="bool"),
        optional_field("fallback_reason", "Stable fallback reason code.", type_hint="str"),
        optional_field("fallback_source_job_id", "Source API job that requested fallback.", type_hint="str"),
    ),
    business_key_template="{product_id_or_url}",
    dedupe_key_template="{request_id}:{stage_code}:{product_id_or_url}",
    side_effects=("runtime_db",),
    notes=(
        "Handlers should prefer request/API collection and only request browser fallback for recoverable cases.",
    ),
)

TIKTOK_PRODUCT_BROWSER_FETCH_JOB = JobDefinition(
    job_code="tiktok_product_browser_fetch",
    handler_code="tiktok_product_browser_fetch",
    worker_type="browser_worker",
    runtime_table="task_execution",
    purpose="Collect TikTok product page data through the browser as a fallback path.",
    payload_contract=contract(
        "tiktok_product_browser_fetch_payload",
        required_field("product_identity", "Normalized TikTok product key or URL bundle.", type_hint="dict[str, Any]"),
        required_field("fallback_source_job_id", "Request/API job that triggered the fallback.", type_hint="str"),
        optional_field("resource_code", "Browser resource or profile affinity key.", type_hint="str"),
        optional_field("normalized_product_url", "Canonical product URL for resource and artifact naming.", type_hint="str"),
    ),
    result_contract=contract(
        "tiktok_product_browser_fetch_result",
        required_field("normalized_product_result", "Normalized TikTok product result contract.", type_hint="dict[str, Any]"),
        optional_field("artifact_refs", "Browser artifacts stored for audit or parsing.", type_hint="list[str]"),
        optional_field("fallback_source_job_id", "Original request/API job id.", type_hint="str"),
    ),
    business_key_template="{normalized_product_url}",
    dedupe_key_template="{request_id}:{job_code}:{normalized_product_url}",
    side_effects=("browser", "object_store", "runtime_db"),
)

FASTMOSS_PRODUCT_SEARCH_JOB = JobDefinition(
    job_code="fastmoss_product_search",
    handler_code="fastmoss_product_search",
    worker_type="api_worker",
    runtime_table="api_worker_job",
    purpose="Search FastMoss products from normalized keyword or filter inputs.",
    payload_contract=contract(
        "fastmoss_product_search_payload",
        required_field("search_query", "Keyword or normalized product search query.", type_hint="str"),
        optional_field("filters", "Normalized FastMoss search filters.", type_hint="dict[str, Any]"),
        optional_field("limit", "Requested maximum candidate count.", type_hint="int"),
        optional_field("condition_context", "Output condition context consumed by executor.", type_hint="dict[str, Any]"),
    ),
    result_contract=contract(
        "fastmoss_product_search_result",
        required_field("candidates", "Normalized candidate product list.", type_hint="list[dict[str, Any]]"),
        optional_field("raw_response_ref", "Artifact ref for the raw FastMoss response.", type_hint="str"),
        optional_field("condition_context", "Condition context returned for candidate processing.", type_hint="dict[str, Any]"),
    ),
    business_key_template="{search_query}",
    dedupe_key_template="{request_id}:{job_code}:{search_digest}",
    side_effects=("fastmoss", "runtime_db"),
)

FASTMOSS_PRODUCT_FETCH_JOB = JobDefinition(
    job_code="fastmoss_product_fetch",
    handler_code="fastmoss_product_fetch",
    worker_type="api_worker",
    runtime_table="api_worker_job",
    purpose="Fetch normalized FastMoss product facts, metrics, and optional relation detail.",
    payload_contract=contract(
        "fastmoss_product_fetch_payload",
        required_field("product_identity", "Normalized business key for the product.", type_hint="dict[str, Any]"),
        optional_field("detail_level", "Requested detail level, such as related_creators.", type_hint="str"),
        optional_field("source_context", "Source row or request context.", type_hint="dict[str, Any]"),
    ),
    result_contract=contract(
        "fastmoss_product_fetch_result",
        optional_field("product_fact_bundle", "Normalized product/store metric bundle.", type_hint="dict[str, Any]"),
        optional_field("related_creators", "Related creator candidates for influencer fan-out.", type_hint="list[dict[str, Any]]"),
        optional_field("metrics_snapshot", "Observation snapshot for later fact upsert.", type_hint="dict[str, Any]"),
    ),
    business_key_template="{product_id_or_fastmoss_key}",
    dedupe_key_template="{request_id}:{stage_code}:{product_id_or_fastmoss_key}",
    side_effects=("fastmoss", "runtime_db"),
)

FASTMOSS_CREATOR_FETCH_JOB = JobDefinition(
    job_code="fastmoss_creator_fetch",
    handler_code="fastmoss_creator_fetch",
    worker_type="api_worker",
    runtime_table="api_worker_job",
    purpose="Fetch creator detail, creator facts, and related creator media context from FastMoss.",
    payload_contract=contract(
        "fastmoss_creator_fetch_payload",
        required_field("creator_identity", "Normalized creator or influencer business key.", type_hint="dict[str, Any]"),
        optional_field("source_context", "Source record, product, or parent job context.", type_hint="dict[str, Any]"),
        optional_field("detail_level", "Requested creator detail level.", type_hint="str"),
    ),
    result_contract=contract(
        "fastmoss_creator_fetch_result",
        required_field("creator_fact_bundle", "Normalized creator fact bundle.", type_hint="dict[str, Any]"),
        optional_field("product_relations", "Creator-to-product relation facts.", type_hint="list[dict[str, Any]]"),
        optional_field("media_refs", "Avatar or other media refs that may require sync.", type_hint="list[str]"),
    ),
    business_key_template="{creator_id}",
    dedupe_key_template="{request_id}:{stage_code}:{product_id_or_group}:{creator_id}",
    side_effects=("fastmoss", "runtime_db"),
)

MEDIA_ASSET_SYNC_JOB = JobDefinition(
    job_code="media_asset_sync",
    handler_code="media_asset_sync",
    worker_type="api_worker",
    runtime_table="api_worker_job",
    purpose="Sync normalized media assets into object storage and emit media fact references.",
    payload_contract=contract(
        "media_asset_sync_payload",
        required_field("asset_refs", "Media assets to sync.", type_hint="list[dict[str, Any]]"),
        required_field("entity_keys", "Business entity keys that own the assets.", type_hint="list[str]"),
        optional_field("source_context", "Source task or stage context.", type_hint="dict[str, Any]"),
    ),
    result_contract=contract(
        "media_asset_sync_result",
        required_field("synced_assets", "Synced asset records and object refs.", type_hint="list[dict[str, Any]]"),
        optional_field("artifact_refs", "Object store or raw artifact refs.", type_hint="list[str]"),
    ),
    business_key_template="{entity_key}:{asset_source}",
    dedupe_key_template="{request_id}:{job_code}:{entity_key}:{asset_source}",
    side_effects=("object_store", "runtime_db"),
)

FACT_BUNDLE_UPSERT_JOB = JobDefinition(
    job_code="fact_bundle_upsert",
    handler_code="fact_bundle_upsert",
    worker_type="api_worker",
    runtime_table="api_worker_job",
    purpose="Upsert normalized entities, relations, observations, and raw links into Fact DB.",
    payload_contract=contract(
        "fact_bundle_upsert_payload",
        required_field("fact_bundle", "Normalized entities, relations, and observations.", type_hint="dict[str, Any]"),
        optional_field("mapper_code", "Business relation mapper used before upsert.", type_hint="str"),
        optional_field("observation_context", "Observation and snapshot context.", type_hint="dict[str, Any]"),
    ),
    result_contract=contract(
        "fact_bundle_upsert_result",
        required_field("upserted_entities", "Entity keys written to Fact DB.", type_hint="list[str]"),
        optional_field("upserted_relations", "Relation keys written to Fact DB.", type_hint="list[str]"),
        optional_field("observation_refs", "Observation refs created during upsert.", type_hint="list[str]"),
    ),
    business_key_template="{entity_business_keys}",
    dedupe_key_template="{request_id}:{job_code}:{entity_business_keys}:{observation_at}",
    side_effects=("fact_db", "runtime_db"),
)


def notification_summary_policy(*rules: SummaryStatusRule, notes: tuple[str, ...] = ()) -> SummaryPolicy:
    return SummaryPolicy(
        summary_stage_code="ready_for_summary",
        outbox_job_code=COMMON_TASK_COMPLETED_NOTIFICATION_JOB.job_code,
        rules=tuple(rules),
        notes=notes,
    )


def standard_watchdog_rules(*, include_browser: bool) -> tuple[WatchdogRule, ...]:
    rules = [
        WatchdogRule(
            rule_code="task_request_lease_expired",
            condition="task_request lease expired before the current stage reached a terminal handoff",
            action="reset task_request to pending or ready_for_summary for idempotent replay",
        ),
        WatchdogRule(
            rule_code="api_worker_job_stale_progress",
            condition="api_worker_job has no progress update within the configured stale-progress window",
            action="move the API job to retry_wait or failed based on attempt budget and retry policy",
        ),
        WatchdogRule(
            rule_code="orphaned_children_reconcile",
            condition="child jobs are terminal but parent task_request has not advanced",
            action="rerun reconciler and advance the parent stage or ready_for_summary idempotently",
        ),
    ]
    if include_browser:
        rules.append(
            WatchdogRule(
                rule_code="browser_execution_stale_heartbeat",
                condition="task_execution heartbeat expired during browser fallback",
                action="release the browser lease and requeue or fail the fallback execution idempotently",
            )
        )
    return tuple(rules)


def table_workflow_timeout_rules(*, include_browser: bool) -> tuple[TimeoutRule, ...]:
    rules = [
        TimeoutRule("workflow", 7200, "Overall table workflow orchestration timeout budget."),
        TimeoutRule("feishu_table_read", 180, "Source table reads should finish quickly."),
        TimeoutRule("feishu_table_write", 180, "Projection writes should stay within one worker lease."),
        TimeoutRule("tiktok_product_request_fetch", 300, "TikTok request-first fetch timeout."),
        TimeoutRule("fastmoss_product_fetch", 300, "FastMoss product fetch timeout."),
        TimeoutRule("media_asset_sync", 300, "Media sync timeout."),
        TimeoutRule("fact_bundle_upsert", 180, "Fact DB upsert timeout."),
    ]
    if include_browser:
        rules.append(TimeoutRule("tiktok_product_browser_fetch", 900, "Browser fallback timeout."))
    return tuple(rules)


def single_product_timeout_rules(*, include_browser: bool) -> tuple[TimeoutRule, ...]:
    rules = [
        TimeoutRule("workflow", 1800, "Overall single-product ingest timeout budget."),
        TimeoutRule("feishu_table_read", 180, "Optional selection table read timeout."),
        TimeoutRule("feishu_table_write", 180, "Optional selection table writeback timeout."),
        TimeoutRule("tiktok_product_request_fetch", 300, "TikTok request-first fetch timeout."),
        TimeoutRule("fastmoss_product_fetch", 300, "FastMoss product fetch timeout."),
        TimeoutRule("media_asset_sync", 300, "Media sync timeout."),
        TimeoutRule("fact_bundle_upsert", 180, "Fact DB upsert timeout."),
    ]
    if include_browser:
        rules.append(TimeoutRule("tiktok_product_browser_fetch", 900, "Browser fallback timeout."))
    return tuple(rules)


def influencer_timeout_rules() -> tuple[TimeoutRule, ...]:
    return (
        TimeoutRule("workflow", 5400, "Overall influencer synchronization timeout budget."),
        TimeoutRule("feishu_table_read", 180, "Competitor candidate reads should finish quickly."),
        TimeoutRule("fastmoss_product_fetch", 300, "Related creator discovery timeout."),
        TimeoutRule("fastmoss_creator_fetch", 300, "Creator detail fetch timeout."),
        TimeoutRule("feishu_table_write", 180, "Influencer pool and competitor status write timeout."),
    )


def table_workflow_idempotency_rules(
    *,
    request_scope: str,
    row_scope: str,
) -> tuple[IdempotencyRule, ...]:
    return (
        IdempotencyRule(
            scope="request",
            key_template=request_scope,
            description="One top-level request should map to one workflow orchestration window.",
        ),
        IdempotencyRule(
            scope="row_job",
            key_template=row_scope,
            description="One source row or product identity should fan out to at most one child job per stage.",
        ),
        IdempotencyRule(
            scope="outbox",
            key_template="task_request.completed:{request_id}",
            description="The final notification should only be sent once per request.",
        ),
    )


def ingest_idempotency_rules() -> tuple[IdempotencyRule, ...]:
    return (
        IdempotencyRule(
            scope="request",
            key_template="{request_id}:{workflow_code}",
            description="One ingest request should own one product collection orchestration window.",
        ),
        IdempotencyRule(
            scope="product_job",
            key_template="{request_id}:{stage_code}:{product_id_or_url}",
            description="The same product identity should only produce one child job per ingest stage.",
        ),
        IdempotencyRule(
            scope="writeback",
            key_template="{request_id}:{source_record_id}",
            description="Selection table writeback must be idempotent per request and source row.",
        ),
        IdempotencyRule(
            scope="outbox",
            key_template="task_request.completed:{request_id}",
            description="The final notification should only be sent once per request.",
        ),
    )


def influencer_idempotency_rules() -> tuple[IdempotencyRule, ...]:
    return (
        IdempotencyRule(
            scope="request",
            key_template="{request_id}:{workflow_code}",
            description="One influencer sync request should map to one orchestration window.",
        ),
        IdempotencyRule(
            scope="product_discovery",
            key_template="{request_id}:discover_related_creators:{source_record_id}:{product_id}",
            description="Each product discovery job should be unique within a request.",
        ),
        IdempotencyRule(
            scope="creator_detail",
            key_template="{request_id}:collect_creator_detail:{source_record_id}:{product_id}:{influencer_id}",
            description="Each creator detail job should be unique within a request.",
        ),
        IdempotencyRule(
            scope="outbox",
            key_template="task_request.completed:{request_id}",
            description="The final notification should only be sent once per request.",
        ),
    )
