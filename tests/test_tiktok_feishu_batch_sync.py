from __future__ import annotations

from dataclasses import replace

from automation_business_scaffold.flows.tiktok_feishu_sync_flow import (
    run_tiktok_feishu_batch_sync,
    run_tiktok_feishu_single_sync,
)
from automation_business_scaffold.models import TikTokProductRecord


def test_run_tiktok_feishu_single_sync_live_mode_uploads_and_creates_record(
    monkeypatch,
    tmp_path,
):
    module = __import__(
        "automation_business_scaffold.flows.tiktok_feishu_sync_flow",
        fromlist=["run_tiktok_feishu_single_sync"],
    )

    class FakeClient:
        def __init__(self, access_token: str) -> None:
            self.access_token = access_token
            self.created: list[tuple[str, str, dict[str, object]]] = []

        def list_all_records(
            self,
            *,
            app_token,
            table_id,
            page_size=100,
            filter_expr=None,
            view_id=None,
        ):
            assert app_token == "app999"
            assert table_id == "tbl999"
            assert view_id is None
            return []

        def upload_media(self, *, file_name, file_data, parent_node, parent_type="bitable_file", extra=None):
            assert file_name == "4444444444444444444.jpg"
            assert file_data == b"image"
            assert parent_node == "app999"
            return "file-token-001"

        def create_record(self, *, app_token, table_id, fields):
            self.created.append((app_token, table_id, fields))
            return {"data": {"record": {"record_id": "rec-created-001"}}}

    fake_client = FakeClient("ignored")

    def fake_client_factory(_access_token: str):
        return fake_client

    def fake_fetch(product_url: str):
        return TikTokProductRecord(
            source_url=product_url,
            resolved_url=product_url,
            product_id="4444444444444444444",
            title="Halloween Lights",
            holiday="万圣节",
            main_image_url="https://example.com/4444444444444444444.jpg",
            price_amount="45.67",
            price_currency="USD",
            price_text="$45.67",
            sales_count=0,
            shop_name="",
            shop_url="",
        )

    def fake_download(product: TikTokProductRecord):
        local_path = tmp_path / "4444444444444444444.jpg"
        local_path.write_bytes(b"image")
        return replace(
            product,
            main_image_local_path=str(local_path),
            main_image_file_name=local_path.name,
            main_image_mime_type="image/jpeg",
        )

    sleep_calls: list[float] = []

    monkeypatch.setattr(module, "FeishuBitableClient", fake_client_factory)
    monkeypatch.setattr(module, "fetch_tiktok_product_record", fake_fetch)
    monkeypatch.setattr(module, "download_tiktok_product_main_image", fake_download)
    monkeypatch.setattr(module.random, "uniform", lambda _start, _end: 0.0)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    payload = run_tiktok_feishu_single_sync(
        {
            "product_url": "https://www.tiktok.com/shop/pdp/4444444444444444444",
            "table_url": "https://my.feishu.cn/base/app999?table=tbl999&view=vew999",
            "access_token_env": "TOKEN_DIRECT_VALUE",
            "run_mode": "live",
        }
    )

    assert payload == {
        "status": "inserted",
        "record_id": "rec-created-001",
        "product_url": "https://www.tiktok.com/shop/pdp/4444444444444444444",
        "product_id": "4444444444444444444",
        "fields": {
            "产品链接": "https://www.tiktok.com/shop/pdp/4444444444444444444",
            "SKU-ID": "4444444444444444444",
            "图片": [{"file_token": "file-token-001"}],
            "标题": "Halloween Lights",
            "节日": "万圣节",
            "价格": "45.67",
        },
    }
    assert fake_client.created == [
        (
            "app999",
            "tbl999",
            {
                "产品链接": "https://www.tiktok.com/shop/pdp/4444444444444444444",
                "SKU-ID": "4444444444444444444",
                "图片": [{"file_token": "file-token-001"}],
                "标题": "Halloween Lights",
                "节日": "万圣节",
                "价格": "45.67",
            },
        )
    ]
    assert sleep_calls == [1.0, 1.0, 1.0]


def test_run_tiktok_feishu_single_sync_skips_existing_url_before_fetch(monkeypatch):
    module = __import__(
        "automation_business_scaffold.flows.tiktok_feishu_sync_flow",
        fromlist=["run_tiktok_feishu_single_sync"],
    )

    class FakeClient:
        def __init__(self, access_token: str) -> None:
            self.access_token = access_token

        def list_all_records(
            self,
            *,
            app_token,
            table_id,
            page_size=100,
            filter_expr=None,
            view_id=None,
        ):
            return [
                {
                    "record_id": "rec-existing-url",
                    "fields": {
                        "产品链接": {
                            "link": "https://www.tiktok.com/shop/pdp/1111111111111111111",
                        },
                        "SKU-ID": "sku-existing",
                    },
                }
            ]

    monkeypatch.setattr(module, "FeishuBitableClient", FakeClient)
    monkeypatch.setattr(
        module,
        "fetch_tiktok_product_record",
        lambda _product_url: (_ for _ in ()).throw(AssertionError("fetch should not be called")),
    )

    payload = run_tiktok_feishu_single_sync(
        {
            "product_url": "https://www.tiktok.com/shop/pdp/1111111111111111111",
            "table_url": "https://my.feishu.cn/base/app123?table=tbl123",
            "access_token_env": "TOKEN_DIRECT_VALUE",
            "run_mode": "live",
        }
    )

    assert payload == {
        "status": "skipped_existing",
        "record_id": "rec-existing-url",
        "product_url": "https://www.tiktok.com/shop/pdp/1111111111111111111",
        "product_id": "",
        "fields": {},
        "duplicate_reason": "url",
        "existing_record_id": "rec-existing-url",
    }


def test_run_tiktok_feishu_batch_sync_processes_urls_sequentially_and_keeps_summary(
    monkeypatch,
    tmp_path,
):
    module = __import__(
        "automation_business_scaffold.flows.tiktok_feishu_sync_flow",
        fromlist=["run_tiktok_feishu_batch_sync"],
    )

    class FakeClient:
        def __init__(self, access_token: str) -> None:
            self.access_token = access_token
            self.created: list[tuple[str, str, dict[str, object]]] = []

        def list_all_records(
            self,
            *,
            app_token,
            table_id,
            page_size=100,
            filter_expr=None,
            view_id=None,
        ):
            return []

        def upload_media(self, *, file_name, file_data, parent_node, parent_type="bitable_file", extra=None):
            return f"file-token-{file_name}"

        def create_record(self, *, app_token, table_id, fields):
            record_id = f"rec-{len(self.created) + 1}"
            self.created.append((app_token, table_id, fields))
            return {"data": {"item": {"record_id": record_id}}}

    fake_client = FakeClient("ignored")
    call_order: list[str] = []
    sleep_calls: list[float] = []

    def fake_client_factory(_access_token: str):
        return fake_client

    def fake_fetch(product_url: str):
        call_order.append(f"fetch:{product_url}")
        if product_url.endswith("/5005"):
            raise RuntimeError("fetch failed")
        product_id = product_url.rsplit("/", 1)[-1]
        return TikTokProductRecord(
            source_url=product_url,
            resolved_url=product_url,
            product_id=product_id,
            title=f"title-{product_id}",
            holiday="其他",
            main_image_url=f"https://example.com/{product_id}.jpg",
            price_amount="12.34",
            price_currency="USD",
            price_text="$12.34",
            sales_count=0,
            shop_name="",
            shop_url="",
        )

    def fake_download(product: TikTokProductRecord):
        call_order.append(f"download:{product.product_id}")
        local_path = tmp_path / f"{product.product_id}.jpg"
        local_path.write_bytes(product.product_id.encode("utf-8"))
        return replace(
            product,
            main_image_local_path=str(local_path),
            main_image_file_name=local_path.name,
            main_image_mime_type="image/jpeg",
        )

    monkeypatch.setattr(module, "FeishuBitableClient", fake_client_factory)
    monkeypatch.setattr(module, "fetch_tiktok_product_record", fake_fetch)
    monkeypatch.setattr(module, "download_tiktok_product_main_image", fake_download)
    monkeypatch.setattr(module.random, "uniform", lambda _start, _end: 0.0)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    payload = run_tiktok_feishu_batch_sync(
        {
            "product_urls": [
                "https://www.tiktok.com/shop/pdp/1001",
                "https://www.tiktok.com/shop/pdp/2002",
                "https://www.tiktok.com/shop/pdp/1001",
                "https://www.tiktok.com/shop/pdp/5005",
            ],
            "table_url": "https://my.feishu.cn/base/app555?table=tbl555",
            "access_token_env": "TOKEN_DIRECT_VALUE",
            "run_mode": "live",
            "pause_every": 2,
        }
    )

    assert payload["summary"] == {
        "total": 4,
        "processed": 4,
        "inserted": 2,
        "skipped_existing": 1,
        "previewed": 0,
        "failed": 1,
    }
    assert [item["status"] for item in payload["items"]] == [
        "inserted",
        "inserted",
        "skipped_existing",
        "failed",
    ]
    assert payload["items"][2]["duplicate_reason"] == "url"
    assert payload["items"][3]["error"] == "fetch failed"
    assert payload["settings"] == {
        "run_mode": "live",
        "write_back": True,
        "step_delay_sec": 1.0,
        "step_delay_jitter_sec": 1.0,
        "record_delay_sec": 2.0,
        "record_delay_jitter_sec": 2.0,
        "pause_every": 2,
        "pause_sec": 8.0,
        "continue_on_error": True,
    }
    assert call_order == [
        "fetch:https://www.tiktok.com/shop/pdp/1001",
        "download:1001",
        "fetch:https://www.tiktok.com/shop/pdp/2002",
        "download:2002",
        "fetch:https://www.tiktok.com/shop/pdp/5005",
    ]
    assert sleep_calls == [1.0, 1.0, 1.0, 2.0, 1.0, 1.0, 1.0, 2.0, 8.0, 2.0]
