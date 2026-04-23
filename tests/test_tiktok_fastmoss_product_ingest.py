from __future__ import annotations

import hashlib
import json
from dataclasses import replace

from automation_business_scaffold.business.tasks.tiktok_fastmoss_product_ingest import (
    TikTokFastMossProductIngestTask,
)
from automation_business_scaffold.business.flows.feishu_tk_selection_mapper import PRODUCT_STATUS_UNAVAILABLE
from automation_business_scaffold.business.flows.tiktok_product_flow import (
    TikTokProductExtractionError,
    TikTokProductUnavailableError,
)
from automation_business_scaffold.cli import run_registered_task
from automation_business_scaffold.infrastructure.artifacts.artifact_store import StoredArtifact
from automation_business_scaffold.infrastructure.fastmoss.cookie_cache import build_fastmoss_cookie_cache_context
from automation_business_scaffold.infrastructure.facts.tk_fact_store import TKFactStore
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore
from automation_business_scaffold.models import TikTokProductRecord


PRODUCT_ID = "1732183068040729370"


class _FakeFastMossSession:
    calls: list[tuple[str, str]] = []
    ensured_login = False
    logged_in = False
    imported_cookies: list[dict[str, object]] = []

    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs
        self.base_url = str(kwargs.get("base_url") or "https://www.fastmoss.com").rstrip("/")
        self._auth_refresh_callback = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def ensure_logged_in(self):
        type(self).ensured_login = True
        return {"code": 200, "ext": {"is_login": 1}}

    def login(self):
        type(self).logged_in = True
        return {"code": 200, "ext": {"is_login": 1}}

    def set_auth_refresh_callback(self, callback):
        self._auth_refresh_callback = callback

    def replace_browser_cookies(self, cookies, *, domain_keyword="fastmoss.com"):
        del domain_keyword
        type(self).imported_cookies = [dict(cookie) for cookie in cookies]
        return len(cookies)

    def export_cookies(self, *, domain_keyword="fastmoss.com"):
        del domain_keyword
        return [
            {
                "name": "fd_tk",
                "value": "fake-fd-token",
                "domain": ".fastmoss.com",
                "path": "/",
                "secure": True,
            }
        ]

    def cookie_snapshot(self, *, domain_keyword="fastmoss.com"):
        del domain_keyword
        return {
            "cookie_count": 1,
            "cookie_names": ["fd_tk"],
            "has_fd_tk": True,
            "fd_tk_digest": "fake-digest",
        }

    def get_product_base(self, product_id: str):
        type(self).calls.append(("base", product_id))
        return {
            "product": {
                "product_id": product_id,
                "title": "FastMoss Gift",
                "img": "https://example.com/fastmoss-product.png",
                "product_rating": 4.6,
                "review_count": 321,
                "sold_count": 900,
                "sale_amount": 12345.67,
                "real_price": "$12.99",
                "currency": "USD",
            },
            "shop": {
                "seller_id": "7496166867916327706",
                "name": "FastMoss Shop",
                "region": "US",
            },
        }

    def get_product_overview(self, product_id: str, *, d_type=28):
        type(self).calls.append(("overview", product_id))
        return {
            "product_id": product_id,
            "d_type": d_type,
            "overview": {
                "d_type": d_type,
                "sales_7d": 88,
                "sold_count": 88,
                "sale_amount": 1143.12,
            },
            "chart_list": [
                {
                    "dt": "2026-04-01",
                    "inc_sold_count": 12,
                    "inc_sale_amount": 155.88,
                    "price": 12.99,
                    "currency": "USD",
                }
            ],
            "channel_distribution": {
                "units_sold": {
                    "total_count": 88,
                    "list": [
                        {
                            "source": "common.goods.affiliate",
                            "propotion": 0.75,
                            "sold_count": 66,
                            "sold_count_show": "66",
                        }
                    ],
                },
                "gmv": {
                    "total_count": 1143.12,
                    "list": [
                        {
                            "source": "common.goods.affiliate",
                            "propotion": 0.8,
                            "sale_amount": 914.5,
                            "currency": "USD",
                            "sale_amount_show": "$914.50",
                        }
                    ],
                },
            },
            "content_distribution": {
                "units_sold": {
                    "total_count": 88,
                    "list": [{"category": "video.name", "propotion": 0.6, "sold_count": 53}],
                },
                "gmv": {
                    "total_count": 1143.12,
                    "list": [{"category": "video.name", "propotion": 0.64, "sale_amount": 731.6, "currency": "USD"}],
                },
            },
            "ads_distribution": {
                "units_sold": {
                    "total_count": 88,
                    "list": [{"category": "common.goods.adTraffic", "propotion": 0.3, "sold_count": 26}],
                },
                "gmv": {
                    "total_count": 1143.12,
                    "list": [
                        {
                            "category": "common.goods.adTraffic",
                            "propotion": 0.35,
                            "sale_amount": 400.09,
                            "currency": "USD",
                        }
                    ],
                },
            },
        }

    def get_product_skus(self, product_id: str, *, d_type=28):
        type(self).calls.append(("skus", product_id))
        return {
            "product_id": product_id,
            "d_type": d_type,
            "sku_list": [
                {
                    "sku_id": "sku-pink",
                    "sku_name": "Pink",
                    "real_price": "$12.99",
                    "stock": 7,
                    "sold_count": 31,
                    "sale_amount": 401.69,
                }
            ],
        }

    def get_product_sku_distribution(self, product_id: str, *, d_type=28):
        type(self).calls.append(("sku_distribution", product_id))
        return {
            "product_id": product_id,
            "d_type": d_type,
            "sku_list": [
                {
                    "sku_id": "sku-pink",
                    "sku_name": "Pink",
                    "real_price": "$12.99",
                    "real_price_value": 12.99,
                    "stock": 7,
                    "sku_sale_props": [{"prop_name": "Color", "prop_value": "Pink"}],
                }
            ],
            "sku_units_sold": {
                "Color": {
                    "total_count": 31,
                    "list": [
                        {
                            "source": "Pink",
                            "propotion": 1.0,
                            "sold_count": 31,
                            "sold_count_show": "31",
                        }
                    ],
                }
            },
            "sku_gmv": {
                "Color": {
                    "total_count": 401.69,
                    "list": [
                        {
                            "source": "Pink",
                            "propotion": 1.0,
                            "sale_amount": 401.69,
                            "currency": "USD",
                            "sale_amount_show": "$401.69",
                        }
                    ],
                }
            },
            "sku_stock": {
                "Color": {
                    "list": [
                        {
                            "source": "Pink",
                            "propotion": 1.0,
                            "sold_count": 7,
                            "sold_count_show": "7",
                        }
                    ]
                }
            },
            "best_sku": {
                "sku_name": "Color",
                "sku_value": "Pink",
                "sold_count": 31,
                "sale_amount": 401.69,
                "currency": "USD",
                "price": "$12.99",
                "stock": 7,
            },
        }


class _FakeArtifactStore:
    provider_code = "minio"
    uploads: list[dict[str, object]] = []

    def upload_file(self, *, bucket, object_key, local_path, content_type, metadata=None):
        self.uploads.append(
            {
                "bucket": bucket,
                "object_key": object_key,
                "local_path": str(local_path),
                "content_type": content_type,
                "metadata": dict(metadata or {}),
            }
        )
        return StoredArtifact(
            bucket=bucket,
            object_key=object_key,
            etag="etag-test",
            size=local_path.stat().st_size,
            content_type=content_type,
            uri=f"s3://{bucket}/{object_key}",
            metadata={"storage_backend": self.provider_code},
        )

    def build_uri(self, *, bucket, object_key):
        return f"s3://{bucket}/{object_key}"


class _FakeDownloadResponse:
    headers = {"Content-Type": "image/png"}

    def __init__(self, url: str) -> None:
        self.content = f"image:{url}".encode("utf-8")

    def raise_for_status(self):
        return None


class _FakeDownloadSession:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def get(self, url, *, timeout):
        assert str(url).startswith("https://example.com/")
        assert timeout > 0
        return _FakeDownloadResponse(str(url))


def test_tiktok_fastmoss_product_ingest_workflow_builder_uses_single_orchestration_step():
    task = TikTokFastMossProductIngestTask()

    workflow = task.build_workflow({})

    assert workflow.workflow_id == "tiktok_fastmoss_product_ingest_v1"
    assert [step.step_id for step in workflow.steps] == ["orchestrate_tiktok_fastmoss_product_ingest"]


def test_tiktok_fastmoss_product_ingest_submit_uses_single_async_control_step():
    task = TikTokFastMossProductIngestTask()

    workflow = task.build_workflow({"control_action": "submit"})

    assert workflow.workflow_id == "tiktok_fastmoss_product_ingest_v1"
    assert [step.step_id for step in workflow.steps] == ["orchestrate_tiktok_fastmoss_product_ingest"]


def test_tiktok_product_media_dedupes_by_stable_tiktok_uri():
    module = __import__(
        "automation_business_scaffold.business.flows.tiktok_fastmoss_product_ingest_flow",
        fromlist=["_dedupe_media_assets"],
    )

    deduped = module._dedupe_media_assets(
        [
            {
                "entity_type": "product",
                "entity_external_id": PRODUCT_ID,
                "media_role": "product_gallery_image",
                "source_platform": "tiktok",
                "source_url": "https://p16.example.com/image.webp",
                "file_token": "tiktok_uri:tos-test/same-image",
            },
            {
                "entity_type": "product",
                "entity_external_id": PRODUCT_ID,
                "media_role": "product_gallery_image",
                "source_platform": "tiktok",
                "source_url": "https://p19.example.com/image.webp",
                "file_token": "tiktok_uri:tos-test/same-image",
            },
        ]
    )

    assert len(deduped) == 1
    assert deduped[0]["source_url"] == "https://p16.example.com/image.webp"


def test_tiktok_fastmoss_product_ingest_fetches_apis_and_persists_facts(
    monkeypatch,
    tmp_path,
    runtime_db_url,
):
    product_url = f"https://www.tiktok.com/shop/pdp/{PRODUCT_ID}"
    _patch_product_ingest_dependencies(monkeypatch, tmp_path, product_url)

    payload = run_registered_task(
        "tiktok_fastmoss_product_ingest",
        params={
            "product_url": product_url,
            "execution_control_db_url": runtime_db_url,
            "fastmoss_phone": "18000000000",
            "fastmoss_password": "secret",
            "fastmoss_ensure_login": True,
            "image_download_dir": str(tmp_path / "images"),
            "execution_control_artifact_store_provider": "minio",
            "execution_control_artifact_bucket": "product-media",
            "execution_control_artifact_object_prefix": "test-prefix",
        },
        run_dir=tmp_path / "runs",
    )

    _assert_successful_product_ingest_payload(payload, runtime_db_url)


def test_tiktok_fastmoss_product_ingest_submit_then_executor_once_finishes_request(
    monkeypatch,
    tmp_path,
    runtime_db_url,
):
    product_url = f"https://www.tiktok.com/shop/pdp/{PRODUCT_ID}"
    _patch_product_ingest_dependencies(monkeypatch, tmp_path, product_url)

    submitted = run_registered_task(
        "tiktok_fastmoss_product_ingest",
        params={
            "control_action": "submit",
            "product_url": product_url,
            "execution_control_db_url": runtime_db_url,
            "fastmoss_phone": "18000000000",
            "fastmoss_password": "secret",
            "fastmoss_ensure_login": True,
            "image_download_dir": str(tmp_path / "images"),
        },
        run_dir=tmp_path / "submit-runs",
    )

    submit_step = submitted["result"]["data"]["step_outputs"]["orchestrate_tiktok_fastmoss_product_ingest"]
    request_id = submit_step["request_id"]
    assert submit_step["request_status"] == "pending"
    assert submit_step["summary"]["counts"] == {"queued": 1}

    executor_dispatch = run_registered_task(
        "tiktok_fastmoss_product_ingest",
        params={
            "control_action": "executor_once",
            "execution_control_db_url": runtime_db_url,
            "execution_control_artifact_store_provider": "minio",
            "execution_control_artifact_bucket": "product-media",
            "execution_control_artifact_object_prefix": "test-prefix",
        },
        run_dir=tmp_path / "executor-runs",
    )

    dispatch_step = executor_dispatch["result"]["data"]["step_outputs"]["orchestrate_tiktok_fastmoss_product_ingest"]
    assert dispatch_step["request_id"] == request_id
    assert dispatch_step["request_status"] == "waiting_children"
    assert dispatch_step["current_stage"] == "waiting_api_worker"
    assert dispatch_step["child_total_count"] == 1
    assert dispatch_step["api_worker_jobs"][0]["job_code"] == "tiktok_fastmoss_product_ingest"

    api_worker = run_registered_task(
        "tiktok_fastmoss_product_ingest",
        params={
            "control_action": "api_worker_once",
            "execution_control_db_url": runtime_db_url,
            "execution_control_artifact_store_provider": "minio",
            "execution_control_artifact_bucket": "product-media",
            "execution_control_artifact_object_prefix": "test-prefix",
        },
        run_dir=tmp_path / "api-worker-runs",
    )

    api_worker_step = api_worker["result"]["data"]["step_outputs"]["orchestrate_tiktok_fastmoss_product_ingest"]
    assert api_worker_step["daemon_status"] == "processed"
    assert api_worker_step["request_id"] == request_id
    assert api_worker_step["api_worker_job"]["status"] == "success"
    assert api_worker_step["parent_updates"][0]["updated"] is True

    executor_finalize = run_registered_task(
        "tiktok_fastmoss_product_ingest",
        params={
            "control_action": "executor_once",
            "execution_control_db_url": runtime_db_url,
        },
        run_dir=tmp_path / "executor-finalize-runs",
    )

    finalize_step = executor_finalize["result"]["data"]["step_outputs"]["orchestrate_tiktok_fastmoss_product_ingest"]
    assert finalize_step["request_id"] == request_id
    assert finalize_step["request_status"] == "success"
    assert finalize_step["result"]["product_id"] == PRODUCT_ID
    assert finalize_step["result"]["media_upload"]["summary"]["counts"] == {"uploaded": 4, "failed": 0}

    result = run_registered_task(
        "tiktok_fastmoss_product_ingest",
        params={
            "control_action": "result",
            "request_id": request_id,
            "execution_control_db_url": runtime_db_url,
        },
        run_dir=tmp_path / "result-runs",
    )
    result_step = result["result"]["data"]["step_outputs"]["orchestrate_tiktok_fastmoss_product_ingest"]
    assert result_step["request_status"] == "success"
    assert result_step["result"]["product_id"] == PRODUCT_ID


def test_tiktok_fastmoss_product_ingest_table_mode_reads_ingests_and_writes_back(
    monkeypatch,
    tmp_path,
    runtime_db_url,
):
    del tmp_path
    product_url = f"https://www.tiktok.com/shop/pdp/{PRODUCT_ID}"
    table_url = "https://example.feishu.cn/base/app?table=tblpF46y6SkmVCE5&view=vewhXPD4x1"
    runtime_flow = __import__(
        "automation_business_scaffold.business.flows.refresh_current_competitor_table_flow",
        fromlist=[
            "read_feishu_tk_selection_table_for_product",
            "run_tiktok_fastmoss_product_ingest_sync",
            "writeback_feishu_tk_selection_table",
        ],
    )
    writeback_calls: list[dict[str, object]] = []

    def fake_table_read(params):
        assert params["tk_selection_table_url"] == table_url
        item = {
            "status": "needs_ingest",
            "source_record_id": "recvfHCdzsKwVp",
            "record_id": "recvfHCdzsKwVp",
            "product_url": product_url,
            "normalized_url": product_url,
            "product_id": PRODUCT_ID,
            "table_url": table_url,
            "required_missing_fields": ["商品ID", "商品标题"],
            "source_fields": {"商品链接": {"text": product_url, "link": product_url}},
        }
        return {
            "summary": {"total": 1, "counts": {"needs_ingest": 1}},
            "item": item,
            "items": [item],
            "status": "needs_ingest",
            "product_id": PRODUCT_ID,
            "product_url": product_url,
            "source_record_id": "recvfHCdzsKwVp",
        }

    def fake_product_ingest(params):
        assert params["source_record_id"] == "recvfHCdzsKwVp"
        assert params["fastmoss_visualization_enabled"] is True
        return _fake_product_ingest_result(product_url)

    def fake_writeback(params):
        writeback_calls.append(dict(params))
        assert params["source_record_id"] == "recvfHCdzsKwVp"
        assert params["table_read_result"]["item"]["source_record_id"] == "recvfHCdzsKwVp"
        assert params["product_ingest_result"]["product_id"] == PRODUCT_ID
        item = {
            "status": "writeback_completed",
            "record_id": "recvfHCdzsKwVp",
            "product_id": PRODUCT_ID,
            "updated_fields": ["商品ID", "商品标题", "记录日期"],
        }
        return {
            "summary": {"total": 1, "counts": {"writeback_completed": 1}},
            "item": item,
            "items": [item],
            "status": "writeback_completed",
        }

    monkeypatch.setattr(runtime_flow, "read_feishu_tk_selection_table_for_product", fake_table_read)
    monkeypatch.setattr(runtime_flow, "run_tiktok_fastmoss_product_ingest_sync", fake_product_ingest)
    monkeypatch.setattr(runtime_flow, "writeback_feishu_tk_selection_table", fake_writeback)

    submitted = run_registered_task(
        "tiktok_fastmoss_product_ingest",
        params={
            "control_action": "submit",
            "product_url": product_url,
            "tk_selection_table_url": table_url,
            "feishu_access_token": "test-token",
            "execution_control_db_url": runtime_db_url,
        },
    )
    request_id = submitted["result"]["data"]["step_outputs"]["orchestrate_tiktok_fastmoss_product_ingest"][
        "request_id"
    ]

    dispatch_read = run_registered_task(
        "tiktok_fastmoss_product_ingest",
        params={"control_action": "executor_once", "execution_control_db_url": runtime_db_url},
    )["result"]["data"]["step_outputs"]["orchestrate_tiktok_fastmoss_product_ingest"]
    assert dispatch_read["current_stage"] == "waiting_feishu_tk_selection_table_read"
    assert dispatch_read["api_worker_jobs"][0]["job_code"] == "feishu_tk_selection_table_read"

    table_read = run_registered_task(
        "tiktok_fastmoss_product_ingest",
        params={"control_action": "api_worker_once", "execution_control_db_url": runtime_db_url},
    )["result"]["data"]["step_outputs"]["orchestrate_tiktok_fastmoss_product_ingest"]
    assert table_read["request_id"] == request_id
    assert table_read["api_worker_job"]["status"] == "success"
    assert table_read["parent_updates"][0]["reason"] == "table_read_needs_ingest"

    dispatch_product = run_registered_task(
        "tiktok_fastmoss_product_ingest",
        params={"control_action": "executor_once", "execution_control_db_url": runtime_db_url},
    )["result"]["data"]["step_outputs"]["orchestrate_tiktok_fastmoss_product_ingest"]
    assert dispatch_product["current_stage"] == "waiting_tiktok_fastmoss_product_ingest"
    assert {job["job_code"] for job in dispatch_product["api_worker_jobs"]} == {
        "feishu_tk_selection_table_read",
        "tiktok_fastmoss_product_ingest",
    }

    product_ingest = run_registered_task(
        "tiktok_fastmoss_product_ingest",
        params={"control_action": "api_worker_once", "execution_control_db_url": runtime_db_url},
    )["result"]["data"]["step_outputs"]["orchestrate_tiktok_fastmoss_product_ingest"]
    assert product_ingest["api_worker_job"]["job_code"] == "tiktok_fastmoss_product_ingest"
    assert product_ingest["parent_updates"][0]["next_stage"] == "dispatch_feishu_tk_selection_table_writeback"

    dispatch_writeback = run_registered_task(
        "tiktok_fastmoss_product_ingest",
        params={"control_action": "executor_once", "execution_control_db_url": runtime_db_url},
    )["result"]["data"]["step_outputs"]["orchestrate_tiktok_fastmoss_product_ingest"]
    assert dispatch_writeback["current_stage"] == "waiting_feishu_tk_selection_table_writeback"
    assert dispatch_writeback["api_worker_jobs"][-1]["job_code"] == "feishu_tk_selection_table_writeback"

    writeback = run_registered_task(
        "tiktok_fastmoss_product_ingest",
        params={"control_action": "api_worker_once", "execution_control_db_url": runtime_db_url},
    )["result"]["data"]["step_outputs"]["orchestrate_tiktok_fastmoss_product_ingest"]
    assert writeback["api_worker_job"]["job_code"] == "feishu_tk_selection_table_writeback"
    assert writeback["parent_updates"][0]["reason"] == "all_jobs_terminal"
    assert len(writeback_calls) == 1

    finalized = run_registered_task(
        "tiktok_fastmoss_product_ingest",
        params={"control_action": "executor_once", "execution_control_db_url": runtime_db_url},
    )["result"]["data"]["step_outputs"]["orchestrate_tiktok_fastmoss_product_ingest"]
    assert finalized["request_status"] == "success"
    assert finalized["result"]["product_id"] == PRODUCT_ID
    assert finalized["result"]["feishu_tk_selection_table_writeback"]["item"]["status"] == "writeback_completed"


def test_tiktok_fastmoss_product_ingest_table_mode_writes_status_when_tiktok_unavailable(
    monkeypatch,
    runtime_db_url,
):
    product_url = f"https://www.tiktok.com/shop/pdp/{PRODUCT_ID}"
    table_url = "https://example.feishu.cn/base/app?table=tblpF46y6SkmVCE5&view=vewhXPD4x1"
    runtime_flow = __import__(
        "automation_business_scaffold.business.flows.refresh_current_competitor_table_flow",
        fromlist=[
            "read_feishu_tk_selection_table_for_product",
            "run_tiktok_fastmoss_product_ingest_sync",
            "writeback_feishu_tk_selection_table",
        ],
    )
    writeback_calls: list[dict[str, object]] = []

    def fake_table_read(params):
        del params
        item = {
            "status": "needs_ingest",
            "source_record_id": "rec-unavailable",
            "record_id": "rec-unavailable",
            "product_url": product_url,
            "normalized_url": product_url,
            "product_id": PRODUCT_ID,
            "table_url": table_url,
            "required_missing_fields": ["商品主图", "商品标题"],
            "source_fields": {"商品链接": {"text": product_url, "link": product_url}},
        }
        return {
            "summary": {"total": 1, "counts": {"needs_ingest": 1}},
            "item": item,
            "items": [item],
            "status": "needs_ingest",
            "product_id": PRODUCT_ID,
            "product_url": product_url,
            "source_record_id": "rec-unavailable",
        }

    def fake_product_ingest(params):
        assert params["source_record_id"] == "rec-unavailable"
        raise TikTokProductUnavailableError(
            "TikTok product unavailable: Product not available in this country or region"
        )

    def fake_writeback(params):
        writeback_calls.append(dict(params))
        product_result = params["product_ingest_result"]
        assert product_result["product_status"] == PRODUCT_STATUS_UNAVAILABLE
        assert product_result["item"]["status"] == "product_unavailable"
        return {
            "summary": {"total": 1, "counts": {"status_writeback_completed": 1}},
            "item": {
                "status": "status_writeback_completed",
                "record_id": "rec-unavailable",
                "product_id": PRODUCT_ID,
                "product_status": PRODUCT_STATUS_UNAVAILABLE,
                "updated_fields": ["商品状态", "记录日期"],
            },
            "items": [],
            "status": "status_writeback_completed",
        }

    monkeypatch.setattr(runtime_flow, "read_feishu_tk_selection_table_for_product", fake_table_read)
    monkeypatch.setattr(runtime_flow, "run_tiktok_fastmoss_product_ingest_sync", fake_product_ingest)
    monkeypatch.setattr(runtime_flow, "writeback_feishu_tk_selection_table", fake_writeback)

    submitted = run_registered_task(
        "tiktok_fastmoss_product_ingest",
        params={
            "control_action": "submit",
            "product_url": product_url,
            "tk_selection_table_url": table_url,
            "feishu_access_token": "test-token",
            "execution_control_db_url": runtime_db_url,
        },
    )
    request_id = submitted["result"]["data"]["step_outputs"]["orchestrate_tiktok_fastmoss_product_ingest"][
        "request_id"
    ]

    run_registered_task(
        "tiktok_fastmoss_product_ingest",
        params={"control_action": "executor_once", "execution_control_db_url": runtime_db_url},
    )
    run_registered_task(
        "tiktok_fastmoss_product_ingest",
        params={"control_action": "api_worker_once", "execution_control_db_url": runtime_db_url},
    )
    run_registered_task(
        "tiktok_fastmoss_product_ingest",
        params={"control_action": "executor_once", "execution_control_db_url": runtime_db_url},
    )
    product_ingest = run_registered_task(
        "tiktok_fastmoss_product_ingest",
        params={"control_action": "api_worker_once", "execution_control_db_url": runtime_db_url},
    )["result"]["data"]["step_outputs"]["orchestrate_tiktok_fastmoss_product_ingest"]
    assert product_ingest["api_worker_job"]["status"] == "success"
    assert product_ingest["worker_result"]["status"] == "product_unavailable"
    assert product_ingest["parent_updates"][0]["next_stage"] == "dispatch_feishu_tk_selection_table_writeback"

    run_registered_task(
        "tiktok_fastmoss_product_ingest",
        params={"control_action": "executor_once", "execution_control_db_url": runtime_db_url},
    )
    writeback = run_registered_task(
        "tiktok_fastmoss_product_ingest",
        params={"control_action": "api_worker_once", "execution_control_db_url": runtime_db_url},
    )["result"]["data"]["step_outputs"]["orchestrate_tiktok_fastmoss_product_ingest"]
    assert writeback["api_worker_job"]["job_code"] == "feishu_tk_selection_table_writeback"
    assert len(writeback_calls) == 1

    finalized = run_registered_task(
        "tiktok_fastmoss_product_ingest",
        params={"control_action": "executor_once", "execution_control_db_url": runtime_db_url},
    )["result"]["data"]["step_outputs"]["orchestrate_tiktok_fastmoss_product_ingest"]
    assert finalized["request_id"] == request_id
    assert finalized["request_status"] == "success"
    assert finalized["result"]["product_status"] == PRODUCT_STATUS_UNAVAILABLE
    assert (
        finalized["result"]["feishu_tk_selection_table_writeback"]["item"]["status"]
        == "status_writeback_completed"
    )


def test_tiktok_fastmoss_product_ingest_falls_back_to_browser_loop_after_request_parse_failure(
    monkeypatch,
    runtime_db_url,
):
    product_url = f"https://www.tiktok.com/shop/pdp/{PRODUCT_ID}"
    table_url = "https://example.feishu.cn/base/app?table=tblpF46y6SkmVCE5&view=vewhXPD4x1"
    runtime_flow = __import__(
        "automation_business_scaffold.business.flows.refresh_current_competitor_table_flow",
        fromlist=[
            "fetch_tiktok_product_via_browser",
            "read_feishu_tk_selection_table_for_product",
            "run_tiktok_fastmoss_product_ingest_sync",
            "writeback_feishu_tk_selection_table",
        ],
    )
    product_ingest_calls: list[dict[str, object]] = []
    browser_calls: list[dict[str, object]] = []

    def fake_table_read(params):
        del params
        item = {
            "status": "needs_ingest",
            "source_record_id": "rec-browser-fallback",
            "record_id": "rec-browser-fallback",
            "product_url": product_url,
            "normalized_url": product_url,
            "product_id": PRODUCT_ID,
            "table_url": table_url,
            "required_missing_fields": ["商品ID", "商品标题"],
        }
        return {
            "summary": {"total": 1, "counts": {"needs_ingest": 1}},
            "item": item,
            "items": [item],
            "status": "needs_ingest",
            "product_id": PRODUCT_ID,
            "product_url": product_url,
            "source_record_id": "rec-browser-fallback",
        }

    def fake_product_ingest(params):
        product_ingest_calls.append(dict(params))
        if not isinstance(params.get("tiktok_payload"), dict):
            raise TikTokProductExtractionError("failed to locate script tag: __MODERN_ROUTER_DATA__")
        assert params["tiktok_fetch_source"] == "browser"
        assert params["tiktok_payload"]["product_id"] == PRODUCT_ID
        return _fake_product_ingest_result(product_url)

    def fake_browser_fetch(params):
        browser_calls.append(dict(params))
        assert params["profile_ref"] == "roxy-tiktok"
        assert params["tiktok_browser_fallback_reason"]
        return _fake_tiktok_browser_fetch_result(product_url)

    def fake_writeback(params):
        assert params["product_ingest_result"]["product_id"] == PRODUCT_ID
        return {
            "summary": {"total": 1, "counts": {"writeback_completed": 1}},
            "item": {
                "status": "writeback_completed",
                "record_id": "rec-browser-fallback",
                "product_id": PRODUCT_ID,
                "updated_fields": ["商品ID", "商品标题", "记录日期"],
            },
            "items": [],
            "status": "writeback_completed",
        }

    monkeypatch.setattr(runtime_flow, "read_feishu_tk_selection_table_for_product", fake_table_read)
    monkeypatch.setattr(runtime_flow, "run_tiktok_fastmoss_product_ingest_sync", fake_product_ingest)
    monkeypatch.setattr(runtime_flow, "fetch_tiktok_product_via_browser", fake_browser_fetch)
    monkeypatch.setattr(runtime_flow, "writeback_feishu_tk_selection_table", fake_writeback)

    submitted = run_registered_task(
        "tiktok_fastmoss_product_ingest",
        params={
            "control_action": "submit",
            "product_url": product_url,
            "tk_selection_table_url": table_url,
            "feishu_access_token": "test-token",
            "execution_control_db_url": runtime_db_url,
        },
    )
    request_id = submitted["result"]["data"]["step_outputs"]["orchestrate_tiktok_fastmoss_product_ingest"][
        "request_id"
    ]

    run_registered_task(
        "tiktok_fastmoss_product_ingest",
        params={"control_action": "executor_once", "execution_control_db_url": runtime_db_url},
    )
    run_registered_task(
        "tiktok_fastmoss_product_ingest",
        params={"control_action": "api_worker_once", "execution_control_db_url": runtime_db_url},
    )
    run_registered_task(
        "tiktok_fastmoss_product_ingest",
        params={"control_action": "executor_once", "execution_control_db_url": runtime_db_url},
    )
    fallback_required = run_registered_task(
        "tiktok_fastmoss_product_ingest",
        params={"control_action": "api_worker_once", "execution_control_db_url": runtime_db_url},
    )["result"]["data"]["step_outputs"]["orchestrate_tiktok_fastmoss_product_ingest"]
    assert fallback_required["api_worker_job"]["stage"] == "browser_fallback_required"
    assert fallback_required["parent_updates"][0]["next_stage"] == "dispatch_tiktok_product_browser_fallback"

    browser_dispatch = run_registered_task(
        "tiktok_fastmoss_product_ingest",
        params={"control_action": "executor_once", "execution_control_db_url": runtime_db_url},
    )["result"]["data"]["step_outputs"]["orchestrate_tiktok_fastmoss_product_ingest"]
    assert browser_dispatch["current_stage"] == "waiting_tiktok_product_browser_fetch"
    assert browser_dispatch["executions"][0]["item_code"] == "tiktok_product_browser_fetch"

    browser_fetch = run_registered_task(
        "tiktok_fastmoss_product_ingest",
        params={"control_action": "browser_once", "execution_control_db_url": runtime_db_url},
    )["result"]["data"]["step_outputs"]["orchestrate_tiktok_fastmoss_product_ingest"]
    assert browser_fetch["execution_status"] == "success"
    assert browser_fetch["parent_updates"][0]["next_stage"] == "dispatch_tiktok_fastmoss_product_ingest_api_job"
    assert len(browser_calls) == 1

    product_red_dispatch = run_registered_task(
        "tiktok_fastmoss_product_ingest",
        params={"control_action": "executor_once", "execution_control_db_url": runtime_db_url},
    )["result"]["data"]["step_outputs"]["orchestrate_tiktok_fastmoss_product_ingest"]
    assert product_red_dispatch["current_stage"] == "waiting_tiktok_fastmoss_product_ingest"
    assert [
        job["job_code"]
        for job in product_red_dispatch["api_worker_jobs"]
        if job["job_code"] == "tiktok_fastmoss_product_ingest"
    ] == ["tiktok_fastmoss_product_ingest", "tiktok_fastmoss_product_ingest"]

    product_ingest = run_registered_task(
        "tiktok_fastmoss_product_ingest",
        params={"control_action": "api_worker_once", "execution_control_db_url": runtime_db_url},
    )["result"]["data"]["step_outputs"]["orchestrate_tiktok_fastmoss_product_ingest"]
    assert product_ingest["api_worker_job"]["status"] == "success"
    assert product_ingest["parent_updates"][0]["next_stage"] == "dispatch_feishu_tk_selection_table_writeback"
    assert len(product_ingest_calls) == 2

    run_registered_task(
        "tiktok_fastmoss_product_ingest",
        params={"control_action": "executor_once", "execution_control_db_url": runtime_db_url},
    )
    run_registered_task(
        "tiktok_fastmoss_product_ingest",
        params={"control_action": "api_worker_once", "execution_control_db_url": runtime_db_url},
    )
    finalized = run_registered_task(
        "tiktok_fastmoss_product_ingest",
        params={"control_action": "executor_once", "execution_control_db_url": runtime_db_url},
    )["result"]["data"]["step_outputs"]["orchestrate_tiktok_fastmoss_product_ingest"]
    assert finalized["request_id"] == request_id
    assert finalized["request_status"] == "success"
    assert finalized["result"]["product_id"] == PRODUCT_ID
    assert finalized["result"]["tiktok_browser_fallback_executions"][0]["status"] == "success"


def test_tiktok_fastmoss_product_ingest_table_mode_skips_when_fields_complete(
    monkeypatch,
    runtime_db_url,
):
    product_url = f"https://www.tiktok.com/shop/pdp/{PRODUCT_ID}"
    runtime_flow = __import__(
        "automation_business_scaffold.business.flows.refresh_current_competitor_table_flow",
        fromlist=["read_feishu_tk_selection_table_for_product"],
    )

    def fake_table_read(params):
        item = {
            "status": "skipped_completed",
            "source_record_id": "rec-complete",
            "record_id": "rec-complete",
            "product_url": product_url,
            "normalized_url": product_url,
            "product_id": PRODUCT_ID,
            "required_missing_fields": [],
        }
        return {
            "summary": {"total": 1, "counts": {"skipped_completed": 1}},
            "item": item,
            "items": [item],
            "status": "skipped_completed",
            "product_id": PRODUCT_ID,
            "product_url": product_url,
        }

    monkeypatch.setattr(runtime_flow, "read_feishu_tk_selection_table_for_product", fake_table_read)

    submitted = run_registered_task(
        "tiktok_fastmoss_product_ingest",
        params={
            "control_action": "submit",
            "product_url": product_url,
            "table_read_required": True,
            "feishu_access_token": "test-token",
            "execution_control_db_url": runtime_db_url,
        },
    )
    request_id = submitted["result"]["data"]["step_outputs"]["orchestrate_tiktok_fastmoss_product_ingest"][
        "request_id"
    ]

    run_registered_task(
        "tiktok_fastmoss_product_ingest",
        params={"control_action": "executor_once", "execution_control_db_url": runtime_db_url},
    )
    table_read = run_registered_task(
        "tiktok_fastmoss_product_ingest",
        params={"control_action": "api_worker_once", "execution_control_db_url": runtime_db_url},
    )["result"]["data"]["step_outputs"]["orchestrate_tiktok_fastmoss_product_ingest"]
    assert table_read["parent_updates"][0]["reason"] == "table_read_skipped_completed"

    finalized = run_registered_task(
        "tiktok_fastmoss_product_ingest",
        params={"control_action": "executor_once", "execution_control_db_url": runtime_db_url},
    )["result"]["data"]["step_outputs"]["orchestrate_tiktok_fastmoss_product_ingest"]
    assert finalized["request_id"] == request_id
    assert finalized["request_status"] == "success"
    assert finalized["result"]["item"]["status"] == "skipped_completed"
    assert [job["job_code"] for job in finalized["api_worker_jobs"]] == ["feishu_tk_selection_table_read"]


def test_tiktok_fastmoss_product_ingest_table_mode_skips_when_marked_unavailable(
    monkeypatch,
    runtime_db_url,
):
    product_url = f"https://www.tiktok.com/shop/pdp/{PRODUCT_ID}"
    runtime_flow = __import__(
        "automation_business_scaffold.business.flows.refresh_current_competitor_table_flow",
        fromlist=["read_feishu_tk_selection_table_for_product"],
    )

    def fake_table_read(params):
        del params
        item = {
            "status": "skipped_unavailable",
            "product_status": PRODUCT_STATUS_UNAVAILABLE,
            "source_record_id": "rec-unavailable",
            "record_id": "rec-unavailable",
            "product_url": product_url,
            "normalized_url": product_url,
            "product_id": PRODUCT_ID,
            "required_missing_fields": ["商品主图", "商品标题"],
        }
        return {
            "summary": {"total": 1, "counts": {"skipped_unavailable": 1}},
            "item": item,
            "items": [item],
            "status": "skipped_unavailable",
            "product_id": PRODUCT_ID,
            "product_url": product_url,
        }

    monkeypatch.setattr(runtime_flow, "read_feishu_tk_selection_table_for_product", fake_table_read)

    submitted = run_registered_task(
        "tiktok_fastmoss_product_ingest",
        params={
            "control_action": "submit",
            "product_url": product_url,
            "table_read_required": True,
            "feishu_access_token": "test-token",
            "execution_control_db_url": runtime_db_url,
        },
    )
    request_id = submitted["result"]["data"]["step_outputs"]["orchestrate_tiktok_fastmoss_product_ingest"][
        "request_id"
    ]

    run_registered_task(
        "tiktok_fastmoss_product_ingest",
        params={"control_action": "executor_once", "execution_control_db_url": runtime_db_url},
    )
    table_read = run_registered_task(
        "tiktok_fastmoss_product_ingest",
        params={"control_action": "api_worker_once", "execution_control_db_url": runtime_db_url},
    )["result"]["data"]["step_outputs"]["orchestrate_tiktok_fastmoss_product_ingest"]
    assert table_read["parent_updates"][0]["reason"] == "table_read_skipped_unavailable"

    finalized = run_registered_task(
        "tiktok_fastmoss_product_ingest",
        params={"control_action": "executor_once", "execution_control_db_url": runtime_db_url},
    )["result"]["data"]["step_outputs"]["orchestrate_tiktok_fastmoss_product_ingest"]
    assert finalized["request_id"] == request_id
    assert finalized["request_status"] == "success"
    assert finalized["result"]["item"]["status"] == "skipped_unavailable"
    assert [job["job_code"] for job in finalized["api_worker_jobs"]] == ["feishu_tk_selection_table_read"]


def test_fastmoss_fetch_reuses_db_cookie_without_preflight_login(
    monkeypatch,
    tmp_path,
    runtime_db_url,
):
    product_url = f"https://www.tiktok.com/shop/pdp/{PRODUCT_ID}"
    _patch_product_ingest_dependencies(monkeypatch, tmp_path, product_url)
    module = __import__(
        "automation_business_scaffold.business.flows.tiktok_fastmoss_product_ingest_flow",
        fromlist=["fetch_fastmoss_product_by_sku"],
    )
    store = RuntimeStore(db_url=runtime_db_url)
    context = build_fastmoss_cookie_cache_context(
        base_url="https://www.fastmoss.com",
        account_key="18000000000",
        region="US",
    )
    store.save_fastmoss_cookie_cache(
        cache_key=context["cache_key"],
        namespace="",
        account_key="18000000000",
        base_url="https://www.fastmoss.com",
        region="US",
        cookies=[
            {
                "name": "fd_tk",
                "value": "cached-fd-token",
                "domain": ".fastmoss.com",
                "path": "/",
                "secure": True,
            }
        ],
        cookie_count=1,
        has_fd_tk=True,
        fd_tk_digest="cached-digest",
        expires_at=9999999999.0,
    )

    payload = module.fetch_fastmoss_product_by_sku(
        {
            "product_id": PRODUCT_ID,
            "execution_control_db_url": runtime_db_url,
            "fastmoss_phone": "18000000000",
            "fastmoss_password": "secret",
        }
    )

    assert payload["settings"]["cookie_cache"]["status"] == "loaded"
    assert payload["settings"]["ensure_login"] is False
    assert _FakeFastMossSession.ensured_login is False
    assert _FakeFastMossSession.logged_in is False
    assert _FakeFastMossSession.imported_cookies[0]["value"] == "cached-fd-token"


def _patch_product_ingest_dependencies(monkeypatch, tmp_path, product_url: str) -> None:
    module = __import__(
        "automation_business_scaffold.business.flows.tiktok_fastmoss_product_ingest_flow",
        fromlist=[
            "fetch_tiktok_product_record",
            "download_tiktok_product_main_image",
            "FastMossHTTPSession",
        ],
    )
    image_path = tmp_path / "main.webp"
    _FakeFastMossSession.calls = []
    _FakeFastMossSession.ensured_login = False
    _FakeFastMossSession.logged_in = False
    _FakeFastMossSession.imported_cookies = []
    _FakeArtifactStore.uploads = []

    def fake_fetch_tiktok_product_record(product_url_arg, *, timeout=30, session=None, request_pacer=None):
        return TikTokProductRecord(
            source_url=product_url_arg,
            resolved_url=product_url_arg,
            normalized_url=product_url,
            product_id=PRODUCT_ID,
            title="TikTok Gift",
            holiday="情人节",
            main_image_url="https://example.com/main.webp",
            price_amount="12.99",
            price_currency="USD",
            price_text="$12.99",
            sales_count=94151,
            shop_name="TikTok Shop",
            shop_url="https://shop.tiktok.com/us/store/sample-shop/123",
            gallery_images=[
                {
                    "source_url": "https://example.com/main.webp",
                    "display_order": 0,
                    "media_role": "product_gallery_image",
                    "uri": "tos-test/main",
                },
                {
                    "source_url": "https://example.com/side.webp",
                    "display_order": 1,
                    "media_role": "product_gallery_image",
                    "uri": "tos-test/side",
                },
            ],
            sku_images=[
                {
                    "source_url": "https://example.com/sku-pink.webp",
                    "display_order": 0,
                    "media_role": "product_sku_image",
                    "sku_property_key": "Color:Pink",
                    "uri": "tos-test/sku-pink",
                }
            ],
            sku_options=[
                {
                    "name": "Color",
                    "values": [
                        {
                            "value": "Pink",
                            "value_id": "pink-id",
                            "sku_property_key": "Color:Pink",
                        }
                    ],
                    "source_platform": "tiktok",
                }
            ],
            skus=[
                {
                    "sku_id": "",
                    "sku_name": "Pink",
                    "spec_name": "Color: Pink",
                    "properties": [
                        {
                            "name": "Color",
                            "value": "Pink",
                            "value_id": "pink-id",
                            "sku_property_key": "Color:Pink",
                        }
                    ],
                    "sku_property_keys": ["Color:Pink"],
                    "source_platform": "tiktok",
                }
            ],
            rating_score=4.8,
            review_count=123,
            comment_count=45,
        )

    def fake_download_tiktok_product_main_image(product, *, download_dir, timeout=30, session=None, request_pacer=None):
        image_path.write_bytes(b"image")
        return replace(
            product,
            main_image_local_path=str(image_path),
            main_image_file_name="main.webp",
            main_image_mime_type="image/webp",
        )

    monkeypatch.setattr(module, "fetch_tiktok_product_record", fake_fetch_tiktok_product_record)
    monkeypatch.setattr(module, "download_tiktok_product_main_image", fake_download_tiktok_product_main_image)
    monkeypatch.setattr(module, "FastMossHTTPSession", _FakeFastMossSession)
    monkeypatch.setattr(module, "create_artifact_store", lambda settings: _FakeArtifactStore())
    monkeypatch.setattr(module.requests, "Session", _FakeDownloadSession)


def _fake_tiktok_browser_fetch_result(product_url: str) -> dict[str, object]:
    product = TikTokProductRecord(
        source_url=product_url,
        resolved_url=product_url,
        normalized_url=product_url,
        product_id=PRODUCT_ID,
        title="TikTok Browser Gift",
        holiday="其他",
        main_image_url="https://example.com/browser-main.webp",
        price_amount="12.99",
        price_currency="USD",
        price_text="$12.99",
        sales_count=3098,
        shop_name="TikTok Browser Shop",
        shop_url="https://shop.tiktok.com/us/store/browser-shop/123",
        gallery_images=[
            {
                "source_url": "https://example.com/browser-main.webp",
                "display_order": 0,
                "media_role": "product_gallery_image",
                "uri": "tos-test/browser-main",
            }
        ],
        skus=[
            {
                "sku_id": "",
                "sku_name": "Default",
                "spec_name": "Specification: Default",
                "source_platform": "tiktok",
            }
        ],
        rating_score=4.4,
        review_count=392,
        comment_count=392,
    )
    item = {
        "product_id": PRODUCT_ID,
        "source_url": product_url,
        "resolved_url": product_url,
        "normalized_url": product_url,
        "status": "fetched",
        "fetch_source": "browser",
        "logical_fields": product.to_dict(),
        "main_image_local_path": "",
        "main_image_file_name": "",
        "main_image_mime_type": "",
    }
    return {
        "summary": {"total": 1, "counts": {"fetched": 1}},
        "item": item,
        "items": [item],
        "product": product.to_dict(),
        "product_id": PRODUCT_ID,
        "normalized_url": product_url,
        "fetch_source": "browser",
    }


def _fake_product_ingest_result(product_url: str) -> dict[str, object]:
    return {
        "summary": {"total": 1, "counts": {"success": 1}, "fact_entity_count": 2},
        "item": {"product_id": PRODUCT_ID, "status": "success"},
        "items": [{"product_id": PRODUCT_ID, "status": "success"}],
        "product_id": PRODUCT_ID,
        "tiktok": {
            "product_id": PRODUCT_ID,
            "product": {
                "product_id": PRODUCT_ID,
                "normalized_url": product_url,
                "title": "TikTok Gift",
                "shop_name": "TikTok Shop",
                "price_text": "$12.99",
                "comment_count": 392,
                "rating_score": 4.4,
                "sales_count": 3098,
            },
        },
        "fastmoss": {"product_id": PRODUCT_ID, "fastmoss": {}},
        "media_upload": {"summary": {"total": 0, "counts": {"uploaded": 0, "failed": 0}}, "items": []},
        "persisted": {"summary": {"fact_entity_count": 2}},
    }


def _assert_successful_product_ingest_payload(payload, db_url) -> None:
    assert payload["status"] == "success"
    step_outputs = payload["result"]["data"]["step_outputs"]
    assert list(step_outputs) == ["orchestrate_tiktok_fastmoss_product_ingest"]
    ingest_output = step_outputs["orchestrate_tiktok_fastmoss_product_ingest"]
    assert ingest_output["product_id"] == PRODUCT_ID
    assert ingest_output["tiktok"]["product_id"] == PRODUCT_ID
    assert ingest_output["fastmoss"]["fastmoss"]["base"]["shop"]["name"] == "FastMoss Shop"
    media_upload = ingest_output["media_upload"]
    assert media_upload["summary"]["counts"] == {"uploaded": 4, "failed": 0}
    assert len(_FakeArtifactStore.uploads) == 4
    assert all(
        str(item["object_key"]).startswith(f"test-prefix/product-media/{PRODUCT_ID}/")
        for item in _FakeArtifactStore.uploads
    )
    persisted = ingest_output["persisted"]
    assert persisted["summary"]["raw_api_response_count"] >= 4
    assert any(
        raw.get("source_endpoint") == "fastmoss.goods.v3.base"
        for raw in persisted["raw_api_responses"]
    )
    assert persisted["fact_entities"]
    assert persisted["fact_media_assets"]
    assert persisted["fact_metric_observations"]
    assert _FakeFastMossSession.ensured_login is True
    assert _FakeFastMossSession.calls == [
        ("base", PRODUCT_ID),
        ("overview", PRODUCT_ID),
        ("skus", PRODUCT_ID),
        ("sku_distribution", PRODUCT_ID),
    ]

    fact_store = TKFactStore(runtime_store=RuntimeStore(db_url=db_url))
    product = fact_store.get_product(product_id=PRODUCT_ID)
    assert product["title"] == "FastMoss Gift"
    assert product["seller_name"] == "FastMoss Shop"

    with fact_store._engine.connect() as connection:  # noqa: SLF001
        media_rows = connection.execute(
            fact_store._text(  # noqa: SLF001
                """
                SELECT object_key, file_token, metadata_json
                FROM tk_media_assets
                WHERE object_key <> ''
                ORDER BY object_key
                """
            )
        ).mappings().all()
        media_role_rows = connection.execute(
            fact_store._text(  # noqa: SLF001
                """
                SELECT ema.media_role
                FROM tk_entity_media_assets ema
                JOIN tk_media_assets ma ON ma.asset_id = ema.asset_id
                WHERE ema.entity_type = 'product'
                  AND ema.entity_external_id = :product_id
                  AND ma.object_key <> ''
                ORDER BY ema.media_role
                """
            ),
            {"product_id": PRODUCT_ID},
        ).mappings().all()
        latest_metric = connection.execute(
            fact_store._text(  # noqa: SLF001
                """
                SELECT payload_json
                FROM tk_product_window_latest
                WHERE product_id = :product_id
                  AND source_platform = 'tiktok'
                  AND source_endpoint = 'tiktok.product.http_request'
                  AND window_days = 0
                LIMIT 1
                """
            ),
            {"product_id": PRODUCT_ID},
        ).mappings().first()
        latest_fastmoss_metric = connection.execute(
            fact_store._text(  # noqa: SLF001
                """
                SELECT payload_json
                FROM tk_product_window_latest
                WHERE product_id = :product_id
                  AND source_platform = 'fastmoss'
                  AND source_endpoint = 'fastmoss.goods.v3.base'
                  AND window_days = 0
                LIMIT 1
                """
            ),
            {"product_id": PRODUCT_ID},
        ).mappings().first()
        latest_fastmoss_overview_metric = connection.execute(
            fact_store._text(  # noqa: SLF001
                """
                SELECT payload_json
                FROM tk_product_window_latest
                WHERE product_id = :product_id
                  AND source_platform = 'fastmoss'
                  AND source_endpoint = 'fastmoss.goods.v3.overview'
                  AND window_days = 28
                LIMIT 1
                """
            ),
            {"product_id": PRODUCT_ID},
        ).mappings().first()
        sku_row = connection.execute(
            fact_store._text(  # noqa: SLF001
                """
                SELECT price_text, stock_count, facts_json
                FROM tk_product_skus
                WHERE product_id = :product_id
                  AND sku_id = 'sku-pink'
                LIMIT 1
                """
            ),
            {"product_id": PRODUCT_ID},
        ).mappings().first()
        latest_sku_metric = connection.execute(
            fact_store._text(  # noqa: SLF001
                """
                SELECT sold_count, sale_amount, stock_count, payload_json
                FROM tk_product_sku_window_latest
                WHERE product_id = :product_id
                  AND sku_id = 'sku-pink'
                  AND source_platform = 'fastmoss'
                  AND window_days = 28
                LIMIT 1
                """
            ),
            {"product_id": PRODUCT_ID},
        ).mappings().first()
        daily_metric = connection.execute(
            fact_store._text(  # noqa: SLF001
                """
                SELECT sold_count, sale_amount, price_amount, payload_json
                FROM tk_product_daily_metrics
                WHERE product_id = :product_id
                  AND source_platform = 'fastmoss'
                  AND metric_date = '2026-04-01'
                LIMIT 1
                """
            ),
            {"product_id": PRODUCT_ID},
        ).mappings().first()
        channel_distribution = connection.execute(
            fact_store._text(  # noqa: SLF001
                """
                SELECT source_name, metric_value, metric_amount, payload_json
                FROM tk_product_distribution_window_latest
                WHERE product_id = :product_id
                  AND distribution_type = 'channel'
                  AND source_key = 'common.goods.affiliate'
                  AND source_platform = 'fastmoss'
                  AND window_days = 28
                LIMIT 1
                """
            ),
            {"product_id": PRODUCT_ID},
        ).mappings().first()
        distribution_count = connection.execute(
            fact_store._text(  # noqa: SLF001
                """
                SELECT COUNT(*) AS count
                FROM tk_product_distribution_window_latest
                WHERE product_id = :product_id
                  AND source_platform = 'fastmoss'
                  AND window_days = 28
                """
            ),
            {"product_id": PRODUCT_ID},
        ).mappings().first()
        observation_count = connection.execute(
            fact_store._text(  # noqa: SLF001
                """
                SELECT COUNT(*) AS count
                FROM tk_product_window_observations
                WHERE product_id = :product_id
                  AND source_platform = 'tiktok'
                  AND source_endpoint = 'tiktok.product.http_request'
                  AND window_days = 0
                """
            ),
            {"product_id": PRODUCT_ID},
        ).mappings().first()
    assert len(media_rows) == 4
    assert all(str(row["object_key"]).startswith("test-prefix/product-media/") for row in media_rows)
    file_tokens = {str(row["file_token"]) for row in media_rows if str(row["file_token"] or "")}
    assert file_tokens == {
        "tiktok_uri:tos-test/main",
        "tiktok_uri:tos-test/side",
        "tiktok_uri:tos-test/sku-pink",
    }
    main_digest = hashlib.sha1("tiktok_uri:tos-test/main".encode("utf-8")).hexdigest()[:16]
    side_digest = hashlib.sha1("tiktok_uri:tos-test/side".encode("utf-8")).hexdigest()[:16]
    sku_digest = hashlib.sha1("tiktok_uri:tos-test/sku-pink".encode("utf-8")).hexdigest()[:16]
    object_keys = {str(row["object_key"]) for row in media_rows}
    assert any(f"product_main_image-{main_digest}-main.webp" in object_key for object_key in object_keys)
    assert any(f"product_gallery_image-{side_digest}-" in object_key for object_key in object_keys)
    assert any(f"product_sku_image-{sku_digest}-" in object_key for object_key in object_keys)
    media_roles = {str(row["media_role"]) for row in media_role_rows}
    assert {"product_main_image", "product_gallery_image", "product_sku_image"} <= media_roles
    assert latest_metric is not None
    metric_payload = json.loads(str(latest_metric["payload_json"]))
    assert metric_payload["rating_score"] == 4.8
    assert metric_payload["review_count"] == 123
    assert metric_payload["comment_count"] == 45
    assert latest_fastmoss_metric is not None
    fastmoss_metric_payload = json.loads(str(latest_fastmoss_metric["payload_json"]))
    assert fastmoss_metric_payload["rating_score"] == 4.6
    assert fastmoss_metric_payload["review_count"] == 321
    assert fastmoss_metric_payload["sales_count"] == 900
    assert latest_fastmoss_overview_metric is not None
    overview_metric_payload = json.loads(str(latest_fastmoss_overview_metric["payload_json"]))
    assert overview_metric_payload["d_type"] == 28
    assert overview_metric_payload["sales_7d"] == 88
    assert overview_metric_payload["sales_count"] == 88
    assert sku_row is not None
    assert sku_row["price_text"] == ""
    assert sku_row["stock_count"] == 0
    sku_facts = json.loads(str(sku_row["facts_json"]))
    assert sku_facts["tiktok_sku_name"] == "Pink"
    assert sku_facts["tiktok_spec_name"] == "Color: Pink"
    assert sku_facts["tiktok_properties"][0]["sku_property_key"] == "Color:Pink"
    assert latest_sku_metric is not None
    assert latest_sku_metric["sold_count"] == 31
    assert latest_sku_metric["sale_amount"] == 401.69
    assert latest_sku_metric["stock_count"] == 7
    sku_metric_payload = json.loads(str(latest_sku_metric["payload_json"]))
    assert sku_metric_payload["price_text"] == "$12.99"
    assert sku_metric_payload["stock_count"] == 7
    assert sku_metric_payload["source_endpoint"] == "fastmoss.goods.productSku"
    assert sku_metric_payload["sold_proportion"] == 1.0
    assert daily_metric is not None
    assert daily_metric["sold_count"] == 12
    assert daily_metric["sale_amount"] == 155.88
    assert daily_metric["price_amount"] == 12.99
    daily_payload = json.loads(str(daily_metric["payload_json"]))
    assert daily_payload["source_endpoint"] == "fastmoss.goods.v3.overview"
    assert channel_distribution is not None
    assert channel_distribution["source_name"] == "达人联盟"
    assert channel_distribution["metric_value"] == 66
    assert channel_distribution["metric_amount"] == 914.5
    channel_payload = json.loads(str(channel_distribution["payload_json"]))
    assert channel_payload["sold_proportion"] == 0.75
    assert channel_payload["gmv_proportion"] == 0.8
    assert distribution_count is not None
    assert distribution_count["count"] == 3
    assert "fastmoss_snapshot" not in product["facts"]
    assert "logical_fields" not in product["facts"]
    assert "raw" not in product["facts"]
    assert observation_count is not None
    assert observation_count["count"] >= 1
