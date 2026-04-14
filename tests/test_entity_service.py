from __future__ import annotations

from automation_business_scaffold.flows.entity_service import (
    build_product_canonical_key,
    build_snapshot_diff,
    extract_entity_payloads,
)


def test_build_product_canonical_key_prefers_product_id_over_url():
    key = build_product_canonical_key(
        {
            "product_id": "1731098351299629802",
            "normalized_url": "https://www.tiktok.com/shop/pdp/1731098351299629802",
        }
    )
    assert key == "tiktok_product:1731098351299629802"


def test_build_snapshot_diff_only_keeps_changed_fields():
    diff = build_snapshot_diff(
        {"status": "updated", "logical_fields": {"title": "Old"}, "price": "1.00"},
        {"status": "updated", "logical_fields": {"title": "New"}, "price": "1.00"},
    )
    assert diff == {
        "logical_fields": {
            "before": {"title": "Old"},
            "after": {"title": "New"},
        }
    }


def test_extract_entity_payloads_dedupes_entities_bindings_and_snapshots():
    entity = {"entity_id": "ent-1", "canonical_key": "tiktok_product:1"}
    binding = {"binding_id": "bind-1", "target_id": "rec-1"}
    snapshot = {"snapshot_id": "snap-1", "entity_id": "ent-1"}

    entities, bindings, snapshots = extract_entity_payloads(
        [
            {"record_id": "rec-1", "entity": entity, "binding": binding, "entity_snapshot": snapshot},
            {"record_id": "rec-1-copy", "entity": dict(entity), "binding": dict(binding), "entity_snapshot": dict(snapshot)},
        ]
    )

    assert entities == [entity]
    assert bindings == [binding]
    assert snapshots == [snapshot]
