from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from automation_business_scaffold.contracts.handler.contract import (
    HandlerContext,
    HandlerError,
    HandlerResult,
)


ASIN = "B0ABC12345"


def _context(
    payload: dict[str, Any] | None = None,
    *,
    attempt_count: int = 1,
    max_attempts: int = 1,
) -> HandlerContext:
    return HandlerContext(
        request_id="req-amazon-1",
        job_id="job-amazon-persist-1",
        handler_code="amazon_product_row_persist",
        worker_type="api_worker",
        runtime_table="api_worker_job",
        payload=payload or _payload(),
        workflow_code="refresh_amazon_product_row_by_asin",
        stage_code="persist_amazon_product_detail",
        job_code="amazon_product_row_persist",
        business_key=f"amazon:US:{ASIN}",
        dedupe_key=f"req-amazon-1:amazon_persist:rec-1:{ASIN}",
        attempt_count=attempt_count,
        max_attempts=max_attempts,
        metadata={"run_id": "run-amazon-1"},
    )


def _payload(*, collection_status: str = "success") -> dict[str, Any]:
    normalized_ref = {
        "capture_kind": "normalized_capture",
        "bucket": "test-artifacts",
        "object_key": f"test/raw-captures/amazon/us/{ASIN}/2026/07/15/run-amazon-1/capture.json",
        "content_digest": "a" * 64,
        "content_type": "application/json",
    }
    html_ref = {
        "capture_kind": "html",
        "bucket": "test-artifacts",
        "object_key": f"test/raw-captures/amazon/us/{ASIN}/2026/07/15/run-amazon-1/page.html.gz",
        "content_digest": "b" * 64,
        "content_type": "application/gzip",
        "sanitization_status": "sanitized",
    }
    return {
        "table_ref": "AMAZON_PRODUCTS",
        "source_record_id": "rec-1",
        "source_table_identity": {"base_id": "app-1", "table_id": "tbl-1"},
        "requested_asin": ASIN,
        "resolved_asin": ASIN,
        "canonical_url": f"https://www.amazon.com/dp/{ASIN}",
        "run_id": "run-amazon-1",
        "collection_status": collection_status,
        "field_coverage": {"total": 20, "observed": 20, "missing": 0, "percentage": 100.0},
        "normalized_capture_ref": normalized_ref,
        "raw_capture_refs": [normalized_ref, html_ref],
        "media_source_refs": [
            {
                "source_url": "https://images.example/main.jpg",
                "source_platform": "amazon",
                "marketplace_code": "US",
                "product_id": ASIN,
                "media_role": "main_image",
                "position": 0,
            },
            {
                "source_url": "https://images.example/gallery.jpg",
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
            "source_url": "https://images.example/main.jpg",
            "source_platform": "amazon",
            "marketplace_code": "US",
            "product_id": ASIN,
            "media_role": "main_image",
            "position": 0,
            "sync_state": "uploaded",
            "bucket": "test-artifacts",
            "object_key": f"test/product-media/amazon/us/{ASIN}/main_image/{'1' * 64}.jpg",
            "remote_uri": (
                f"s3://test-artifacts/test/product-media/amazon/us/{ASIN}/"
                f"main_image/{'1' * 64}.jpg"
            ),
            "content_digest": "1" * 64,
            "size_bytes": 10,
        }
    ]
    if include_gallery:
        assets.append(
            {
                "source_url": "https://images.example/gallery.jpg",
                "source_platform": "amazon",
                "marketplace_code": "US",
                "product_id": ASIN,
                "media_role": "gallery_image",
                "position": 1,
                "sync_state": "uploaded",
                "bucket": "test-artifacts",
                "object_key": (
                    f"test/product-media/amazon/us/{ASIN}/gallery_image/{'2' * 64}.jpg"
                ),
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


def test_row_persist_serially_dispatches_media_fact_and_same_record_writeback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from automation_business_scaffold.domains.amazon.jobs.amazon_product_row_persist import (
        amazon_product_row_persist_handler,
    )

    payload = _payload()
    payload["request_payload"]["table_refs"] = {
        "AMAZON_PRODUCTS": {
            "app_token": "must-not-override-resolved-identity",
            "table_id": "must-not-override-resolved-identity",
            "access_token": "must-not-be-forwarded",
            "access_token_env": "AMAZON_FEISHU_TOKEN",
            "access_token_ref": "secret://feishu/amazon-token",
        }
    }
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
    assert [item["position"] for item in media_payload["asset_refs"]] == [0, 1]

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

    result = amazon_product_row_persist_handler(
        _context(attempt_count=1, max_attempts=3)
    )

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

    result = amazon_product_row_persist_handler(
        _context(attempt_count=1, max_attempts=3)
    )

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
