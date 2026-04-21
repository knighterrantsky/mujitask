from __future__ import annotations

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from automation_business_scaffold.flows.influencer_pool_support import persist_influencer_fact_bundle
from automation_business_scaffold.flows.phase1_runtime_store import Phase1RuntimeStore
from automation_business_scaffold.flows.tk_fact_store import TKFactStore, extract_fact_payloads


def test_tk_fact_schema_replaces_legacy_entity_tables(tmp_path):
    store = Phase1RuntimeStore(db_path=tmp_path / "tk-facts.sqlite3")
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


def test_alembic_upgrade_creates_tk_fact_tables_and_downgrade_restores_legacy_entities(tmp_path):
    db_path = tmp_path / "alembic-tk-facts.sqlite3"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")

    command.upgrade(config, "head")
    table_names = _list_sqlite_tables(db_path)

    assert "tk_products" in table_names
    assert "tk_raw_api_responses" in table_names
    assert "entity_registry" not in table_names

    command.downgrade(config, "20260412_0001")
    downgraded_table_names = _list_sqlite_tables(db_path)

    assert "tk_products" not in downgraded_table_names
    assert "entity_registry" in downgraded_table_names
    assert "entity_snapshot" in downgraded_table_names


def test_tk_fact_store_upserts_entities_media_relations_and_raw_links(tmp_path):
    store = Phase1RuntimeStore(db_path=tmp_path / "tk-facts-upsert.sqlite3")
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


def test_persist_influencer_fact_bundle_writes_creator_product_shop_and_media(tmp_path):
    store = Phase1RuntimeStore(db_path=tmp_path / "tk-influencer-facts.sqlite3")
    execution = type(
        "Execution",
        (),
        {"request_id": "req-1", "execution_id": "exec-1", "run_id": "run-1"},
    )()

    payload = persist_influencer_fact_bundle(
        store=store,
        execution=execution,
        influencer_state={
            "influencer_id": "creator-1",
            "uid": "7094679250578015274",
            "source_product_ids": ["1729440407432826887"],
            "source_product_sales_by_id": {"1729440407432826887": 66},
            "source_product_image_refs_by_id": {
                "1729440407432826887": [{"file_token": "file-token-main"}]
            },
            "holiday_names": ["Valentine"],
            "cooperation_shop_names": ["Holiday Shop"],
            "avatar": "https://example.com/avatar.png",
            "follower_count": 10000,
        },
        table_url="https://example.feishu.cn/base/appXXX?table=tblXXX",
        target_record_id="rec-creator",
        source_key="creator-1",
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


def test_extract_fact_payloads_dedupes_fact_payload_groups():
    entity = {"product_id": "1", "title": "A"}
    relation = {"relation_key": "creator:1:product:1"}
    media = {"asset_id": "asset-1"}
    raw = {"raw_response_id": "raw-1"}

    payload = extract_fact_payloads(
        [
            {
                "fact_entities": [entity],
                "fact_relations": [relation],
                "fact_media_assets": [media],
                "raw_api_responses": [raw],
            },
            {
                "fact_entities": [dict(entity)],
                "fact_relations": [dict(relation)],
                "fact_media_assets": [dict(media)],
                "raw_api_responses": [dict(raw)],
            },
        ]
    )

    assert payload["fact_entities"] == [entity]
    assert payload["fact_relations"] == [relation]
    assert payload["fact_media_assets"] == [media]
    assert payload["raw_api_responses"] == [raw]


def _list_sqlite_tables(db_path):
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'")
        ).mappings().all()
    return {str(row["name"]) for row in rows}
