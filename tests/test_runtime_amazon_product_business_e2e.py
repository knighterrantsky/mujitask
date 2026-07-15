from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pytest
from sqlalchemy import create_engine, text

import automation_business_scaffold.capabilities.browser.amazon_product_fetch_handler as browser_handler_module
import automation_business_scaffold.capabilities.input_sources.feishu.table_common as feishu_common
import automation_business_scaffold.capabilities.media.asset_sync_handler as media_handler_module
import automation_business_scaffold.capabilities.persistence.database.amazon_product_fact_upsert_handler as fact_handler_module
import automation_business_scaffold.control_plane.executor.runner as runtime_orchestrator
from automation_business_scaffold.capabilities.browser.amazon.product_page import (
    AmazonAccessBlockedError,
    AmazonIdentityMismatchError,
    extract_amazon_product_capture,
)
from automation_business_scaffold.contracts.handler.contract import HandlerContext
from automation_business_scaffold.domains.amazon.jobs.amazon_product_row_persist import (
    amazon_product_row_persist_handler,
)
from automation_business_scaffold.infrastructure.artifacts.artifact_store import (
    StoredArtifact,
)
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore


TASK_CODE = "refresh_amazon_product_row_by_asin"
SOURCE_APP_TOKEN = "appAmazonToken"
SOURCE_TABLE_ID = "tblAmazon"
SOURCE_TABLE_URL = (
    f"https://muji.feishu.cn/base/{SOURCE_APP_TOKEN}?table={SOURCE_TABLE_ID}"
)
SOURCE_RECORD_ID = "rec-amazon-business-e2e"
ASIN = "B0CHILD001"
CANONICAL_URL = f"https://www.amazon.com/dp/{ASIN}"
ARTIFACT_BUCKET = "pytest-amazon-artifacts"
ARTIFACT_PREFIX = "pytest-business-e2e"
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "amazon"
OBSERVED_AT = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)

FACT_TABLES = (
    "amazon_products",
    "amazon_product_snapshots",
    "amazon_offer_snapshots",
    "amazon_product_variants",
    "amazon_bsr_snapshots",
    "amazon_media_assets",
    "amazon_product_media_assets",
    "amazon_raw_captures",
    "amazon_feishu_bindings",
)
SUCCESS_FACT_COUNTS = {
    "amazon_products": 1,
    "amazon_product_snapshots": 1,
    "amazon_offer_snapshots": 1,
    "amazon_product_variants": 2,
    "amazon_bsr_snapshots": 2,
    "amazon_media_assets": 3,
    "amazon_product_media_assets": 3,
    "amazon_raw_captures": 2,
    "amazon_feishu_bindings": 1,
}


class FakeObjectStore:
    provider_code = "minio"
    artifact_bucket = ARTIFACT_BUCKET
    artifact_object_prefix = ARTIFACT_PREFIX

    def __init__(self) -> None:
        self.blobs: dict[tuple[str, str], bytes] = {}
        self.upload_calls: list[dict[str, Any]] = []
        self.read_calls: list[tuple[str, str]] = []

    def upload_file(
        self,
        *,
        bucket: str,
        object_key: str,
        local_path: Path,
        content_type: str,
        metadata: dict[str, Any] | None = None,
    ) -> StoredArtifact:
        payload = local_path.read_bytes()
        self.blobs[(bucket, object_key)] = payload
        self.upload_calls.append(
            {
                "bucket": bucket,
                "object_key": object_key,
                "content_type": content_type,
                "metadata": dict(metadata or {}),
            }
        )
        return StoredArtifact(
            bucket=bucket,
            object_key=object_key,
            etag=hashlib.sha256(payload).hexdigest(),
            size=len(payload),
            content_type=content_type,
            uri=f"s3://{bucket}/{object_key}",
            metadata={"storage_backend": "fake"},
        )

    def read_bytes(self, *, bucket: str, object_key: str) -> bytes:
        self.read_calls.append((bucket, object_key))
        return self.blobs[(bucket, object_key)]

    def build_uri(self, *, bucket: str, object_key: str) -> str:
        return f"s3://{bucket}/{object_key}"


class FakeImageResponse:
    headers = {"Content-Type": "image/jpeg"}

    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self) -> FakeImageResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


def _runtime_params(runtime_db_url: str, **overrides: object) -> dict[str, object]:
    params: dict[str, object] = {
        "allow_test_persistence_overrides": True,
        "execution_control_db_url": runtime_db_url,
        "fact_db_url": runtime_db_url,
        "execution_control_artifact_store_provider": "minio",
        "execution_control_artifact_bucket": ARTIFACT_BUCKET,
        "execution_control_artifact_object_prefix": ARTIFACT_PREFIX,
        "execution_control_minio_endpoint": "127.0.0.1:9000",
        "execution_control_minio_access_key": "minioadmin",
        "execution_control_minio_secret_key": "miniosecret",
        "execution_control_db_health_preflight_enabled": False,
        "execution_child_runner_mode": "inline",
        "execution_control_stop_when_idle": True,
        "execution_control_max_iterations": 1,
        "requested_by": "pytest",
    }
    params.update(overrides)
    return params


def _configure_runtime(
    monkeypatch: pytest.MonkeyPatch,
    runtime_db_url: str,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("TK_FACT_DB_URL", runtime_db_url)
    monkeypatch.setenv("MUJITASK_FEISHU_ACCESS_TOKEN", "test-feishu-token")
    monkeypatch.setenv("AMAZON_US_BROWSER_PROFILE_REF", "amazon-us-e2e-profile")
    monkeypatch.setenv("AMAZON_US_LOCALE", "en_US")
    monkeypatch.setenv("AMAZON_US_DELIVERY_REGION", "US")
    monkeypatch.setenv("BUSINESS_EXECUTION_CONTROL_ARTIFACT_STORE_PROVIDER", "minio")
    monkeypatch.setenv("BUSINESS_EXECUTION_CONTROL_ARTIFACT_BUCKET", ARTIFACT_BUCKET)
    monkeypatch.setenv(
        "BUSINESS_EXECUTION_CONTROL_ARTIFACT_OBJECT_PREFIX",
        ARTIFACT_PREFIX,
    )
    monkeypatch.setenv(
        "BUSINESS_EXECUTION_CONTROL_ARTIFACT_ROOT",
        str(tmp_path / "artifacts"),
    )
    monkeypatch.setenv("BUSINESS_EXECUTION_CONTROL_MINIO_ENDPOINT", "127.0.0.1:9000")
    monkeypatch.setenv("BUSINESS_EXECUTION_CONTROL_MINIO_ACCESS_KEY", "minioadmin")
    monkeypatch.setenv("BUSINESS_EXECUTION_CONTROL_MINIO_SECRET_KEY", "miniosecret")
    monkeypatch.setenv("BUSINESS_EXECUTION_CONTROL_SYNC_REFERENCED_FILES", "true")
    monkeypatch.setattr(runtime_orchestrator, "API_HANDLER_REGISTRY", None, raising=False)
    monkeypatch.setattr(runtime_orchestrator, "BROWSER_HANDLER_REGISTRY", None, raising=False)
    monkeypatch.setattr(
        runtime_orchestrator,
        "resolve_automation_browser_target_digest",
        lambda *, profile_ref: "target-digest",
    )


def _bind_fake_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    store: FakeObjectStore,
) -> dict[str, Any]:
    state: dict[str, Any] = {
        "record": {
            "record_id": SOURCE_RECORD_ID,
            "fields": {
                "ASIN": ASIN,
                "商品链接": {"text": CANONICAL_URL, "link": CANONICAL_URL},
                "强制刷新": True,
                "采集状态": "pending",
                "来源关键词": "desk lamp",
                "人工备注": "must remain",
            },
        },
        "updates": [],
        "creates": [],
    }

    class FakeFeishuBitableClient:
        def __init__(
            self,
            access_token: str,
            request_pacer: object | None = None,
            timeout: int = 30,
        ) -> None:
            del request_pacer, timeout
            assert access_token == "test-feishu-token"

        def get_record(
            self,
            app_token: str,
            table_id: str,
            record_id: str,
        ) -> dict[str, Any]:
            assert (app_token, table_id, record_id) == (
                SOURCE_APP_TOKEN,
                SOURCE_TABLE_ID,
                SOURCE_RECORD_ID,
            )
            return {"code": 0, "data": {"record": deepcopy(state["record"])}}

        def list_all_fields(
            self,
            app_token: str,
            table_id: str,
            page_size: int = 100,
        ) -> list[dict[str, Any]]:
            del page_size
            assert (app_token, table_id) == (SOURCE_APP_TOKEN, SOURCE_TABLE_ID)
            return []

        def update_record(
            self,
            app_token: str,
            table_id: str,
            record_id: str,
            fields: dict[str, Any],
        ) -> dict[str, Any]:
            assert (app_token, table_id, record_id) == (
                SOURCE_APP_TOKEN,
                SOURCE_TABLE_ID,
                SOURCE_RECORD_ID,
            )
            state["record"]["fields"].update(deepcopy(fields))
            state["updates"].append(
                {
                    "app_token": app_token,
                    "table_id": table_id,
                    "record_id": record_id,
                    "fields": deepcopy(fields),
                }
            )
            return {
                "code": 0,
                "data": {"record": {"record_id": record_id}},
            }

        def create_record(
            self,
            app_token: str,
            table_id: str,
            fields: dict[str, Any],
        ) -> dict[str, Any]:
            state["creates"].append(
                {
                    "app_token": app_token,
                    "table_id": table_id,
                    "fields": deepcopy(fields),
                }
            )
            raise AssertionError("Amazon writeback must update the source record")

    monkeypatch.setattr(feishu_common, "FeishuBitableClient", FakeFeishuBitableClient)
    monkeypatch.setattr(browser_handler_module, "create_artifact_store", lambda _settings: store)
    monkeypatch.setattr(media_handler_module, "create_store_from_settings", lambda _settings: store)
    monkeypatch.setattr(
        media_handler_module,
        "urlopen",
        lambda request, timeout: FakeImageResponse(
            f"fake-amazon-image:{request.full_url}".encode()
        ),
    )
    monkeypatch.setattr(fact_handler_module, "create_artifact_store", lambda _settings: store)
    return state


def _success_collection() -> dict[str, Any]:
    html = (FIXTURE_DIR / "product_detail_child.html").read_text(encoding="utf-8")
    capture = extract_amazon_product_capture(
        html,
        requested_asin=ASIN,
        resolved_url=CANONICAL_URL,
        observed_at=OBSERVED_AT,
    )
    return {
        "capture": capture,
        "html": html,
        "resolved_url": CANONICAL_URL,
        "browser_target_digest": "target-digest",
        "screenshot_bytes": b"",
    }


def _terminal_collection(error: Exception) -> dict[str, Any]:
    html = (FIXTURE_DIR / "product_detail_blocked.html").read_text(encoding="utf-8")
    return {
        "capture": None,
        "html": html,
        "resolved_url": "https://www.amazon.com/errors/validateCaptcha",
        "browser_target_digest": "target-digest",
        "screenshot_bytes": b"png-evidence",
        "error": error,
    }


def _submit(runtime_db_url: str) -> str:
    submitted = runtime_orchestrator.run_task_request(
        TASK_CODE,
        _runtime_params(
            runtime_db_url,
            control_action="submit",
            table_ref=SOURCE_TABLE_URL,
            source_record_id=SOURCE_RECORD_ID,
        ),
    )
    assert submitted["request_status"] == "pending"
    return str(submitted["request_id"])


def _executor(runtime_db_url: str) -> dict[str, Any]:
    return runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))


def _api_worker(runtime_db_url: str) -> dict[str, Any]:
    return runtime_orchestrator.execute_api_worker_once(_runtime_params(runtime_db_url))


def _browser_worker(runtime_db_url: str) -> dict[str, Any]:
    return runtime_orchestrator.execute_browser_once(_runtime_params(runtime_db_url))


def _fact_counts(runtime_db_url: str) -> dict[str, int]:
    engine = create_engine(runtime_db_url, future=True)
    try:
        with engine.connect() as connection:
            return {
                table_name: int(
                    connection.execute(text(f"SELECT COUNT(*) FROM {table_name}")).scalar_one()
                )
                for table_name in FACT_TABLES
            }
    finally:
        engine.dispose()


def _fact_row(runtime_db_url: str, statement: str) -> dict[str, Any]:
    engine = create_engine(runtime_db_url, future=True)
    try:
        with engine.connect() as connection:
            row = connection.execute(text(statement)).mappings().one()
            return dict(row)
    finally:
        engine.dispose()


def _assert_runtime_is_compact(store: RuntimeStore, request_id: str) -> None:
    request = store.load_task_request(request_id=request_id)
    runtime_documents: list[Any] = [request.to_dict()]
    runtime_documents.extend(
        execution.to_dict()
        for execution in store.list_task_executions(request_id=request_id)
    )
    runtime_documents.extend(
        store.list_api_worker_jobs_for_request(request_id=request_id)
    )
    forbidden_keys = {
        "capture",
        "html",
        "screenshot_bytes",
        "projection_facts",
        "synced_assets",
        "raw_rows",
        "raw_rows_all",
        "browser_cookies",
        "cookies",
        "authorization",
        "body_bytes",
    }

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            assert forbidden_keys.isdisjoint(value)
            for child in value.values():
                visit(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child)

    visit(runtime_documents)
    serialized = json.dumps(runtime_documents, ensure_ascii=False, default=str)
    assert "<html" not in serialized.lower()
    assert "secret-cookie-must-not-leak" not in serialized
    assert "Structured product title" not in serialized


def test_real_amazon_runtime_success_chain_persists_writes_back_and_replays_idempotently(
    runtime_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_runtime(monkeypatch, runtime_db_url, tmp_path)
    object_store = FakeObjectStore()
    feishu_state = _bind_fake_boundaries(monkeypatch, object_store)
    monkeypatch.setattr(
        browser_handler_module,
        "_collect_browser_page",
        lambda **_kwargs: _success_collection(),
    )

    request_id = _submit(runtime_db_url)
    store = RuntimeStore(db_url=runtime_db_url)
    assert store.load_task_request(request_id=request_id).payload == {
        "table_ref": SOURCE_TABLE_URL,
        "source_record_id": SOURCE_RECORD_ID,
    }
    assert store.load_task_request(request_id=request_id).stage_cursor[
        "runtime_context"
    ] == {
        "browser_target_digest": "target-digest",
        "browser_resource_code": "browser:amazon:target-digest",
    }

    read_dispatch = _executor(runtime_db_url)
    assert read_dispatch["current_stage"] == "read_amazon_product_row"
    read_worker = _api_worker(runtime_db_url)
    assert read_worker["api_worker_job"]["job_code"] == "feishu_table_read"
    assert read_worker["api_worker_job"]["result_status"] == "success"

    browser_dispatch = _executor(runtime_db_url)
    assert browser_dispatch["current_stage"] == "collect_amazon_product_detail"
    browser_worker = _browser_worker(runtime_db_url)
    assert browser_worker["execution"]["item_code"] == "amazon_product_browser_fetch"
    assert browser_worker["execution"]["resource_code"] == "browser:amazon:target-digest"
    assert browser_worker["execution"]["result_status"] == "success"
    assert browser_worker["worker_result"]["result"]["requested_asin"] == ASIN
    assert "normalized_capture_ref" in browser_worker["worker_result"]["result"]

    persist_dispatch = _executor(runtime_db_url)
    assert persist_dispatch["current_stage"] == "persist_amazon_product_detail"
    persist_jobs = store.list_api_worker_jobs_for_request(
        request_id=request_id,
        job_code="amazon_product_row_persist",
    )
    assert len(persist_jobs) == 1
    persist_payload = deepcopy(persist_jobs[0]["payload"])
    assert persist_payload["source_table_identity"] == {
        "base_id": SOURCE_APP_TOKEN,
        "table_id": SOURCE_TABLE_ID,
    }
    assert persist_payload["source_record_id"] == SOURCE_RECORD_ID
    assert persist_payload["requested_asin"] == ASIN
    assert not {"capture", "html", "projection_facts"} & set(persist_payload)

    persist_worker = _api_worker(runtime_db_url)
    assert persist_worker["api_worker_job"]["job_code"] == "amazon_product_row_persist"
    assert persist_worker["api_worker_job"]["result_status"] == "success"
    finalized = _executor(runtime_db_url)
    assert finalized["request_status"] == "success"
    assert finalized["current_stage"] == "ready_for_summary"
    assert len(finalized["outbox"]) == 1

    assert feishu_state["creates"] == []
    assert len(feishu_state["updates"]) == 1
    assert feishu_state["updates"][0]["record_id"] == SOURCE_RECORD_ID
    written_fields = feishu_state["record"]["fields"]
    assert written_fields["ASIN"] == ASIN
    assert written_fields["来源关键词"] == "desk lamp"
    assert written_fields["人工备注"] == "must remain"
    assert written_fields["标题"] == "Structured product title"
    assert written_fields["采集状态"] == "success"

    assert _fact_counts(runtime_db_url) == SUCCESS_FACT_COUNTS
    product = _fact_row(
        runtime_db_url,
        "SELECT marketplace_code, asin, title FROM amazon_products",
    )
    assert product == {
        "marketplace_code": "US",
        "asin": ASIN,
        "title": "Structured product title",
    }
    binding = _fact_row(
        runtime_db_url,
        "SELECT base_id, table_id, record_id, source_asin FROM amazon_feishu_bindings",
    )
    assert binding == {
        "base_id": SOURCE_APP_TOKEN,
        "table_id": SOURCE_TABLE_ID,
        "record_id": SOURCE_RECORD_ID,
        "source_asin": ASIN,
    }
    assert all(call["bucket"] == ARTIFACT_BUCKET for call in object_store.upload_calls)
    assert any("raw-captures/amazon/us" in call["object_key"] for call in object_store.upload_calls)
    assert any("product-media/amazon/us" in call["object_key"] for call in object_store.upload_calls)

    replay = amazon_product_row_persist_handler(
        HandlerContext(
            request_id=request_id,
            job_id="persist-same-run-replay",
            handler_code="amazon_product_row_persist",
            worker_type="api_worker",
            runtime_table="api_worker_job",
            payload=persist_payload,
            workflow_code=TASK_CODE,
            stage_code="persist_amazon_product_detail",
            job_code="amazon_product_row_persist",
            business_key=f"{SOURCE_RECORD_ID}:{ASIN}",
            dedupe_key=f"{request_id}:same-run-replay",
            attempt_count=2,
            max_attempts=3,
            metadata={"run_id": persist_payload["run_id"]},
        )
    )
    assert replay.status == "success"
    assert _fact_counts(runtime_db_url) == SUCCESS_FACT_COUNTS
    assert len(feishu_state["updates"]) == 2
    assert {item["record_id"] for item in feishu_state["updates"]} == {
        SOURCE_RECORD_ID
    }
    _assert_runtime_is_compact(store, request_id)


@pytest.mark.parametrize(
    ("error_factory", "expected_error_code", "expected_collection_status"),
    [
        pytest.param(
            lambda: AmazonAccessBlockedError(
                "robot check",
                error_code="captcha_required",
            ),
            "captcha_required",
            "blocked",
            id="captcha-blocked",
        ),
        pytest.param(
            lambda: AmazonIdentityMismatchError("unrelated ASIN"),
            "identity_mismatch",
            "failed",
            id="identity-mismatch",
        ),
    ],
)
def test_terminal_browser_failure_writes_status_only_and_never_enters_row_persist(
    runtime_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error_factory: Callable[[], Exception],
    expected_error_code: str,
    expected_collection_status: str,
) -> None:
    _configure_runtime(monkeypatch, runtime_db_url, tmp_path)
    object_store = FakeObjectStore()
    feishu_state = _bind_fake_boundaries(monkeypatch, object_store)
    monkeypatch.setattr(
        browser_handler_module,
        "_collect_browser_page",
        lambda **_kwargs: _terminal_collection(error_factory()),
    )

    request_id = _submit(runtime_db_url)
    store = RuntimeStore(db_url=runtime_db_url)
    assert _executor(runtime_db_url)["current_stage"] == "read_amazon_product_row"
    assert _api_worker(runtime_db_url)["api_worker_job"]["result_status"] == "success"
    assert _executor(runtime_db_url)["current_stage"] == "collect_amazon_product_detail"

    browser_worker = _browser_worker(runtime_db_url)
    assert browser_worker["execution"]["result_status"] == "failed"
    assert browser_worker["worker_result"]["error"]["error_code"] == expected_error_code
    assert (
        browser_worker["worker_result"]["summary"]["collection_status"]
        == expected_collection_status
    )
    assert {
        ref["capture_kind"]
        for ref in browser_worker["worker_result"]["result"]["artifact_refs"]
    } == {"html", "screenshot"}

    writeback_dispatch = _executor(runtime_db_url)
    assert writeback_dispatch["request_status"] == "waiting"
    assert writeback_dispatch["current_stage"] == "collect_amazon_product_detail"
    writeback_jobs = store.list_api_worker_jobs_for_request(
        request_id=request_id,
        job_code="feishu_table_write",
    )
    assert len(writeback_jobs) == 1
    writeback_payload = writeback_jobs[0]["payload"]
    assert writeback_payload["stage_code"] == "collect_amazon_product_detail"
    assert writeback_payload["source_record_id"] == SOURCE_RECORD_ID
    assert writeback_payload["row_status"] == expected_collection_status
    assert writeback_payload["error_code"] == expected_error_code
    assert writeback_payload["write_mode"] == "update_existing"
    assert writeback_payload["writeback_kind"] == "amazon_terminal_status"
    assert writeback_payload["mapper_code"] == "amazon_product_projection_mapper"
    assert len(writeback_payload["records"]) == 1
    assert writeback_payload["records"][0] == {
        "source_record_id": SOURCE_RECORD_ID,
        "requested_asin": ASIN,
        "collection_status": expected_collection_status,
        "collected_at": writeback_payload["records"][0]["collected_at"],
        "error_code": expected_error_code,
        "error_message": "Amazon browser collection failed.",
    }
    assert writeback_payload["records"][0]["collected_at"].endswith("Z")
    assert store.list_api_worker_jobs_for_request(
        request_id=request_id,
        job_code="amazon_product_row_persist",
    ) == []

    writeback_worker = _api_worker(runtime_db_url)
    assert writeback_worker["api_worker_job"]["job_code"] == "feishu_table_write"
    assert writeback_worker["api_worker_job"]["result_status"] == "success"

    finalized = _executor(runtime_db_url)
    assert finalized["request_status"] == "failed"
    assert finalized["summary"]["row_status_counts"] == {
        expected_collection_status: 1
    }
    assert finalized["result"]["row_results"][0]["source_record_id"] == SOURCE_RECORD_ID
    assert finalized["result"]["row_results"][0]["writeback"] == {
        "written_count": 1,
        "target_record_ids": [SOURCE_RECORD_ID],
    }
    assert store.list_api_worker_jobs_for_request(
        request_id=request_id,
        job_code="amazon_product_row_persist",
    ) == []
    assert len(feishu_state["updates"]) == 1
    assert feishu_state["updates"][0]["record_id"] == SOURCE_RECORD_ID
    status_fields = feishu_state["updates"][0]["fields"]
    assert set(status_fields) == {"采集状态", "上次采集时间", "采集错误"}
    assert status_fields["采集状态"] == expected_collection_status
    assert status_fields["上次采集时间"]
    assert status_fields["采集错误"] == (
        f"{expected_error_code}: Amazon browser collection failed."
    )
    assert feishu_state["record"]["fields"]["ASIN"] == ASIN
    assert feishu_state["record"]["fields"]["来源关键词"] == "desk lamp"
    assert feishu_state["record"]["fields"]["人工备注"] == "must remain"
    assert feishu_state["creates"] == []
    assert _fact_counts(runtime_db_url) == {table_name: 0 for table_name in FACT_TABLES}
    assert len(object_store.upload_calls) == 2
    _assert_runtime_is_compact(store, request_id)
