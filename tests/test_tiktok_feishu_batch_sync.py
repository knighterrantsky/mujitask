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
            "run_mode": "draft",
        }
    )

    assert payload["summary"]["counts"] == {
        "preview": 2,
        "delete_preview": 1,
        "invalid_url": 1,
    }
    keeper = next(item for item in payload["items"] if item["record_id"] == "rec-1")
    assert keeper["status"] == "preview"
    assert keeper["deleted_record_ids"] == ["rec-2"]
    assert keeper["normalized_url"] == "https://www.tiktok.com/shop/pdp/1111111111111111111"
    assert next(item for item in payload["items"] if item["record_id"] == "rec-2")["status"] == "delete_preview"
    assert next(item for item in payload["items"] if item["record_id"] == "rec-4")["status"] == "invalid_url"


def test_run_tiktok_product_link_cleanup_canary_deletes_duplicates_and_updates_only_url(monkeypatch):
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
                }
            },
        )
    ]
    assert payload["summary"]["counts"] == {"updated": 1, "deleted": 1}


def test_run_tiktok_feishu_batch_sync_draft_requires_cleaned_rows_and_previews_existing_stage1_fields(
    monkeypatch,
    tmp_path,
):
    module = __import__(
        "automation_business_scaffold.flows.tiktok_feishu_sync_flow",
        fromlist=["run_tiktok_feishu_batch_sync"],
    )
    fetch_calls: list[str] = []

    class FakeClient:
        def __init__(self, access_token: str) -> None:
            self.access_token = access_token

        def list_all_records(self, *, app_token, table_id, page_size=100, filter_expr=None, view_id=None):
            return [
                {
                    "record_id": "rec-1",
                    "fields": {
                        "产品链接": {"link": "https://www.tiktok.com/shop/pdp/1111111111111111111"}
                    },
                },
                {
                    "record_id": "rec-2",
                    "fields": {
                        "产品链接": {"link": "https://www.tiktok.com/shop/pdp/2222222222222222222"}
                    },
                },
                {
                    "record_id": "rec-3",
                    "fields": {
                        "产品链接": {"link": "https://www.tiktok.com/shop/pdp/2222222222222222222"}
                    },
                },
                {
                    "record_id": "rec-4",
                    "fields": {
                        "产品链接": {
                            "link": "https://www.tiktok.com/shop/pdp/3333333333333333333?source=product_detail"
                        }
                    },
                },
                {
                    "record_id": "rec-5",
                    "fields": {
                        "产品链接": {"link": "https://www.tiktok.com/shop/pdp/not-a-product"}
                    },
                },
                {
                    "record_id": "rec-6",
                    "fields": {
                        "产品链接": {"link": "https://www.tiktok.com/shop/pdp/6666666666666666666"},
                        "SKU-ID": "6666666666666666666",
                        "图片": [{"file_token": "img"}],
                        "标题": "Done",
                        "节日": "其他",
                        "卖家": "Done Shop",
                        "前台截图": [{"file_token": "page"}],
                        "价格": "10.00",
                        "记录日期": "2026/04/01",
                    },
                },
            ]

    monkeypatch.setattr(module, "FeishuBitableClient", FakeClient)
    monkeypatch.setattr(module, "_current_record_date", lambda: "2026/04/01")

    def fake_fetch(source_url, profile_ref=None, capture_page_screenshot=True):
        fetch_calls.append(source_url)
        return _sample_browser_product(
            source_url=source_url,
            normalized_url="https://www.tiktok.com/shop/pdp/1111111111111111111",
            tmp_path=tmp_path,
        )

    monkeypatch.setattr(module, "fetch_tiktok_product_record_via_browser", fake_fetch)

    payload = run_tiktok_feishu_batch_sync(
        {
            "table_url": "https://my.feishu.cn/base/app999?table=tbl999",
            "access_token_env": "TOKEN_DIRECT_VALUE",
            "run_mode": "draft",
            "profile_ref": "local-chrome",
        }
    )

    assert fetch_calls == ["https://www.tiktok.com/shop/pdp/1111111111111111111"]
    assert payload["summary"]["counts"] == {
        "skipped_duplicate_needs_cleanup": 2,
        "skipped_not_cleaned": 1,
        "invalid_url": 1,
        "skipped_completed": 1,
        "preview": 1,
    }
    preview_item = next(item for item in payload["items"] if item["record_id"] == "rec-1")
    assert preview_item["status"] == "preview"
    assert set(preview_item["fields"]) == {
        "SKU-ID",
        "图片",
        "标题",
        "节日",
        "卖家",
        "前台截图",
        "价格",
        "记录日期",
    }
    assert preview_item["fields"]["卖家"] == "Sample Shop"
    assert preview_item["fields"]["图片"]["type"] == "local_file"
    assert preview_item["fields"]["前台截图"]["type"] == "local_file"
    assert preview_item["fields"]["记录日期"] == "2026/04/01"


def test_run_tiktok_feishu_batch_sync_canary_uploads_and_updates_only_existing_stage1_fields(
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
            self.uploads: list[str] = []
            self.updated: list[tuple[str, dict[str, object]]] = []

        def list_all_records(self, *, app_token, table_id, page_size=100, filter_expr=None, view_id=None):
            return [
                {
                    "record_id": "rec-1",
                    "fields": {
                        "产品链接": {"link": "https://www.tiktok.com/shop/pdp/1111111111111111111"}
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
    monkeypatch.setattr(module, "_current_record_date", lambda: "2026/04/01")
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
            "run_mode": "canary",
            "profile_ref": "local-chrome",
        }
    )

    assert fake_client.uploads == ["main-image.png", "page.png"]
    assert fake_client.updated == [
        (
            "rec-1",
            {
                "SKU-ID": "4444444444444444444",
                "图片": [{"file_token": "file-token-main-image.png"}],
                "标题": "Halloween Lights",
                "节日": "万圣节",
                "卖家": "Sample Shop",
                "前台截图": [{"file_token": "file-token-page.png"}],
                "价格": "45.67",
                "记录日期": "2026/04/01",
            },
        )
    ]
    assert payload["summary"]["counts"] == {"updated": 1}


def test_run_tiktok_feishu_batch_sync_canary_updates_only_missing_fields_without_unneeded_uploads(
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
            self.uploads: list[str] = []
            self.updated: list[tuple[str, dict[str, object]]] = []

        def list_all_records(self, *, app_token, table_id, page_size=100, filter_expr=None, view_id=None):
            return [
                {
                    "record_id": "rec-1",
                    "fields": {
                        "产品链接": {"link": "https://www.tiktok.com/shop/pdp/1111111111111111111"},
                        "SKU-ID": "4444444444444444444",
                        "图片": [{"file_token": "img"}],
                        "节日": "万圣节",
                        "卖家": "Sample Shop",
                        "前台截图": [{"file_token": "page"}],
                        "价格": "45.67",
                        "记录日期": "2026/04/01",
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
    monkeypatch.setattr(module, "_current_record_date", lambda: "2026/04/01")
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
            "run_mode": "canary",
            "profile_ref": "local-chrome",
        }
    )

    assert fake_client.uploads == []
    assert fake_client.updated == [
        (
            "rec-1",
            {
                "标题": "Halloween Lights",
                "记录日期": "2026/04/01",
            },
        )
    ]
    assert payload["summary"]["counts"] == {"updated": 1}


def test_run_tiktok_feishu_batch_sync_canary_retries_record_date_as_timestamp_on_datetime_field_error(
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
            self.uploads: list[str] = []
            self.updated: list[tuple[str, dict[str, object]]] = []

        def list_all_records(self, *, app_token, table_id, page_size=100, filter_expr=None, view_id=None):
            return [
                {
                    "record_id": "rec-1",
                    "fields": {
                        "产品链接": {"link": "https://www.tiktok.com/shop/pdp/1111111111111111111"}
                    },
                }
            ]

        def upload_media(self, *, file_name, file_data, parent_node, parent_type="bitable_file", extra=None):
            self.uploads.append(file_name)
            return f"file-token-{file_name}"

        def update_record(self, app_token: str, table_id: str, record_id: str, fields: dict[str, object]):
            self.updated.append((record_id, fields))
            if len(self.updated) == 1:
                raise RuntimeError("DatetimeFieldConvFail (code=1254064, status=200)")
            return {"code": 0}

    fake_client = FakeClient("ignored")
    monkeypatch.setattr(module, "FeishuBitableClient", lambda _access_token: fake_client)
    monkeypatch.setattr(module, "_current_record_date", lambda: "2026/04/01")
    monkeypatch.setattr(module, "_current_record_date_timestamp_ms", lambda: 1774972800000)
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
            "run_mode": "canary",
            "profile_ref": "local-chrome",
        }
    )

    assert fake_client.uploads == ["main-image.png", "page.png"]
    assert len(fake_client.updated) == 2
    assert fake_client.updated[0][1]["记录日期"] == "2026/04/01"
    assert fake_client.updated[1][1]["记录日期"] == 1774972800000
    assert payload["summary"]["counts"] == {"updated": 1}
    assert payload["failed_items"] == []


def test_run_tiktok_feishu_batch_sync_canary_does_not_write_status_fields_on_browser_error(monkeypatch):
    module = __import__(
        "automation_business_scaffold.flows.tiktok_feishu_sync_flow",
        fromlist=["run_tiktok_feishu_batch_sync"],
    )
    attempts = {"count": 0}

    class FakeClient:
        def __init__(self, access_token: str) -> None:
            self.access_token = access_token
            self.updated: list[tuple[str, dict[str, object]]] = []

        def list_all_records(self, *, app_token, table_id, page_size=100, filter_expr=None, view_id=None):
            return [
                {
                    "record_id": "rec-1",
                    "fields": {
                        "产品链接": {"link": "https://www.tiktok.com/shop/pdp/1111111111111111111"}
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
        lambda *args, **kwargs: (
            attempts.__setitem__("count", attempts["count"] + 1),
            (_ for _ in ()).throw(RuntimeError("browser failed")),
        )[1],
    )

    payload = run_tiktok_feishu_batch_sync(
        {
            "table_url": "https://my.feishu.cn/base/app999?table=tbl999",
            "access_token_env": "TOKEN_DIRECT_VALUE",
            "run_mode": "canary",
            "profile_ref": "local-chrome",
        }
    )

    assert attempts["count"] == 4
    assert fake_client.updated == []
    assert payload["summary"]["counts"] == {"failed": 1}
    assert payload["failed_items"] == [
        {
            "record_id": "rec-1",
            "source_url": "https://www.tiktok.com/shop/pdp/1111111111111111111",
            "normalized_url": "https://www.tiktok.com/shop/pdp/1111111111111111111",
            "error": "browser failed",
            "attempt_count": 4,
            "retry_errors": [
                {"attempt": 1, "error": "browser failed"},
                {"attempt": 2, "error": "browser failed"},
                {"attempt": 3, "error": "browser failed"},
                {"attempt": 4, "error": "browser failed"},
            ],
        }
    ]


def test_run_tiktok_feishu_batch_sync_retries_single_row_until_success(monkeypatch, tmp_path):
    module = __import__(
        "automation_business_scaffold.flows.tiktok_feishu_sync_flow",
        fromlist=["run_tiktok_feishu_batch_sync"],
    )
    attempts = {"count": 0}

    class FakeClient:
        def __init__(self, access_token: str) -> None:
            self.access_token = access_token

        def list_all_records(self, *, app_token, table_id, page_size=100, filter_expr=None, view_id=None):
            return [
                {
                    "record_id": "rec-1",
                    "fields": {
                        "产品链接": {"link": "https://www.tiktok.com/shop/pdp/1111111111111111111"}
                    },
                }
            ]

    def flaky_fetch(source_url, profile_ref=None, capture_page_screenshot=True):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise RuntimeError(f"browser failed #{attempts['count']}")
        return _sample_browser_product(
            source_url=source_url,
            normalized_url="https://www.tiktok.com/shop/pdp/1111111111111111111",
            tmp_path=tmp_path,
        )

    monkeypatch.setattr(module, "FeishuBitableClient", FakeClient)
    monkeypatch.setattr(module, "_current_record_date", lambda: "2026/04/01")
    monkeypatch.setattr(module, "fetch_tiktok_product_record_via_browser", flaky_fetch)

    payload = run_tiktok_feishu_batch_sync(
        {
            "table_url": "https://my.feishu.cn/base/app999?table=tbl999",
            "access_token_env": "TOKEN_DIRECT_VALUE",
            "run_mode": "draft",
            "profile_ref": "local-chrome",
        }
    )

    assert attempts["count"] == 3
    preview_item = next(item for item in payload["items"] if item["record_id"] == "rec-1")
    assert preview_item["status"] == "preview"
    assert preview_item["attempt_count"] == 3
    assert preview_item["retry_errors"] == [
        {"attempt": 1, "error": "browser failed #1"},
        {"attempt": 2, "error": "browser failed #2"},
    ]
    assert payload["failed_items"] == []
