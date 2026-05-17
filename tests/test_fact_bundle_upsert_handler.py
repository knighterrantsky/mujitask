from __future__ import annotations

import importlib

from automation_business_scaffold.capabilities.persistence.database.fact_bundle_upsert_handler import (
    HANDLER_CODE,
    fact_bundle_upsert_handler,
)
from automation_business_scaffold.contracts.handler.contract import HandlerContext

fact_bundle_module = importlib.import_module(
    "automation_business_scaffold.capabilities.persistence.database.fact_bundle_upsert_handler"
)

_FACT_DB_ENV_KEYS = (
    "TK_FACT_DB_URL",
    "BUSINESS_EXECUTION_CONTROL_FACT_DB_URL",
    "EXECUTION_CONTROL_FACT_DB_URL",
    "FACT_DB_URL",
)


def _context(payload: dict) -> HandlerContext:
    return HandlerContext(
        request_id="req-facts",
        job_id="job-facts",
        handler_code=HANDLER_CODE,
        worker_type="api_worker",
        runtime_table="api_worker_job",
        payload=payload,
    )


def _clear_fact_db_env(monkeypatch) -> None:
    for key in _FACT_DB_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_fact_bundle_upsert_accepts_top_level_fact_bundle_only(monkeypatch) -> None:
    _clear_fact_db_env(monkeypatch)

    result = fact_bundle_upsert_handler(
        _context(
            {
                "fact_bundle": {
                    "products": [
                        {
                            "product_id": "1730964478199763166",
                            "product_url": "https://www.tiktok.com/shop/pdp/1730964478199763166",
                            "title": "Sample product",
                        }
                    ],
                    "product_metric_snapshots": [
                        {
                            "product_id": "1730964478199763166",
                            "source_platform": "fastmoss",
                            "source_endpoint": "fastmoss.product.overview",
                            "window_days": 28,
                            "window_start": "2026-02-10",
                            "window_end": "2026-03-09",
                            "payload": {"sold_count": 90},
                        }
                    ],
                }
            }
        )
    )

    assert result.status == "success"
    assert result.result["persisted_counts"]["products"] == 1
    assert result.result["persisted_counts"]["observations"] == 1
    assert result.result["fact_bundle"]["product_metric_snapshots"][0]["window_days"] == 28


def test_fact_bundle_upsert_ignores_legacy_nested_fact_bundle_inputs() -> None:
    result = fact_bundle_upsert_handler(
        _context(
            {
                "product_fact_bundle": {
                    "products": [
                        {
                            "product_id": "1730964478199763166",
                            "title": "Should not be accepted",
                        }
                    ]
                },
                "media_fact_bundle": {
                    "media_assets": [
                        {
                            "source_url": "https://cdn.example.com/main.webp",
                        }
                    ]
                },
                "normalized_product_result": {
                    "fact_bundle": {
                        "products": [
                            {
                                "product_id": "1730964478199763166",
                            }
                        ]
                    }
                },
            }
        )
    )

    assert result.status == "skipped"
    assert result.summary["entity_count"] == 0
    assert result.result["upserted_entities"] == []


def test_fact_bundle_upsert_uses_project_fact_db_url(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_persist_fact_bundle(fact_bundle: dict, *, fact_db_url: str) -> dict:
        captured["fact_bundle"] = fact_bundle
        captured["fact_db_url"] = fact_db_url
        return {
            "upserted_entities": ["product:123456789"],
            "upserted_relations": [],
            "observation_refs": [],
            "persisted_counts": {"products": 1},
        }

    monkeypatch.setattr(fact_bundle_module, "_persist_fact_bundle", fake_persist_fact_bundle)
    monkeypatch.setenv("TK_FACT_DB_URL", "postgresql+psycopg://facts")

    result = fact_bundle_upsert_handler(
        _context(
            {
                "request_payload": {},
                "fact_bundle": {
                    "products": [{"product_id": "123456789"}],
                },
            }
        )
    )

    assert result.status == "success"
    assert result.result["persistence_mode"] == "database"
    assert captured["fact_db_url"] == "postgresql+psycopg://facts"


def test_fact_bundle_upsert_fails_when_database_persistence_is_required_without_url(monkeypatch) -> None:
    _clear_fact_db_env(monkeypatch)

    result = fact_bundle_upsert_handler(
        _context(
            {
                "require_database_persistence": True,
                "fact_bundle": {
                    "products": [{"product_id": "123456789"}],
                },
            }
        )
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "fact_database_persistence_required"
    assert result.summary["persistence_mode"] == "missing_database"


def test_fact_bundle_upsert_persists_unavailable_product_status(monkeypatch) -> None:
    captured: dict[str, str] = {}

    class FakeFactStore:
        def __init__(self, *, db_url: str):
            assert db_url == "postgresql+psycopg://facts"

        def upsert_product(self, **kwargs):
            captured.update(kwargs)
            return {"product_id": kwargs["product_id"], "status": kwargs["status"]}

        def upsert_product_sku(self, **kwargs):
            return {}

        def upsert_shop(self, **kwargs):
            return {}

        def upsert_creator(self, **kwargs):
            return {}

        def upsert_video(self, **kwargs):
            return {}

        def upsert_media_asset(self, **kwargs):
            return {}

        def link_media_asset(self, **kwargs):
            return {}

        def upsert_product_shop_relation(self, **kwargs):
            return {}

        def upsert_creator_product_relation(self, **kwargs):
            return {}

        def upsert_creator_video_relation(self, **kwargs):
            return {}

        def upsert_video_product_relation(self, **kwargs):
            return {}

        def upsert_shop_creator_relation(self, **kwargs):
            return {}

        def record_raw_api_response(self, **kwargs):
            return {}

        def upsert_product_window_latest(self, **kwargs):
            return {}

        def record_product_window_observation(self, **kwargs):
            return {}

        def upsert_product_daily_metric(self, **kwargs):
            return {}

        def upsert_product_distribution_window_latest(self, **kwargs):
            return {}

        def record_product_distribution_window_observation(self, **kwargs):
            return {}

        def upsert_product_sku_window_latest(self, **kwargs):
            return {}

        def record_product_sku_window_observation(self, **kwargs):
            return {}

    monkeypatch.setattr(fact_bundle_module, "TKFactStore", FakeFactStore)

    result = fact_bundle_upsert_handler(
        _context(
            {
                "fact_db_url": "postgresql+psycopg://facts",
                "fact_bundle": {
                    "products": [
                        {
                            "product_id": "1732308866040173150",
                            "facts": {"availability_status": "unavailable"},
                        }
                    ]
                },
            }
        )
    )

    assert result.status == "success"
    assert captured["status"] == "off_shelf_or_region_unavailable"


def test_fact_bundle_upsert_reports_created_creator_product_relations(monkeypatch) -> None:
    class FakeFactStore:
        def __init__(self, *, db_url: str):
            assert db_url == "postgresql+psycopg://facts"

        def upsert_product(self, **kwargs):
            return {}

        def upsert_product_sku(self, **kwargs):
            return {}

        def upsert_shop(self, **kwargs):
            return {}

        def upsert_creator(self, **kwargs):
            return {"creator_key": "creator_id:creator-1"}

        def upsert_video(self, **kwargs):
            return {}

        def upsert_media_asset(self, **kwargs):
            return {}

        def link_media_asset(self, **kwargs):
            return {}

        def upsert_product_shop_relation(self, **kwargs):
            return {}

        def upsert_creator_product_relation(self, **kwargs):
            assert kwargs["include_mutation_status"] is True
            return {
                "relation_key": "creator_id:creator-1:product-1",
                "creator_key": "creator_id:creator-1",
                "creator_id": "creator-1",
                "product_id": "product-1",
                "sold_count": 63,
                "_mutation_status": "created",
            }

        def upsert_creator_video_relation(self, **kwargs):
            return {}

        def upsert_video_product_relation(self, **kwargs):
            return {}

        def upsert_shop_creator_relation(self, **kwargs):
            return {}

        def record_raw_api_response(self, **kwargs):
            return {}

        def upsert_product_window_latest(self, **kwargs):
            return {}

        def record_product_window_observation(self, **kwargs):
            return {}

        def upsert_product_daily_metric(self, **kwargs):
            return {}

        def upsert_product_distribution_window_latest(self, **kwargs):
            return {}

        def record_product_distribution_window_observation(self, **kwargs):
            return {}

        def upsert_product_sku_window_latest(self, **kwargs):
            return {}

        def record_product_sku_window_observation(self, **kwargs):
            return {}

    monkeypatch.setattr(fact_bundle_module, "TKFactStore", FakeFactStore)

    result = fact_bundle_upsert_handler(
        _context(
            {
                "fact_db_url": "postgresql+psycopg://facts",
                "fact_bundle": {
                    "creators": [{"creator_id": "creator-1"}],
                    "relations": {
                        "creator_products": [
                            {
                                "creator_id": "creator-1",
                                "product_id": "product-1",
                                "sold_count": 63,
                            }
                        ]
                    },
                },
            }
        )
    )

    assert result.status == "success"
    assert result.result["created_relations"] == ["creator_product:creator_id:creator-1:product-1"]
    assert result.result["created_creator_product_relations"] == [
        {
            "relation_key": "creator_id:creator-1:product-1",
            "creator_key": "creator_id:creator-1",
            "creator_id": "creator-1",
            "product_id": "product-1",
            "sold_count": 63,
        }
    ]
