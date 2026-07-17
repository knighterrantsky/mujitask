from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from automation_business_scaffold.control_plane.executor.workflow_registry import (
    get_workflow_definition as get_registered_workflow_definition,
    load_workflow_runtime,
)
from automation_business_scaffold.control_plane.runtime_config.settings import (
    AMAZON_PRODUCT_ROW_TASK_CODE,
    FORMAL_TASK_CODES,
)
from automation_business_scaffold.domains.amazon.tasks.refresh_amazon_product_row_by_asin import (
    RefreshAmazonProductRowByAsinTask,
)
from automation_business_scaffold.domains.amazon.flows.refresh_amazon_product_row_by_asin import (
    orchestrator as amazon_orchestrator,
)
from automation_business_scaffold.domains.amazon.workflows import (
    get_workflow_definition,
)
from automation_business_scaffold.domains.amazon.workflows.refresh_amazon_product_row_by_asin import (
    REFRESH_AMAZON_PRODUCT_ROW_BY_ASIN_DEFINITION,
)


TASK_CODE = "refresh_amazon_product_row_by_asin"
EXPECTED_STAGES = (
    "read_amazon_product_row",
    "collect_amazon_product_detail",
    "persist_amazon_product_detail",
    "ready_for_summary",
)
ROW_STATUS_CODES = (
    "success",
    "partial_success",
    "unavailable",
    "blocked",
    "failed",
    "skipped",
)
TOP_LEVEL_SUMMARY_FIELDS = {
    "final_status",
    "row_total_count",
    "row_status_counts",
    "aggregate_metrics",
    "row_summary",
    "failed_stage",
    "error_code",
}
AGGREGATE_METRIC_FIELDS = {
    "average_row_duration_ms",
    "max_row_duration_ms",
    "blocked_rate",
    "average_parse_coverage_percentage",
    "media_failure_rate",
    "feishu_failure_rate",
}


def _assert_fixed_summary_shape(
    summary: dict[str, Any],
    *,
    final_status: str,
    row_status: str,
) -> None:
    assert set(summary) == TOP_LEVEL_SUMMARY_FIELDS
    assert summary["final_status"] == final_status
    assert summary["row_total_count"] == 1
    assert summary["row_status_counts"] == {
        status: int(status == row_status) for status in ROW_STATUS_CODES
    }
    assert set(summary["aggregate_metrics"]) == AGGREGATE_METRIC_FIELDS


def test_amazon_single_product_task_uses_the_formal_runtime_shell() -> None:
    task = RefreshAmazonProductRowByAsinTask()

    workflow = task.build_workflow({})

    assert task.name == TASK_CODE
    assert workflow.workflow_id == TASK_CODE
    assert workflow.run_mode == "full_auto"
    assert [step.step_id for step in workflow.steps] == ["dispatch_task_request"]
    assert workflow.steps[0].action.type == "dispatch_task_request"


def test_amazon_single_product_workflow_has_exact_four_stage_contract() -> None:
    definition = REFRESH_AMAZON_PRODUCT_ROW_BY_ASIN_DEFINITION

    assert definition.task_code == TASK_CODE
    assert definition.workflow_code == TASK_CODE
    assert definition.entry_stage_code == EXPECTED_STAGES[0]
    assert definition.stage_codes == EXPECTED_STAGES
    assert definition.payload_contract.field_names(required_only=True) == (
        "table_ref",
        "source_record_id",
        "table_refs",
    )
    assert definition.payload_contract.field_names() == (
        "table_ref",
        "source_record_id",
        "table_refs",
    )
    assert [transition.from_stage_code for transition in definition.transitions] == list(
        EXPECTED_STAGES[:-1]
    )
    assert [transition.to_stage_code for transition in definition.transitions] == list(
        EXPECTED_STAGES[1:]
    )

    read_stage, browser_stage, persist_stage, summary_stage = definition.stages
    assert read_stage.execution_mode == "worker_jobs"
    assert read_stage.job_codes == ("feishu_table_read", "feishu_table_write")
    assert read_stage.job_bindings[0].adapter_code == "amazon_product_table_source_adapter"
    assert read_stage.job_bindings[1].optional is True
    assert browser_stage.execution_mode == "worker_jobs"
    assert browser_stage.job_codes == ("feishu_table_write", "amazon_product_browser_fetch")
    assert browser_stage.job_bindings[0].optional is True
    assert "fallback" not in browser_stage.stage_code
    assert persist_stage.execution_mode == "worker_jobs"
    assert persist_stage.job_codes == ("feishu_table_write", "amazon_product_row_persist")
    assert persist_stage.job_bindings[0].optional is True
    assert summary_stage.execution_mode == "summary"
    assert summary_stage.job_codes == ("task_completed_notification",)
    assert definition.summary_contract.field_names(required_only=True) == (
        "final_status",
        "row_total_count",
        "row_status_counts",
        "aggregate_metrics",
        "row_summary",
        "failed_stage",
        "error_code",
    )

    assert definition.require_job("feishu_table_read").runtime_table == "api_worker_job"
    assert definition.require_job("feishu_table_write").handler_code == "feishu_table_write"
    assert definition.require_job("feishu_table_write").runtime_table == "api_worker_job"
    assert definition.require_job("amazon_product_browser_fetch").runtime_table == "task_execution"
    assert definition.require_job("amazon_product_browser_fetch").worker_type == "browser_worker"
    assert definition.require_job("amazon_product_row_persist").runtime_table == "api_worker_job"
    assert definition.require_job("task_completed_notification").runtime_table == (
        "notification_outbox"
    )


def test_amazon_workflow_is_available_through_domain_and_control_plane_registries() -> None:
    assert AMAZON_PRODUCT_ROW_TASK_CODE == TASK_CODE
    assert TASK_CODE in FORMAL_TASK_CODES
    assert get_workflow_definition(TASK_CODE) is REFRESH_AMAZON_PRODUCT_ROW_BY_ASIN_DEFINITION
    assert (
        get_registered_workflow_definition(TASK_CODE)
        is REFRESH_AMAZON_PRODUCT_ROW_BY_ASIN_DEFINITION
    )
    assert load_workflow_runtime(TASK_CODE) is not None


def test_amazon_early_and_forced_failures_keep_the_fixed_summary_shape() -> None:
    early_failure = amazon_orchestrator.advance_stage(
        store=None,
        request=None,
        workflow=None,
        stage_code="unsupported_stage",
    )

    _assert_fixed_summary_shape(
        early_failure["summary"],
        final_status="failed",
        row_status="failed",
    )
    assert early_failure["summary"]["aggregate_metrics"] == {
        "average_row_duration_ms": 0.0,
        "max_row_duration_ms": 0.0,
        "blocked_rate": 0.0,
        "average_parse_coverage_percentage": 0.0,
        "media_failure_rate": 0.0,
        "feishu_failure_rate": 0.0,
    }

    forced_summary, forced_result, final_status, _ = amazon_orchestrator._final_payload(
        store=None,
        request=SimpleNamespace(payload={"source_record_id": "rec-amazon-1"}),
        force_result={
            "failed_stage": "persist_amazon_product_detail",
            "error_code": "feishu_table_write_failed",
            "result": {
                "row_status": "failed",
                "requested_asin": "B0ABC12345",
                "step_statuses": {
                    "media_asset_sync": "success",
                    "amazon_product_fact_upsert": "success",
                    "feishu_table_write": "failed",
                    "unsafe_extra_step": {"token": "must-not-cross-runtime-boundary"},
                },
                "observability": {
                    "stage_durations_ms": {"feishu": 1.25, "unsafe": 999},
                    "field_coverage": {
                        "total": 20,
                        "observed": 10,
                        "missing": 10,
                        "percentage": 99.0,
                    },
                    "media_observed_count": 2,
                    "media_materialized_count": 1,
                },
            },
        },
    )

    assert final_status == "failed"
    _assert_fixed_summary_shape(
        forced_summary,
        final_status="failed",
        row_status="failed",
    )
    assert forced_summary["aggregate_metrics"] == {
        "average_row_duration_ms": 1.25,
        "max_row_duration_ms": 1.25,
        "blocked_rate": 0.0,
        "average_parse_coverage_percentage": 50.0,
        "media_failure_rate": 0.5,
        "feishu_failure_rate": 1.0,
    }
    assert forced_result["row_results"][0]["step_statuses"] == {
        "media_asset_sync": "success",
        "amazon_product_fact_upsert": "success",
        "feishu_table_write": "failed",
    }
    assert "must-not-cross-runtime-boundary" not in repr(forced_result)
    assert "step_statuses" not in amazon_orchestrator._compact_failure_result(
        {"step_statuses": {"unsafe_extra_step": {"token": "must-not-cross-runtime-boundary"}}}
    )

    unsafe_summary, unsafe_result, _, unsafe_error = amazon_orchestrator._final_payload(
        store=None,
        request=SimpleNamespace(payload={"source_record_id": "rec-amazon-1"}),
        force_result={
            "failed_stage": "persist_amazon_product_detail",
            "error_code": "token=must-not-cross-runtime-boundary",
            "result": {
                "row_status": "failed",
                "original_error_code": "token=must-not-cross-runtime-boundary",
            },
        },
    )
    assert unsafe_summary["error_code"] == "amazon_product_workflow_failed"
    assert unsafe_result["error_code"] == "amazon_product_workflow_failed"
    assert "original_error_code" not in unsafe_result["row_results"][0]
    assert unsafe_error["error_code"] == "amazon_product_workflow_failed"
    assert "must-not-cross-runtime-boundary" not in repr(
        (unsafe_summary, unsafe_result, unsafe_error)
    )


def test_amazon_final_result_compacts_nested_worker_references() -> None:
    capture_context = {
        "request_id": "1" * 32,
        "execution_id": "2" * 32,
        "run_id": "3" * 64,
        "requested_asin": "B0ABC12345",
        "artifact_bucket": "amazon-artifacts",
        "artifact_object_prefix": "",
    }
    normalized_ref = {
        "capture_kind": "normalized_capture",
        "bucket": "amazon-artifacts",
        "object_key": (
            "raw-captures/amazon/us/B0ABC12345/2026/07/15/"
            + "3" * 64
            + "/"
            + "a" * 64
            + "/normalized.json"
        ),
        "content_digest": "a" * 64,
        "content_type": "application/json",
        "sanitization_status": "normalized",
        "request_id": "1" * 32,
        "execution_id": "2" * 32,
        "run_id": "3" * 64,
        "collected_at": "2026-07-15T00:00:00Z",
        "created_at": "2026-07-15T00:00:00Z",
        "remote_uri": "s3://amazon-artifacts/secret-query",
        "access_token": "persist-token-must-not-leak",
    }
    compact = amazon_orchestrator._compact_persist_result(
        {
            "row_status": "success",
            "source_record_id": "rec-amazon-1",
            "requested_asin": "B0ABC12345",
            "resolved_asin": "B0ABC12345",
            "run_id": "3" * 64,
            "fact_refs": {
                "product_id": "4" * 32,
                "snapshot_id": "5" * 32,
                "binding_id": "6" * 32,
                "raw_capture_ids": ["7" * 32, {"token": "nested-token-must-not-leak"}],
                "normalized_capture_ref": normalized_ref,
                "cookie": "persist-cookie-must-not-leak",
            },
            "media_coverage": {
                "expected": 3,
                "materialized": 2,
                "missing": 999,
                "complete": True,
                "access_token": "coverage-token-must-not-leak",
            },
            "writeback": {
                "written_count": 1,
                "skipped_count": 0,
                "failed_count": 0,
                "target_record_ids": [
                    "rec-amazon-1",
                    {"html": "<body>must-not-leak</body>"},
                    "another-record",
                ],
                "access_token": "writeback-token-must-not-leak",
            },
            "failed_step": "token=failed-step-must-not-leak",
        },
        capture_context=capture_context,
    )

    assert compact["fact_refs"] == {
        "product_id": "4" * 32,
        "snapshot_id": "5" * 32,
        "binding_id": "6" * 32,
        "raw_capture_ids": ["7" * 32],
        "normalized_capture_ref": {
            key: value
            for key, value in normalized_ref.items()
            if key
            in {
                "capture_kind",
                "bucket",
                "object_key",
                "content_digest",
                "content_type",
                "sanitization_status",
                "request_id",
                "execution_id",
                "run_id",
                "collected_at",
                "created_at",
            }
        },
    }
    assert compact["media_coverage"] == {
        "expected": 3,
        "materialized": 2,
        "missing": 1,
        "complete": False,
    }
    assert compact["writeback"] == {
        "written_count": 1,
        "skipped_count": 0,
        "failed_count": 0,
        "target_record_ids": ["rec-amazon-1"],
    }
    assert "failed_step" not in compact

    evidence_ref = {
        **normalized_ref,
        "capture_kind": "screenshot",
        "object_key": (
            "raw-captures/amazon/us/B0ABC12345/2026/07/15/"
            + "3" * 64
            + "/"
            + "b" * 64
            + "/page.png"
        ),
        "content_type": "image/png",
        "sanitization_status": "not_applicable",
        "content_digest": "b" * 64,
        "cookie": "failure-cookie-must-not-leak",
    }
    failure = amazon_orchestrator._compact_failure_result(
        {
            "requested_asin": "B0ABC12345",
            "collection_status": "blocked",
            "evidence_refs": [evidence_ref],
            "writeback": {
                "written_count": 1,
                "skipped_count": 0,
                "failed_count": 0,
                "target_record_ids": ["rec-amazon-1", "token-must-not-leak"],
                "access_token": "failure-token-must-not-leak",
            },
        },
        source_record_id="rec-amazon-1",
        capture_context=capture_context,
    )

    assert set(failure["evidence_refs"][0]) == {
        "capture_kind",
        "bucket",
        "object_key",
        "content_digest",
        "content_type",
        "sanitization_status",
        "request_id",
        "execution_id",
        "run_id",
        "collected_at",
        "created_at",
    }
    assert failure["writeback"]["target_record_ids"] == ["rec-amazon-1"]
    serialized = repr((compact, failure))
    for forbidden in (
        "persist-token-must-not-leak",
        "nested-token-must-not-leak",
        "persist-cookie-must-not-leak",
        "coverage-token-must-not-leak",
        "writeback-token-must-not-leak",
        "failure-cookie-must-not-leak",
        "failure-token-must-not-leak",
        "<body>must-not-leak</body>",
    ):
        assert forbidden not in serialized

    poisoned = amazon_orchestrator._compact_persist_result(
        {
            "row_status": "success",
            "fact_refs": {
                "product_id": "<html>COOKIE=session-secret</html>",
                "raw_capture_ids": ["Bearer runtime-token"],
                "normalized_capture_ref": {
                    "capture_kind": "normalized_capture",
                    "bucket": "access-token-secret",
                    "object_key": "<html>raw page</html>",
                    "content_digest": "c" * 64,
                    "content_type": "application/json",
                    "sanitization_status": "normalized",
                    "request_id": "8" * 32,
                    "execution_id": "9" * 32,
                    "run_id": "a" * 64,
                },
            },
        }
    )
    assert "fact_refs" not in poisoned
    assert "session-secret" not in repr(poisoned)
    assert "runtime-token" not in repr(poisoned)
    assert "access-token-secret" not in repr(poisoned)


def test_amazon_persist_writeback_requires_raw_exact_source_convergence() -> None:
    converged = {
        "written_count": 1,
        "skipped_count": 0,
        "failed_count": 0,
        "target_record_ids": ["rec-amazon-1"],
    }
    assert amazon_orchestrator._persist_writeback_converged(
        converged,
        source_record_id="rec-amazon-1",
    )
    for malformed in (
        {**converged, "target_record_ids": ["rec-amazon-1", "rec-other"]},
        {**converged, "target_record_ids": ["rec-other"]},
        {**converged, "written_count": True},
        {**converged, "failed_count": 1},
    ):
        assert not amazon_orchestrator._persist_writeback_converged(
            malformed,
            source_record_id="rec-amazon-1",
        )


def test_amazon_status_writeback_requires_raw_exact_source_convergence() -> None:
    converged = {
        "written_count": 1,
        "skipped_count": 0,
        "failed_count": 0,
        "target_record_ids": ["rec-amazon-1"],
    }
    assert amazon_orchestrator._status_writeback_converged(
        handler_status="success",
        value=converged,
        source_record_id="rec-amazon-1",
    )
    for malformed in (
        {**converged, "target_record_ids": ["rec-amazon-1", {"record_id": "rec-other"}]},
        {**converged, "target_record_ids": [" rec-amazon-1 "]},
        {**converged, "written_count": True},
    ):
        assert not amazon_orchestrator._status_writeback_converged(
            handler_status="success",
            value=malformed,
            source_record_id="rec-amazon-1",
        )


def test_amazon_status_writeback_accepts_only_missing_optional_fields_skip() -> None:
    skipped = {
        "written_count": 0,
        "skipped_count": 1,
        "failed_count": 0,
        "target_record_ids": [],
        "records": [
            {
                "record_id": "rec-amazon-1",
                "status": "skipped",
                "message": "empty_fields",
            }
        ],
    }

    assert amazon_orchestrator._status_writeback_converged(
        handler_status="skipped",
        value=skipped,
        source_record_id="rec-amazon-1",
    )
    assert not amazon_orchestrator._status_writeback_converged(
        handler_status="skipped",
        value={**skipped, "records": [{**skipped["records"][0], "message": "existing_record"}]},
        source_record_id="rec-amazon-1",
    )


def test_amazon_media_source_refs_are_compact_governed_runtime_values() -> None:
    refs = amazon_orchestrator._compact_media_source_refs(
        [
            {
                "source_url": (
                    "https://m.media-amazon.com/images/I/main.jpg?access_token=must-not-leak"
                ),
                "source_platform": "amazon",
                "marketplace_code": "US",
                "product_id": "B0ABC12345",
                "media_role": "main_image",
                "position": 0,
                "cookie": "must-not-leak",
            },
            {
                "source_url": "https://evil.example/image.jpg?token=must-not-leak",
                "source_platform": "amazon",
                "marketplace_code": "US",
                "product_id": "B0ABC12345",
                "media_role": "gallery_image",
                "position": 1,
            },
            {
                "source_url": "https://m.media-amazon.com/images/I/wrong.jpg",
                "source_platform": "amazon",
                "marketplace_code": "US",
                "product_id": "B0OTHER001",
                "media_role": "gallery_image",
                "position": 2,
            },
            {
                "source_url": "https://m.media-amazon.com/images/%3Cscript%3Ebad.jpg",
                "source_platform": "amazon",
                "marketplace_code": "US",
                "product_id": "B0ABC12345",
                "media_role": "gallery_image",
                "position": 2,
            },
            {
                "source_url": "https://m.media-amazon.com/images/Bearer-runtime-token.jpg",
                "source_platform": "amazon",
                "marketplace_code": "US",
                "product_id": "B0ABC12345",
                "media_role": "gallery_image",
                "position": 3,
            },
            {
                "source_url": "https://m.media-amazon.com/images/I/duplicate-main.jpg",
                "source_platform": "amazon",
                "marketplace_code": "US",
                "product_id": "B0ABC12345",
                "media_role": "main_image",
                "position": 0,
            },
        ],
        product_id="B0ABC12345",
    )

    assert refs == [
        {
            "source_url": "https://m.media-amazon.com/images/I/main.jpg",
            "source_platform": "amazon",
            "marketplace_code": "US",
            "product_id": "B0ABC12345",
            "media_role": "main_image",
            "position": 0,
        }
    ]
    assert "must-not-leak" not in repr(refs)


def test_amazon_media_source_refs_preserve_same_url_for_distinct_roles() -> None:
    source_url = "https://m.media-amazon.com/images/I/shared.jpg"
    refs = amazon_orchestrator._compact_media_source_refs(
        [
            {
                "source_url": source_url,
                "source_platform": "amazon",
                "marketplace_code": "US",
                "product_id": "B0ABC12345",
                "media_role": "main_image",
                "position": 0,
            },
            {
                "source_url": source_url,
                "source_platform": "amazon",
                "marketplace_code": "US",
                "product_id": "B0ABC12345",
                "media_role": "gallery_image",
                "position": 0,
            },
        ],
        product_id="B0ABC12345",
    )

    assert [(item["media_role"], item["position"]) for item in refs] == [
        ("main_image", 0),
        ("gallery_image", 0),
    ]


def test_amazon_capture_refs_bind_current_runtime_identity_and_exact_coordinate() -> None:
    context = {
        "request_id": "1" * 32,
        "execution_id": "2" * 32,
        "run_id": "3" * 64,
        "requested_asin": "B0ABC12345",
        "artifact_bucket": "amazon-artifacts",
        "artifact_object_prefix": "prod",
    }
    valid = {
        "capture_kind": "normalized_capture",
        "bucket": "amazon-artifacts",
        "object_key": (
            "prod/raw-captures/amazon/us/B0ABC12345/2026/07/15/"
            + "3" * 64
            + "/"
            + "a" * 64
            + "/normalized.json"
        ),
        "content_digest": "a" * 64,
        "content_type": "application/json",
        "sanitization_status": "normalized",
        "request_id": "1" * 32,
        "execution_id": "2" * 32,
        "run_id": "3" * 64,
        "collected_at": "2026-07-15T00:00:00Z",
    }

    assert amazon_orchestrator._compact_capture_ref(
        valid,
        capture_context=context,
    )
    poisoned = (
        {**valid, "request_id": "4" * 32},
        {**valid, "execution_id": "5" * 32},
        {**valid, "run_id": "6" * 64},
        {**valid, "bucket": "other-artifacts"},
        {
            **valid,
            "object_key": valid["object_key"].replace("B0ABC12345", "B0OTHER001"),
        },
        {**valid, "object_key": "foreign/" + valid["object_key"]},
        {**valid, "collected_at": "2026-07-16T00:00:00Z"},
    )
    for ref in poisoned:
        assert not amazon_orchestrator._compact_capture_ref(
            ref,
            capture_context=context,
        )

    failure = amazon_orchestrator._compact_failure_result(
        {
            "requested_asin": "B0ABC12345",
            "collection_status": "failed",
            "evidence_refs": [{**valid, "request_id": "4" * 32}],
        },
        source_record_id="rec-amazon-1",
        capture_context=context,
    )
    assert "evidence_refs" not in failure


def test_amazon_persist_stage_rejects_nonconverged_child_writeback(monkeypatch) -> None:
    request = SimpleNamespace(
        request_id="request-1",
        payload={"source_record_id": "rec-amazon-1"},
    )
    row = {
        "source_record_id": "rec-amazon-1",
        "requested_asin": "B0ABC12345",
    }
    expected_run_id = amazon_orchestrator._stable_run_id(
        request_id=request.request_id,
        source_record_id=row["source_record_id"],
        requested_asin=row["requested_asin"],
    )
    persist_result = {
        "row_status": "success",
        "source_record_id": row["source_record_id"],
        "requested_asin": row["requested_asin"],
        "resolved_asin": row["requested_asin"],
        "run_id": expected_run_id,
        "writeback": {
            "written_count": 1,
            "skipped_count": 0,
            "failed_count": 0,
            "target_record_ids": [row["source_record_id"], "rec-other"],
        },
        "step_statuses": {"feishu_table_write": "success"},
    }
    monkeypatch.setattr(amazon_orchestrator, "_read_context", lambda **_kwargs: row)
    monkeypatch.setattr(
        amazon_orchestrator,
        "_browser_runtime_context",
        lambda _request: {"browser_target_digest": "digest-only"},
    )
    monkeypatch.setattr(
        amazon_orchestrator,
        "_browser_result",
        lambda **_kwargs: (
            {
                "resolved_asin": row["requested_asin"],
                "artifact_refs": [],
            },
            {},
        ),
    )
    monkeypatch.setattr(
        amazon_orchestrator,
        "_ensure_stage_status_writeback",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        amazon_orchestrator,
        "_jobs_for_stage",
        lambda **_kwargs: [{"status": "finished"}],
    )
    monkeypatch.setattr(
        amazon_orchestrator,
        "extract_handler_result_status",
        lambda _job: "success",
    )
    monkeypatch.setattr(
        amazon_orchestrator,
        "extract_effective_result_payload",
        lambda _job: persist_result,
    )
    terminal_call: dict[str, Any] = {}

    def terminal_failure(**kwargs: Any) -> dict[str, Any]:
        terminal_call.update(kwargs)
        return {"action": "finalize"}

    monkeypatch.setattr(
        amazon_orchestrator,
        "_terminal_failure_with_writeback",
        terminal_failure,
    )

    decision = amazon_orchestrator._advance_persist(
        store=object(),
        request=request,
        workflow=object(),
    )

    assert decision == {"action": "finalize"}
    assert terminal_call["error_code"] == "amazon_persist_writeback_not_converged"
    assert terminal_call["step_statuses"]["feishu_table_write"] == "failed"
