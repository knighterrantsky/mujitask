from __future__ import annotations

from pathlib import Path

from automation_business_scaffold.flows.tiktok_feishu_sync_flow import (
    run_tiktok_feishu_batch_sync,
    run_tiktok_product_link_cleanup,
)
from automation_business_scaffold.models import TikTokProductRecord


def _sample_browser_product(*, source_url: str, normalized_url: str, tmp_path: Path) -> TikTokProductRecord:
    main_image = tmp_path / "main-image.png"
    page_image = tmp_path / "page.png"
    main_image.write_bytes(b"main-image")
    page_image.write_bytes(b"page-image")
    return TikTokProductRecord(
        source_url=source_url,
        resolved_url=normalized_url,
        normalized_url=normalized_url,
        product_id="4444444444444444444",
        title="Halloween Lights",
        holiday="万圣节",
        main_image_url="https://example.com/main-image.png",
        price_amount="45.67",
        price_currency="USD",
        price_text="$45.67",
        sales_count=0,
        shop_name="Sample Shop",
        shop_url="https://shop.tiktok.com/us/store/sample-shop/123",
        main_image_local_path=str(main_image),
        main_image_file_name=main_image.name,
        main_image_mime_type="image/png",
        product_page_screenshot_local_path=str(page_image),
        product_page_screenshot_file_name=page_image.name,
        product_page_screenshot_mime_type="image/png",
    )


def test_run_tiktok_product_link_cleanup_draft_previews_duplicate_deletions(monkeypatch):
    module = __import__(
        "automation_business_scaffold.flows.tiktok_feishu_sync_flow",
        fromlist=["run_tiktok_product_link_cleanup"],
    )

    class FakeClient:
        def __init__(self, access_token: str) -> None:
            self.access_token = access_token

        def list_all_records(self, *, app_token, table_id, page_size=100, filter_expr=None, view_id=None):
            assert app_token == "app999"
            assert table_id == "tbl999"
            assert view_id == "vew999"
            return [
                {
                    "record_id": "rec-1",
                    "fields": {
                        "产品链接": {
                            "link": "https://www.tiktok.com/shop/pdp/1111111111111111111?source=product_detail"
                        }
                    },
                },
                {
                    "record_id": "rec-2",
                    "fields": {
                        "产品链接": {
                            "link": "https://www.tiktok.com/shop/pdp/1111111111111111111?foo=bar"
                        }
                    },
                },
                {
                    "record_id": "rec-3",
                    "fields": {
                        "产品链接": {
                            "link": "https://www.tiktok.com/shop/pdp/2222222222222222222"
                        }
                    },
                },
                {
                    "record_id": "rec-4",
                    "fields": {
                        "产品链接": {
                            "link": "https://www.tiktok.com/shop/pdp/not-a-product"
                        }
                    },
                },
            ]

    monkeypatch.setattr(module, "FeishuBitableClient", FakeClient)

    payload = run_tiktok_product_link_cleanup(
        {
            "table_url": "https://my.feishu.cn/base/app999?table=tbl999&view=vew999",
            "access_token_env": "TOKEN_DIRECT_VALUE",
            "url_field_name": "产品链接",
            "normalized_url_field_name": "标准产品链接",
            "cleanup_status_field_name": "链接整理状态",
            "run_mode": "draft",
        }
    )

    assert payload["summary"]["counts"] == {
        "preview": 2,
        "delete_preview": 1,
        "invalid_url": 1,
    }
    assert payload["items"][0]["deleted_record_ids"] == ["rec-2"]
    assert payload["items"][1]["status"] == "delete_preview"
    assert payload["items"][3]["status"] == "invalid_url"


def test_run_tiktok_product_link_cleanup_canary_deletes_duplicates_and_updates_kept_rows(monkeypatch):
    module = __import__(
        "automation_business_scaffold.flows.tiktok_feishu_sync_flow",
        fromlist=["run_tiktok_product_link_cleanup"],
    )

    class FakeClient:
        def __init__(self, access_token: str) -> None:
            self.access_token = access_token
            self.deleted: list[str] = []
            self.updated: list[tuple[str, dict[str, object]]] = []

        def list_all_records(self, *, app_token, table_id, page_size=100, filter_expr=None, view_id=None):
            return [
                {
                    "record_id": "rec-1",
                    "fields": {
                        "产品链接": {
                            "link": "https://www.tiktok.com/shop/pdp/1111111111111111111?source=product_detail"
                        }
                    },
                },
                {
                    "record_id": "rec-2",
                    "fields": {
                        "产品链接": {
                            "link": "https://www.tiktok.com/shop/pdp/1111111111111111111?foo=bar"
                        }
                    },
                },
            ]

        def delete_record(self, app_token: str, table_id: str, record_id: str):
            self.deleted.append(record_id)
            return {"code": 0}

        def update_record(self, app_token: str, table_id: str, record_id: str, fields: dict[str, object]):
            self.updated.append((record_id, fields))
            return {"code": 0}

    fake_client = FakeClient("ignored")
    monkeypatch.setattr(module, "FeishuBitableClient", lambda _access_token: fake_client)

    payload = run_tiktok_product_link_cleanup(
        {
            "table_url": "https://my.feishu.cn/base/app999?table=tbl999",
            "access_token_env": "TOKEN_DIRECT_VALUE",
            "url_field_name": "产品链接",
            "normalized_url_field_name": "标准产品链接",
            "cleanup_status_field_name": "链接整理状态",
            "run_mode": "canary",
        }
    )

    assert fake_client.deleted == ["rec-2"]
    assert fake_client.updated == [
        (
            "rec-1",
            {
                "产品链接": {
                    "text": "https://www.tiktok.com/shop/pdp/1111111111111111111",
                    "link": "https://www.tiktok.com/shop/pdp/1111111111111111111",
                },
                "链接整理状态": "deduplicated",
                "删除重复数": 1,
                "标准产品链接": {
                    "text": "https://www.tiktok.com/shop/pdp/1111111111111111111",
                    "link": "https://www.tiktok.com/shop/pdp/1111111111111111111",
                },
            },
        )
    ]
    assert payload["summary"]["counts"] == {"updated": 1, "deleted": 1}


def test_run_tiktok_feishu_batch_sync_draft_previews_updates_and_skips_rows(monkeypatch, tmp_path):
    module = __import__(
        "automation_business_scaffold.flows.tiktok_feishu_sync_flow",
        fromlist=["run_tiktok_feishu_batch_sync"],
    )

    class FakeClient:
        def __init__(self, access_token: str) -> None:
            self.access_token = access_token

        def list_all_records(self, *, app_token, table_id, page_size=100, filter_expr=None, view_id=None):
            return [
                {
                    "record_id": "rec-1",
                    "fields": {
                        "产品链接": {
                            "link": "https://www.tiktok.com/shop/pdp/1111111111111111111?source=product_detail"
                        }
                    },
                },
                {
                    "record_id": "rec-2",
                    "fields": {
                        "产品链接": {
                            "link": "https://www.tiktok.com/shop/pdp/1111111111111111111?foo=bar"
                        }
                    },
                },
                {
                    "record_id": "rec-3",
                    "fields": {
                        "产品链接": {
                            "link": "https://www.tiktok.com/shop/pdp/2222222222222222222"
                        },
                        "采集状态": "success",
                    },
                },
                {
                    "record_id": "rec-4",
                    "fields": {
                        "产品链接": {
                            "link": "https://www.tiktok.com/shop/pdp/3333333333333333333"
                        },
                        "链接整理状态": "invalid_url",
                    },
                },
            ]

    monkeypatch.setattr(module, "FeishuBitableClient", FakeClient)
    monkeypatch.setattr(
        module,
        "fetch_tiktok_product_record_via_browser",
        lambda source_url, profile_ref=None, capture_page_screenshot=True: _sample_browser_product(
            source_url=source_url,
            normalized_url="https://www.tiktok.com/shop/pdp/1111111111111111111",
            tmp_path=tmp_path,
        ),
    )

    payload = run_tiktok_feishu_batch_sync(
        {
            "table_url": "https://my.feishu.cn/base/app999?table=tbl999",
            "access_token_env": "TOKEN_DIRECT_VALUE",
            "url_field_name": "产品链接",
            "run_mode": "draft",
            "profile_ref": "local-chrome",
        }
    )

    assert payload["summary"]["counts"] == {
        "duplicate_blocked": 1,
        "skipped_completed": 1,
        "skipped_cleanup_error": 1,
        "preview": 1,
    }
    preview_item = next(item for item in payload["items"] if item["record_id"] == "rec-1")
    assert preview_item["status"] == "preview"
    assert preview_item["fields"]["采集状态"] == "success"


def test_run_tiktok_feishu_batch_sync_canary_uploads_and_updates_rows(monkeypatch, tmp_path):
    module = __import__(
        "automation_business_scaffold.flows.tiktok_feishu_sync_flow",
        fromlist=["run_tiktok_feishu_batch_sync"],
    )

    class FakeClient:
        def __init__(self, access_token: str) -> None:
            self.access_token = access_token
            self.uploads: list[str] = []
            self.updated: list[tuple[str, dict[str, object]]] = []

        def list_all_records(self, *, app_token, table_id, page_size=100, filter_expr=None, view_id=None):
            return [
                {
                    "record_id": "rec-1",
                    "fields": {
                        "产品链接": {
                            "link": "https://www.tiktok.com/shop/pdp/1111111111111111111"
                        }
                    },
                }
            ]

        def upload_media(self, *, file_name, file_data, parent_node, parent_type="bitable_file", extra=None):
            self.uploads.append(file_name)
            return f"file-token-{file_name}"

        def update_record(self, app_token: str, table_id: str, record_id: str, fields: dict[str, object]):
            self.updated.append((record_id, fields))
            return {"code": 0}

    fake_client = FakeClient("ignored")
    monkeypatch.setattr(module, "FeishuBitableClient", lambda _access_token: fake_client)
    monkeypatch.setattr(
        module,
        "fetch_tiktok_product_record_via_browser",
        lambda source_url, profile_ref=None, capture_page_screenshot=True: _sample_browser_product(
            source_url=source_url,
            normalized_url="https://www.tiktok.com/shop/pdp/1111111111111111111",
            tmp_path=tmp_path,
        ),
    )

    payload = run_tiktok_feishu_batch_sync(
        {
            "table_url": "https://my.feishu.cn/base/app999?table=tbl999",
            "access_token_env": "TOKEN_DIRECT_VALUE",
            "url_field_name": "产品链接",
            "run_mode": "canary",
            "profile_ref": "local-chrome",
        }
    )

    assert fake_client.uploads == ["main-image.png", "page.png"]
    assert fake_client.updated[0][0] == "rec-1"
    assert fake_client.updated[0][1]["商品主图"] == [{"file_token": "file-token-main-image.png"}]
    assert fake_client.updated[0][1]["商品页截图"] == [{"file_token": "file-token-page.png"}]
    assert fake_client.updated[0][1]["采集状态"] == "success"
    assert payload["summary"]["counts"] == {"updated": 1}


def test_run_tiktok_feishu_batch_sync_canary_writes_failed_status_on_browser_error(monkeypatch):
    module = __import__(
        "automation_business_scaffold.flows.tiktok_feishu_sync_flow",
        fromlist=["run_tiktok_feishu_batch_sync"],
    )

    class FakeClient:
        def __init__(self, access_token: str) -> None:
            self.access_token = access_token
            self.updated: list[tuple[str, dict[str, object]]] = []

        def list_all_records(self, *, app_token, table_id, page_size=100, filter_expr=None, view_id=None):
            return [
                {
                    "record_id": "rec-1",
                    "fields": {
                        "产品链接": {
                            "link": "https://www.tiktok.com/shop/pdp/1111111111111111111"
                        }
                    },
                }
            ]

        def update_record(self, app_token: str, table_id: str, record_id: str, fields: dict[str, object]):
            self.updated.append((record_id, fields))
            return {"code": 0}

    fake_client = FakeClient("ignored")
    monkeypatch.setattr(module, "FeishuBitableClient", lambda _access_token: fake_client)
    monkeypatch.setattr(
        module,
        "fetch_tiktok_product_record_via_browser",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("browser failed")),
    )

    payload = run_tiktok_feishu_batch_sync(
        {
            "table_url": "https://my.feishu.cn/base/app999?table=tbl999",
            "access_token_env": "TOKEN_DIRECT_VALUE",
            "url_field_name": "产品链接",
            "run_mode": "canary",
        }
    )

    assert fake_client.updated == [
        (
            "rec-1",
            {
                "采集状态": "failed",
                "采集错误": "browser failed",
            },
        )
    ]
    assert payload["summary"]["counts"] == {"failed": 1}
