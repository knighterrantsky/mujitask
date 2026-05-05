from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

import automation_business_scaffold.control_plane.executor.runner as runtime_orchestrator
from automation_business_scaffold.capabilities.media.asset_sync_handler import media_asset_sync_handler
from automation_business_scaffold.capabilities.persistence.database.fact_bundle_upsert_handler import (
    fact_bundle_upsert_handler,
)
from automation_business_scaffold.contracts.handler.api import build_api_handler_registry, register_api_handler
from automation_business_scaffold.contracts.handler.contract import HandlerContext, HandlerResult
from automation_business_scaffold.domains.tiktok.tasks.tiktok_fastmoss_product_ingest import (
    TikTokFastMossProductIngestTask,
)
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore

PRODUCT_ID = "1731047720588251844"
PRODUCT_URL = f"https://www.tiktok.com/shop/pdp/{PRODUCT_ID}"


def _env_first(*keys: str) -> str:
    for key in keys:
        value = str(os.getenv(key, "") or "").strip()
        if value:
            return value
    return ""


def _real_artifact_config() -> dict[str, str]:
    provider = _env_first(
        "TEST_ARTIFACT_STORE_PROVIDER",
        "BUSINESS_EXECUTION_CONTROL_ARTIFACT_STORE_PROVIDER",
        "EXECUTION_CONTROL_ARTIFACT_STORE_PROVIDER",
    )
    bucket = _env_first(
        "TEST_ARTIFACT_BUCKET",
        "BUSINESS_EXECUTION_CONTROL_ARTIFACT_BUCKET",
        "EXECUTION_CONTROL_ARTIFACT_BUCKET",
    )
    endpoint = _env_first(
        "TEST_MINIO_ENDPOINT",
        "BUSINESS_EXECUTION_CONTROL_MINIO_ENDPOINT",
        "EXECUTION_CONTROL_MINIO_ENDPOINT",
    )
    access_key = _env_first(
        "TEST_MINIO_ACCESS_KEY",
        "BUSINESS_EXECUTION_CONTROL_MINIO_ACCESS_KEY",
        "EXECUTION_CONTROL_MINIO_ACCESS_KEY",
    )
    secret_key = _env_first(
        "TEST_MINIO_SECRET_KEY",
        "BUSINESS_EXECUTION_CONTROL_MINIO_SECRET_KEY",
        "EXECUTION_CONTROL_MINIO_SECRET_KEY",
    )
    if provider != "minio" or not bucket or not endpoint or not access_key or not secret_key:
        pytest.skip("Real MinIO/S3 integration config is required for this test.")
    return {
        "execution_control_artifact_store_provider": provider,
        "execution_control_artifact_bucket": bucket,
        "execution_control_artifact_object_prefix": _env_first(
            "TEST_ARTIFACT_OBJECT_PREFIX",
            "BUSINESS_EXECUTION_CONTROL_ARTIFACT_OBJECT_PREFIX",
            "EXECUTION_CONTROL_ARTIFACT_OBJECT_PREFIX",
        )
        or "pytest/real-skill-submit",
        "execution_control_minio_endpoint": endpoint,
        "execution_control_minio_access_key": access_key,
        "execution_control_minio_secret_key": secret_key,
        "execution_control_minio_region": _env_first(
            "TEST_MINIO_REGION",
            "BUSINESS_EXECUTION_CONTROL_MINIO_REGION",
            "EXECUTION_CONTROL_MINIO_REGION",
        ),
        "execution_control_minio_secure": _env_first(
            "TEST_MINIO_SECURE",
            "BUSINESS_EXECUTION_CONTROL_MINIO_SECURE",
            "EXECUTION_CONTROL_MINIO_SECURE",
        )
        or "false",
        "execution_control_minio_create_bucket": _env_first(
            "TEST_MINIO_CREATE_BUCKET",
            "BUSINESS_EXECUTION_CONTROL_MINIO_CREATE_BUCKET",
            "EXECUTION_CONTROL_MINIO_CREATE_BUCKET",
        )
        or "true",
    }


def _runtime_params(runtime_db_url: str, artifact_config: dict[str, str], **overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "allow_test_persistence_overrides": True,
        "execution_control_db_url": runtime_db_url,
        "fact_db_url": runtime_db_url,
        "execution_control_stop_when_idle": True,
        "execution_control_max_iterations": 1,
        "requested_by": "pytest-real-submit",
        **artifact_config,
    }
    params.update(overrides)
    return params


def test_real_skill_submit_requires_and_uses_database_and_object_storage(
    runtime_db_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_config = _real_artifact_config()
    monkeypatch.setenv("TK_FACT_DB_URL", runtime_db_url)
    monkeypatch.setenv("BUSINESS_EXECUTION_CONTROL_ARTIFACT_STORE_PROVIDER", artifact_config["execution_control_artifact_store_provider"])
    monkeypatch.setenv("BUSINESS_EXECUTION_CONTROL_ARTIFACT_BUCKET", artifact_config["execution_control_artifact_bucket"])
    monkeypatch.setenv(
        "BUSINESS_EXECUTION_CONTROL_ARTIFACT_OBJECT_PREFIX",
        artifact_config["execution_control_artifact_object_prefix"],
    )
    monkeypatch.setenv("BUSINESS_EXECUTION_CONTROL_MINIO_ENDPOINT", artifact_config["execution_control_minio_endpoint"])
    monkeypatch.setenv("BUSINESS_EXECUTION_CONTROL_MINIO_ACCESS_KEY", artifact_config["execution_control_minio_access_key"])
    monkeypatch.setenv("BUSINESS_EXECUTION_CONTROL_MINIO_SECRET_KEY", artifact_config["execution_control_minio_secret_key"])
    monkeypatch.setenv("BUSINESS_EXECUTION_CONTROL_MINIO_REGION", artifact_config["execution_control_minio_region"])
    monkeypatch.setenv("BUSINESS_EXECUTION_CONTROL_MINIO_SECURE", artifact_config["execution_control_minio_secure"])
    monkeypatch.setenv("BUSINESS_EXECUTION_CONTROL_MINIO_CREATE_BUCKET", artifact_config["execution_control_minio_create_bucket"])
    task = TikTokFastMossProductIngestTask()
    submitted = task.run_runtime_request(
        _runtime_params(
            runtime_db_url,
            artifact_config,
            control_action="submit",
            product_url=PRODUCT_URL,
            product_id=PRODUCT_ID,
            fallback_allowed=True,
            source_channel_code="console",
            reply_target="reply://real-skill-submit",
        )
    )
    request_id = str(submitted["request_id"])
    store = RuntimeStore(db_url=runtime_db_url)
    request_payload = store.load_task_request(request_id=request_id).payload

    assert request_payload["requires_fact_db"] is True
    assert request_payload["requires_object_storage"] is True
    assert request_payload["persistence"]["fact_db_configured"] is True
    assert request_payload["artifact_store"]["provider"] == "minio"
    assert "fact_db_url" not in request_payload
    assert "execution_control_db_url" not in request_payload
    assert "execution_control_minio_secret_key" not in request_payload

    dispatched = runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url, artifact_config))
    row_job = next(job for job in dispatched["api_worker_jobs"] if job["job_code"] == "selection_row_refresh")
    row_payload = row_job["payload"]
    assert row_payload["requires_fact_db"] is True
    assert row_payload["requires_object_storage"] is True
    assert row_payload["artifact_store"]["provider"] == "minio"
    assert "fact_db_url" not in row_payload
    assert "execution_control_minio_secret_key" not in row_payload

    media_file = tmp_path / "main.webp"
    media_file.write_bytes(b"fake-webp-bytes")
    registry = build_api_handler_registry()

    def fake_selection_row_refresh(context: HandlerContext) -> HandlerResult:
        media_context = HandlerContext(
            request_id=context.request_id,
            job_id=f"{context.job_id}:media",
            handler_code="media_asset_sync",
            worker_type="api_worker",
            runtime_table="api_worker_job",
            job_code="media_asset_sync",
            payload={
                **dict(context.payload),
                "asset_refs": [
                    {
                        "entity_type": "product",
                        "entity_external_id": PRODUCT_ID,
                        "media_role": "product_main_image",
                        "local_path": str(media_file),
                        "mime_type": "image/webp",
                    }
                ],
            },
        )
        media_result = media_asset_sync_handler(media_context)
        assert media_result.status == "success", media_result.error
        fact_context = HandlerContext(
            request_id=context.request_id,
            job_id=f"{context.job_id}:facts",
            handler_code="fact_bundle_upsert",
            worker_type="api_worker",
            runtime_table="api_worker_job",
            job_code="fact_bundle_upsert",
            payload={
                **dict(context.payload),
                "fact_bundle": {
                    "products": [{"product_id": PRODUCT_ID, "product_url": PRODUCT_URL}],
                    "media_assets": media_result.result["media_fact_bundle"]["media_assets"],
                },
            },
        )
        fact_result = fact_bundle_upsert_handler(fact_context)
        assert fact_result.status == "success", fact_result.error
        return HandlerResult.success(
            context,
            summary={"row_status": "success", "product_business_key": PRODUCT_ID},
            result={
                "row_status": "success",
                "fact_upsert": fact_result.result,
                "media_result": media_result.result,
            },
        )

    register_api_handler(registry, "selection_row_refresh", fake_selection_row_refresh)
    monkeypatch.setattr(runtime_orchestrator, "build_api_handler_registry", lambda: registry, raising=False)
    monkeypatch.setattr(runtime_orchestrator, "API_HANDLER_REGISTRY", registry, raising=False)

    worker_payload = runtime_orchestrator.execute_api_worker_once(_runtime_params(runtime_db_url, artifact_config))
    handler_result = worker_payload["api_worker_job"]["result"]["handler_result"]["result"]
    fact_upsert = handler_result["fact_upsert"]
    media_result = handler_result["media_result"]
    uploaded_asset = media_result["synced_assets"][0]

    assert fact_upsert["persistence_mode"] == "database"
    assert uploaded_asset["sync_state"] == "uploaded"
    assert uploaded_asset["object_key"]
    assert uploaded_asset["remote_uri"]
    assert uploaded_asset["sync_state"] != "linked_local"
