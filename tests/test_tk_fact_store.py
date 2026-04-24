from __future__ import annotations

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore
from automation_business_scaffold.infrastructure.facts.tk_fact_ingestion_service import TKFactIngestionService
from automation_business_scaffold.infrastructure.facts.tk_fact_store import TKFactStore, extract_fact_payloads


def test_tk_fact_schema_replaces_legacy_entity_tables(runtime_db_url):
    store = RuntimeStore(db_url=runtime_db_url)
    fact_store = TKFactStore(runtime_store=store)

    table_names = fact_store.table_names()

    assert "tk_products" in table_names
    assert "tk_creators" in table_names
    assert "tk_media_assets" in table_names
    assert "tk_creator_product_relations" in table_names
    assert "tk_raw_api_responses" in table_names
    assert "tk_product_daily_metrics" in table_names
    assert "entity_registry" not in table_names
    assert "external_binding" not in table_names
    assert "entity_snapshot" not in table_names


def test_alembic_upgrade_creates_tk_fact_tables_and_downgrade_restores_legacy_entities(runtime_db_url):
    config = Config("alembic.ini")

    command.upgrade(config, "head")
    table_names = _list_postgres_tables(runtime_db_url)

    assert "tk_products" in table_names
    assert "tk_raw_api_responses" in table_names
    assert "entity_registry" not in table_names

    command.downgrade(config, "20260412_0001")
    downgraded_table_names = _list_postgres_tables(runtime_db_url)

    assert "tk_products" not in downgraded_table_names
    assert "entity_registry" in downgraded_table_names
    assert "entity_snapshot" in downgraded_table_names


def test_tk_fact_store_upserts_entities_media_relations_and_raw_links(runtime_db_url):
    store = RuntimeStore(db_url=runtime_db_url)
    fact_store = TKFactStore(runtime_store=store)

    product_a = fact_store.upsert_product(product_id="1729440407432826887", title="Rose Bear")
    product_b = fact_store.upsert_product(product_id="1729440407432826887", title="Rose Bear Updated")
    shop = fact_store.upsert_shop(shop_name="Holiday Shop")
    creator = fact_store.upsert_creator(creator_id="creator-1", uid="7094679250578015274")
    asset_a = fact_store.upsert_media_asset(source_url="https://example.com/main.png")
    asset_b = fact_store.upsert_media_asset(source_url="https://example.com/main.png")
    media_link = fact_store.link_media_asset(
        entity_type="product",
        entity_external_id="1729440407432826887",
        asset_id=asset_a["asset_id"],
        media_role="product_main_image",
    )
    product_shop = fact_store.upsert_product_shop_relation(
        product_id="1729440407432826887",
        shop_key=shop["shop_key"],
        shop_name=shop["shop_name"],
    )
    creator_product = fact_store.upsert_creator_product_relation(
        creator_key=creator["creator_key"],
        creator_id="creator-1",
        product_id="1729440407432826887",
        sold_count=88,
    )
    raw = fact_store.record_raw_api_response(
        source_platform="fastmoss",
        source_endpoint="goods.v3.overview",
        request_params={"product_id": "1729440407432826887"},
        response_payload={"ok": True},
    )
    raw_link = fact_store.link_raw_entity(
        raw_response_id=raw["raw_response_id"],
        entity_type="product",
        entity_external_id="1729440407432826887",
    )

    assert product_a["id"] == product_b["id"]
    assert product_b["title"] == "Rose Bear Updated"
    assert asset_a["asset_id"] == asset_b["asset_id"]
    assert media_link["entity_external_id"] == "1729440407432826887"
    assert product_shop["product_id"] == "1729440407432826887"
    assert creator_product["sold_count"] == 88
    assert fact_store.creator_has_product(creator_id="creator-1", product_id="1729440407432826887")
    assert raw_link["raw_response_id"] == raw["raw_response_id"]


def test_tk_fact_store_records_product_window_snapshots(runtime_db_url):
    store = RuntimeStore(db_url=runtime_db_url)
    fact_store = TKFactStore(runtime_store=store)

    observation = fact_store.record_product_window_observation(
        product_id="1729440407432826887",
        source_platform="tiktok",
        source_endpoint="tiktok.product.http_request",
        window_days=0,
        observation_reason="product_ingest",
        payload={"rating_score": 4.8, "review_count": 123},
    )
    latest_a = fact_store.upsert_product_window_latest(
        product_id="1729440407432826887",
        source_platform="tiktok",
        source_endpoint="tiktok.product.http_request",
        window_days=0,
        payload={"rating_score": 4.8, "review_count": 123},
    )
    latest_b = fact_store.upsert_product_window_latest(
        product_id="1729440407432826887",
        source_platform="tiktok",
        source_endpoint="tiktok.product.http_request",
        window_days=0,
        payload={"rating_score": 4.9, "review_count": 130},
    )

    assert observation["product_id"] == "1729440407432826887"
    assert observation["is_persisted_snapshot"] == 1
    assert observation["payload"]["rating_score"] == 4.8
    assert latest_a["latest_id"] == latest_b["latest_id"]
    assert latest_b["payload"]["rating_score"] == 4.9
    assert latest_b["payload"]["review_count"] == 130


def test_tk_fact_store_records_product_sku_window_snapshots(runtime_db_url):
    store = RuntimeStore(db_url=runtime_db_url)
    fact_store = TKFactStore(runtime_store=store)

    observation = fact_store.record_product_sku_window_observation(
        product_id="1729440407432826887",
        sku_id="sku-pink",
        sku_name="Pink",
        source_platform="fastmoss",
        window_days=28,
        sold_count=31,
        sale_amount=401.69,
        stock_count=7,
        observation_reason="product_sku_ingest",
        payload={"price_text": "$12.99", "source_endpoint": "fastmoss.goods.v3.productSku"},
    )
    latest_a = fact_store.upsert_product_sku_window_latest(
        product_id="1729440407432826887",
        sku_id="sku-pink",
        sku_name="Pink",
        source_platform="fastmoss",
        window_days=28,
        sold_count=31,
        stock_count=7,
        payload={"price_text": "$12.99"},
    )
    latest_b = fact_store.upsert_product_sku_window_latest(
        product_id="1729440407432826887",
        sku_id="sku-pink",
        sku_name="Pink",
        source_platform="fastmoss",
        window_days=28,
        sold_count=33,
        stock_count=5,
        payload={"price_text": "$13.99"},
    )

    assert observation["sku_key"] == "1729440407432826887:sku-pink"
    assert observation["stock_count"] == 7
    assert observation["payload"]["source_endpoint"] == "fastmoss.goods.v3.productSku"
    assert latest_a["latest_id"] == latest_b["latest_id"]
    assert latest_b["sold_count"] == 33
    assert latest_b["stock_count"] == 5
    assert latest_b["payload"]["price_text"] == "$13.99"


def test_tk_fact_store_records_daily_and_distribution_snapshots(runtime_db_url):
    store = RuntimeStore(db_url=runtime_db_url)
    fact_store = TKFactStore(runtime_store=store)

    daily_a = fact_store.upsert_product_daily_metric(
        product_id="1729440407432826887",
        metric_date="2026-04-01",
        source_platform="fastmoss",
        sold_count=12,
        sale_amount=155.88,
        price_amount=12.99,
        currency="USD",
        payload={"source_endpoint": "fastmoss.goods.v3.overview"},
    )
    daily_b = fact_store.upsert_product_daily_metric(
        product_id="1729440407432826887",
        metric_date="2026-04-01",
        source_platform="fastmoss",
        sold_count=14,
        sale_amount=181.86,
        price_amount=12.99,
        currency="USD",
        payload={"source_endpoint": "fastmoss.goods.v3.overview", "refreshed": True},
    )
    observation = fact_store.record_product_distribution_window_observation(
        product_id="1729440407432826887",
        distribution_type="channel",
        source_key="common.goods.affiliate",
        source_name="达人联盟",
        source_platform="fastmoss",
        window_days=28,
        metric_value=66,
        metric_amount=914.5,
        observation_reason="overview_distribution",
        payload={"sold_proportion": 0.75, "gmv_proportion": 0.8},
    )
    latest_a = fact_store.upsert_product_distribution_window_latest(
        product_id="1729440407432826887",
        distribution_type="channel",
        source_key="common.goods.affiliate",
        source_name="达人联盟",
        source_platform="fastmoss",
        window_days=28,
        metric_value=66,
        metric_amount=914.5,
        payload={"sold_proportion": 0.75},
    )
    latest_b = fact_store.upsert_product_distribution_window_latest(
        product_id="1729440407432826887",
        distribution_type="channel",
        source_key="common.goods.affiliate",
        source_name="达人联盟",
        source_platform="fastmoss",
        window_days=28,
        metric_value=70,
        metric_amount=1000.0,
        payload={"sold_proportion": 0.8},
    )

    assert daily_a["metric_id"] == daily_b["metric_id"]
    assert daily_b["sold_count"] == 14
    assert daily_b["payload"]["refreshed"] is True
    assert observation["source_name"] == "达人联盟"
    assert observation["payload"]["gmv_proportion"] == 0.8
    assert latest_a["latest_id"] == latest_b["latest_id"]
    assert latest_b["metric_value"] == 70
    assert latest_b["metric_amount"] == 1000.0
    assert latest_b["payload"]["sold_proportion"] == 0.8


def test_persist_influencer_fact_bundle_writes_creator_product_shop_and_media(runtime_db_url):
    store = RuntimeStore(db_url=runtime_db_url)
    service = TKFactIngestionService(runtime_store=store)
    execution = type(
        "Execution",
        (),
        {"request_id": "req-1", "execution_id": "exec-1", "run_id": "run-1"},
    )()

    payload = service.ingest_api_response(
        source_platform="fastmoss",
        source_endpoint="influencer_pool.fact_bundle",
        request_params={"source_key": "creator-1"},
        response_payload={"source_key": "creator-1"},
        products=[
            {
                "product_id": "1729440407432826887",
                "shop_name": "Holiday Shop",
            }
        ],
        creators=[
            {
                "creator_id": "creator-1",
                "uid": "7094679250578015274",
                "nickname": "Holiday Creator",
                "avatar_url": "https://example.com/avatar.png",
                "follower_count": 10000,
            }
        ],
        shops=[
            {
                "shop_name": "Holiday Shop",
                "source_platform": "fastmoss",
            }
        ],
        media_assets=[
            {
                "entity_type": "creator",
                "entity_external_id": "creator-1",
                "media_role": "avatar",
                "source_url": "https://example.com/avatar.png",
                "source_platform": "fastmoss",
            },
            {
                "entity_type": "product",
                "entity_external_id": "1729440407432826887",
                "media_role": "product_main_image",
                "file_token": "file-token-main",
                "source_platform": "feishu",
            },
        ],
        relations={
            "creator_products": [
                {
                    "creator_id": "creator-1",
                    "product_id": "1729440407432826887",
                    "sold_count": 66,
                }
            ],
            "shop_creators": [
                {
                    "shop_name": "Holiday Shop",
                    "creator_id": "creator-1",
                }
            ],
        },
        execution=execution,
    )
    fact_store = TKFactStore(runtime_store=store)

    assert any(entity.get("creator_key") == "creator_id:creator-1" for entity in payload["fact_entities"])
    assert any(entity.get("product_id") == "1729440407432826887" for entity in payload["fact_entities"])
    assert any(entity.get("shop_name") == "Holiday Shop" for entity in payload["fact_entities"])
    assert fact_store.creator_has_product(
        creator_id="creator-1",
        product_id="1729440407432826887",
    )
    assert payload["fact_media_assets"]
    assert payload["raw_api_responses"]


def test_tk_fact_ingestion_service_links_fastmoss_api_entities_and_relations(runtime_db_url):
    store = RuntimeStore(db_url=runtime_db_url)
    service = TKFactIngestionService(runtime_store=store)

    payload = service.ingest_api_response(
        source_platform="fastmoss",
        source_endpoint="goods.detail.bundle",
        request_params={"product_id": "1729440407432826887"},
        response_payload={"ok": True},
        products=[
            {
                "product_id": "1729440407432826887",
                "title": "Rose Bear",
                "shop_name": "Holiday Shop",
            }
        ],
        creators=[
            {
                "creator_id": "creator-1",
                "uid": "7094679250578015274",
                "nickname": "Holiday Creator",
            }
        ],
        videos=[
            {
                "video_id": "7623147954093690143",
                "creator_id": "creator-1",
                "product_id": "1729440407432826887",
                "title": "Gift video",
            }
        ],
        media_assets=[
            {
                "entity_type": "video",
                "entity_external_id": "video:7623147954093690143",
                "media_role": "video_cover",
                "source_url": "https://example.com/video-cover.png",
                "source_platform": "fastmoss",
            }
        ],
        relations={
            "creator_products": [
                {
                    "creator_id": "creator-1",
                    "product_id": "1729440407432826887",
                    "sold_count": 99,
                }
            ],
            "shop_creators": [
                {
                    "shop_name": "Holiday Shop",
                    "creator_id": "creator-1",
                }
            ],
        },
    )
    fact_store = TKFactStore(runtime_store=store)

    assert any(entity.get("product_id") == "1729440407432826887" for entity in payload["fact_entities"])
    assert any(entity.get("creator_key") == "creator_id:creator-1" for entity in payload["fact_entities"])
    assert any(entity.get("video_key") == "video:7623147954093690143" for entity in payload["fact_entities"])
    assert any(
        relation.get("product_id") == "1729440407432826887"
        and relation.get("shop_key") == "shop_name:Holiday Shop"
        for relation in payload["fact_relations"]
    )
    assert any(relation.get("sold_count") == 99 for relation in payload["fact_relations"])
    assert any(relation.get("video_key") == "video:7623147954093690143" for relation in payload["fact_relations"])
    assert fact_store.creator_has_product(creator_id="creator-1", product_id="1729440407432826887")
    assert payload["fact_media_assets"]
    assert payload["raw_api_responses"]


def test_extract_fact_payloads_dedupes_fact_payload_groups():
    entity = {"product_id": "1", "title": "A"}
    relation = {"relation_key": "creator:1:product:1"}
    media = {"asset_id": "asset-1"}
    metric = {"observation_id": "observation-1"}
    raw = {"raw_response_id": "raw-1"}

    payload = extract_fact_payloads(
        [
            {
                "fact_entities": [entity],
                "fact_relations": [relation],
                "fact_media_assets": [media],
                "fact_metric_observations": [metric],
                "raw_api_responses": [raw],
            },
            {
                "fact_entities": [dict(entity)],
                "fact_relations": [dict(relation)],
                "fact_media_assets": [dict(media)],
                "fact_metric_observations": [dict(metric)],
                "raw_api_responses": [dict(raw)],
            },
        ]
    )

    assert payload["fact_entities"] == [entity]
    assert payload["fact_relations"] == [relation]
    assert payload["fact_media_assets"] == [media]
    assert payload["fact_metric_observations"] == [metric]
    assert payload["raw_api_responses"] == [raw]


def _list_postgres_tables(db_url):
    engine = create_engine(db_url, future=True)
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = current_schema()
                """
            )
        ).mappings().all()
    return {str(row["table_name"]) for row in rows}
