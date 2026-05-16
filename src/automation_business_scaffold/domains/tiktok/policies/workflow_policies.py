from __future__ import annotations

from automation_business_scaffold.contracts.workflow import (
    IdempotencyRule,
    SummaryPolicy,
    SummaryStatusRule,
    TimeoutRule,
    WatchdogRule,
    contract,
    optional_field,
    required_field,
)
from automation_business_scaffold.domains.tiktok.jobs import (
    TASK_COMPLETED_NOTIFICATION_JOB,
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


def notification_summary_policy(*rules: SummaryStatusRule, notes: tuple[str, ...] = ()) -> SummaryPolicy:
    return SummaryPolicy(
        summary_stage_code="ready_for_summary",
        outbox_job_code=TASK_COMPLETED_NOTIFICATION_JOB.job_code,
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
        TimeoutRule("competitor_row_refresh", 1800, "One competitor row pipeline should finish within one orchestration window."),
        TimeoutRule("tiktok_product_request_fetch", 300, "TikTok request-first fetch timeout."),
        TimeoutRule("fastmoss_product_fetch", 300, "FastMoss product fetch timeout."),
        TimeoutRule("media_asset_sync", 300, "Media sync timeout."),
        TimeoutRule("fact_bundle_upsert", 180, "Fact DB upsert timeout."),
    ]
    if include_browser:
        rules.append(TimeoutRule("tiktok_product_browser_fetch", 900, "Browser fallback timeout."))
        rules.append(
            TimeoutRule(
                "fastmoss_security_browser_resolve",
                900,
                "FastMoss search security browser fallback timeout.",
            )
        )
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
        TimeoutRule("product_creator_discovery", 420, "One-product creator discovery business job timeout."),
        TimeoutRule("influencer_creator_sync", 720, "One-creator detail, fact, media, and Feishu sync business job timeout."),
        TimeoutRule("fastmoss_security_browser_resolve", 900, "FastMoss auth/security browser fallback timeout."),
        TimeoutRule("fastmoss_product_fetch", 300, "Related creator discovery timeout."),
        TimeoutRule("fastmoss_creator_fetch", 300, "Creator detail fetch timeout."),
        TimeoutRule("fact_bundle_upsert", 180, "Influencer fact persistence timeout."),
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
            scope="creator_sync",
            key_template="{request_id}:sync_influencer_pool:{creator_id}",
            description="Each unique creator sync business job should be unique within a request.",
        ),
        IdempotencyRule(
            scope="outbox",
            key_template="task_request.completed:{request_id}",
            description="The final notification should only be sent once per request.",
        ),
    )
