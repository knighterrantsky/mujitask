from __future__ import annotations

from automation_business_scaffold.flows.tiktok_feishu_sync_flow import run_tiktok_product_link_cleanup


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
                {
                    "record_id": "rec-5",
                    "fields": {},
                },
                {
                    "record_id": "rec-6",
                    "fields": {
                        "产品链接": {
                            "link": "",
                            "text": "",
                        },
                        "备注": "需要保留",
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
        "delete_preview": 2,
        "invalid_url": 1,
        "skipped_empty": 1,
    }
    keeper = next(item for item in payload["items"] if item["record_id"] == "rec-1")
    assert keeper["status"] == "preview"
    assert keeper["deleted_record_ids"] == ["rec-2"]
    assert keeper["normalized_url"] == "https://www.tiktok.com/shop/pdp/1111111111111111111"
    assert next(item for item in payload["items"] if item["record_id"] == "rec-2")["status"] == "delete_preview"
    assert next(item for item in payload["items"] if item["record_id"] == "rec-4")["status"] == "invalid_url"
    assert next(item for item in payload["items"] if item["record_id"] == "rec-5")["status"] == "delete_preview"
    assert next(item for item in payload["items"] if item["record_id"] == "rec-6")["status"] == "skipped_empty"


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
                {
                    "record_id": "rec-3",
                    "fields": {},
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

    assert fake_client.deleted == ["rec-2", "rec-3"]
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
    assert payload["summary"]["counts"] == {"updated": 1, "deleted": 2}
