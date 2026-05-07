from __future__ import annotations


def creator_dedupe_key(creator_id: str, product_key: str) -> str:
    return f"{creator_id}:{product_key}"
