from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from automation_business_scaffold.contracts.handler.contract import (
    HandlerContext,
    HandlerError,
    HandlerResult,
)
from automation_business_scaffold.control_plane.executor import worker_dispatch
from automation_business_scaffold.control_plane.supervisor.execution_supervisor import (
    ExecutionProgressEvent,
    ExecutionSupervisorError,
    ExecutionSupervisorOutcome,
)
from automation_business_scaffold.domains.amazon.projections import (
    runtime_result_projection as amazon_runtime_projection,
)


ASIN = "B0ABC12345"
MAIN_IMAGE_URL = "https://m.media-amazon.com/images/I/main.jpg"
GALLERY_IMAGE_URL = "https://images-na.ssl-images-amazon.com/images/I/gallery.jpg"
REQUEST_ID = "a" * 32
BROWSER_EXECUTION_ID = "b" * 32
PERSIST_JOB_ID = "c" * 32
STABLE_RUN_ID = "d" * 64


def _context(
    payload: dict[str, Any] | None = None,
    *,
    attempt_count: int = 1,
    max_attempts: int = 1,
) -> HandlerContext:
    return HandlerContext(
        request_id=REQUEST_ID,
        job_id=PERSIST_JOB_ID,
        handler_code="amazon_product_row_persist",
        worker_type="api_worker",
        runtime_table="api_worker_job",
        payload=payload or _payload(),
        workflow_code="refresh_amazon_product_row_by_asin",
        stage_code="persist_amazon_product_detail",
        job_code="amazon_product_row_persist",
        business_key=f"amazon:US:{ASIN}",
        dedupe_key=f"{REQUEST_ID}:amazon_persist:rec-1:{ASIN}",
        attempt_count=attempt_count,
        max_attempts=max_attempts,
        metadata={"run_id": STABLE_RUN_ID},
    )


def _payload(*, collection_status: str = "success") -> dict[str, Any]:
    normalized_digest = "e" * 64
    html_digest = "f" * 64
    normalized_ref = {
        "capture_kind": "normalized_capture",
        "bucket": "test-artifacts",
        "object_key": (
            f"test/raw-captures/amazon/us/{ASIN}/2026/07/15/"
            f"{STABLE_RUN_ID}/{normalized_digest}/normalized.json"
        ),
        "content_digest": normalized_digest,
        "content_type": "application/json",
        "sanitization_status": "normalized",
        "request_id": REQUEST_ID,
        "execution_id": BROWSER_EXECUTION_ID,
        "run_id": STABLE_RUN_ID,
        "collected_at": "2026-07-15T00:00:00Z",
    }
    html_ref = {
        "capture_kind": "html",
        "bucket": "test-artifacts",
        "object_key": (
            f"test/raw-captures/amazon/us/{ASIN}/2026/07/15/"
            f"{STABLE_RUN_ID}/{html_digest}/page.html.gz"
        ),
        "content_digest": html_digest,
        "content_type": "application/gzip",
        "sanitization_status": "sanitized",
        "request_id": REQUEST_ID,
        "execution_id": BROWSER_EXECUTION_ID,
        "run_id": STABLE_RUN_ID,
        "collected_at": "2026-07-15T00:00:00Z",
    }
    return {
        "table_ref": "AMAZON_PRODUCTS",
        "source_record_id": "rec-1",
        "source_table_identity": {"base_id": "app-1", "table_id": "tbl-1"},
        "requested_asin": ASIN,
        "resolved_asin": ASIN,
        "canonical_url": f"https://www.amazon.com/dp/{ASIN}",
        "run_id": STABLE_RUN_ID,
        "collection_status": collection_status,
        "field_coverage": {"total": 20, "observed": 20, "missing": 0, "percentage": 100.0},
        "browser_provider_name": "roxy",
        "stage_durations_ms": {
            "navigation": 12.5,
            "parse": 3.25,
            "artifact": 8.75,
        },
        "normalized_capture_ref": normalized_ref,
        "raw_capture_refs": [normalized_ref, html_ref],
        "media_source_refs": [
            {
                "source_url": MAIN_IMAGE_URL,
                "source_platform": "amazon",
                "marketplace_code": "US",
                "product_id": ASIN,
                "media_role": "main_image",
                "position": 0,
            },
            {
                "source_url": GALLERY_IMAGE_URL,
                "source_platform": "amazon",
                "marketplace_code": "US",
                "product_id": ASIN,
                "media_role": "gallery_image",
                "position": 1,
            },
        ],
        "request_payload": {
            "table_ref": "AMAZON_PRODUCTS",
            "source_record_id": "rec-1",
        },
    }


def _materialized_assets(*, include_gallery: bool = True) -> list[dict[str, Any]]:
    assets = [
        {
            "source_url": MAIN_IMAGE_URL,
            "source_platform": "amazon",
            "marketplace_code": "US",
            "product_id": ASIN,
            "media_role": "main_image",
            "position": 0,
            "sync_state": "uploaded",
            "bucket": "test-artifacts",
            "object_key": f"test/product-media/amazon/us/{ASIN}/main_image/{'1' * 64}.jpg",
            "remote_uri": (
                f"s3://test-artifacts/test/product-media/amazon/us/{ASIN}/main_image/{'1' * 64}.jpg"
            ),
            "content_digest": "1" * 64,
            "size_bytes": 10,
        }
    ]
    if include_gallery:
        assets.append(
            {
                "source_url": GALLERY_IMAGE_URL,
                "source_platform": "amazon",
                "marketplace_code": "US",
                "product_id": ASIN,
                "media_role": "gallery_image",
                "position": 1,
                "sync_state": "uploaded",
                "bucket": "test-artifacts",
                "object_key": (f"test/product-media/amazon/us/{ASIN}/gallery_image/{'2' * 64}.jpg"),
                "remote_uri": (
                    f"s3://test-artifacts/test/product-media/amazon/us/{ASIN}/"
                    f"gallery_image/{'2' * 64}.jpg"
                ),
                "content_digest": "2" * 64,
                "size_bytes": 11,
            }
        )
    return assets


def _projection_facts(*, collection_status: str = "success") -> dict[str, Any]:
    return {
        "source_record_id": "rec-1",
        "requested_asin": ASIN,
        "resolved_asin": ASIN,
        "canonical_url": f"https://www.amazon.com/dp/{ASIN}",
        "captured_at": "2026-07-15T00:00:00Z",
        "collection_status": collection_status,
        "product": {},
        "commerce": {"availability_status": "in_stock", "featured_offer": {}},
        "variants": {"parent_asin": None, "child_asins": []},
        "rankings": [],
        "media": {},
        "field_evidence": {},
    }


def _fake_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    outcomes: dict[str, Callable[[HandlerContext], HandlerResult]],
) -> list[HandlerContext]:
    from automation_business_scaffold.domains.amazon.flows.amazon_product_row_persist import (
        orchestrator,
    )

    calls: list[HandlerContext] = []

    def resolve(handler_code: str) -> Callable[[HandlerContext], HandlerResult]:
        def dispatch(context: HandlerContext) -> HandlerResult:
            calls.append(context)
            return outcomes[handler_code](context)

        return dispatch

    monkeypatch.setattr(orchestrator, "api_handler_callable", resolve)
    return calls


def _success_outcomes() -> dict[str, Callable[[HandlerContext], HandlerResult]]:
    def media(context: HandlerContext) -> HandlerResult:
        assets = _materialized_assets()
        return HandlerResult.success(
            context,
            summary={"asset_count": 2, "synced_count": 2, "artifact_count": 2},
            result={"synced_assets": assets, "artifact_refs": []},
        )

    def facts(context: HandlerContext) -> HandlerResult:
        return HandlerResult.success(
            context,
            summary={
                "collection_status": "success",
                "persistence_mode": "database",
                "persisted_counts": {"products": 1, "product_snapshots": 1},
                "media_coverage": {
                    "expected": 2,
                    "materialized": 2,
                    "missing": 0,
                    "complete": True,
                },
            },
            result={
                "product_id": "product-1",
                "snapshot_id": "snapshot-1",
                "binding_id": "binding-1",
                "raw_capture_ids": ["raw-1", "raw-2"],
                "normalized_capture_ref": context.payload["normalized_capture_ref"],
                "persisted_counts": {"products": 1, "product_snapshots": 1},
                "media_coverage": {
                    "expected": 2,
                    "materialized": 2,
                    "missing": 0,
                    "complete": True,
                },
                "projection_facts": _projection_facts(),
            },
        )

    def write(context: HandlerContext) -> HandlerResult:
        return HandlerResult.success(
            context,
            summary={"written_count": 1, "skipped_count": 0, "failed_count": 0},
            result={
                "written_count": 1,
                "skipped_count": 0,
                "failed_count": 0,
                "target_record_ids": ["rec-1"],
            },
        )

    return {
        "media_asset_sync": media,
        "amazon_product_fact_upsert": facts,
        "feishu_table_write": write,
    }


def test_job_contract_declares_compact_serial_row_persistence() -> None:
    from automation_business_scaffold.domains.amazon.jobs.feishu_table_write import (
        FEISHU_TABLE_WRITE_JOB,
    )
    from automation_business_scaffold.domains.amazon.jobs.amazon_product_row_persist import (
        JOB_DEFINITION,
    )

    assert JOB_DEFINITION.job_code == "amazon_product_row_persist"
    assert JOB_DEFINITION.handler_code == "amazon_product_row_persist"
    assert JOB_DEFINITION.worker_type == "api_worker"
    assert JOB_DEFINITION.runtime_table == "api_worker_job"
    assert JOB_DEFINITION.dedupe_key_template == (
        "{request_id}:amazon_persist:{source_record_id}:{requested_asin}"
    )
    assert "observability" in JOB_DEFINITION.result_contract.field_names()
    assert FEISHU_TABLE_WRITE_JOB.result_contract.field_names(required_only=True) == (
        "written_count",
        "skipped_count",
        "failed_count",
        "target_record_ids",
    )


def test_row_persist_serially_dispatches_media_fact_and_same_record_writeback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from automation_business_scaffold.domains.amazon.jobs.amazon_product_row_persist import (
        amazon_product_row_persist_handler,
    )

    payload = _payload()
    payload.update(
        {
            "browser_profile_id": "profile-must-not-be-returned",
            "browser_workspace_id": "workspace-must-not-be-returned",
            "browser_provider_token": "token-must-not-be-returned",
        }
    )
    payload["request_payload"]["table_refs"] = {
        "AMAZON_PRODUCTS": {
            "app_token": "must-not-override-resolved-identity",
            "table_id": "must-not-override-resolved-identity",
            "access_token": "must-not-be-forwarded",
            "access_token_env": "AMAZON_FEISHU_TOKEN",
            "access_token_ref": "secret://feishu/amazon-token",
        }
    }
    payload["media_source_refs"][0]["source_url"] = f"{MAIN_IMAGE_URL}?tracking=1#fragment"
    from automation_business_scaffold.domains.amazon.flows.amazon_product_row_persist import (
        orchestrator,
    )

    ticks = iter((1.0, 1.012, 2.0, 2.02, 3.0, 3.03))
    monkeypatch.setattr(orchestrator, "perf_counter", lambda: next(ticks))
    calls = _fake_dispatch(monkeypatch, _success_outcomes())
    result = amazon_product_row_persist_handler(_context(payload))

    assert result.status == "success"
    assert result.result["row_status"] == "success"
    assert [call.handler_code for call in calls] == [
        "media_asset_sync",
        "amazon_product_fact_upsert",
        "feishu_table_write",
    ]

    media_payload = calls[0].payload
    assert media_payload["source_platform"] == "amazon"
    assert media_payload["marketplace_code"] == "US"
    assert media_payload["require_object_storage"] is True
    assert media_payload["require_materialized_assets"] is True
    assert media_payload["sync_referenced_files"] is True
    assert media_payload["media_download_max_bytes"] == 25 * 1024 * 1024
    assert media_payload["media_download_allowed_host_suffixes"] == [
        "media-amazon.com",
        "ssl-images-amazon.com",
    ]
    assert [item["position"] for item in media_payload["asset_refs"]] == [0, 1]
    assert [item["source_url"] for item in media_payload["asset_refs"]] == [
        MAIN_IMAGE_URL,
        GALLERY_IMAGE_URL,
    ]

    fact_payload = calls[1].payload
    assert fact_payload["source_table_ref"] == {"base_id": "app-1", "table_id": "tbl-1"}
    assert fact_payload["source_record_id"] == "rec-1"
    assert fact_payload["requested_asin"] == ASIN
    assert fact_payload["materialized_media_assets"] == _materialized_assets()
    assert calls[1].metadata["include_transient_projection_facts"] is True

    write_payload = calls[2].payload
    assert write_payload["target_table_ref"] == "AMAZON_PRODUCTS"
    assert write_payload["mapper_code"] == "amazon_product_projection_mapper"
    assert write_payload["write_mode"] == "update"
    assert write_payload["write_policy"] == {
        "ignore_missing_fields": True,
        "field_allowlist": [
            "主图",
            "侧边栏图片",
            "30天购买人数",
            "送达日期",
            "包装规格",
            "促销活动记录",
        ],
    }
    assert write_payload["source_record_id"] == "rec-1"
    assert write_payload["records"][0]["source_record_id"] == "rec-1"
    assert write_payload["feishu_table"] == {
        "app_token": "app-1",
        "table_id": "tbl-1",
        "access_token_env": "AMAZON_FEISHU_TOKEN",
        "access_token_ref": "secret://feishu/amazon-token",
    }
    assert write_payload["request_payload"] == {
        "table_ref": "AMAZON_PRODUCTS",
        "source_record_id": "rec-1",
    }
    assert "table_refs" not in repr(write_payload)
    assert "must-not-be-forwarded" not in repr(write_payload)

    serialized = repr(result.result)
    for forbidden in (
        "projection_facts",
        "media_source_refs",
        "synced_assets",
        "raw_capture_refs",
        "field_evidence",
        "cookie",
        "html",
    ):
        assert forbidden not in serialized
    assert result.result["fact_refs"] == {
        "product_id": "product-1",
        "snapshot_id": "snapshot-1",
        "binding_id": "binding-1",
        "raw_capture_ids": ["raw-1", "raw-2"],
        "normalized_capture_ref": _payload()["normalized_capture_ref"],
    }
    assert result.result["observability"] == {
        "stage_durations_ms": {
            "navigation": 12.5,
            "parse": 3.25,
            "artifact": 8.75,
            "media": 12.0,
            "fact": 20.0,
            "feishu": 30.0,
        },
        "field_coverage": {
            "total": 20,
            "observed": 20,
            "missing": 0,
            "percentage": 100.0,
        },
        "artifact_count": 2,
        "media_observed_count": 2,
        "media_materialized_count": 2,
        "final_status": "success",
        "error_code": "",
        "browser_provider_name": "roxy",
    }
    serialized_result = repr(result.result)
    assert "profile-must-not-be-returned" not in serialized_result
    assert "workspace-must-not-be-returned" not in serialized_result
    assert "token-must-not-be-returned" not in serialized_result


@pytest.mark.parametrize(
    ("summary_written_count", "written_count", "target_record_ids"),
    (
        (0, 0, []),
        (1, 0, ["rec-1"]),
        (1, 1, ["rec-other"]),
        (1, 1, ["rec-1", {"record_id": "rec-other"}]),
    ),
)
def test_row_persist_rejects_writeback_that_does_not_update_the_source_record(
    monkeypatch: pytest.MonkeyPatch,
    summary_written_count: int,
    written_count: int,
    target_record_ids: list[Any],
) -> None:
    from automation_business_scaffold.domains.amazon.jobs.amazon_product_row_persist import (
        amazon_product_row_persist_handler,
    )

    outcomes = _success_outcomes()

    def invalid_writeback(context: HandlerContext) -> HandlerResult:
        return HandlerResult.success(
            context,
            summary={
                "written_count": summary_written_count,
                "skipped_count": 0,
                "failed_count": 0,
            },
            result={
                "written_count": written_count,
                "skipped_count": 0,
                "failed_count": 0,
                "target_record_ids": target_record_ids,
            },
        )

    outcomes["feishu_table_write"] = invalid_writeback
    _fake_dispatch(monkeypatch, outcomes)

    result = amazon_product_row_persist_handler(_context(_payload()))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "feishu_writeback_not_converged"
    assert result.error.retryable is False
    assert result.result["failed_step"] == "feishu_table_write"
    assert result.result["step_statuses"]["feishu_table_write"] == "failed"


def test_row_persist_rejects_foreign_nested_fact_reference_before_writeback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from automation_business_scaffold.domains.amazon.jobs.amazon_product_row_persist import (
        amazon_product_row_persist_handler,
    )

    outcomes = _success_outcomes()
    valid_facts = outcomes["amazon_product_fact_upsert"]

    def foreign_facts(context: HandlerContext) -> HandlerResult:
        valid = valid_facts(context)
        nested_ref = {
            **valid.result["normalized_capture_ref"],
            "request_id": "foreign-request",
            "access_token": "must-not-cross-runtime-boundary",
        }
        return HandlerResult.success(
            context,
            summary=valid.summary,
            result={**valid.result, "normalized_capture_ref": nested_ref},
        )

    outcomes["amazon_product_fact_upsert"] = foreign_facts
    calls = _fake_dispatch(monkeypatch, outcomes)

    result = amazon_product_row_persist_handler(_context(_payload()))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "amazon_fact_reference_mismatch"
    assert [call.handler_code for call in calls] == [
        "media_asset_sync",
        "amazon_product_fact_upsert",
    ]
    assert "foreign-request" not in repr(result.result)
    assert "must-not-cross-runtime-boundary" not in repr(result.result)


def test_retryable_media_failure_persists_fact_before_retry_and_skips_writeback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from automation_business_scaffold.domains.amazon.jobs.amazon_product_row_persist import (
        amazon_product_row_persist_handler,
    )

    outcomes = _success_outcomes()

    def media_failed(context: HandlerContext) -> HandlerResult:
        return HandlerResult.failed(
            context,
            error=HandlerError(
                error_type="media_sync_failed",
                error_code="media_asset_materialization_failed",
                message="one image failed",
                retryable=True,
            ),
            result={"synced_assets": _materialized_assets(include_gallery=False)},
        )

    outcomes["media_asset_sync"] = media_failed
    calls = _fake_dispatch(monkeypatch, outcomes)

    result = amazon_product_row_persist_handler(_context(attempt_count=1, max_attempts=3))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "media_sync_failed"
    assert result.error.retryable is True
    assert [call.handler_code for call in calls] == [
        "media_asset_sync",
        "amazon_product_fact_upsert",
    ]
    assert calls[1].payload["materialized_media_assets"] == _materialized_assets(
        include_gallery=False
    )
    assert result.result["fact_refs"]["snapshot_id"] == "snapshot-1"


def test_final_media_failure_writes_partial_projection_without_losing_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from automation_business_scaffold.domains.amazon.jobs.amazon_product_row_persist import (
        amazon_product_row_persist_handler,
    )

    outcomes = _success_outcomes()

    def media_failed(context: HandlerContext) -> HandlerResult:
        return HandlerResult.failed(
            context,
            error=HandlerError(
                error_type="media_sync_failed",
                error_code="media_asset_materialization_failed",
                message="gallery failed",
                retryable=True,
            ),
            result={"synced_assets": _materialized_assets(include_gallery=False)},
        )

    def partial_facts(context: HandlerContext) -> HandlerResult:
        result = _success_outcomes()["amazon_product_fact_upsert"](context)
        result.result["projection_facts"]["collection_status"] = "partial_success"
        result.result["media_coverage"] = {
            "expected": 2,
            "materialized": 1,
            "missing": 1,
            "complete": False,
        }
        return HandlerResult.partial_success(
            context,
            summary={"collection_status": "partial_success"},
            result=result.result,
        )

    outcomes["media_asset_sync"] = media_failed
    outcomes["amazon_product_fact_upsert"] = partial_facts
    calls = _fake_dispatch(monkeypatch, outcomes)

    result = amazon_product_row_persist_handler(_context())

    assert result.status == "partial_success"
    assert result.result["row_status"] == "partial_success"
    assert [call.handler_code for call in calls] == [
        "media_asset_sync",
        "amazon_product_fact_upsert",
        "feishu_table_write",
    ]
    projection_record = calls[2].payload["records"][0]
    assert projection_record["projection_facts"]["collection_status"] == "partial_success"
    assert projection_record["materialized_media_assets"] == _materialized_assets(
        include_gallery=False
    )
    assert result.result["media_coverage"]["missing"] == 1


def test_row_persist_recomputes_compact_media_coverage_from_materialized_assets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from automation_business_scaffold.domains.amazon.jobs.amazon_product_row_persist import (
        amazon_product_row_persist_handler,
    )

    outcomes = _success_outcomes()
    facts = outcomes["amazon_product_fact_upsert"]

    def poisoned_facts(context: HandlerContext) -> HandlerResult:
        result = facts(context)
        poisoned = {
            "expected": "wrong-type",
            "materialized": 999,
            "missing": -1,
            "complete": False,
            "cookie": "Bearer must-not-cross-runtime-boundary",
        }
        result.result["media_coverage"] = poisoned
        result.summary["media_coverage"] = poisoned
        return result

    outcomes["amazon_product_fact_upsert"] = poisoned_facts
    _fake_dispatch(monkeypatch, outcomes)

    result = amazon_product_row_persist_handler(_context())

    assert result.status == "success"
    assert result.result["media_coverage"] == {
        "expected": 2,
        "materialized": 2,
        "missing": 0,
        "complete": True,
    }
    assert "must-not-cross-runtime-boundary" not in repr(result.result)
    assert "must-not-cross-runtime-boundary" not in repr(result.summary)


def test_unexpected_media_exception_still_persists_facts_before_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from automation_business_scaffold.domains.amazon.jobs.amazon_product_row_persist import (
        amazon_product_row_persist_handler,
    )

    outcomes = _success_outcomes()

    def media_raises(context: HandlerContext) -> HandlerResult:
        del context
        raise OSError("socket failed with sensitive upstream details")

    outcomes["media_asset_sync"] = media_raises
    calls = _fake_dispatch(monkeypatch, outcomes)

    result = amazon_product_row_persist_handler(_context(attempt_count=1, max_attempts=3))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "media_sync_failed"
    assert result.error.retryable is True
    assert "sensitive upstream details" not in repr(result)
    assert [call.handler_code for call in calls] == [
        "media_asset_sync",
        "amazon_product_fact_upsert",
    ]


def test_fact_failure_stops_before_feishu_and_propagates_retryability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from automation_business_scaffold.domains.amazon.jobs.amazon_product_row_persist import (
        amazon_product_row_persist_handler,
    )

    outcomes = _success_outcomes()

    def facts_failed(context: HandlerContext) -> HandlerResult:
        return HandlerResult.failed(
            context,
            error=HandlerError(
                error_type="persistence_failure",
                error_code="amazon_product_fact_upsert_failed",
                message="database unavailable",
                retryable=True,
            ),
        )

    outcomes["amazon_product_fact_upsert"] = facts_failed
    calls = _fake_dispatch(monkeypatch, outcomes)

    result = amazon_product_row_persist_handler(_context())

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "amazon_product_fact_upsert_failed"
    assert result.error.retryable is True
    assert result.result["observability"]["final_status"] == "failed"
    assert result.result["observability"]["error_code"] == "amazon_product_fact_upsert_failed"
    assert "fact" in result.result["observability"]["stage_durations_ms"]
    assert "feishu" not in result.result["observability"]["stage_durations_ms"]
    assert [call.handler_code for call in calls] == [
        "media_asset_sync",
        "amazon_product_fact_upsert",
    ]


def test_feishu_failure_keeps_compact_fact_refs_for_idempotent_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from automation_business_scaffold.domains.amazon.jobs.amazon_product_row_persist import (
        amazon_product_row_persist_handler,
    )

    outcomes = _success_outcomes()

    def write_failed(context: HandlerContext) -> HandlerResult:
        return HandlerResult.failed(
            context,
            error=HandlerError(
                error_type="upstream_error",
                error_code="feishu_write_failed",
                message="Feishu unavailable",
                retryable=True,
            ),
        )

    outcomes["feishu_table_write"] = write_failed
    calls = _fake_dispatch(monkeypatch, outcomes)

    result = amazon_product_row_persist_handler(_context())

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "feishu_write_failed"
    assert result.error.retryable is True
    assert result.result["fact_refs"]["product_id"] == "product-1"
    assert result.result["fact_refs"]["snapshot_id"] == "snapshot-1"
    assert [call.handler_code for call in calls] == [
        "media_asset_sync",
        "amazon_product_fact_upsert",
        "feishu_table_write",
    ]


def test_unavailable_row_skips_media_and_finishes_as_runtime_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from automation_business_scaffold.domains.amazon.jobs.amazon_product_row_persist import (
        amazon_product_row_persist_handler,
    )

    payload = _payload(collection_status="unavailable")
    payload["media_source_refs"] = []
    outcomes = _success_outcomes()

    def facts(context: HandlerContext) -> HandlerResult:
        result = _success_outcomes()["amazon_product_fact_upsert"](context)
        result.result["projection_facts"]["collection_status"] = "unavailable"
        return result

    outcomes["amazon_product_fact_upsert"] = facts
    calls = _fake_dispatch(monkeypatch, outcomes)

    result = amazon_product_row_persist_handler(_context(payload))

    assert result.status == "success"
    assert result.result["row_status"] == "unavailable"
    assert [call.handler_code for call in calls] == [
        "amazon_product_fact_upsert",
        "feishu_table_write",
    ]


def test_unavailable_row_materializes_observed_media_before_terminal_writeback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from automation_business_scaffold.domains.amazon.jobs.amazon_product_row_persist import (
        amazon_product_row_persist_handler,
    )

    payload = _payload(collection_status="unavailable")
    outcomes = _success_outcomes()

    def facts(context: HandlerContext) -> HandlerResult:
        result = _success_outcomes()["amazon_product_fact_upsert"](context)
        result.result["projection_facts"]["collection_status"] = "unavailable"
        return result

    outcomes["amazon_product_fact_upsert"] = facts
    calls = _fake_dispatch(monkeypatch, outcomes)

    result = amazon_product_row_persist_handler(_context(payload))

    assert result.status == "success"
    assert result.result["row_status"] == "unavailable"
    assert [call.handler_code for call in calls] == [
        "media_asset_sync",
        "amazon_product_fact_upsert",
        "feishu_table_write",
    ]


def test_invalid_source_binding_fails_before_any_child_side_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from automation_business_scaffold.domains.amazon.jobs.amazon_product_row_persist import (
        amazon_product_row_persist_handler,
    )

    payload = _payload()
    payload["source_table_identity"] = {"base_id": "app-1"}
    calls = _fake_dispatch(monkeypatch, _success_outcomes())

    result = amazon_product_row_persist_handler(_context(payload))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "invalid_amazon_persist_payload"
    assert result.error.retryable is False
    assert calls == []


@pytest.mark.parametrize(
    "source_url",
    [
        "http://169.254.169.254/latest/meta-data",
        "https://images.example/main.jpg",
        "https://media-amazon.com.evil.example/main.jpg",
        "https://user@images-na.ssl-images-amazon.com/main.jpg",
        "https://m.media-amazon.com/images/%3Cscript%3Ebad.jpg",
        "https://m.media-amazon.com/images/Bearer-runtime-token.jpg",
        "https://m.media-amazon.com/images/Cookie=session-secret.jpg",
    ],
)
def test_ungoverned_media_url_fails_before_media_download(
    monkeypatch: pytest.MonkeyPatch,
    source_url: str,
) -> None:
    from automation_business_scaffold.domains.amazon.jobs.amazon_product_row_persist import (
        amazon_product_row_persist_handler,
    )

    payload = _payload()
    payload["media_source_refs"][0]["source_url"] = source_url
    calls = _fake_dispatch(monkeypatch, _success_outcomes())

    result = amazon_product_row_persist_handler(_context(payload))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "invalid_amazon_persist_payload"
    assert result.error.retryable is False
    assert calls == []


def test_duplicate_media_role_position_fails_before_media_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from automation_business_scaffold.domains.amazon.jobs.amazon_product_row_persist import (
        amazon_product_row_persist_handler,
    )

    payload = _payload()
    payload["media_source_refs"].append(
        {
            "source_url": "https://m.media-amazon.com/images/I/other-main.jpg",
            "source_platform": "amazon",
            "marketplace_code": "US",
            "product_id": ASIN,
            "media_role": "main_image",
            "position": 0,
        }
    )
    calls = _fake_dispatch(monkeypatch, _success_outcomes())

    result = amazon_product_row_persist_handler(_context(payload))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "invalid_amazon_persist_payload"
    assert result.error.retryable is False
    assert calls == []


def test_media_ref_local_path_fails_before_local_file_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from automation_business_scaffold.domains.amazon.jobs.amazon_product_row_persist import (
        amazon_product_row_persist_handler,
    )

    payload = _payload()
    payload["media_source_refs"][0]["local_path"] = "/etc/passwd"
    calls = _fake_dispatch(monkeypatch, _success_outcomes())

    result = amazon_product_row_persist_handler(_context(payload))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "invalid_amazon_persist_payload"
    assert result.error.retryable is False
    assert calls == []


def test_boolean_media_position_fails_before_media_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from automation_business_scaffold.domains.amazon.jobs.amazon_product_row_persist import (
        amazon_product_row_persist_handler,
    )

    payload = _payload()
    payload["media_source_refs"][0]["position"] = True
    calls = _fake_dispatch(monkeypatch, _success_outcomes())

    result = amazon_product_row_persist_handler(_context(payload))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "invalid_amazon_persist_payload"
    assert result.error.retryable is False
    assert calls == []


def test_api_worker_projects_row_persist_result_before_first_runtime_write() -> None:
    secret = "Bearer must-not-cross-runtime-boundary"
    context = _context()
    normalized_ref = context.payload["normalized_capture_ref"]
    worker_result = HandlerResult.success(
        context,
        summary={"row_status": "success", "cookie": secret},
        result={
            "row_status": "success",
            "source_record_id": "rec-1",
            "requested_asin": ASIN,
            "resolved_asin": ASIN,
            "run_id": STABLE_RUN_ID,
            "step_statuses": {
                "media_asset_sync": "success",
                "amazon_product_fact_upsert": "success",
                "feishu_table_write": "success",
                "cookie": secret,
            },
            "fact_refs": {
                "product_id": "1" * 32,
                "snapshot_id": "2" * 32,
                "binding_id": "3" * 32,
                "raw_capture_ids": ["4" * 32],
                "normalized_capture_ref": {**normalized_ref, "cookie": secret},
                "cookie": secret,
            },
            "media_coverage": {
                "expected": "wrong-type",
                "materialized": 999,
                "missing": -1,
                "complete": False,
                "cookie": secret,
            },
            "writeback": {
                "written_count": 1,
                "skipped_count": 0,
                "failed_count": 0,
                "target_record_ids": ["rec-1"],
                "cookie": secret,
            },
            "observability": {
                "stage_durations_ms": {"fact": 1.25, "cookie": secret},
                "field_coverage": {"total": 999, "observed": 999, "cookie": secret},
                "artifact_count": 999,
                "media_observed_count": 999,
                "media_materialized_count": 999,
                "final_status": "success",
                "error_code": "",
                "browser_provider_name": "roxy",
                "cookie": secret,
            },
            "cookie": secret,
        },
        warnings=(secret,),
    )
    outcome = ExecutionSupervisorOutcome(
        context=context,
        worker_result=worker_result,
        supervisor_status="handler_completed",
        started_at=1.0,
        finished_at=2.0,
        heartbeat_count=0,
        progress_events=(
            ExecutionProgressEvent(
                progress_stage="fact",
                message=secret,
                details={"cookie": secret},
            ),
        ),
    )

    class Store:
        def __init__(self) -> None:
            self.marked: dict[str, Any] = {}

        def mark_api_worker_job_success(self, **kwargs: Any) -> dict[str, Any]:
            self.marked = dict(kwargs)
            return {"result_status": "success"}

    store = Store()

    marked, success_count, failed_count = worker_dispatch.persist_api_worker_outcome(
        store=store,
        job_id=context.job_id,
        run_id="claim-run-1",
        outcome=outcome,
        retry_delay_seconds=5,
    )

    assert marked["result_status"] == "success"
    assert (success_count, failed_count) == (1, 0)
    assert secret not in repr(store.marked["result"])
    assert secret not in repr(store.marked["summary"])
    assert store.marked["result"]["handler_result"]["status"] == "success"
    assert store.marked["result"]["handler_result"]["contract_revision"] == "runtime_contract"
    assert "warnings" not in store.marked["result"]["handler_result"]
    assert "progress_events" not in store.marked["result"]["supervisor"]
    assert store.marked["result"]["media_coverage"] == {
        "expected": 2,
        "materialized": 2,
        "missing": 0,
        "complete": True,
    }
    assert set(store.marked["result"]["fact_refs"]) == {
        "product_id",
        "snapshot_id",
        "binding_id",
        "raw_capture_ids",
        "normalized_capture_ref",
    }
    assert store.marked["result"]["observability"] == {
        "stage_durations_ms": {
            "navigation": 12.5,
            "parse": 3.25,
            "artifact": 8.75,
            "fact": 1.25,
        },
        "field_coverage": {
            "total": 20,
            "observed": 20,
            "explicitly_unavailable": 0,
            "missing": 0,
            "percentage": 100.0,
        },
        "artifact_count": 2,
        "media_observed_count": 2,
        "media_materialized_count": 2,
        "final_status": "success",
        "browser_provider_name": "roxy",
    }


def _runtime_success_result(context: HandlerContext) -> dict[str, Any]:
    return {
        "row_status": "success",
        "source_record_id": "rec-1",
        "requested_asin": ASIN,
        "resolved_asin": ASIN,
        "run_id": STABLE_RUN_ID,
        "step_statuses": {
            "media_asset_sync": "success",
            "amazon_product_fact_upsert": "success",
            "feishu_table_write": "success",
        },
        "fact_refs": {
            "product_id": "1" * 32,
            "snapshot_id": "2" * 32,
            "binding_id": "3" * 32,
            "raw_capture_ids": ["4" * 32],
            "normalized_capture_ref": context.payload["normalized_capture_ref"],
        },
        "media_coverage": {
            "expected": 2,
            "materialized": 2,
            "missing": 0,
            "complete": True,
        },
        "writeback": {
            "written_count": 1,
            "skipped_count": 0,
            "failed_count": 0,
            "target_record_ids": ["rec-1"],
        },
        "observability": {
            "stage_durations_ms": {"fact": 1.25},
            "field_coverage": {"total": 2, "observed": 2},
            "artifact_count": 2,
            "media_observed_count": 2,
            "media_materialized_count": 2,
            "final_status": "success",
            "browser_provider_name": "roxy",
        },
    }


class _RuntimeOutcomeStore:
    def __init__(self) -> None:
        self.success: dict[str, Any] | None = None
        self.failed: dict[str, Any] | None = None
        self.waiting: dict[str, Any] | None = None

    def mark_api_worker_job_success(self, **kwargs: Any) -> dict[str, Any]:
        self.success = dict(kwargs)
        return {"result_status": "success"}

    def mark_api_worker_job_retry_or_failed(self, **kwargs: Any) -> dict[str, Any]:
        self.failed = dict(kwargs)
        return {"result_status": "failed"}

    def mark_api_worker_job_waiting(self, **kwargs: Any) -> dict[str, Any]:
        self.waiting = dict(kwargs)
        return {"result_status": "waiting"}


def _runtime_outcome(
    context: HandlerContext,
    worker_result: HandlerResult,
    *,
    error: ExecutionSupervisorError | None = None,
) -> ExecutionSupervisorOutcome:
    return ExecutionSupervisorOutcome(
        context=context,
        worker_result=worker_result,
        supervisor_status="handler_failed" if error else "handler_completed",
        started_at=1.0,
        finished_at=2.0,
        heartbeat_count=0,
        error=error,
    )


def test_api_worker_rejects_incomplete_success_before_first_runtime_write() -> None:
    context = _context()
    store = _RuntimeOutcomeStore()

    marked, success_count, failed_count = worker_dispatch.persist_api_worker_outcome(
        store=store,
        job_id=context.job_id,
        run_id="claim-run-1",
        outcome=_runtime_outcome(
            context,
            HandlerResult.success(context, result={"row_status": "success"}),
        ),
        retry_delay_seconds=5,
    )

    assert marked["result_status"] == "failed"
    assert (success_count, failed_count) == (0, 1)
    assert store.success is None
    assert store.failed is not None
    assert store.failed["force_terminal"] is True
    assert store.failed["dead_letter_reason"] == "invalid_handler_result"
    assert store.failed["error_code"] == "invalid_handler_result"


@pytest.mark.parametrize("status", ["fallback_required", "skipped"])
def test_api_worker_rejects_nonterminal_amazon_row_status(status: str) -> None:
    context = _context()
    if status == "fallback_required":
        worker_result = HandlerResult.fallback_required(
            context,
            error=HandlerError(
                error_type="upstream_error",
                error_code="feishu_write_failed",
                message="fallback is not part of the Amazon row workflow",
                retryable=True,
            ),
        )
    else:
        worker_result = HandlerResult.skipped(context)
    store = _RuntimeOutcomeStore()

    marked, success_count, failed_count = worker_dispatch.persist_api_worker_outcome(
        store=store,
        job_id=context.job_id,
        run_id="claim-run-1",
        outcome=_runtime_outcome(context, worker_result),
        retry_delay_seconds=5,
    )

    assert marked["result_status"] == "failed"
    assert (success_count, failed_count) == (0, 1)
    assert store.success is None
    assert store.waiting is None
    assert store.failed is not None
    assert store.failed["force_terminal"] is True
    assert store.failed["error_code"] == "invalid_handler_result"


def test_api_worker_rejects_foreign_writeback_target_before_runtime_write() -> None:
    context = _context()
    result = _runtime_success_result(context)
    result["writeback"] = {
        "written_count": 1,
        "skipped_count": 0,
        "failed_count": 0,
        "target_record_ids": ["foreign-secret-record"],
    }
    store = _RuntimeOutcomeStore()

    worker_dispatch.persist_api_worker_outcome(
        store=store,
        job_id=context.job_id,
        run_id="claim-run-1",
        outcome=_runtime_outcome(context, HandlerResult.success(context, result=result)),
        retry_delay_seconds=5,
    )

    assert store.success is None
    assert store.failed is not None
    assert "foreign-secret-record" not in repr(store.failed)
    assert store.failed["error_code"] == "invalid_handler_result"


def test_api_worker_rejects_handler_and_row_status_mismatch() -> None:
    context = _context()
    result = _runtime_success_result(context)
    result["row_status"] = "partial_success"
    result["observability"]["final_status"] = "partial_success"
    store = _RuntimeOutcomeStore()

    worker_dispatch.persist_api_worker_outcome(
        store=store,
        job_id=context.job_id,
        run_id="claim-run-1",
        outcome=_runtime_outcome(context, HandlerResult.success(context, result=result)),
        retry_delay_seconds=5,
    )

    assert store.success is None
    assert store.failed is not None
    assert store.failed["error_code"] == "invalid_handler_result"


def test_api_worker_rejects_failed_feishu_step_with_success_row() -> None:
    context = _context()
    result = _runtime_success_result(context)
    result["step_statuses"]["feishu_table_write"] = "failed"
    store = _RuntimeOutcomeStore()

    worker_dispatch.persist_api_worker_outcome(
        store=store,
        job_id=context.job_id,
        run_id="claim-run-1",
        outcome=_runtime_outcome(context, HandlerResult.success(context, result=result)),
        retry_delay_seconds=5,
    )

    assert store.success is None
    assert store.failed is not None
    assert store.failed["error_code"] == "invalid_handler_result"


@pytest.mark.parametrize(
    "step_code",
    ["amazon_product_fact_upsert", "feishu_table_write"],
)
def test_api_worker_rejects_skipped_required_step_with_success_row(step_code: str) -> None:
    context = _context()
    result = _runtime_success_result(context)
    result["step_statuses"][step_code] = "skipped"
    store = _RuntimeOutcomeStore()

    worker_dispatch.persist_api_worker_outcome(
        store=store,
        job_id=context.job_id,
        run_id="claim-run-1",
        outcome=_runtime_outcome(context, HandlerResult.success(context, result=result)),
        retry_delay_seconds=5,
    )

    assert store.success is None
    assert store.failed is not None
    assert store.failed["error_code"] == "invalid_handler_result"


def test_api_worker_rejects_skipped_media_step_when_media_was_observed() -> None:
    context = _context()
    result = _runtime_success_result(context)
    result["step_statuses"]["media_asset_sync"] = "skipped"
    store = _RuntimeOutcomeStore()

    worker_dispatch.persist_api_worker_outcome(
        store=store,
        job_id=context.job_id,
        run_id="claim-run-1",
        outcome=_runtime_outcome(context, HandlerResult.success(context, result=result)),
        retry_delay_seconds=5,
    )

    assert store.success is None
    assert store.failed is not None
    assert store.failed["error_code"] == "invalid_handler_result"


def test_api_worker_rejects_incomplete_media_coverage_with_success_row() -> None:
    context = _context()
    result = _runtime_success_result(context)
    result["media_coverage"]["materialized"] = 0
    result["media_coverage"]["missing"] = 2
    result["media_coverage"]["complete"] = False
    store = _RuntimeOutcomeStore()

    worker_dispatch.persist_api_worker_outcome(
        store=store,
        job_id=context.job_id,
        run_id="claim-run-1",
        outcome=_runtime_outcome(context, HandlerResult.success(context, result=result)),
        retry_delay_seconds=5,
    )

    assert store.success is None
    assert store.failed is not None
    assert store.failed["error_code"] == "invalid_handler_result"


def test_api_worker_rejects_ungoverned_normalized_capture_reference() -> None:
    payload = _payload()
    payload["normalized_capture_ref"]["request_id"] = "request-1"
    context = _context(payload)
    result = _runtime_success_result(context)
    store = _RuntimeOutcomeStore()

    worker_dispatch.persist_api_worker_outcome(
        store=store,
        job_id=context.job_id,
        run_id="claim-run-1",
        outcome=_runtime_outcome(context, HandlerResult.success(context, result=result)),
        retry_delay_seconds=5,
    )

    assert store.success is None
    assert store.failed is not None
    assert "request-1" not in repr(store.failed["result"])
    assert store.failed["error_code"] == "invalid_handler_result"


@pytest.mark.parametrize(
    "secret_code",
    [
        "xBearer-runtime-secret",
        "sk_live_51ABCxyz",
        "AKIAIOSFODNN7EXAMPLE",
        "ghp_abcdefghijklmnopqrstuvwxyz123456",
    ],
)
def test_api_worker_sanitizes_amazon_error_columns_and_nested_envelope(
    secret_code: str,
) -> None:
    context = _context()
    handler_error = HandlerError(
        error_type=secret_code,
        error_code=secret_code,
        message="Bearer must-not-cross-runtime-boundary",
        retryable=False,
    )
    supervisor_error = ExecutionSupervisorError(
        error_type=secret_code,
        error_code=secret_code,
        message="Bearer must-not-cross-runtime-boundary",
        retryable=False,
        terminal=True,
    )
    store = _RuntimeOutcomeStore()

    worker_dispatch.persist_api_worker_outcome(
        store=store,
        job_id=context.job_id,
        run_id="claim-run-1",
        outcome=_runtime_outcome(
            context,
            HandlerResult.failed(context, error=handler_error, result={"row_status": "failed"}),
            error=supervisor_error,
        ),
        retry_delay_seconds=5,
    )

    assert store.failed is not None
    assert store.failed["error_type"] == "amazon_row_persistence_failure"
    assert store.failed["error_code"] == "amazon_row_persistence_failed"
    assert secret_code not in repr(store.failed)
    assert "must-not-cross-runtime-boundary" not in repr(store.failed)


@pytest.mark.parametrize(
    ("retryable", "expected_force_terminal", "expected_dead_letter_reason"),
    [
        (False, True, "terminal_handler_failure"),
        (True, False, ""),
    ],
)
def test_api_worker_failure_policy_respects_handler_retryability(
    retryable: bool,
    expected_force_terminal: bool,
    expected_dead_letter_reason: str,
) -> None:
    context = _context(attempt_count=1, max_attempts=3)
    handler_error = HandlerError(
        error_type="invalid_input" if not retryable else "persistence_failure",
        error_code=(
            "invalid_amazon_persist_payload"
            if not retryable
            else "amazon_product_fact_upsert_failed"
        ),
        message="row persistence failed",
        retryable=retryable,
    )
    store = _RuntimeOutcomeStore()

    worker_dispatch.persist_api_worker_outcome(
        store=store,
        job_id=context.job_id,
        run_id="claim-run-1",
        outcome=_runtime_outcome(
            context,
            HandlerResult.failed(
                context,
                error=handler_error,
                result={"row_status": "failed"},
            ),
        ),
        retry_delay_seconds=5,
    )

    assert store.failed is not None
    assert store.failed["force_terminal"] is expected_force_terminal
    assert store.failed["dead_letter_reason"] == expected_dead_letter_reason


@pytest.mark.parametrize(
    "code",
    [
        "xBearer-runtime-secret",
        "sk_live_51ABCxyz",
        "AKIAIOSFODNN7EXAMPLE",
        "ghp_abcdefghijklmnopqrstuvwxyz123456",
    ],
)
def test_amazon_runtime_fields_reject_unknown_child_controlled_codes(code: str) -> None:
    assert amazon_runtime_projection._amazon_provider_code(code) == ""
    assert amazon_runtime_projection._amazon_contract_revision(code) == "runtime_contract"
    assert (
        amazon_runtime_projection._runtime_progress_stage("amazon_product_browser_fetch", code)
        == "handler_progress"
    )
    assert (
        amazon_runtime_projection._runtime_progress_stage("amazon_product_row_persist", code)
        == "handler_progress"
    )


@pytest.mark.parametrize("provider_code", ["chrome", "chrome_cdp", "roxy"])
def test_amazon_runtime_provider_allowlist_preserves_known_codes(
    provider_code: str,
) -> None:
    assert amazon_runtime_projection._amazon_provider_code(provider_code) == provider_code


@pytest.mark.parametrize(
    ("handler_code", "expected_message"),
    [
        ("amazon_product_browser_fetch", "Amazon browser collection progress updated."),
        ("amazon_product_row_persist", "Amazon row persistence progress updated."),
    ],
)
def test_amazon_progress_values_are_sanitized_before_runtime_update(
    handler_code: str,
    expected_message: str,
) -> None:
    secret = "xBearer-runtime-secret"

    assert (
        amazon_runtime_projection._runtime_progress_stage(handler_code, secret)
        == "handler_progress"
    )
    assert (
        amazon_runtime_projection._runtime_progress_message(handler_code, secret)
        == expected_message
    )
    assert secret not in expected_message


def test_api_worker_once_does_not_return_or_persist_raw_amazon_child_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    secret = "Bearer must-not-cross-runtime-or-log-boundary"
    progress_updates: list[dict[str, Any]] = []

    class Store:
        def claim_next_api_worker_job(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "request_id": context.request_id,
                "job_id": context.job_id,
                "job_code": context.handler_code,
                "run_id": "claim-run-1",
                "payload": context.payload,
                "business_key": context.business_key,
                "dedupe_key": context.dedupe_key,
                "attempt_count": 1,
                "max_attempts": 1,
                "max_execution_seconds": 30,
            }

        def update_api_worker_job_progress(self, **kwargs: Any) -> dict[str, Any]:
            progress_updates.append(dict(kwargs))
            return {}

        def heartbeat_api_worker_job(self, **_kwargs: Any) -> bool:
            return True

        def mark_api_worker_job_success(self, **kwargs: Any) -> dict[str, Any]:
            return {
                "result_status": "success",
                "summary": kwargs["summary"],
                "result": kwargs["result"],
                "error_type": "",
                "error_code": "",
            }

    def supervised(**kwargs: Any) -> ExecutionSupervisorOutcome:
        runtime_context = kwargs["context"]
        kwargs["callbacks"].on_progress(
            ExecutionProgressEvent(
                progress_stage="xBearer-runtime-secret",
                message=secret,
                details={"cookie": secret},
            )
        )
        return ExecutionSupervisorOutcome(
            context=runtime_context,
            worker_result=HandlerResult.success(
                runtime_context,
                summary={"cookie": secret},
                result={**_runtime_success_result(runtime_context), "cookie": secret},
                warnings=(secret,),
            ),
            supervisor_status="handler_completed",
            started_at=1.0,
            finished_at=2.0,
            heartbeat_count=0,
            progress_events=(
                ExecutionProgressEvent(
                    progress_stage="fact",
                    message=secret,
                    details={"cookie": secret},
                ),
            ),
        )

    monkeypatch.setattr(
        worker_dispatch,
        "build_runtime_settings",
        lambda _params: SimpleNamespace(
            worker_id="api-worker-1",
            lease_seconds=30,
            heartbeat_interval_seconds=1,
            retry_delay_seconds=5,
        ),
    )
    monkeypatch.setattr(worker_dispatch, "create_runtime_store", lambda _settings: Store())
    monkeypatch.setattr(worker_dispatch, "run_supervised_handler", supervised)
    monkeypatch.setattr(
        worker_dispatch,
        "build_runtime_request_payload",
        lambda **_kwargs: {},
    )

    payload = worker_dispatch.execute_api_worker_once({})

    assert secret not in repr(progress_updates)
    assert secret not in repr(payload)
    assert progress_updates[-1]["progress_stage"] == "handler_progress"
    assert progress_updates[-1]["message"] == "Amazon row persistence progress updated."
    assert payload["worker_result"]["result"]["source_record_id"] == "rec-1"
    assert "progress_events" not in payload["supervisor"]
