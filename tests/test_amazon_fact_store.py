from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import create_engine, text

from automation_business_scaffold.infrastructure.facts.amazon_fact_store import AmazonFactStore


def test_store_constructor_does_not_bootstrap_or_touch_the_database() -> None:
    engine = object()
    runtime_store = SimpleNamespace(_engine=engine, _text=text)

    store = AmazonFactStore(runtime_store=runtime_store)

    assert store._engine is engine  # noqa: SLF001


def test_product_master_upsert_is_idempotent_and_missing_values_do_not_erase(
    runtime_db_url,
) -> None:
    store = AmazonFactStore(db_url=runtime_db_url)

    first = store.upsert_product(
        marketplace_code="US",
        asin="B0CHILD001",
        canonical_url="https://www.amazon.com/dp/B0CHILD001",
        parent_asin="B0PARENT01",
        title="First title",
        category_path=["Home", "Lighting"],
        facts={"source": "fixture"},
        observed_at=1000.0,
    )
    repeated = store.upsert_product(
        marketplace_code="US",
        asin="B0CHILD001",
        title=None,
        brand="Updated Brand",
        category_path=None,
        facts=None,
        observed_at=2000.0,
    )

    assert first["id"] == repeated["id"]
    assert repeated["title"] == "First title"
    assert repeated["brand"] == "Updated Brand"
    assert repeated["category_path"] == ["Home", "Lighting"]
    assert repeated["facts"] == {"source": "fixture"}
    assert repeated["first_seen_at"] == 1000.0
    assert repeated["last_seen_at"] == 2000.0
    assert _count(runtime_db_url, "amazon_products") == 1


def test_product_snapshot_is_immutable_per_run_and_new_runs_append(runtime_db_url) -> None:
    store = AmazonFactStore(db_url=runtime_db_url)
    product = store.upsert_product(
        marketplace_code="US",
        asin="B0CHILD001",
        observed_at=1000.0,
    )

    first = store.record_product_snapshot(
        product_id=product["id"],
        marketplace_code="US",
        asin="B0CHILD001",
        run_id="run-1",
        request_id="request-1",
        execution_id="execution-1",
        resolved_asin="B0CHILD001",
        parent_asin="B0PARENT01",
        availability_status="in_stock",
        title="First observation",
        category_path=["Home"],
        bullet_points=["One", "Two"],
        technical_details={"Material": "Oak"},
        rating=4.7,
        review_count=1234,
        variant_attributes={"Color": "Blue"},
        child_asins=["B0CHILD001", "B0CHILD002"],
        field_coverage={"percent": 100.0},
        payload={"collection_status": "success"},
        content_digest="digest-1",
        collected_at=1000.0,
    )
    repeated = store.record_product_snapshot(
        product_id=product["id"],
        marketplace_code="US",
        asin="B0CHILD001",
        run_id="run-1",
        title="Must not replace immutable snapshot",
        collected_at=2000.0,
    )
    second_run = store.record_product_snapshot(
        product_id=product["id"],
        marketplace_code="US",
        asin="B0CHILD001",
        run_id="run-2",
        title="Second observation",
        collected_at=3000.0,
    )
    latest = store.set_latest_snapshot(
        product_id=product["id"],
        snapshot_id=second_run["snapshot_id"],
        observed_at=3000.0,
    )

    assert first["snapshot_id"] == repeated["snapshot_id"]
    assert repeated["title"] == "First observation"
    assert repeated["bullet_points"] == ["One", "Two"]
    assert first["snapshot_id"] != second_run["snapshot_id"]
    assert latest["latest_snapshot_id"] == second_run["snapshot_id"]
    assert _count(runtime_db_url, "amazon_product_snapshots") == 2


def test_stale_product_replay_cannot_regress_master_or_latest_snapshot(runtime_db_url) -> None:
    store = AmazonFactStore(db_url=runtime_db_url)
    product = store.upsert_product(
        marketplace_code="US",
        asin="B0CHILD001",
        title="Old title",
        status="active",
        observed_at=1000.0,
    )
    first_snapshot = store.record_product_snapshot(
        product_id=product["id"],
        marketplace_code="US",
        asin="B0CHILD001",
        run_id="run-1",
        title="Old title",
        collected_at=1000.0,
    )
    store.set_latest_snapshot(
        product_id=product["id"],
        snapshot_id=first_snapshot["snapshot_id"],
        observed_at=1000.0,
    )
    store.upsert_product(
        marketplace_code="US",
        asin="B0CHILD001",
        title="New unavailable title",
        status="unavailable",
        observed_at=2000.0,
    )
    second_snapshot = store.record_product_snapshot(
        product_id=product["id"],
        marketplace_code="US",
        asin="B0CHILD001",
        run_id="run-2",
        title="New unavailable title",
        collected_at=2000.0,
    )
    store.set_latest_snapshot(
        product_id=product["id"],
        snapshot_id=second_snapshot["snapshot_id"],
        observed_at=2000.0,
    )

    stale = store.upsert_product(
        marketplace_code="US",
        asin="B0CHILD001",
        title="Old title",
        status="active",
        observed_at=1000.0,
    )
    after_stale_pointer = store.set_latest_snapshot(
        product_id=product["id"],
        snapshot_id=first_snapshot["snapshot_id"],
        observed_at=1000.0,
    )

    assert stale["title"] == "New unavailable title"
    assert stale["status"] == "unavailable"
    assert stale["last_seen_at"] == 2000.0
    assert after_stale_pointer["latest_snapshot_id"] == second_snapshot["snapshot_id"]

    other_product = store.upsert_product(
        marketplace_code="US",
        asin="B0CHILD002",
        observed_at=3000.0,
    )
    other_snapshot = store.record_product_snapshot(
        product_id=other_product["id"],
        marketplace_code="US",
        asin="B0CHILD002",
        run_id="other-run",
        collected_at=3000.0,
    )
    after_foreign_pointer = store.set_latest_snapshot(
        product_id=product["id"],
        snapshot_id=other_snapshot["snapshot_id"],
        observed_at=3000.0,
    )

    assert after_foreign_pointer["latest_snapshot_id"] == second_snapshot["snapshot_id"]


def test_offer_variant_and_bsr_writes_are_independently_idempotent(runtime_db_url) -> None:
    store = AmazonFactStore(db_url=runtime_db_url)
    product = store.upsert_product(
        marketplace_code="US",
        asin="B0CHILD001",
        observed_at=1000.0,
    )
    snapshot = store.record_product_snapshot(
        product_id=product["id"],
        marketplace_code="US",
        asin="B0CHILD001",
        run_id="run-1",
        collected_at=1000.0,
    )

    offer = store.record_featured_offer(
        product_snapshot_id=snapshot["snapshot_id"],
        product_id=product["id"],
        seller_id="SELLER123",
        seller_name="Example Seller",
        is_featured_offer=True,
        price_amount="29.99",
        list_price_amount="39.99",
        currency="USD",
        availability_status="in_stock",
        fulfillment_channel="amazon",
        promotions=["Buy 2, save 5%"],
        collected_at=1000.0,
    )
    repeated_offer = store.record_featured_offer(
        product_snapshot_id=snapshot["snapshot_id"],
        product_id=product["id"],
        seller_id="OTHER",
        price_amount="99.99",
        collected_at=2000.0,
    )
    variant = store.upsert_variant(
        marketplace_code="US",
        parent_asin="B0PARENT01",
        child_asin="B0CHILD001",
        attributes={"Color": "Blue"},
        dimensions={"Color": ["Blue", "Red"]},
        source_asin="B0CHILD001",
        observed_at=1000.0,
    )
    repeated_variant = store.upsert_variant(
        marketplace_code="US",
        parent_asin="B0PARENT01",
        child_asin="B0CHILD001",
        attributes={"Color": "Navy"},
        dimensions={"Color": ["Navy", "Red"]},
        source_asin="B0CHILD001",
        observed_at=2000.0,
    )
    first_rank = store.record_bsr_snapshot(
        product_snapshot_id=snapshot["snapshot_id"],
        product_id=product["id"],
        category_name="Table Lamps",
        category_path=["Home", "Lighting", "Table Lamps"],
        rank_value=7,
        collected_at=1000.0,
    )
    repeated_rank = store.record_bsr_snapshot(
        product_snapshot_id=snapshot["snapshot_id"],
        product_id=product["id"],
        category_name="Table Lamps",
        category_path=["Home", "Lighting", "Table Lamps"],
        rank_value=99,
        collected_at=2000.0,
    )
    store.record_bsr_snapshot(
        product_snapshot_id=snapshot["snapshot_id"],
        product_id=product["id"],
        category_name="Home & Kitchen",
        category_path=["Home & Kitchen"],
        rank_value=321,
        collected_at=1000.0,
    )

    assert offer["offer_snapshot_id"] == repeated_offer["offer_snapshot_id"]
    assert repeated_offer["seller_id"] == "SELLER123"
    assert repeated_offer["price_amount"] == 29.99
    assert repeated_offer["promotions"] == ["Buy 2, save 5%"]
    assert variant["relation_id"] == repeated_variant["relation_id"]
    assert repeated_variant["attributes"] == {"Color": "Navy"}
    assert first_rank["bsr_snapshot_id"] == repeated_rank["bsr_snapshot_id"]
    assert repeated_rank["rank_value"] == 7
    assert _count(runtime_db_url, "amazon_offer_snapshots") == 1
    assert _count(runtime_db_url, "amazon_product_variants") == 1
    assert _count(runtime_db_url, "amazon_bsr_snapshots") == 2


def test_media_raw_capture_and_feishu_binding_writes_are_idempotent(runtime_db_url) -> None:
    store = AmazonFactStore(db_url=runtime_db_url)
    product = store.upsert_product(
        marketplace_code="US",
        asin="B0CHILD001",
        observed_at=1000.0,
    )
    snapshot = store.record_product_snapshot(
        product_id=product["id"],
        marketplace_code="US",
        asin="B0CHILD001",
        run_id="run-1",
        collected_at=1000.0,
    )

    asset = store.upsert_media_asset(
        source_url="https://images.example.test/main.jpg",
        content_digest="content-sha256",
        bucket="artifacts",
        object_key="product-media/amazon/us/B0CHILD001/main.jpg",
        remote_uri="s3://artifacts/product-media/amazon/us/B0CHILD001/main.jpg",
        file_name="main.jpg",
        mime_type="image/jpeg",
        size_bytes=123,
        observed_at=1000.0,
    )
    repeated_asset = store.upsert_media_asset(
        source_url="https://images.example.test/main.jpg",
        content_digest="content-sha256",
        bucket="artifacts",
        object_key="product-media/amazon/us/B0CHILD001/main.jpg",
        remote_uri="s3://artifacts/product-media/amazon/us/B0CHILD001/main.jpg",
        observed_at=2000.0,
    )
    link = store.link_product_media_asset(
        product_id=product["id"],
        asset_id=asset["asset_id"],
        media_role="main_image",
        position=0,
        observed_at=1000.0,
    )
    repeated_link = store.link_product_media_asset(
        product_id=product["id"],
        asset_id=asset["asset_id"],
        media_role="main_image",
        position=0,
        observed_at=2000.0,
    )
    raw = store.record_raw_capture(
        product_id=product["id"],
        snapshot_id=snapshot["snapshot_id"],
        capture_kind="normalized_capture",
        bucket="artifacts",
        object_key="raw-captures/amazon/us/B0CHILD001/run-1/normalized.json",
        content_digest="capture-sha256",
        content_type="application/json",
        request_id="request-1",
        execution_id="execution-1",
        run_id="run-1",
        sanitization_status="sanitized",
        collected_at=1000.0,
    )
    repeated_raw = store.record_raw_capture(
        product_id=product["id"],
        snapshot_id=snapshot["snapshot_id"],
        capture_kind="normalized_capture",
        bucket="artifacts",
        object_key="raw-captures/amazon/us/B0CHILD001/run-1/normalized.json",
        collected_at=2000.0,
    )
    binding = store.upsert_feishu_binding(
        product_id=product["id"],
        base_id="base-1",
        table_id="table-1",
        record_id="record-1",
        source_asin="B0CHILD001",
        status="pending",
        last_synced_snapshot_id="",
        observed_at=1000.0,
    )
    repeated_binding = store.upsert_feishu_binding(
        product_id=product["id"],
        base_id="base-1",
        table_id="table-1",
        record_id="record-1",
        source_asin="B0CHILD001",
        status="facts_persisted",
        last_synced_snapshot_id=snapshot["snapshot_id"],
        observed_at=2000.0,
    )

    assert asset["asset_id"] == repeated_asset["asset_id"]
    assert repeated_asset["last_seen_at"] == 2000.0
    assert link["relation_id"] == repeated_link["relation_id"]
    assert raw["raw_capture_id"] == repeated_raw["raw_capture_id"]
    assert binding["binding_id"] == repeated_binding["binding_id"]
    assert repeated_binding["status"] == "facts_persisted"
    assert repeated_binding["last_synced_snapshot_id"] == snapshot["snapshot_id"]
    assert _count(runtime_db_url, "amazon_media_assets") == 1
    assert _count(runtime_db_url, "amazon_product_media_assets") == 1
    assert _count(runtime_db_url, "amazon_raw_captures") == 1
    assert _count(runtime_db_url, "amazon_feishu_bindings") == 1


def test_stale_mutable_replays_do_not_overwrite_newer_facts(runtime_db_url) -> None:
    store = AmazonFactStore(db_url=runtime_db_url)
    product = store.upsert_product(
        marketplace_code="US",
        asin="B0CHILD001",
        observed_at=2000.0,
    )
    snapshot = store.record_product_snapshot(
        product_id=product["id"],
        marketplace_code="US",
        asin="B0CHILD001",
        run_id="run-2",
        collected_at=2000.0,
    )
    variant = store.upsert_variant(
        marketplace_code="US",
        parent_asin="B0PARENT01",
        child_asin="B0CHILD001",
        attributes={"Color": "Navy"},
        dimensions={"Color": ["Navy", "Red"]},
        observed_at=2000.0,
    )
    asset = store.upsert_media_asset(
        asset_key="asset-1",
        source_url="https://images.example.test/new.jpg",
        content_digest="new-digest",
        bucket="artifacts",
        object_key="new.jpg",
        metadata={"version": "new"},
        observed_at=2000.0,
    )
    relation = store.link_product_media_asset(
        product_id=product["id"],
        asset_id=asset["asset_id"],
        media_role="main_image",
        metadata={"version": "new"},
        observed_at=2000.0,
    )
    binding = store.upsert_feishu_binding(
        product_id=product["id"],
        base_id="base-1",
        table_id="table-1",
        record_id="record-1",
        source_asin="B0CHILD001",
        status="facts_persisted",
        last_synced_snapshot_id=snapshot["snapshot_id"],
        observed_at=2000.0,
    )

    stale_variant = store.upsert_variant(
        marketplace_code="US",
        parent_asin="B0PARENT01",
        child_asin="B0CHILD001",
        attributes={"Color": "Old blue"},
        dimensions={"Color": ["Old blue"]},
        observed_at=1000.0,
    )
    stale_asset = store.upsert_media_asset(
        asset_key="asset-1",
        source_url="https://images.example.test/old.jpg",
        content_digest="old-digest",
        bucket="artifacts",
        object_key="old.jpg",
        metadata={"version": "old"},
        observed_at=1000.0,
    )
    stale_relation = store.link_product_media_asset(
        product_id=product["id"],
        asset_id=asset["asset_id"],
        media_role="main_image",
        metadata={"version": "old"},
        observed_at=1000.0,
    )
    stale_binding = store.upsert_feishu_binding(
        product_id=product["id"],
        base_id="base-1",
        table_id="table-1",
        record_id="record-1",
        source_asin="B0CHILD001",
        status="active",
        last_synced_snapshot_id="old-snapshot",
        observed_at=1000.0,
    )
    omitted_status = store.upsert_feishu_binding(
        product_id=product["id"],
        base_id="base-1",
        table_id="table-1",
        record_id="record-1",
        observed_at=3000.0,
    )

    assert variant["relation_id"] == stale_variant["relation_id"]
    assert stale_variant["attributes"] == {"Color": "Navy"}
    assert stale_variant["dimensions"] == {"Color": ["Navy", "Red"]}
    assert stale_variant["first_seen_at"] == 1000.0
    assert stale_variant["last_seen_at"] == 2000.0
    assert stale_asset["source_url"] == "https://images.example.test/new.jpg"
    assert stale_asset["content_digest"] == "new-digest"
    assert stale_asset["metadata"] == {"version": "new"}
    assert stale_asset["last_seen_at"] == 2000.0
    assert relation["relation_id"] == stale_relation["relation_id"]
    assert stale_relation["metadata"] == {"version": "new"}
    assert stale_relation["last_seen_at"] == 2000.0
    assert binding["binding_id"] == stale_binding["binding_id"]
    assert stale_binding["status"] == "facts_persisted"
    assert stale_binding["last_synced_snapshot_id"] == snapshot["snapshot_id"]
    assert omitted_status["status"] == "facts_persisted"
    assert omitted_status["last_synced_snapshot_id"] == snapshot["snapshot_id"]
    assert omitted_status["last_synced_at"] == 3000.0


def _count(db_url: str, table_name: str) -> int:
    engine = create_engine(db_url, future=True)
    try:
        with engine.connect() as connection:
            return int(connection.execute(text(f"SELECT COUNT(*) FROM {table_name}")).scalar_one())
    finally:
        engine.dispose()
