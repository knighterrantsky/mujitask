from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, text

from automation_business_scaffold.capabilities.browser.amazon.product_page import (
    extract_amazon_product_capture,
)
from automation_business_scaffold.capabilities.persistence.database.amazon_product_fact_upsert_handler import (
    amazon_product_fact_upsert_handler,
)
from automation_business_scaffold.contracts.handler.allowlist import API_HANDLER_CONTRACTS
from automation_business_scaffold.contracts.handler.api import BOUND_API_HANDLERS
from automation_business_scaffold.contracts.handler.contract import HandlerContext
from automation_business_scaffold.control_plane.supervisor.execution_supervisor import (
    ExecutionSupervisorOutcome,
)
from automation_business_scaffold.domains.amazon.jobs.amazon_product_fact_upsert import (
    AMAZON_PRODUCT_FACT_UPSERT_JOB,
)
from automation_business_scaffold.domains.amazon.projections.feishu_product_projection import (
    amazon_product_projection_mapper,
)
from automation_business_scaffold.infrastructure.artifacts.minio_store import MinioArtifactStore
from automation_business_scaffold.infrastructure.facts.amazon_fact_store import AmazonFactStore


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "amazon"
FACT_DB_ENV_KEYS = (
    "TK_FACT_DB_URL",
    "BUSINESS_EXECUTION_CONTROL_FACT_DB_URL",
    "EXECUTION_CONTROL_FACT_DB_URL",
    "FACT_DB_URL",
)
ARTIFACT_ENV_KEYS = (
    "BUSINESS_EXECUTION_CONTROL_ARTIFACT_STORE_PROVIDER",
    "EXECUTION_CONTROL_ARTIFACT_STORE_PROVIDER",
    "BUSINESS_EXECUTION_CONTROL_MINIO_ENDPOINT",
    "EXECUTION_CONTROL_MINIO_ENDPOINT",
    "BUSINESS_EXECUTION_CONTROL_MINIO_ACCESS_KEY",
    "EXECUTION_CONTROL_MINIO_ACCESS_KEY",
    "BUSINESS_EXECUTION_CONTROL_MINIO_SECRET_KEY",
    "EXECUTION_CONTROL_MINIO_SECRET_KEY",
)


class FakeArtifactStore:
    provider_code = "fake"
    artifact_bucket = "artifacts"
    artifact_object_prefix = ""

    def __init__(self, blobs: dict[tuple[str, str], bytes]) -> None:
        self.blobs = blobs
        self.read_calls: list[tuple[str, str]] = []

    def read_bytes(self, *, bucket: str, object_key: str) -> bytes:
        self.read_calls.append((bucket, object_key))
        return self.blobs[(bucket, object_key)]


def _capture_bytes(name: str, *, asin: str, resolved_url: str) -> bytes:
    html = (FIXTURE_DIR / name).read_text(encoding="utf-8")
    capture = extract_amazon_product_capture(
        html,
        requested_asin=asin,
        resolved_url=resolved_url,
        observed_at=datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc),
    )
    capture["profile_context"] = {
        "locale": "en_US",
        "currency": "USD",
        "delivery_region": "US test region",
        "profile_context_digest": "profile-digest",
    }
    return json.dumps(capture, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _payload(capture_bytes: bytes, *, asin: str = "B0CHILD001") -> dict[str, object]:
    normalized_key = f"raw-captures/amazon/us/{asin}/run-1/normalized.json"
    html_key = f"raw-captures/amazon/us/{asin}/run-1/page.html.gz"
    normalized_ref = {
        "capture_kind": "normalized_capture",
        "bucket": "artifacts",
        "object_key": normalized_key,
        "content_digest": hashlib.sha256(capture_bytes).hexdigest(),
        "content_type": "application/json",
        "sanitization_status": "sanitized",
    }
    capture = json.loads(capture_bytes)
    evidence = capture.get("field_evidence", {})
    media = capture.get("media", {})
    materialized_media_assets: list[dict[str, object]] = []
    main_image = media.get("main_image")
    main_url = ""
    if evidence.get("media.main_image", {}).get("status") == "observed" and isinstance(
        main_image,
        dict,
    ):
        main_url = str(main_image.get("url") or "")
        materialized_media_assets.append(
            _materialized_asset(
                asin=asin,
                source_url=main_url,
                media_role="main_image",
                position=0,
            )
        )
    if evidence.get("media.gallery_images", {}).get("status") == "observed":
        seen_urls = {main_url} if main_url else set()
        for position, image in enumerate(media.get("gallery_images", [])):
            source_url = str(image.get("url") or "") if isinstance(image, dict) else ""
            if not source_url or source_url in seen_urls:
                continue
            seen_urls.add(source_url)
            materialized_media_assets.append(
                _materialized_asset(
                    asin=asin,
                    source_url=source_url,
                    media_role="gallery_image",
                    position=position,
                )
            )
    return {
        "normalized_capture_ref": normalized_ref,
        "raw_capture_refs": [
            normalized_ref,
            {
                "capture_kind": "html",
                "bucket": "artifacts",
                "object_key": html_key,
                "content_digest": "html-digest",
                "content_type": "text/html",
                "sanitization_status": "sanitized",
            },
        ],
        "source_table_ref": {"base_id": "base-1", "table_id": "table-1"},
        "source_record_id": "record-1",
        "requested_asin": asin,
        "run_id": "run-1",
        "materialized_media_assets": materialized_media_assets,
    }


def _materialized_asset(
    *,
    asin: str,
    source_url: str,
    media_role: str,
    position: int,
) -> dict[str, object]:
    file_name = f"{media_role}-{position}.jpg"
    object_key = f"product-media/amazon/us/{asin}/{media_role}/{file_name}"
    return {
        "source_url": source_url,
        "content_digest": f"image-digest-{position}",
        "bucket": "artifacts",
        "object_key": object_key,
        "remote_uri": f"s3://artifacts/{object_key}",
        "file_name": file_name,
        "mime_type": "image/jpeg",
        "size_bytes": 123 + position,
        "media_role": media_role,
        "position": position,
        "sync_state": "uploaded",
    }


def _context(
    payload: dict[str, object],
    *,
    metadata: dict[str, object] | None = None,
) -> HandlerContext:
    return HandlerContext(
        request_id="request-1",
        job_id="job-1",
        handler_code="amazon_product_fact_upsert",
        worker_type="api_worker",
        runtime_table="api_worker_job",
        payload=payload,
        workflow_code="refresh_amazon_product_row_by_asin",
        stage_code="persist_amazon_product_detail",
        job_code="amazon_product_fact_upsert",
        metadata=dict(metadata or {}),
    )


def test_handler_is_allowlisted_bound_and_has_a_strict_job_contract() -> None:
    assert "amazon_product_fact_upsert" in API_HANDLER_CONTRACTS
    assert BOUND_API_HANDLERS["amazon_product_fact_upsert"] is amazon_product_fact_upsert_handler
    assert AMAZON_PRODUCT_FACT_UPSERT_JOB.handler_code == "amazon_product_fact_upsert"
    assert AMAZON_PRODUCT_FACT_UPSERT_JOB.worker_type == "api_worker"
    assert AMAZON_PRODUCT_FACT_UPSERT_JOB.runtime_table == "api_worker_job"
    assert AMAZON_PRODUCT_FACT_UPSERT_JOB.payload_contract.field_names(required_only=True) == (
        "normalized_capture_ref",
        "raw_capture_refs",
        "source_table_ref",
        "source_record_id",
        "requested_asin",
        "run_id",
    )
    assert AMAZON_PRODUCT_FACT_UPSERT_JOB.business_key_template == "{source_record_id}"
    assert "projection_facts" not in AMAZON_PRODUCT_FACT_UPSERT_JOB.result_contract.field_names()


def test_handler_fails_when_fact_database_configuration_is_missing(monkeypatch) -> None:
    for key in FACT_DB_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    capture_bytes = _capture_bytes(
        "product_detail_child.html",
        asin="B0CHILD001",
        resolved_url="https://www.amazon.com/dp/B0CHILD001",
    )
    payload = _payload(capture_bytes)
    store = FakeArtifactStore(
        {("artifacts", payload["normalized_capture_ref"]["object_key"]): capture_bytes}
    )

    result = amazon_product_fact_upsert_handler(
        _context(payload, metadata={"artifact_store": store})
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "fact_database_persistence_required"
    assert store.read_calls == []


def test_handler_fails_when_object_storage_configuration_is_missing(monkeypatch) -> None:
    for key in ARTIFACT_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    capture_bytes = _capture_bytes(
        "product_detail_child.html",
        asin="B0CHILD001",
        resolved_url="https://www.amazon.com/dp/B0CHILD001",
    )

    result = amazon_product_fact_upsert_handler(
        _context(_payload(capture_bytes), metadata={"fact_store": object()})
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "object_storage_required"


def test_handler_rejects_missing_raw_capture_evidence_before_writing(runtime_db_url) -> None:
    capture_bytes = _capture_bytes(
        "product_detail_child.html",
        asin="B0CHILD001",
        resolved_url="https://www.amazon.com/dp/B0CHILD001",
    )
    payload = _payload(capture_bytes)
    payload["raw_capture_refs"] = [payload["normalized_capture_ref"]]
    artifact_store = FakeArtifactStore(
        {("artifacts", payload["normalized_capture_ref"]["object_key"]): capture_bytes}
    )

    result = amazon_product_fact_upsert_handler(
        _context(
            payload,
            metadata={
                "fact_store": AmazonFactStore(db_url=runtime_db_url),
                "artifact_store": artifact_store,
            },
        )
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "raw_capture_evidence_missing"
    assert artifact_store.read_calls == []
    assert _count(runtime_db_url, "amazon_products") == 0


def test_handler_rejects_raw_capture_in_foreign_bucket_before_reading(runtime_db_url) -> None:
    capture_bytes = _capture_bytes(
        "product_detail_child.html",
        asin="B0CHILD001",
        resolved_url="https://www.amazon.com/dp/B0CHILD001",
    )
    payload = _payload(capture_bytes)
    for raw_ref in payload["raw_capture_refs"]:
        raw_ref["bucket"] = "foreign-bucket"
    artifact_store = FakeArtifactStore({})

    result = amazon_product_fact_upsert_handler(
        _context(
            payload,
            metadata={
                "fact_store": AmazonFactStore(db_url=runtime_db_url),
                "artifact_store": artifact_store,
            },
        )
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "raw_capture_evidence_missing"
    assert artifact_store.read_calls == []
    assert _count(runtime_db_url, "amazon_products") == 0


def test_handler_rejects_raw_prefix_that_only_appears_mid_path(runtime_db_url) -> None:
    capture_bytes = _capture_bytes(
        "product_detail_child.html",
        asin="B0CHILD001",
        resolved_url="https://www.amazon.com/dp/B0CHILD001",
    )
    payload = _payload(capture_bytes)
    for raw_ref in payload["raw_capture_refs"]:
        raw_ref["object_key"] = f"evil/{raw_ref['object_key']}"
    artifact_store = FakeArtifactStore({})

    result = amazon_product_fact_upsert_handler(
        _context(
            payload,
            metadata={
                "fact_store": AmazonFactStore(db_url=runtime_db_url),
                "artifact_store": artifact_store,
            },
        )
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "raw_capture_evidence_missing"
    assert artifact_store.read_calls == []
    assert _count(runtime_db_url, "amazon_products") == 0


def test_handler_rejects_invalid_capture_payload(runtime_db_url) -> None:
    invalid_bytes = b'{"contract_revision":1}'
    payload = _payload(invalid_bytes)
    artifact_store = FakeArtifactStore(
        {("artifacts", payload["normalized_capture_ref"]["object_key"]): invalid_bytes}
    )

    result = amazon_product_fact_upsert_handler(
        _context(
            payload,
            metadata={
                "fact_store": AmazonFactStore(db_url=runtime_db_url),
                "artifact_store": artifact_store,
            },
        )
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "invalid_amazon_capture"
    assert _count(runtime_db_url, "amazon_products") == 0


def test_handler_rejects_source_asin_mismatch_before_any_fact_write(
    runtime_db_url,
) -> None:
    capture_bytes = _capture_bytes(
        "product_detail_child.html",
        asin="B0CHILD001",
        resolved_url="https://www.amazon.com/dp/B0CHILD001",
    )
    payload = _payload(capture_bytes)
    payload["requested_asin"] = "B0PARENT01"
    artifact_store = FakeArtifactStore(
        {("artifacts", payload["normalized_capture_ref"]["object_key"]): capture_bytes}
    )

    result = amazon_product_fact_upsert_handler(
        _context(
            payload,
            metadata={
                "fact_store": AmazonFactStore(db_url=runtime_db_url),
                "artifact_store": artifact_store,
            },
        )
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "invalid_amazon_capture"
    assert _count(runtime_db_url, "amazon_products") == 0


def test_handler_rejects_invalid_nested_capture_before_any_fact_write(runtime_db_url) -> None:
    capture_bytes = _capture_bytes(
        "product_detail_child.html",
        asin="B0CHILD001",
        resolved_url="https://www.amazon.com/dp/B0CHILD001",
    )
    capture = json.loads(capture_bytes)
    capture["variants"]["child_asins"] = ["INVALID"]
    invalid_bytes = json.dumps(capture, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload = _payload(invalid_bytes)
    artifact_store = FakeArtifactStore(
        {("artifacts", payload["normalized_capture_ref"]["object_key"]): invalid_bytes}
    )

    result = amazon_product_fact_upsert_handler(
        _context(
            payload,
            metadata={
                "fact_store": AmazonFactStore(db_url=runtime_db_url),
                "artifact_store": artifact_store,
            },
        )
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "invalid_amazon_capture"
    assert _count(runtime_db_url, "amazon_products") == 0


def test_handler_rejects_capture_digest_mismatch_before_any_fact_write(runtime_db_url) -> None:
    capture_bytes = _capture_bytes(
        "product_detail_child.html",
        asin="B0CHILD001",
        resolved_url="https://www.amazon.com/dp/B0CHILD001",
    )
    payload = _payload(capture_bytes)
    payload["normalized_capture_ref"]["content_digest"] = "0" * 64
    artifact_store = FakeArtifactStore(
        {("artifacts", payload["normalized_capture_ref"]["object_key"]): capture_bytes}
    )

    result = amazon_product_fact_upsert_handler(
        _context(
            payload,
            metadata={
                "fact_store": AmazonFactStore(db_url=runtime_db_url),
                "artifact_store": artifact_store,
            },
        )
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "capture_artifact_digest_mismatch"
    assert _count(runtime_db_url, "amazon_products") == 0


def test_handler_rejects_unrelated_resolved_asin_before_any_fact_write(runtime_db_url) -> None:
    capture_bytes = _capture_bytes(
        "product_detail_child.html",
        asin="B0CHILD001",
        resolved_url="https://www.amazon.com/dp/B0CHILD001",
    )
    capture = json.loads(capture_bytes)
    capture["resolved_asin"] = "B0UNAVL001"
    invalid_bytes = json.dumps(capture, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload = _payload(invalid_bytes)
    artifact_store = FakeArtifactStore(
        {("artifacts", payload["normalized_capture_ref"]["object_key"]): invalid_bytes}
    )

    result = amazon_product_fact_upsert_handler(
        _context(
            payload,
            metadata={
                "fact_store": AmazonFactStore(db_url=runtime_db_url),
                "artifact_store": artifact_store,
            },
        )
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "invalid_amazon_capture"
    assert _count(runtime_db_url, "amazon_products") == 0


def test_handler_rejects_parent_redirect_with_unsuppressed_child_facts(runtime_db_url) -> None:
    capture_bytes = _capture_bytes(
        "product_detail_child.html",
        asin="B0PARENT01",
        resolved_url="https://www.amazon.com/dp/B0CHILD001",
    )
    capture = json.loads(capture_bytes)
    capture["product"]["title"] = "Leaked child title"
    invalid_bytes = json.dumps(capture, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload = _payload(invalid_bytes, asin="B0PARENT01")
    artifact_store = FakeArtifactStore(
        {("artifacts", payload["normalized_capture_ref"]["object_key"]): invalid_bytes}
    )

    result = amazon_product_fact_upsert_handler(
        _context(
            payload,
            metadata={
                "fact_store": AmazonFactStore(db_url=runtime_db_url),
                "artifact_store": artifact_store,
            },
        )
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "invalid_amazon_capture"
    assert _count(runtime_db_url, "amazon_products") == 0


def test_handler_persists_partial_media_and_omits_incomplete_gallery_projection(
    runtime_db_url,
) -> None:
    capture_bytes = _capture_bytes(
        "product_detail_child.html",
        asin="B0CHILD001",
        resolved_url="https://www.amazon.com/dp/B0CHILD001",
    )
    payload = _payload(capture_bytes)
    payload["materialized_media_assets"] = payload["materialized_media_assets"][:-1]
    artifact_store = FakeArtifactStore(
        {("artifacts", payload["normalized_capture_ref"]["object_key"]): capture_bytes}
    )

    materialized_assets = payload["materialized_media_assets"]
    assert isinstance(materialized_assets, list)
    assert [asset["media_role"] for asset in materialized_assets] == [
        "main_image",
        "gallery_image",
    ]

    result = amazon_product_fact_upsert_handler(
        _context(
            payload,
            metadata={
                "fact_store": AmazonFactStore(db_url=runtime_db_url),
                "artifact_store": artifact_store,
                "include_transient_projection_facts": True,
            },
        )
    )

    assert result.status == "partial_success"
    assert result.summary["collection_status"] == "partial_success"
    assert result.summary["media_coverage"] == {
        "expected": 3,
        "materialized": 2,
        "missing": 1,
        "complete": False,
    }
    assert result.result["persisted_counts"]["media_assets"] == 2
    projection = result.result["projection_facts"]
    assert projection["collection_status"] == "partial_success"
    assert projection["field_evidence"]["media.main_image"]["status"] == "observed"
    assert projection["field_evidence"]["media.gallery_images"]["status"] == "missing"

    command = amazon_product_projection_mapper(
        {
            "projection_facts": projection,
            "materialized_media_assets": materialized_assets,
        },
        {},
    )
    assert "主图" in command["fields"]
    assert "图库" not in command["fields"]
    assert _count(runtime_db_url, "amazon_products") == 1
    assert _count(runtime_db_url, "amazon_media_assets") == 2
    assert _count(runtime_db_url, "amazon_product_media_assets") == 2


def test_same_run_partial_media_retry_converges_to_full_without_duplicate_facts(
    runtime_db_url,
) -> None:
    capture_bytes = _capture_bytes(
        "product_detail_child.html",
        asin="B0CHILD001",
        resolved_url="https://www.amazon.com/dp/B0CHILD001",
    )
    partial_payload = _payload(capture_bytes)
    partial_payload["materialized_media_assets"] = partial_payload[
        "materialized_media_assets"
    ][:-1]
    full_payload = _payload(capture_bytes)
    normalized_ref = full_payload["normalized_capture_ref"]
    artifact_store = FakeArtifactStore(
        {("artifacts", normalized_ref["object_key"]): capture_bytes}
    )
    metadata = {
        "fact_store": AmazonFactStore(db_url=runtime_db_url),
        "artifact_store": artifact_store,
    }

    partial = amazon_product_fact_upsert_handler(
        _context(partial_payload, metadata=metadata)
    )
    complete = amazon_product_fact_upsert_handler(
        _context(full_payload, metadata=metadata)
    )

    assert partial.status == "partial_success"
    assert partial.summary["media_coverage"]["complete"] is False
    assert complete.status == "success"
    assert complete.summary["media_coverage"] == {
        "expected": 3,
        "materialized": 3,
        "missing": 0,
        "complete": True,
    }
    assert partial.result["product_id"] == complete.result["product_id"]
    assert partial.result["snapshot_id"] == complete.result["snapshot_id"]
    assert partial.result["binding_id"] == complete.result["binding_id"]
    assert _count(runtime_db_url, "amazon_products") == 1
    assert _count(runtime_db_url, "amazon_product_snapshots") == 1
    assert _count(runtime_db_url, "amazon_offer_snapshots") == 1
    assert _count(runtime_db_url, "amazon_product_variants") == 2
    assert _count(runtime_db_url, "amazon_bsr_snapshots") == 2
    assert _count(runtime_db_url, "amazon_media_assets") == 3
    assert _count(runtime_db_url, "amazon_product_media_assets") == 3
    assert _count(runtime_db_url, "amazon_raw_captures") == 2
    assert _count(runtime_db_url, "amazon_feishu_bindings") == 1


def test_handler_rejects_materialized_media_not_observed_in_capture(runtime_db_url) -> None:
    capture_bytes = _capture_bytes(
        "product_detail_child.html",
        asin="B0CHILD001",
        resolved_url="https://www.amazon.com/dp/B0CHILD001",
    )
    payload = _payload(capture_bytes)
    payload["materialized_media_assets"].append(
        _materialized_asset(
            asin="B0CHILD001",
            source_url="https://images.example.test/unobserved-extra.jpg",
            media_role="gallery_image",
            position=99,
        )
    )
    artifact_store = FakeArtifactStore(
        {("artifacts", payload["normalized_capture_ref"]["object_key"]): capture_bytes}
    )

    result = amazon_product_fact_upsert_handler(
        _context(
            payload,
            metadata={
                "fact_store": AmazonFactStore(db_url=runtime_db_url),
                "artifact_store": artifact_store,
            },
        )
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "invalid_amazon_capture"
    assert _count(runtime_db_url, "amazon_products") == 0


def test_handler_rejects_duplicate_materialized_media_mapping(runtime_db_url) -> None:
    capture_bytes = _capture_bytes(
        "product_detail_child.html",
        asin="B0CHILD001",
        resolved_url="https://www.amazon.com/dp/B0CHILD001",
    )
    payload = _payload(capture_bytes)
    payload["materialized_media_assets"].append(
        dict(payload["materialized_media_assets"][0])
    )
    artifact_store = FakeArtifactStore(
        {("artifacts", payload["normalized_capture_ref"]["object_key"]): capture_bytes}
    )

    result = amazon_product_fact_upsert_handler(
        _context(
            payload,
            metadata={
                "fact_store": AmazonFactStore(db_url=runtime_db_url),
                "artifact_store": artifact_store,
            },
        )
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "invalid_amazon_capture"
    assert _count(runtime_db_url, "amazon_products") == 0


def test_handler_rejects_invalid_media_scalar_before_fact_write(runtime_db_url) -> None:
    capture_bytes = _capture_bytes(
        "product_detail_child.html",
        asin="B0CHILD001",
        resolved_url="https://www.amazon.com/dp/B0CHILD001",
    )
    payload = _payload(capture_bytes)
    payload["materialized_media_assets"][0]["size_bytes"] = "not-a-number"
    artifact_store = FakeArtifactStore(
        {("artifacts", payload["normalized_capture_ref"]["object_key"]): capture_bytes}
    )

    result = amazon_product_fact_upsert_handler(
        _context(
            payload,
            metadata={
                "fact_store": AmazonFactStore(db_url=runtime_db_url),
                "artifact_store": artifact_store,
            },
        )
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "invalid_amazon_capture"
    assert _count(runtime_db_url, "amazon_products") == 0


def test_handler_rejects_media_prefix_that_only_appears_mid_path(runtime_db_url) -> None:
    capture_bytes = _capture_bytes(
        "product_detail_child.html",
        asin="B0CHILD001",
        resolved_url="https://www.amazon.com/dp/B0CHILD001",
    )
    payload = _payload(capture_bytes)
    asset = payload["materialized_media_assets"][0]
    asset["object_key"] = f"evil/{asset['object_key']}"
    asset["remote_uri"] = f"s3://artifacts/{asset['object_key']}"
    artifact_store = FakeArtifactStore(
        {("artifacts", payload["normalized_capture_ref"]["object_key"]): capture_bytes}
    )

    result = amazon_product_fact_upsert_handler(
        _context(
            payload,
            metadata={
                "fact_store": AmazonFactStore(db_url=runtime_db_url),
                "artifact_store": artifact_store,
            },
        )
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "invalid_amazon_capture"
    assert _count(runtime_db_url, "amazon_products") == 0


def test_handler_rejects_invalid_commerce_scalar_before_fact_write(runtime_db_url) -> None:
    capture_bytes = _capture_bytes(
        "product_detail_child.html",
        asin="B0CHILD001",
        resolved_url="https://www.amazon.com/dp/B0CHILD001",
    )
    capture = json.loads(capture_bytes)
    capture["commerce"]["rating"] = "not-a-rating"
    invalid_bytes = json.dumps(capture, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload = _payload(invalid_bytes)
    artifact_store = FakeArtifactStore(
        {("artifacts", payload["normalized_capture_ref"]["object_key"]): invalid_bytes}
    )

    result = amazon_product_fact_upsert_handler(
        _context(
            payload,
            metadata={
                "fact_store": AmazonFactStore(db_url=runtime_db_url),
                "artifact_store": artifact_store,
            },
        )
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "invalid_amazon_capture"
    assert _count(runtime_db_url, "amazon_products") == 0


def test_handler_persists_capture_facts_and_retries_idempotently(runtime_db_url) -> None:
    capture_bytes = _capture_bytes(
        "product_detail_child.html",
        asin="B0CHILD001",
        resolved_url="https://www.amazon.com/dp/B0CHILD001",
    )
    payload = _payload(capture_bytes)
    normalized_ref = payload["normalized_capture_ref"]
    artifact_store = FakeArtifactStore({("artifacts", normalized_ref["object_key"]): capture_bytes})
    metadata = {
        "fact_store": AmazonFactStore(db_url=runtime_db_url),
        "artifact_store": artifact_store,
    }

    first = amazon_product_fact_upsert_handler(_context(payload, metadata=metadata))
    transient = amazon_product_fact_upsert_handler(
        _context(
            payload,
            metadata={**metadata, "include_transient_projection_facts": True},
        )
    )

    assert first.status == "success"
    assert transient.status == "success"
    assert first.summary["collection_status"] == "success"
    assert first.result["product_id"] == transient.result["product_id"]
    assert first.result["snapshot_id"] == transient.result["snapshot_id"]
    assert first.result["binding_id"] == transient.result["binding_id"]
    assert first.result["normalized_capture_ref"] == normalized_ref
    assert first.result["persisted_counts"] == {
        "products": 1,
        "product_snapshots": 1,
        "offer_snapshots": 1,
        "variant_relations": 2,
        "bsr_snapshots": 2,
        "media_assets": 3,
        "media_relations": 3,
        "raw_captures": 2,
        "feishu_bindings": 1,
    }
    assert "projection_facts" not in first.result
    stored_runtime_result = ExecutionSupervisorOutcome(
        context=_context(payload, metadata=metadata),
        worker_result=first,
        supervisor_status="completed",
        started_at=1.0,
        finished_at=2.0,
        heartbeat_count=0,
    ).storage_result()
    assert "projection_facts" not in stored_runtime_result
    assert "Structured product title" not in json.dumps(stored_runtime_result)
    projection = transient.result["projection_facts"]
    assert projection["product"]["title"] == "Structured product title"
    assert projection["commerce"]["featured_offer"]["price_amount"] == 29.99
    assert projection["source_record_id"] == "record-1"
    assert "artifact_refs" not in projection
    assert _count(runtime_db_url, "amazon_products") == 1
    assert _count(runtime_db_url, "amazon_product_snapshots") == 1
    assert _count(runtime_db_url, "amazon_offer_snapshots") == 1
    assert _count(runtime_db_url, "amazon_product_variants") == 2
    assert _count(runtime_db_url, "amazon_bsr_snapshots") == 2
    assert _count(runtime_db_url, "amazon_media_assets") == 3
    assert _count(runtime_db_url, "amazon_product_media_assets") == 3
    assert _count(runtime_db_url, "amazon_raw_captures") == 2
    assert _count(runtime_db_url, "amazon_feishu_bindings") == 1


def test_handler_persists_unavailable_terminal_fact_without_offer_or_media(runtime_db_url) -> None:
    capture_bytes = _capture_bytes(
        "product_detail_unavailable.html",
        asin="B0UNAVL001",
        resolved_url="https://www.amazon.com/dp/B0UNAVL001",
    )
    payload = _payload(capture_bytes, asin="B0UNAVL001")
    normalized_ref = payload["normalized_capture_ref"]
    artifact_store = FakeArtifactStore({("artifacts", normalized_ref["object_key"]): capture_bytes})

    result = amazon_product_fact_upsert_handler(
        _context(
            payload,
            metadata={
                "fact_store": AmazonFactStore(db_url=runtime_db_url),
                "artifact_store": artifact_store,
            },
        )
    )

    assert result.status == "success"
    assert result.summary["collection_status"] == "unavailable"
    assert result.result["persisted_counts"]["product_snapshots"] == 1
    assert result.result["persisted_counts"]["offer_snapshots"] == 0
    assert result.result["persisted_counts"]["media_assets"] == 0
    assert (
        _scalar(
            runtime_db_url,
            "SELECT status FROM amazon_products WHERE asin = 'B0UNAVL001'",
        )
        == "unavailable"
    )


def test_handler_persists_parent_redirect_relationships_without_child_facts(runtime_db_url) -> None:
    capture_bytes = _capture_bytes(
        "product_detail_child.html",
        asin="B0PARENT01",
        resolved_url="https://www.amazon.com/dp/B0CHILD001",
    )
    payload = _payload(capture_bytes, asin="B0PARENT01")
    normalized_ref = payload["normalized_capture_ref"]
    artifact_store = FakeArtifactStore({("artifacts", normalized_ref["object_key"]): capture_bytes})

    result = amazon_product_fact_upsert_handler(
        _context(
            payload,
            metadata={
                "fact_store": AmazonFactStore(db_url=runtime_db_url),
                "artifact_store": artifact_store,
            },
        )
    )

    assert result.status == "partial_success"
    assert result.result["persisted_counts"]["product_snapshots"] == 1
    assert result.result["persisted_counts"]["offer_snapshots"] == 0
    assert result.result["persisted_counts"]["variant_relations"] == 2
    assert result.result["persisted_counts"]["media_assets"] == 0
    assert (
        _scalar(
            runtime_db_url,
            "SELECT title FROM amazon_products WHERE asin = 'B0PARENT01'",
        )
        == ""
    )


def test_newer_partial_capture_preserves_known_status_and_variant_values(runtime_db_url) -> None:
    store = AmazonFactStore(db_url=runtime_db_url)
    observed_before_capture = datetime(2026, 7, 14, 7, 0, tzinfo=timezone.utc).timestamp()
    store.upsert_product(
        marketplace_code="US",
        asin="B0CHILD001",
        status="unavailable",
        observed_at=observed_before_capture,
    )
    store.upsert_variant(
        marketplace_code="US",
        parent_asin="B0PARENT01",
        child_asin="B0CHILD001",
        attributes={"Color": "Navy", "Size": "Large"},
        dimensions={"Color": ["Navy", "Red"], "Size": ["Large", "Small"]},
        source_asin="B0CHILD001",
        observed_at=observed_before_capture,
    )
    capture = json.loads(
        _capture_bytes(
            "product_detail_child.html",
            asin="B0CHILD001",
            resolved_url="https://www.amazon.com/dp/B0CHILD001",
        )
    )
    capture["collection_status"] = "partial_success"
    capture["commerce"]["availability_status"] = "unknown"
    capture["field_evidence"]["commerce.availability_status"]["status"] = "missing"
    capture["variants"]["current_attributes"] = {}
    capture["field_evidence"]["variants.current_attributes"]["status"] = "missing"
    capture["variants"]["dimensions"] = {}
    capture["field_evidence"]["variants.dimensions"]["status"] = "missing"
    partial_bytes = json.dumps(capture, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload = _payload(partial_bytes)
    artifact_store = FakeArtifactStore(
        {("artifacts", payload["normalized_capture_ref"]["object_key"]): partial_bytes}
    )

    result = amazon_product_fact_upsert_handler(
        _context(
            payload,
            metadata={"fact_store": store, "artifact_store": artifact_store},
        )
    )

    assert result.status == "partial_success"
    assert (
        _scalar(
            runtime_db_url,
            "SELECT status FROM amazon_products WHERE asin = 'B0CHILD001'",
        )
        == "unavailable"
    )
    attributes = _scalar(
        runtime_db_url,
        "SELECT attributes_json FROM amazon_product_variants "
        "WHERE parent_asin = 'B0PARENT01' AND child_asin = 'B0CHILD001'",
    )
    dimensions = _scalar(
        runtime_db_url,
        "SELECT dimensions_json FROM amazon_product_variants "
        "WHERE parent_asin = 'B0PARENT01' AND child_asin = 'B0CHILD001'",
    )
    assert json.loads(str(attributes)) == {"Color": "Navy", "Size": "Large"}
    assert json.loads(str(dimensions)) == {
        "Color": ["Navy", "Red"],
        "Size": ["Large", "Small"],
    }


def test_minio_read_bytes_closes_and_releases_the_response() -> None:
    class Response:
        def __init__(self) -> None:
            self.closed = False
            self.released = False

        def read(self) -> bytes:
            return b"capture"

        def close(self) -> None:
            self.closed = True

        def release_conn(self) -> None:
            self.released = True

    class Client:
        def __init__(self) -> None:
            self.response = Response()

        def get_object(self, bucket: str, object_key: str) -> Response:
            assert (bucket, object_key) == ("artifacts", "capture.json")
            return self.response

    client = Client()
    store = object.__new__(MinioArtifactStore)
    store._client = client  # noqa: SLF001

    assert store.read_bytes(bucket="artifacts", object_key="capture.json") == b"capture"
    assert client.response.closed is True
    assert client.response.released is True


def _count(db_url: str, table_name: str) -> int:
    return int(_scalar(db_url, f"SELECT COUNT(*) FROM {table_name}"))


def _scalar(db_url: str, statement: str) -> object:
    engine = create_engine(db_url, future=True)
    try:
        with engine.connect() as connection:
            return connection.execute(text(statement)).scalar_one()
    finally:
        engine.dispose()
