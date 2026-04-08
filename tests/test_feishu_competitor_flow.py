from __future__ import annotations

from pathlib import Path

from automation_business_scaffold.flows.feishu_competitor_flow import (
    run_fastmoss_login_check,
    run_fastmoss_keyword_candidate_discovery,
    run_feishu_pending_rows_scan,
    run_feishu_seed_row_insert,
    run_feishu_single_row_update,
)
from automation_business_scaffold.models import FastMossProductSalesSnapshot, TikTokProductRecord


def _build_fake_target(fake_client):
    return type(
        "_Target",
        (),
        {
            "client": fake_client,
            "app_token": "app999",
            "table_id": "tbl999",
            "view_id": "vew999",
        },
    )()


def _sample_product(*, url: str, tmp_path: Path) -> TikTokProductRecord:
    main_image = tmp_path / "main-image.png"
    screenshot = tmp_path / "product-page.png"
    main_image.write_bytes(b"main-image")
    screenshot.write_bytes(b"page")
    return TikTokProductRecord(
        source_url=url,
        resolved_url=url,
        normalized_url=url,
        product_id="1732268173492064949",
        title="Easter Eggs",
        holiday="复活节",
        main_image_url="https://example.com/image.png",
        price_amount="12.34",
        price_currency="USD",
        price_text="$12.34",
        sales_count=0,
        shop_name="Sample Shop",
        shop_url="https://shop.example.com",
        main_image_local_path=str(main_image),
        main_image_file_name=main_image.name,
        main_image_mime_type="image/png",
        product_page_screenshot_local_path=str(screenshot),
        product_page_screenshot_file_name=screenshot.name,
        product_page_screenshot_mime_type="image/png",
    )


def _sample_snapshot(*, product_id: str, tmp_path: Path) -> FastMossProductSalesSnapshot:
    screenshot = tmp_path / "fastmoss-detail.png"
    screenshot.write_bytes(b"fastmoss")
    return FastMossProductSalesSnapshot(
        product_id=product_id,
        search_url="https://www.fastmoss.com/zh/e-commerce/search?page=1&words=1732268173492064949",
        detail_url=f"https://www.fastmoss.com/zh/e-commerce/detail/{product_id}",
        product_title="Easter Eggs",
        login_state="already_logged_in",
        yesterday_sales="11",
        sales_7d="222",
        sales_28d="999",
        sales_90d="1888",
        detail_page_screenshot_local_path=str(screenshot),
        detail_page_screenshot_file_name=screenshot.name,
        detail_page_screenshot_mime_type="image/png",
    )


def test_run_feishu_pending_rows_scan_only_checks_auto_update_fields(monkeypatch):
    module = __import__(
        "automation_business_scaffold.flows.feishu_competitor_flow",
        fromlist=["run_feishu_pending_rows_scan"],
    )

    class FakeClient:
        def list_all_records(self, *, app_token, table_id, page_size=100, view_id=None):
            return [
                {
                    "record_id": "rec-1",
                    "fields": {
                        "产品链接": {"link": "https://www.tiktok.com/shop/pdp/1111111111111111111"},
                        "SKU-ID": "1111111111111111111",
                        "图片": [{"file_token": "img"}],
                        "节日": "复活节",
                        "卖家": "Shop",
                        "前台截图": [{"file_token": "page"}],
                        "价格": "10.00",
                        "Fastmoss截图": [{"file_token": "fastmoss"}],
                        "昨日销量": "1",
                        "近7天销量": "2",
                        "近90天销量": "3",
                        "记录日期": "2026/04/07",
                        "备注": "",
                    },
                },
                {
                    "record_id": "rec-2",
                    "fields": {
                        "产品链接": {"link": "https://www.tiktok.com/shop/pdp/2222222222222222222"},
                        "SKU-ID": "2222222222222222222",
                        "图片": [{"file_token": "img"}],
                        "标题": "Done",
                        "节日": "复活节",
                        "卖家": "Shop",
                        "前台截图": [{"file_token": "page"}],
                        "价格": "10.00",
                        "Fastmoss截图": [{"file_token": "fastmoss"}],
                        "昨日销量": "1",
                        "近7天销量": "2",
                        "近90天销量": "3",
                        "记录日期": "2026/04/07",
                        "备注": "",
                    },
                },
                {
                    "record_id": "rec-3",
                    "fields": {
                        "备注": "manual",
                    },
                },
            ]

    monkeypatch.setattr(module, "_build_table_target", lambda table_url, access_token: _build_fake_target(FakeClient()))

    payload = run_feishu_pending_rows_scan(
        {
            "table_url": "https://my.feishu.cn/base/app999?table=tbl999&view=vew999",
            "access_token_env": "TOKEN_DIRECT_VALUE",
            "run_mode": "draft",
        }
    )

    assert payload["summary"]["counts"] == {
        "pending": 1,
        "skipped_completed": 1,
        "blocked_missing_locator": 1,
    }
    assert payload["target_rows"] == [
        {
            "record_id": "rec-1",
            "source_url": "https://www.tiktok.com/shop/pdp/1111111111111111111",
            "normalized_url": "https://www.tiktok.com/shop/pdp/1111111111111111111",
            "sku_id": "1111111111111111111",
            "missing_fields": ["标题"],
        }
    ]


def test_run_feishu_single_row_update_canary_writes_tiktok_and_fastmoss_fields(monkeypatch, tmp_path):
    module = __import__(
        "automation_business_scaffold.flows.feishu_competitor_flow",
        fromlist=["run_feishu_single_row_update"],
    )

    class FakeClient:
        def __init__(self) -> None:
            self.uploads: list[str] = []
            self.updated: list[tuple[str, dict[str, object]]] = []

        def get_record(self, app_token: str, table_id: str, record_id: str):
            return {
                "data": {
                    "record": {
                        "record_id": record_id,
                        "fields": {
                            "产品链接": {"link": "https://www.tiktok.com/shop/pdp/1732268173492064949"},
                        },
                    }
                }
            }

        def upload_media(self, *, file_name, file_data, parent_node, parent_type="bitable_file", extra=None):
            self.uploads.append(file_name)
            return f"file-token-{file_name}"

        def update_record(self, app_token: str, table_id: str, record_id: str, fields: dict[str, object]):
            self.updated.append((record_id, fields))
            return {"code": 0}

    fake_client = FakeClient()
    monkeypatch.setattr(module, "_build_table_target", lambda table_url, access_token: _build_fake_target(fake_client))
    monkeypatch.setattr(module, "_current_record_date", lambda: "2026/04/07")
    monkeypatch.setattr(
        module,
        "fetch_tiktok_product_record_via_browser",
        lambda source_url, profile_ref=None, capture_page_screenshot=True: _sample_product(
            url="https://www.tiktok.com/shop/pdp/1732268173492064949",
            tmp_path=tmp_path,
        ),
    )
    monkeypatch.setattr(
        module,
        "fetch_fastmoss_product_sales_via_browser",
        lambda product_id, **kwargs: _sample_snapshot(product_id=product_id, tmp_path=tmp_path),
    )

    payload = run_feishu_single_row_update(
        {
            "table_url": "https://my.feishu.cn/base/app999?table=tbl999",
            "access_token_env": "TOKEN_DIRECT_VALUE",
            "run_mode": "canary",
            "record_id": "rec-1",
            "profile_ref": "roxy-united-states",
            "fastmoss_phone_env": "FASTMOSS_PHONE",
            "fastmoss_password_env": "FASTMOSS_PASSWORD",
        }
    )

    assert fake_client.uploads == ["main-image.png", "product-page.png", "fastmoss-detail.png"]
    assert payload["summary"]["counts"] == {"updated": 1}
    updated_fields = fake_client.updated[0][1]
    assert updated_fields["SKU-ID"] == "1732268173492064949"
    assert updated_fields["昨日销量"] == "11"
    assert updated_fields["近7天销量"] == "222"
    assert updated_fields["近90天销量"] == "1888"
    assert updated_fields["记录日期"] == "2026/04/07"
    assert "sales_28d" not in updated_fields
    assert updated_fields["Fastmoss截图"] == [{"file_token": "file-token-fastmoss-detail.png"}]


def test_run_feishu_single_row_update_can_skip_fastmoss_login_validation(monkeypatch, tmp_path):
    module = __import__(
        "automation_business_scaffold.flows.feishu_competitor_flow",
        fromlist=["run_feishu_single_row_update"],
    )

    class FakeClient:
        def get_record(self, app_token: str, table_id: str, record_id: str):
            return {
                "data": {
                    "record": {
                        "record_id": record_id,
                        "fields": {"产品链接": {"link": "https://www.tiktok.com/shop/pdp/1732268173492064949"}},
                    }
                }
            }

        def upload_media(self, *, file_name, file_data, parent_node, parent_type="bitable_file", extra=None):
            return f"file-token-{file_name}"

        def update_record(self, app_token: str, table_id: str, record_id: str, fields: dict[str, object]):
            return {"code": 0}

    captured: dict[str, object] = {}
    monkeypatch.setattr(module, "_build_table_target", lambda table_url, access_token: _build_fake_target(FakeClient()))
    monkeypatch.setattr(module, "_current_record_date", lambda: "2026/04/07")
    monkeypatch.setattr(
        module,
        "fetch_tiktok_product_record_via_browser",
        lambda source_url, profile_ref=None, capture_page_screenshot=True: _sample_product(
            url="https://www.tiktok.com/shop/pdp/1732268173492064949",
            tmp_path=tmp_path,
        ),
    )

    def fake_fetch_snapshot(product_id, **kwargs):
        captured.update(kwargs)
        return _sample_snapshot(product_id=product_id, tmp_path=tmp_path)

    monkeypatch.setattr(module, "fetch_fastmoss_product_sales_via_browser", fake_fetch_snapshot)

    run_feishu_single_row_update(
        {
            "table_url": "https://my.feishu.cn/base/app999?table=tbl999",
            "access_token_env": "TOKEN_DIRECT_VALUE",
            "run_mode": "canary",
            "record_id": "rec-1",
            "verify_fastmoss_login": False,
        }
    )

    assert captured["verify_login"] is False


def test_run_feishu_single_row_update_skips_follow_up_when_tiktok_security_check_detected(monkeypatch):
    module = __import__(
        "automation_business_scaffold.flows.feishu_competitor_flow",
        fromlist=["run_feishu_single_row_update"],
    )

    class FakeClient:
        def get_record(self, app_token: str, table_id: str, record_id: str):
            return {
                "data": {
                    "record": {
                        "record_id": record_id,
                        "fields": {
                            "产品链接": {"link": "https://www.tiktok.com/shop/pdp/1732268173492064949"},
                        },
                    }
                }
            }

    fastmoss_called = {"value": False}
    monkeypatch.setattr(module, "_build_table_target", lambda table_url, access_token: _build_fake_target(FakeClient()))
    monkeypatch.setattr(
        module,
        "fetch_tiktok_product_record_via_browser",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            module.TikTokSecurityCheckError("TikTok security check detected: captcha")
        ),
    )
    monkeypatch.setattr(
        module,
        "fetch_fastmoss_product_sales_via_browser",
        lambda *args, **kwargs: fastmoss_called.__setitem__("value", True),
    )

    payload = run_feishu_single_row_update(
        {
            "table_url": "https://my.feishu.cn/base/app999?table=tbl999",
            "access_token_env": "TOKEN_DIRECT_VALUE",
            "run_mode": "canary",
            "record_id": "rec-1",
            "profile_ref": "roxy-united-states",
            "fastmoss_phone_env": "FASTMOSS_PHONE",
            "fastmoss_password_env": "FASTMOSS_PASSWORD",
        }
    )

    assert payload["summary"]["counts"] == {"skipped_security_check": 1}
    assert payload["item"]["status"] == "skipped_security_check"
    assert "security check" in payload["item"]["error"].lower()
    assert fastmoss_called["value"] is False


def test_run_feishu_single_row_update_preserves_existing_non_exempt_fields(monkeypatch, tmp_path):
    module = __import__(
        "automation_business_scaffold.flows.feishu_competitor_flow",
        fromlist=["run_feishu_single_row_update"],
    )

    class FakeClient:
        def __init__(self) -> None:
            self.uploads: list[str] = []
            self.updated: list[tuple[str, dict[str, object]]] = []

        def get_record(self, app_token: str, table_id: str, record_id: str):
            return {
                "data": {
                    "record": {
                        "record_id": record_id,
                        "fields": {
                            "产品链接": {"link": "https://www.tiktok.com/shop/pdp/1732268173492064949?foo=bar"},
                            "SKU-ID": "1732268173492064949",
                            "图片": [{"file_token": "existing-image"}],
                            "标题": "Manual Title",
                            "节日": "手工节日",
                            "卖家": "Manual Seller",
                            "前台截图": [{"file_token": "existing-page"}],
                            "价格": "99",
                            "Fastmoss截图": [{"file_token": "existing-fastmoss"}],
                            "昨日销量": "3",
                            "近7天销量": "",
                            "近90天销量": "88",
                            "记录日期": "2026/04/01",
                            "备注": "manual note",
                        },
                    }
                }
            }

        def upload_media(self, *, file_name, file_data, parent_node, parent_type="bitable_file", extra=None):
            self.uploads.append(file_name)
            return f"file-token-{file_name}"

        def update_record(self, app_token: str, table_id: str, record_id: str, fields: dict[str, object]):
            self.updated.append((record_id, fields))
            return {"code": 0}

    fake_client = FakeClient()
    monkeypatch.setattr(module, "_build_table_target", lambda table_url, access_token: _build_fake_target(fake_client))
    monkeypatch.setattr(module, "_current_record_date", lambda: "2026/04/07")
    monkeypatch.setattr(
        module,
        "fetch_tiktok_product_record_via_browser",
        lambda source_url, profile_ref=None, capture_page_screenshot=True: _sample_product(
            url="https://www.tiktok.com/shop/pdp/1732268173492064949",
            tmp_path=tmp_path,
        ),
    )
    monkeypatch.setattr(
        module,
        "fetch_fastmoss_product_sales_via_browser",
        lambda product_id, **kwargs: _sample_snapshot(product_id=product_id, tmp_path=tmp_path),
    )

    payload = run_feishu_single_row_update(
        {
            "table_url": "https://my.feishu.cn/base/app999?table=tbl999",
            "access_token_env": "TOKEN_DIRECT_VALUE",
            "run_mode": "canary",
            "record_id": "rec-keep-existing",
        }
    )

    assert payload["summary"]["counts"] == {"updated": 1}
    assert fake_client.uploads == []
    assert fake_client.updated == [
        (
            "rec-keep-existing",
            {
                "近7天销量": "222",
                "记录日期": "2026/04/07",
            },
        )
    ]


def test_run_feishu_single_row_update_does_not_write_when_no_fields_are_missing(monkeypatch, tmp_path):
    module = __import__(
        "automation_business_scaffold.flows.feishu_competitor_flow",
        fromlist=["run_feishu_single_row_update"],
    )

    class FakeClient:
        def __init__(self) -> None:
            self.uploads: list[str] = []
            self.updated: list[tuple[str, dict[str, object]]] = []

        def get_record(self, app_token: str, table_id: str, record_id: str):
            return {
                "data": {
                    "record": {
                        "record_id": record_id,
                        "fields": {
                            "产品链接": {"link": "https://www.tiktok.com/shop/pdp/1732268173492064949"},
                            "SKU-ID": "1732268173492064949",
                            "图片": [{"file_token": "existing-image"}],
                            "标题": "Manual Title",
                            "节日": "复活节",
                            "卖家": "Manual Seller",
                            "前台截图": [{"file_token": "existing-page"}],
                            "价格": "99",
                            "Fastmoss截图": [{"file_token": "existing-fastmoss"}],
                            "昨日销量": "3",
                            "近7天销量": "22",
                            "近90天销量": "88",
                            "记录日期": "2026/04/01",
                        },
                    }
                }
            }

        def upload_media(self, *, file_name, file_data, parent_node, parent_type="bitable_file", extra=None):
            self.uploads.append(file_name)
            return f"file-token-{file_name}"

        def update_record(self, app_token: str, table_id: str, record_id: str, fields: dict[str, object]):
            self.updated.append((record_id, fields))
            return {"code": 0}

    fake_client = FakeClient()
    monkeypatch.setattr(module, "_build_table_target", lambda table_url, access_token: _build_fake_target(fake_client))
    monkeypatch.setattr(module, "_current_record_date", lambda: "2026/04/07")
    monkeypatch.setattr(
        module,
        "fetch_tiktok_product_record_via_browser",
        lambda source_url, profile_ref=None, capture_page_screenshot=True: _sample_product(
            url="https://www.tiktok.com/shop/pdp/1732268173492064949",
            tmp_path=tmp_path,
        ),
    )
    monkeypatch.setattr(
        module,
        "fetch_fastmoss_product_sales_via_browser",
        lambda product_id, **kwargs: _sample_snapshot(product_id=product_id, tmp_path=tmp_path),
    )

    payload = run_feishu_single_row_update(
        {
            "table_url": "https://my.feishu.cn/base/app999?table=tbl999",
            "access_token_env": "TOKEN_DIRECT_VALUE",
            "run_mode": "canary",
            "record_id": "rec-complete",
        }
    )

    assert payload["summary"]["counts"] == {"skipped_completed": 1}
    assert payload["item"]["fields"] == {}
    assert fake_client.uploads == []
    assert fake_client.updated == []


def test_run_feishu_single_row_update_skips_completed_row_before_fetch(monkeypatch):
    module = __import__(
        "automation_business_scaffold.flows.feishu_competitor_flow",
        fromlist=["run_feishu_single_row_update"],
    )

    class FakeClient:
        def get_record(self, app_token: str, table_id: str, record_id: str):
            return {
                "data": {
                    "record": {
                        "record_id": record_id,
                        "fields": {
                            "产品链接": {"link": "https://www.tiktok.com/shop/pdp/1732268173492064949"},
                            "SKU-ID": "1732268173492064949",
                            "图片": [{"file_token": "existing-image"}],
                            "标题": "Done",
                            "节日": "复活节",
                            "卖家": "Manual Seller",
                            "前台截图": [{"file_token": "existing-page"}],
                            "价格": "99",
                            "Fastmoss截图": [{"file_token": "existing-fastmoss"}],
                            "昨日销量": "3",
                            "近7天销量": "22",
                            "近90天销量": "88",
                            "记录日期": "2026/04/01",
                        },
                    }
                }
            }

    monkeypatch.setattr(module, "_build_table_target", lambda table_url, access_token: _build_fake_target(FakeClient()))

    def fail_fetch(*args, **kwargs):
        raise AssertionError("completed rows should not trigger browser fetching")

    monkeypatch.setattr(module, "fetch_tiktok_product_record_via_browser", fail_fetch)
    monkeypatch.setattr(module, "fetch_fastmoss_product_sales_via_browser", fail_fetch)

    payload = run_feishu_single_row_update(
        {
            "table_url": "https://my.feishu.cn/base/app999?table=tbl999",
            "access_token_env": "TOKEN_DIRECT_VALUE",
            "run_mode": "canary",
            "record_id": "rec-complete",
        }
    )

    assert payload["summary"]["counts"] == {"skipped_completed": 1}
    assert payload["item"]["fields"] == {}
    assert payload["item"]["logical_fields"] == {}
    assert payload["item"]["fastmoss_snapshot"] == {}


def test_run_fastmoss_keyword_candidate_discovery_applies_two_level_dedup(monkeypatch):
    module = __import__(
        "automation_business_scaffold.flows.feishu_competitor_flow",
        fromlist=["run_fastmoss_keyword_candidate_discovery"],
    )

    class FakeClient:
        def list_all_records(self, *, app_token, table_id, page_size=100, view_id=None):
            return [
                {
                    "record_id": "rec-by-url",
                    "fields": {
                        "产品链接": {
                            "link": "https://www.tiktok.com/shop/pdp/1111111111111111111?foo=bar"
                        }
                    },
                },
                {
                    "record_id": "rec-by-sku",
                    "fields": {
                        "SKU-ID": "2222222222222222222",
                    },
                },
            ]

    monkeypatch.setattr(module, "_build_table_target", lambda table_url, access_token: _build_fake_target(FakeClient()))
    monkeypatch.setattr(
        module,
        "discover_fastmoss_keyword_candidates_via_browser",
        lambda *args, **kwargs: {
            "search_url": "https://www.fastmoss.com/zh/e-commerce/search?words=east%20egg",
            "pages_scanned": 2,
            "rows_scanned": 3,
            "items": [
                {
                    "product_id": "1111111111111111111",
                    "normalized_product_url": "https://www.tiktok.com/shop/pdp/1111111111111111111",
                    "detail_url": "https://www.fastmoss.com/zh/e-commerce/detail/1111111111111111111",
                    "sales_7d": "300",
                    "sales_7d_value": 300,
                },
                {
                    "product_id": "2222222222222222222",
                    "normalized_product_url": "https://www.tiktok.com/shop/pdp/2222222222222222222",
                    "detail_url": "https://www.fastmoss.com/zh/e-commerce/detail/2222222222222222222",
                    "sales_7d": "301",
                    "sales_7d_value": 301,
                },
                {
                    "product_id": "3333333333333333333",
                    "normalized_product_url": "https://www.tiktok.com/shop/pdp/3333333333333333333",
                    "detail_url": "https://www.fastmoss.com/zh/e-commerce/detail/3333333333333333333",
                    "sales_7d": "500",
                    "sales_7d_value": 500,
                },
            ],
        },
    )

    payload = run_fastmoss_keyword_candidate_discovery(
        {
            "table_url": "https://my.feishu.cn/base/app999?table=tbl999",
            "access_token_env": "TOKEN_DIRECT_VALUE",
            "search_keyword": "east egg",
            "sales_7d_threshold": 200,
            "profile_ref": "roxy-united-states",
        }
    )

    assert payload["summary"]["counts"] == {"skipped_existing": 2, "candidate_new": 1}
    assert [item["product_id"] for item in payload["target_items"]] == ["3333333333333333333"]


def test_run_fastmoss_keyword_candidate_discovery_can_skip_fastmoss_login_validation(monkeypatch):
    module = __import__(
        "automation_business_scaffold.flows.feishu_competitor_flow",
        fromlist=["run_fastmoss_keyword_candidate_discovery"],
    )

    class FakeClient:
        def list_all_records(self, *, app_token, table_id, page_size=100, view_id=None):
            return []

    captured: dict[str, object] = {}
    monkeypatch.setattr(module, "_build_table_target", lambda table_url, access_token: _build_fake_target(FakeClient()))

    def fake_discovery(*args, **kwargs):
        captured.update(kwargs)
        return {
            "search_url": "https://www.fastmoss.com/zh/e-commerce/search?words=test",
            "pages_scanned": 1,
            "rows_scanned": 0,
            "items": [],
        }

    monkeypatch.setattr(module, "discover_fastmoss_keyword_candidates_via_browser", fake_discovery)

    run_fastmoss_keyword_candidate_discovery(
        {
            "table_url": "https://my.feishu.cn/base/app999?table=tbl999",
            "access_token_env": "TOKEN_DIRECT_VALUE",
            "search_keyword": "test",
            "sales_7d_threshold": 200,
            "verify_fastmoss_login": False,
        }
    )

    assert captured["verify_login"] is False


def test_run_fastmoss_login_check_returns_validated_payload(monkeypatch):
    module = __import__(
        "automation_business_scaffold.flows.feishu_competitor_flow",
        fromlist=["run_fastmoss_login_check"],
    )

    monkeypatch.setattr(
        module,
        "validate_fastmoss_login_via_browser",
        lambda **kwargs: {
            "login_state": "already_logged_in",
            "profile_ref": "roxy-united-states",
            "provider_name": "roxy",
            "target_key": "roxy:84278:sample",
        },
    )

    payload = run_fastmoss_login_check(
        {
            "profile_ref": "roxy-united-states",
            "fastmoss_phone_env": "FASTMOSS_PHONE",
            "fastmoss_password_env": "FASTMOSS_PASSWORD",
        }
    )

    assert payload["summary"]["counts"] == {"validated": 1}
    assert payload["item"]["login_state"] == "already_logged_in"


def test_run_feishu_seed_row_insert_creates_seed_fields(monkeypatch):
    module = __import__(
        "automation_business_scaffold.flows.feishu_competitor_flow",
        fromlist=["run_feishu_seed_row_insert"],
    )

    class FakeClient:
        def __init__(self) -> None:
            self.created: list[dict[str, object]] = []

        def list_all_records(self, *, app_token, table_id, page_size=100, view_id=None):
            return []

        def create_record(self, app_token: str, table_id: str, fields: dict[str, object]):
            self.created.append(fields)
            return {"data": {"record": {"record_id": "rec-new"}}}

    fake_client = FakeClient()
    monkeypatch.setattr(module, "_build_table_target", lambda table_url, access_token: _build_fake_target(fake_client))

    payload = run_feishu_seed_row_insert(
        {
            "table_url": "https://my.feishu.cn/base/app999?table=tbl999",
            "access_token_env": "TOKEN_DIRECT_VALUE",
            "run_mode": "canary",
            "sku_id": "3333333333333333333",
            "search_keyword": "east egg",
        }
    )

    assert payload["summary"]["counts"] == {"inserted": 1}
    assert fake_client.created == [
        {
            "SKU-ID": "3333333333333333333",
            "产品链接": {
                "text": "https://www.tiktok.com/shop/pdp/3333333333333333333",
                "link": "https://www.tiktok.com/shop/pdp/3333333333333333333",
            },
            "备注": "通过搜索关键字：east egg",
        }
    ]
