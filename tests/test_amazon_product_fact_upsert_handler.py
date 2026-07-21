from __future__ import annotations

import gzip
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from sqlalchemy import create_engine, text

import automation_business_scaffold.capabilities.persistence.database.amazon_product_fact_upsert_handler as fact_handler_module
from automation_business_scaffold.capabilities.browser.amazon.product_page import (
    extract_amazon_network_product_data,
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
from automation_business_scaffold.infrastructure.facts.amazon_fact_store import (
    AmazonFactStore,
)


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
SANITIZED_HTML_BYTES = gzip.compress(
    b"<html><body><h1>Sanitized Amazon product evidence</h1></body></html>",
    mtime=0,
)
MATERIALIZED_MEDIA_BLOBS: dict[tuple[str, str], bytes] = {}


class FakeArtifactStore:
    provider_code = "fake"
    artifact_bucket = "artifacts"
    artifact_object_prefix = ""

    def __init__(
        self,
        blobs: dict[tuple[str, str], bytes],
        *,
        default_html_bytes: bytes | None = SANITIZED_HTML_BYTES,
    ) -> None:
        self.blobs = blobs
        self.default_html_bytes = default_html_bytes
        self.read_calls: list[tuple[str, str]] = []

    def read_bytes(self, *, bucket: str, object_key: str) -> bytes:
        self.read_calls.append((bucket, object_key))
        if (bucket, object_key) in self.blobs:
            return self.blobs[(bucket, object_key)]
        if object_key.endswith("/page.html.gz"):
            if self.default_html_bytes is not None:
                return self.default_html_bytes
        if (bucket, object_key) in MATERIALIZED_MEDIA_BLOBS:
            return MATERIALIZED_MEDIA_BLOBS[(bucket, object_key)]
        raise KeyError((bucket, object_key))


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
    media = capture.get("media")
    evidence = capture.get("field_evidence")
    if isinstance(media, dict) and isinstance(evidence, dict):
        governed_urls: dict[str, str] = {}

        def governed_media(value: object) -> object:
            if not isinstance(value, dict) or not isinstance(value.get("url"), str):
                return value
            original = value["url"]
            governed_url = governed_urls.setdefault(
                original,
                f"https://m.media-amazon.com/images/I/fact-{len(governed_urls)}.jpg",
            )
            return {"url": governed_url}

        media["main_image"] = governed_media(media.get("main_image"))
        media["gallery_images"] = [governed_media(item) for item in media.get("gallery_images", [])]
        if isinstance(evidence.get("media.main_image"), dict):
            evidence["media.main_image"]["value"] = media["main_image"]
        if isinstance(evidence.get("media.gallery_images"), dict):
            evidence["media.gallery_images"]["value"] = media["gallery_images"]
    return json.dumps(capture, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _network_capture_bytes(asin: str = "B0CHILD001") -> bytes:
    network_capture = extract_amazon_network_product_data(
        [
            {
                "source_path": "/page-data",
                "payload": {
                    "asin": asin,
                    "product": {"title": "Allowlisted network title"},
                },
            }
        ],
        expected_asin=asin,
    )
    return json.dumps(network_capture, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _payload(capture_bytes: bytes, *, asin: str = "B0CHILD001") -> dict[str, object]:
    normalized_digest = hashlib.sha256(capture_bytes).hexdigest()
    html_digest = hashlib.sha256(SANITIZED_HTML_BYTES).hexdigest()
    normalized_key = (
        f"raw-captures/amazon/us/{asin}/2026/07/14/run-1/{normalized_digest}/normalized.json"
    )
    html_key = f"raw-captures/amazon/us/{asin}/2026/07/14/run-1/{html_digest}/page.html.gz"
    normalized_ref = {
        "capture_kind": "normalized_capture",
        "bucket": "artifacts",
        "object_key": normalized_key,
        "content_digest": normalized_digest,
        "content_type": "application/json",
        "sanitization_status": "normalized",
        "request_id": "request-1",
        "execution_id": "browser-execution-1",
        "run_id": "run-1",
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
        for position, image in enumerate(media.get("gallery_images", [])):
            source_url = str(image.get("url") or "") if isinstance(image, dict) else ""
            if not source_url:
                continue
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
                "content_digest": html_digest,
                "content_type": "application/gzip",
                "sanitization_status": "sanitized",
                "request_id": "request-1",
                "execution_id": "browser-execution-1",
                "run_id": "run-1",
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
    media_bytes = b"\xff\xd8\xff\xe0" + (
        f"amazon-media:{asin}:{media_role}:{position}:{source_url}".encode()
    )
    content_digest = hashlib.sha256(media_bytes).hexdigest()
    file_name = f"{content_digest}.jpg"
    object_key = f"product-media/amazon/us/{asin}/{media_role}/{file_name}"
    MATERIALIZED_MEDIA_BLOBS[("artifacts", object_key)] = media_bytes
    return {
        "source_url": source_url,
        "asset_key": f"content_sha256:{content_digest}",
        "content_digest": content_digest,
        "bucket": "artifacts",
        "object_key": object_key,
        "remote_uri": f"s3://artifacts/{object_key}",
        "file_name": file_name,
        "mime_type": "image/jpeg",
        "size_bytes": len(media_bytes),
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


def test_field_evidence_paths_match_the_machine_fact_contract() -> None:
    contract_path = (
        Path(__file__).resolve().parents[1] / "contracts" / "facts" / "product-fact-collection.yaml"
    )
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    target_fields = contract["platform_contracts"]["amazon_us"]["field_evidence_policy"][
        "target_fields"
    ]

    assert set(target_fields) == fact_handler_module._REQUIRED_FIELD_EVIDENCE_PATHS


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


def test_handler_checks_fact_schema_before_reading_artifacts(runtime_db_url) -> None:
    capture_bytes = _capture_bytes(
        "product_detail_child.html",
        asin="B0CHILD001",
        resolved_url="https://www.amazon.com/dp/B0CHILD001",
    )
    payload = _payload(capture_bytes)
    artifact_store = FakeArtifactStore(
        {("artifacts", payload["normalized_capture_ref"]["object_key"]): capture_bytes}
    )
    engine = create_engine(runtime_db_url, future=True)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE fact_alembic_version SET version_num = 'outdated_revision'")
            )
    finally:
        engine.dispose()

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
    assert result.error.error_code == "amazon_fact_schema_not_ready"
    assert result.result["required_fact_schema_revision"] == "20260714_0007"
    assert artifact_store.read_calls == []


def test_handler_retries_when_fact_schema_revision_query_is_temporarily_unavailable() -> None:
    class UnavailableFactStore:
        def require_schema_revision(self) -> str:
            raise fact_handler_module.AmazonFactSchemaUnavailableError(
                "temporary database connection failure"
            )

    capture_bytes = _capture_bytes(
        "product_detail_child.html",
        asin="B0CHILD001",
        resolved_url="https://www.amazon.com/dp/B0CHILD001",
    )
    payload = _payload(capture_bytes)
    artifact_store = FakeArtifactStore(
        {("artifacts", payload["normalized_capture_ref"]["object_key"]): capture_bytes}
    )

    result = amazon_product_fact_upsert_handler(
        _context(
            payload,
            metadata={
                "fact_store": UnavailableFactStore(),
                "artifact_store": artifact_store,
            },
        )
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "amazon_fact_schema_check_failed"
    assert result.error.retryable is True
    assert artifact_store.read_calls == []


def test_handler_closes_fact_store_it_constructs_on_early_failure(monkeypatch) -> None:
    class ClosingFactStore:
        closed = False

        def require_schema_revision(self) -> str:
            return "20260714_0007"

        def close(self) -> None:
            self.closed = True

    fact_store = ClosingFactStore()
    monkeypatch.setattr(
        fact_handler_module,
        "_resolve_fact_store",
        lambda _context, _payload: fact_store,
    )
    monkeypatch.setattr(
        fact_handler_module,
        "_resolve_artifact_store",
        lambda _context, _payload: None,
    )

    result = amazon_product_fact_upsert_handler(_context({}))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "object_storage_required"
    assert fact_store.closed is True


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


@pytest.mark.parametrize(
    ("capture_kind", "content_type", "sanitization_status"),
    [
        ("network_data", "application/json", "raw"),
        ("arbitrary_secret_blob", "application/octet-stream", "unknown"),
    ],
)
def test_handler_rejects_raw_capture_outside_the_governed_contract_before_reading(
    runtime_db_url,
    capture_kind: str,
    content_type: str,
    sanitization_status: str,
) -> None:
    capture_bytes = _capture_bytes(
        "product_detail_child.html",
        asin="B0CHILD001",
        resolved_url="https://www.amazon.com/dp/B0CHILD001",
    )
    payload = _payload(capture_bytes)
    extra_digest = hashlib.sha256(capture_kind.encode("utf-8")).hexdigest()
    payload["raw_capture_refs"].append(
        {
            "capture_kind": capture_kind,
            "bucket": "artifacts",
            "object_key": (
                "raw-captures/amazon/us/B0CHILD001/2026/07/14/run-1/"
                f"{extra_digest}/{capture_kind}.json"
            ),
            "content_digest": extra_digest,
            "content_type": content_type,
            "sanitization_status": sanitization_status,
            "request_id": "request-1",
            "execution_id": "browser-execution-1",
            "run_id": "run-1",
        }
    )
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


def test_handler_persists_allowlisted_network_data_evidence(runtime_db_url) -> None:
    capture_bytes = _capture_bytes(
        "product_detail_child.html",
        asin="B0CHILD001",
        resolved_url="https://www.amazon.com/dp/B0CHILD001",
    )
    payload = _payload(capture_bytes)
    network_bytes = _network_capture_bytes()
    network_digest = hashlib.sha256(network_bytes).hexdigest()
    payload["raw_capture_refs"].append(
        {
            "capture_kind": "network_data",
            "bucket": "artifacts",
            "object_key": (
                "raw-captures/amazon/us/B0CHILD001/2026/07/14/run-1/"
                f"{network_digest}/page-data.json"
            ),
            "content_digest": network_digest,
            "content_type": "application/json",
            "sanitization_status": "allowlisted",
            "request_id": "request-1",
            "execution_id": "browser-execution-1",
            "run_id": "run-1",
        }
    )
    normalized_ref = payload["normalized_capture_ref"]
    network_ref = payload["raw_capture_refs"][-1]
    artifact_store = FakeArtifactStore(
        {
            ("artifacts", normalized_ref["object_key"]): capture_bytes,
            ("artifacts", network_ref["object_key"]): network_bytes,
        }
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

    assert result.status == "success"
    assert result.result["persisted_counts"]["raw_captures"] == 3
    assert (
        _scalar(
            runtime_db_url,
            "SELECT sanitization_status FROM amazon_raw_captures "
            "WHERE capture_kind = 'network_data'",
        )
        == "allowlisted"
    )


def test_handler_rejects_missing_html_object_before_any_fact_write(runtime_db_url) -> None:
    capture_bytes = _capture_bytes(
        "product_detail_child.html",
        asin="B0CHILD001",
        resolved_url="https://www.amazon.com/dp/B0CHILD001",
    )
    payload = _payload(capture_bytes)
    normalized_ref = payload["normalized_capture_ref"]
    artifact_store = FakeArtifactStore(
        {("artifacts", normalized_ref["object_key"]): capture_bytes},
        default_html_bytes=None,
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
    assert result.error.error_code == "raw_capture_read_failed"
    assert result.error.retryable is True
    assert _count(runtime_db_url, "amazon_products") == 0


def test_handler_rejects_html_digest_mismatch_before_any_fact_write(runtime_db_url) -> None:
    capture_bytes = _capture_bytes(
        "product_detail_child.html",
        asin="B0CHILD001",
        resolved_url="https://www.amazon.com/dp/B0CHILD001",
    )
    payload = _payload(capture_bytes)
    normalized_ref = payload["normalized_capture_ref"]
    html_ref = payload["raw_capture_refs"][1]
    artifact_store = FakeArtifactStore(
        {
            ("artifacts", normalized_ref["object_key"]): capture_bytes,
            ("artifacts", html_ref["object_key"]): b"tampered-html",
        }
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
    assert result.error.error_code == "raw_capture_digest_mismatch"
    assert _count(runtime_db_url, "amazon_products") == 0


@pytest.mark.parametrize(
    "html_bytes",
    [
        b"not-a-gzip-stream",
        gzip.compress(
            b'<html><body><input value="private"><h1>Product</h1></body></html>',
            mtime=0,
        ),
        gzip.compress(
            b'<html><body><div data-token="private-token">Product</div></body></html>',
            mtime=0,
        ),
        gzip.compress(
            b'<html><body><p id="shippingAddress">Jane Doe, 123 Main St</p></body></html>',
            mtime=0,
        ),
        gzip.compress(
            b'<html><body><script>window.secret="private"</script></body></html>',
            mtime=0,
        ),
    ],
)
def test_handler_rejects_invalid_or_unsanitized_html_before_any_fact_write(
    runtime_db_url,
    html_bytes: bytes,
) -> None:
    capture_bytes = _capture_bytes(
        "product_detail_child.html",
        asin="B0CHILD001",
        resolved_url="https://www.amazon.com/dp/B0CHILD001",
    )
    payload = _payload(capture_bytes)
    normalized_ref = payload["normalized_capture_ref"]
    html_ref = payload["raw_capture_refs"][1]
    html_digest = hashlib.sha256(html_bytes).hexdigest()
    html_ref["content_digest"] = html_digest
    html_ref["object_key"] = (
        f"raw-captures/amazon/us/B0CHILD001/2026/07/14/run-1/{html_digest}/page.html.gz"
    )
    artifact_store = FakeArtifactStore(
        {
            ("artifacts", normalized_ref["object_key"]): capture_bytes,
            ("artifacts", html_ref["object_key"]): html_bytes,
        }
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
    assert result.error.error_code == "raw_capture_digest_mismatch"
    assert result.error.retryable is False
    assert _count(runtime_db_url, "amazon_products") == 0


def test_handler_rejects_html_that_expands_beyond_the_decompressed_limit(
    runtime_db_url,
) -> None:
    capture_bytes = _capture_bytes(
        "product_detail_child.html",
        asin="B0CHILD001",
        resolved_url="https://www.amazon.com/dp/B0CHILD001",
    )
    payload = _payload(capture_bytes)
    oversized_html = gzip.compress(
        b"<html><body>"
        + b"A" * (fact_handler_module._MAX_SANITIZED_HTML_BYTES + 1)
        + b"</body></html>",
        mtime=0,
    )
    html_ref = payload["raw_capture_refs"][1]
    html_digest = hashlib.sha256(oversized_html).hexdigest()
    html_ref["content_digest"] = html_digest
    html_ref["object_key"] = (
        f"raw-captures/amazon/us/B0CHILD001/2026/07/14/run-1/{html_digest}/page.html.gz"
    )
    normalized_ref = payload["normalized_capture_ref"]
    artifact_store = FakeArtifactStore(
        {
            ("artifacts", normalized_ref["object_key"]): capture_bytes,
            ("artifacts", html_ref["object_key"]): oversized_html,
        }
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
    assert result.error.error_code == "raw_capture_digest_mismatch"
    assert _count(runtime_db_url, "amazon_products") == 0


def test_handler_rejects_raw_object_key_from_another_run_before_fact_write(
    runtime_db_url,
) -> None:
    capture_bytes = _capture_bytes(
        "product_detail_child.html",
        asin="B0CHILD001",
        resolved_url="https://www.amazon.com/dp/B0CHILD001",
    )
    payload = _payload(capture_bytes)
    html_ref = payload["raw_capture_refs"][1]
    html_ref["object_key"] = html_ref["object_key"].replace("/run-1/", "/old-run/")
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

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "invalid_amazon_capture"
    assert artifact_store.read_calls == [
        ("artifacts", normalized_ref["object_key"]),
    ]
    assert _count(runtime_db_url, "amazon_products") == 0


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("request_id", "another-request"),
        ("execution_id", "another-browser-execution"),
        ("run_id", "another-run"),
    ],
)
def test_handler_rejects_inconsistent_raw_capture_provenance_before_fact_write(
    runtime_db_url,
    field_name: str,
    invalid_value: str,
) -> None:
    capture_bytes = _capture_bytes(
        "product_detail_child.html",
        asin="B0CHILD001",
        resolved_url="https://www.amazon.com/dp/B0CHILD001",
    )
    payload = _payload(capture_bytes)
    payload["raw_capture_refs"][1][field_name] = invalid_value
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

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "invalid_amazon_capture"
    assert artifact_store.read_calls == [("artifacts", normalized_ref["object_key"])]
    assert _count(runtime_db_url, "amazon_products") == 0


def test_handler_rejects_network_data_outside_canonical_allowlist(runtime_db_url) -> None:
    capture_bytes = _capture_bytes(
        "product_detail_child.html",
        asin="B0CHILD001",
        resolved_url="https://www.amazon.com/dp/B0CHILD001",
    )
    payload = _payload(capture_bytes)
    network_payload = json.loads(_network_capture_bytes())
    network_payload["secret"] = "must-not-cross-the-boundary"
    network_bytes = json.dumps(
        network_payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    network_digest = hashlib.sha256(network_bytes).hexdigest()
    network_ref = {
        "capture_kind": "network_data",
        "bucket": "artifacts",
        "object_key": (
            f"raw-captures/amazon/us/B0CHILD001/2026/07/14/run-1/{network_digest}/page-data.json"
        ),
        "content_digest": network_digest,
        "content_type": "application/json",
        "sanitization_status": "allowlisted",
        "request_id": "request-1",
        "execution_id": "browser-execution-1",
        "run_id": "run-1",
    }
    payload["raw_capture_refs"].append(network_ref)
    normalized_ref = payload["normalized_capture_ref"]
    artifact_store = FakeArtifactStore(
        {
            ("artifacts", normalized_ref["object_key"]): capture_bytes,
            ("artifacts", network_ref["object_key"]): network_bytes,
        }
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
    assert result.error.error_code == "raw_capture_digest_mismatch"
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


def test_capture_validator_accepts_legacy_revision_1_promotion_texts() -> None:
    capture = json.loads(
        _capture_bytes(
            "product_detail_child.html",
            asin="B0CHILD001",
            resolved_url="https://www.amazon.com/dp/B0CHILD001",
        )
    )
    legacy_promotions = [
        item["raw_text"] for item in capture["commerce"]["featured_offer"]["promotions"]
    ]
    capture["contract_revision"] = 1
    capture["commerce"]["featured_offer"]["promotions"] = legacy_promotions
    capture["field_evidence"]["commerce.featured_offer.promotions"]["value"] = legacy_promotions

    validated = fact_handler_module._decode_and_validate_capture(
        json.dumps(capture, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )

    assert validated["contract_revision"] == 1
    assert validated["commerce"]["featured_offer"]["promotions"] == legacy_promotions


def test_capture_validator_adapts_legacy_revision_2_thumbnail_urls() -> None:
    capture = json.loads(
        _capture_bytes(
            "product_detail_child.html",
            asin="B0CHILD001",
            resolved_url="https://www.amazon.com/dp/B0CHILD001",
        )
    )
    derivative_url = "https://m.media-amazon.com/images/I/legacy-gallery._AC_US40_.jpg"
    capture["contract_revision"] = 2
    capture["media"]["gallery_images"][0]["url"] = derivative_url
    capture["field_evidence"]["media.gallery_images"]["value"][0]["url"] = derivative_url

    validated = fact_handler_module._decode_and_validate_capture(
        json.dumps(capture, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )

    expected_url = "https://m.media-amazon.com/images/I/legacy-gallery.jpg"
    assert validated["contract_revision"] == 2
    assert validated["media"]["gallery_images"][0]["url"] == expected_url
    assert validated["field_evidence"]["media.gallery_images"]["value"][0]["url"] == expected_url


def test_capture_validator_rejects_sensitive_revision_2_promotion_text() -> None:
    capture = json.loads(
        _capture_bytes(
            "product_detail_child.html",
            asin="B0CHILD001",
            resolved_url="https://www.amazon.com/dp/B0CHILD001",
        )
    )
    promotions = capture["commerce"]["featured_offer"]["promotions"]
    promotions[0]["raw_text"] = "Apply 10% coupon token=must-not-persist"
    capture["field_evidence"]["commerce.featured_offer.promotions"]["value"] = promotions

    with pytest.raises(ValueError, match="sensitive promotion text"):
        fact_handler_module._decode_and_validate_capture(
            json.dumps(capture, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )


def test_capture_validator_rejects_non_whitelisted_revision_2_promotion() -> None:
    capture = json.loads(
        _capture_bytes(
            "product_detail_child.html",
            asin="B0CHILD001",
            resolved_url="https://www.amazon.com/dp/B0CHILD001",
        )
    )
    promotions = capture["commerce"]["featured_offer"]["promotions"]
    promotions[0]["promotion_type"] = "checkout_discount"
    capture["field_evidence"]["commerce.featured_offer.promotions"]["value"] = promotions

    with pytest.raises(ValueError, match="promotion_type is invalid"):
        fact_handler_module._decode_and_validate_capture(
            json.dumps(capture, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )


def test_handler_rejects_capture_with_incomplete_field_evidence_before_any_fact_write(
    runtime_db_url,
) -> None:
    capture_bytes = _capture_bytes(
        "product_detail_child.html",
        asin="B0CHILD001",
        resolved_url="https://www.amazon.com/dp/B0CHILD001",
    )
    capture = json.loads(capture_bytes)
    capture["field_evidence"] = {}
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


@pytest.mark.parametrize("metadata_key", ["source_kind", "source_locator", "confidence"])
def test_handler_requires_complete_field_evidence_source_metadata(
    runtime_db_url,
    metadata_key: str,
) -> None:
    capture = json.loads(
        _capture_bytes(
            "product_detail_child.html",
            asin="B0CHILD001",
            resolved_url="https://www.amazon.com/dp/B0CHILD001",
        )
    )
    capture["field_evidence"]["product.title"].pop(metadata_key)
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


def test_handler_rejects_field_evidence_value_that_differs_from_capture(
    runtime_db_url,
) -> None:
    capture = json.loads(
        _capture_bytes(
            "product_detail_child.html",
            asin="B0CHILD001",
            resolved_url="https://www.amazon.com/dp/B0CHILD001",
        )
    )
    capture["field_evidence"]["product.title"]["value"] = "Another title"
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


def test_handler_rejects_missing_evidence_for_a_present_capture_value(runtime_db_url) -> None:
    capture = json.loads(
        _capture_bytes(
            "product_detail_child.html",
            asin="B0CHILD001",
            resolved_url="https://www.amazon.com/dp/B0CHILD001",
        )
    )
    title_evidence = capture["field_evidence"]["product.title"]
    title_evidence["status"] = "missing"
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
    payload["normalized_capture_ref"]["object_key"] = (
        f"raw-captures/amazon/us/B0CHILD001/2026/07/14/run-1/{'0' * 64}/normalized.json"
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
        "expected": 4,
        "materialized": 3,
        "missing": 1,
        "complete": False,
    }
    assert result.result["persisted_counts"]["media_assets"] == 3
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
    assert "侧边栏图片" not in command["fields"]
    assert _count(runtime_db_url, "amazon_products") == 1
    assert _count(runtime_db_url, "amazon_media_assets") == 3
    assert _count(runtime_db_url, "amazon_product_media_assets") == 3


def test_handler_accepts_partial_capture_with_observed_subset_and_no_missing_evidence(
    runtime_db_url,
) -> None:
    capture_bytes = _capture_bytes(
        "product_detail_child.html",
        asin="B0CHILD001",
        resolved_url="https://www.amazon.com/dp/B0CHILD001",
    )
    capture = json.loads(capture_bytes)
    assert all(item["status"] != "missing" for item in capture["field_evidence"].values())
    capture["collection_status"] = "partial_success"
    partial_bytes = json.dumps(capture, sort_keys=True, separators=(",", ":")).encode()
    payload = _payload(partial_bytes)
    artifact_store = FakeArtifactStore(
        {("artifacts", payload["normalized_capture_ref"]["object_key"]): partial_bytes}
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

    assert result.status == "partial_success"
    assert result.summary["collection_status"] == "partial_success"


def test_handler_retries_when_materialized_media_object_is_missing(runtime_db_url) -> None:
    class MissingMediaArtifactStore(FakeArtifactStore):
        def read_bytes(self, *, bucket: str, object_key: str) -> bytes:
            if "/product-media/amazon/" in f"/{object_key}":
                raise KeyError((bucket, object_key))
            return super().read_bytes(bucket=bucket, object_key=object_key)

    capture_bytes = _capture_bytes(
        "product_detail_child.html",
        asin="B0CHILD001",
        resolved_url="https://www.amazon.com/dp/B0CHILD001",
    )
    payload = _payload(capture_bytes)
    artifact_store = MissingMediaArtifactStore(
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
    assert result.error.error_code == "materialized_media_read_failed"
    assert result.error.retryable is True
    assert _count(runtime_db_url, "amazon_products") == 0


def test_handler_rejects_materialized_media_bytes_that_do_not_match_digest(
    runtime_db_url,
) -> None:
    capture_bytes = _capture_bytes(
        "product_detail_child.html",
        asin="B0CHILD001",
        resolved_url="https://www.amazon.com/dp/B0CHILD001",
    )
    payload = _payload(capture_bytes)
    first_asset = payload["materialized_media_assets"][0]
    artifact_store = FakeArtifactStore(
        {
            ("artifacts", payload["normalized_capture_ref"]["object_key"]): capture_bytes,
            (first_asset["bucket"], first_asset["object_key"]): b"tampered-image-bytes",
        }
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
    assert result.error.error_code == "materialized_media_digest_mismatch"
    assert result.error.retryable is False
    assert _count(runtime_db_url, "amazon_products") == 0


def test_same_run_partial_media_retry_converges_to_full_without_duplicate_facts(
    runtime_db_url,
) -> None:
    capture_bytes = _capture_bytes(
        "product_detail_child.html",
        asin="B0CHILD001",
        resolved_url="https://www.amazon.com/dp/B0CHILD001",
    )
    partial_payload = _payload(capture_bytes)
    partial_payload["materialized_media_assets"] = partial_payload["materialized_media_assets"][:-1]
    full_payload = _payload(capture_bytes)
    normalized_ref = full_payload["normalized_capture_ref"]
    artifact_store = FakeArtifactStore({("artifacts", normalized_ref["object_key"]): capture_bytes})
    metadata = {
        "fact_store": AmazonFactStore(db_url=runtime_db_url),
        "artifact_store": artifact_store,
    }

    partial = amazon_product_fact_upsert_handler(_context(partial_payload, metadata=metadata))
    complete = amazon_product_fact_upsert_handler(_context(full_payload, metadata=metadata))

    assert partial.status == "partial_success"
    assert partial.summary["media_coverage"]["complete"] is False
    assert complete.status == "success"
    assert complete.summary["media_coverage"] == {
        "expected": 4,
        "materialized": 4,
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
    assert _count(runtime_db_url, "amazon_media_assets") == 4
    assert _count(runtime_db_url, "amazon_product_media_assets") == 4
    assert _count(runtime_db_url, "amazon_raw_captures") == 2
    assert _count(runtime_db_url, "amazon_feishu_bindings") == 1
    assert (
        _scalar(
            runtime_db_url,
            "SELECT request_id FROM amazon_product_snapshots WHERE asin = 'B0CHILD001'",
        )
        == "request-1"
    )
    assert (
        _scalar(
            runtime_db_url,
            "SELECT execution_id FROM amazon_product_snapshots WHERE asin = 'B0CHILD001'",
        )
        == "browser-execution-1"
    )
    assert (
        _scalar(
            runtime_db_url,
            "SELECT COUNT(*) FROM amazon_raw_captures "
            "WHERE request_id = 'request-1' "
            "AND execution_id = 'browser-execution-1' AND run_id = 'run-1'",
        )
        == 2
    )


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
    payload["materialized_media_assets"].append(dict(payload["materialized_media_assets"][0]))
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


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "https://evil.example/image.jpg",
        "https://m.media-amazon.com/images/I/image.jpg?token=must-not-persist",
    ],
)
def test_handler_rejects_ungoverned_capture_media_before_fact_write(
    runtime_db_url,
    unsafe_url: str,
) -> None:
    capture_bytes = _capture_bytes(
        "product_detail_child.html",
        asin="B0CHILD001",
        resolved_url="https://www.amazon.com/dp/B0CHILD001",
    )
    capture = json.loads(capture_bytes)
    capture["media"]["main_image"] = {"url": unsafe_url}
    capture["field_evidence"]["media.main_image"]["value"] = {"url": unsafe_url}
    invalid_bytes = json.dumps(capture, sort_keys=True, separators=(",", ":")).encode()
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
        "media_assets": 4,
        "media_relations": 4,
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
    assert _count(runtime_db_url, "amazon_media_assets") == 4
    assert _count(runtime_db_url, "amazon_product_media_assets") == 4
    assert _count(runtime_db_url, "amazon_raw_captures") == 2
    assert _count(runtime_db_url, "amazon_feishu_bindings") == 1


def test_handler_rolls_back_the_fact_bundle_when_final_publish_fails(
    runtime_db_url,
    monkeypatch,
) -> None:
    capture_bytes = _capture_bytes(
        "product_detail_child.html",
        asin="B0CHILD001",
        resolved_url="https://www.amazon.com/dp/B0CHILD001",
    )
    payload = _payload(capture_bytes)
    normalized_ref = payload["normalized_capture_ref"]
    artifact_store = FakeArtifactStore({("artifacts", normalized_ref["object_key"]): capture_bytes})
    fact_store = AmazonFactStore(db_url=runtime_db_url)

    def fail_final_publish(*_args, **_kwargs):
        raise RuntimeError("injected final publish failure")

    monkeypatch.setattr(AmazonFactStore, "set_latest_snapshot", fail_final_publish)
    result = amazon_product_fact_upsert_handler(
        _context(
            payload,
            metadata={"fact_store": fact_store, "artifact_store": artifact_store},
        )
    )
    fact_store.close()

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "amazon_product_fact_upsert_failed"
    assert result.error.retryable is True
    for table_name in (
        "amazon_products",
        "amazon_product_snapshots",
        "amazon_offer_snapshots",
        "amazon_product_variants",
        "amazon_bsr_snapshots",
        "amazon_media_assets",
        "amazon_product_media_assets",
        "amazon_raw_captures",
        "amazon_feishu_bindings",
    ):
        assert _count(runtime_db_url, table_name) == 0


def test_handler_rejects_divergent_capture_for_the_same_run_before_mutable_fact_updates(
    runtime_db_url,
) -> None:
    capture_bytes = _capture_bytes(
        "product_detail_child.html",
        asin="B0CHILD001",
        resolved_url="https://www.amazon.com/dp/B0CHILD001",
    )
    first_payload = _payload(capture_bytes)
    first_ref = first_payload["normalized_capture_ref"]
    first = amazon_product_fact_upsert_handler(
        _context(
            first_payload,
            metadata={
                "fact_store": AmazonFactStore(db_url=runtime_db_url),
                "artifact_store": FakeArtifactStore(
                    {("artifacts", first_ref["object_key"]): capture_bytes}
                ),
            },
        )
    )
    assert first.status == "success"

    changed_capture = json.loads(capture_bytes)
    changed_capture["product"]["title"] = "Divergent retry title"
    changed_capture["field_evidence"]["product.title"]["value"] = "Divergent retry title"
    changed_bytes = json.dumps(
        changed_capture,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    changed_payload = _payload(changed_bytes)
    changed_ref = changed_payload["normalized_capture_ref"]

    conflict = amazon_product_fact_upsert_handler(
        _context(
            changed_payload,
            metadata={
                "fact_store": AmazonFactStore(db_url=runtime_db_url),
                "artifact_store": FakeArtifactStore(
                    {("artifacts", changed_ref["object_key"]): changed_bytes}
                ),
            },
        )
    )

    assert conflict.status == "failed"
    assert conflict.error is not None
    assert conflict.error.error_code == "same_run_capture_conflict"
    assert conflict.error.retryable is False
    assert (
        _scalar(
            runtime_db_url,
            "SELECT title FROM amazon_products WHERE asin = 'B0CHILD001'",
        )
        == "Structured product title"
    )
    assert _count(runtime_db_url, "amazon_products") == 1
    assert _count(runtime_db_url, "amazon_product_snapshots") == 1
    assert _count(runtime_db_url, "amazon_offer_snapshots") == 1
    assert _count(runtime_db_url, "amazon_product_variants") == 2
    assert _count(runtime_db_url, "amazon_bsr_snapshots") == 2
    assert _count(runtime_db_url, "amazon_media_assets") == 4
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
    capture["field_evidence"]["commerce.availability_status"]["value"] = "unknown"
    capture["variants"]["current_attributes"] = {}
    capture["field_evidence"]["variants.current_attributes"]["status"] = "missing"
    capture["field_evidence"]["variants.current_attributes"]["value"] = {}
    capture["variants"]["dimensions"] = {}
    capture["field_evidence"]["variants.dimensions"]["status"] = "missing"
    capture["field_evidence"]["variants.dimensions"]["value"] = {}
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


def test_minio_read_bytes_enforces_limit_and_releases_response() -> None:
    class Response:
        def __init__(self) -> None:
            self.closed = False
            self.released = False
            self.read_sizes: list[int] = []

        def read(self, size: int = -1) -> bytes:
            self.read_sizes.append(size)
            return b"123456"

        def close(self) -> None:
            self.closed = True

        def release_conn(self) -> None:
            self.released = True

    class Client:
        def __init__(self) -> None:
            self.response = Response()

        def get_object(self, bucket: str, object_key: str) -> Response:
            return self.response

    client = Client()
    store = object.__new__(MinioArtifactStore)
    store._client = client  # noqa: SLF001

    with pytest.raises(ValueError, match="size limit"):
        store.read_bytes(
            bucket="artifacts",
            object_key="capture.json",
            max_bytes=5,
        )

    assert client.response.read_sizes == [6]
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
