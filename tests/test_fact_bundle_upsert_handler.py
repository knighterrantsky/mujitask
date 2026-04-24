from __future__ import annotations

from automation_business_scaffold.capabilities.persistence.database.fact_bundle_upsert_handler import (
    HANDLER_CODE,
    fact_bundle_upsert_handler,
)
from automation_business_scaffold.contracts.handler.contract import HandlerContext


def _context(payload: dict) -> HandlerContext:
    return HandlerContext(
        request_id="req-facts",
        job_id="job-facts",
        handler_code=HANDLER_CODE,
        worker_type="api_worker",
        runtime_table="api_worker_job",
        payload=payload,
    )


def test_fact_bundle_upsert_accepts_top_level_fact_bundle_only() -> None:
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
