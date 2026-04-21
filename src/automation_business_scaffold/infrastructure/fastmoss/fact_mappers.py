from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

FactMapping = dict[str, Any]


def map_fastmoss_goods_base(
    payload: Mapping[str, Any],
    *,
    product_id: str = "",
) -> FactMapping:
    data = extract_fastmoss_data(payload)
    product = _as_mapping(data.get("product"))
    shop = _as_mapping(data.get("shop"))
    normalized_product_id = _first_non_empty(
        product_id,
        product.get("product_id"),
        product.get("id"),
        data.get("product_id"),
    )
    result = _empty_mapping()

    product_spec = _product_from_mapping(product, fallback_product_id=normalized_product_id, shop=shop)
    _append(result["products"], product_spec)
    _append(result["shops"], _shop_from_mapping(shop))
    _append(result["relations"]["product_shops"], _product_shop_relation(product_spec, shop))
    _append_media(result, _product_media(product_spec, product))
    return result


def map_fastmoss_goods_overview(
    payload: Mapping[str, Any],
    *,
    product_id: str = "",
) -> FactMapping:
    data = extract_fastmoss_data(payload)
    overview = _as_mapping(data.get("overview"))
    result = _empty_mapping()
    normalized_product_id = _first_non_empty(product_id, data.get("product_id"), overview.get("product_id"))
    if normalized_product_id:
        _append(
            result["products"],
            {
                "product_id": normalized_product_id,
                "source_platform": "fastmoss",
                "facts": {"overview": dict(overview), "raw": dict(data)},
            },
        )
    return result


def map_fastmoss_goods_product_sku(
    payload: Mapping[str, Any],
    *,
    product_id: str = "",
) -> FactMapping:
    data = extract_fastmoss_data(payload)
    result = _empty_mapping()
    normalized_product_id = _first_non_empty(product_id, data.get("product_id"))
    if normalized_product_id:
        _append(result["products"], {"product_id": normalized_product_id, "source_platform": "fastmoss"})
    for row in _extract_rows(data, "sku_list", "list"):
        sku_id = _first_non_empty(row.get("sku_id"), row.get("id"))
        spec_name = _join_spec_values(row.get("sku_sale_props") or row.get("props"))
        sku_name = _first_non_empty(row.get("sku_name"), row.get("name"), spec_name, sku_id)
        _append(
            result["product_skus"],
            {
                "product_id": _first_non_empty(row.get("product_id"), normalized_product_id),
                "sku_id": sku_id,
                "sku_name": sku_name,
                "spec_name": spec_name,
                "price_text": _first_non_empty(
                    row.get("real_price"),
                    row.get("real_price_value"),
                    row.get("price"),
                    row.get("sale_price"),
                ),
                "stock_count": row.get("stock") or row.get("stock_count") or 0,
                "facts": {"raw": dict(row)},
            },
        )
    return result


def map_fastmoss_goods_author(
    payload: Mapping[str, Any],
    *,
    product_id: str = "",
) -> FactMapping:
    data = extract_fastmoss_data(payload)
    result = _empty_mapping()
    normalized_product_id = _first_non_empty(product_id, data.get("product_id"))
    if normalized_product_id:
        _append(result["products"], {"product_id": normalized_product_id, "source_platform": "fastmoss"})

    for row in _extract_rows(data, "list"):
        row_product_id = _first_non_empty(row.get("product_id"), normalized_product_id)
        creator = _creator_from_mapping(row)
        _append(result["creators"], creator)
        _append_media(result, _creator_media(creator, row))
        _append(
            result["relations"]["creator_products"],
            _creator_product_relation(creator, row_product_id, row),
        )
        for video_row in _extract_rows(row, "videos", "video_list", "aweme_list"):
            video = _video_from_mapping(video_row, creator=creator, product_id=row_product_id)
            _append(result["videos"], video)
            _append_media(result, _video_media(video, video_row))
    return result


def map_fastmoss_goods_video(
    payload: Mapping[str, Any],
    *,
    product_id: str = "",
) -> FactMapping:
    data = extract_fastmoss_data(payload)
    result = _empty_mapping()
    normalized_product_id = _first_non_empty(product_id, data.get("product_id"))
    if normalized_product_id:
        _append(result["products"], {"product_id": normalized_product_id, "source_platform": "fastmoss"})

    for row in _extract_rows(data, "list"):
        row_product_id = _first_non_empty(row.get("product_id"), normalized_product_id)
        nested_author = _as_mapping(row.get("author"))
        nested_video = _as_mapping(row.get("video"))
        nested_shop = _as_mapping(row.get("shop") or row.get("shop_info"))
        creator = _creator_from_mapping({**row, **nested_author})
        shop = _shop_from_mapping({**row, **nested_shop})
        product = _product_from_mapping(row, fallback_product_id=row_product_id, shop=shop)
        video = _video_from_mapping({**row, **nested_video}, creator=creator, product_id=row_product_id)

        _append(result["products"], product)
        _append(result["shops"], shop)
        _append(result["creators"], creator)
        _append(result["videos"], video)
        _append(result["relations"]["product_shops"], _product_shop_relation(product, shop))
        _append(result["relations"]["creator_products"], _creator_product_relation(creator, row_product_id, row))
        _append(result["relations"]["shop_creators"], _shop_creator_relation(shop, creator, row))
        _append_media(result, _product_media(product, row))
        _append_media(result, _creator_media(creator, row))
        _append_media(result, _video_media(video, row))
    return result


def map_fastmoss_author_search(payload: Mapping[str, Any]) -> FactMapping:
    data = extract_fastmoss_data(payload)
    result = _empty_mapping()
    for row in _extract_rows(data, "list"):
        creator = _creator_from_mapping(row)
        _append(result["creators"], creator)
        _append_media(result, _creator_media(creator, row))
    return result


def map_fastmoss_author_base_info(
    payload: Mapping[str, Any],
    *,
    uid: str = "",
) -> FactMapping:
    data = extract_fastmoss_data(payload)
    result = _empty_mapping()
    creator = _creator_from_mapping(data, fallback_uid=uid)
    shop = _shop_from_mapping(data)
    _append(result["creators"], creator)
    _append(result["shops"], shop)
    _append(result["relations"]["shop_creators"], _shop_creator_relation(shop, creator, data))
    _append_media(result, _creator_media(creator, data))
    return result


def map_fastmoss_author_index(
    payload: Mapping[str, Any],
    *,
    uid: str = "",
    creator_id: str = "",
    unique_id: str = "",
) -> FactMapping:
    data = extract_fastmoss_data(payload)
    return _creator_metric_mapping(
        data,
        uid=uid,
        creator_id=creator_id,
        unique_id=unique_id,
        fact_key="author_index",
    )


def map_fastmoss_author_cargo_summary(
    payload: Mapping[str, Any],
    *,
    uid: str = "",
    creator_id: str = "",
    unique_id: str = "",
) -> FactMapping:
    data = extract_fastmoss_data(payload)
    return _creator_metric_mapping(
        data,
        uid=uid,
        creator_id=creator_id,
        unique_id=unique_id,
        fact_key="cargo_summary",
    )


def map_fastmoss_author_contact(
    payload: Mapping[str, Any],
    *,
    uid: str = "",
    creator_id: str = "",
    unique_id: str = "",
) -> FactMapping:
    data = extract_fastmoss_data(payload)
    return _creator_metric_mapping(
        data,
        uid=uid,
        creator_id=creator_id,
        unique_id=unique_id,
        fact_key="author_contact",
    )


def map_fastmoss_author_shop_list(
    payload: Mapping[str, Any],
    *,
    uid: str = "",
    creator_id: str = "",
    unique_id: str = "",
) -> FactMapping:
    data = extract_fastmoss_data(payload)
    result = _empty_mapping()
    creator = _creator_from_mapping({"uid": uid, "unique_id": unique_id}, fallback_creator_id=creator_id, fallback_uid=uid)
    _append(result["creators"], creator)
    for row in _extract_rows(data, "list"):
        shop = _shop_from_mapping(row)
        _append(result["shops"], shop)
        _append(result["relations"]["shop_creators"], _shop_creator_relation(shop, creator, row))
        _append_media(result, _shop_media(shop, row))
    return result


def map_fastmoss_author_video_list(
    payload: Mapping[str, Any],
    *,
    uid: str = "",
    creator_id: str = "",
    unique_id: str = "",
) -> FactMapping:
    data = extract_fastmoss_data(payload)
    result = _empty_mapping()
    creator = _creator_from_mapping({"uid": uid, "unique_id": unique_id}, fallback_creator_id=creator_id, fallback_uid=uid)
    _append(result["creators"], creator)
    for row in _extract_rows(data, "list"):
        video = _video_from_mapping(row, creator=creator)
        _append(result["videos"], video)
        _append_media(result, _video_media(video, row))
        for product in _products_from_product_info(row.get("product_info")):
            _append(result["products"], product)
            _append(result["relations"]["video_products"], _video_product_relation(video, product, row))
            _append(result["relations"]["creator_products"], _creator_product_relation(creator, product.get("product_id"), row))
    return result


def map_fastmoss_author_bundle(
    bundle: Mapping[str, Any],
    *,
    source_product_id: str = "",
    source_key: str = "",
    target_record_id: str = "",
    table_url: str = "",
) -> FactMapping:
    result = _empty_mapping()
    base_info = _as_mapping(bundle.get("base_info"))
    author_index = _as_mapping(bundle.get("author_index"))
    cargo_summary = _as_mapping(bundle.get("cargo_summary"))
    author_contact = _as_mapping(bundle.get("author_contact"))
    uid = _first_non_empty(bundle.get("uid"), base_info.get("uid"))
    unique_id = _first_non_empty(bundle.get("unique_id"), base_info.get("unique_id"))
    creator = _creator_from_mapping(base_info, fallback_uid=uid, fallback_creator_id=unique_id)
    if creator:
        creator["facts"] = {
            "base_info": dict(base_info),
            "author_index": dict(author_index),
            "cargo_summary": dict(cargo_summary),
            "author_contact": dict(author_contact),
        }
    _append(result["creators"], creator)
    _append_media(result, _creator_media(creator, base_info))

    if source_product_id:
        _append(result["products"], {"product_id": source_product_id, "source_platform": "fastmoss"})
        relation = _creator_product_relation(creator, source_product_id, cargo_summary)
        if relation:
            relation["source_record_id"] = source_key
            relation["target_record_id"] = target_record_id
            relation["metadata"] = {"table_url": table_url}
        _append(result["relations"]["creator_products"], relation)

    shop_list = _as_mapping(bundle.get("shop_list"))
    for row in _extract_rows(shop_list, "list"):
        shop = _shop_from_mapping(row)
        _append(result["shops"], shop)
        _append(result["relations"]["shop_creators"], _shop_creator_relation(shop, creator, row))
        _append_media(result, _shop_media(shop, row))
    return result


def map_fastmoss_video_overview(
    payload: Mapping[str, Any],
    *,
    video_id: str = "",
) -> FactMapping:
    data = extract_fastmoss_data(payload)
    result = _empty_mapping()
    creator = _creator_from_mapping(data)
    video = _video_from_mapping(data, creator=creator, fallback_video_id=video_id)
    _append(result["creators"], creator)
    _append(result["videos"], video)
    _append_media(result, _creator_media(creator, data))
    _append_media(result, _video_media(video, data))
    return result


def map_fastmoss_video_goods(
    payload: Mapping[str, Any],
    *,
    video_id: str = "",
) -> FactMapping:
    data = extract_fastmoss_data(payload)
    result = _empty_mapping()
    video = _video_from_mapping(data, fallback_video_id=video_id)
    if video_id and not video:
        video = {"video_id": video_id, "source_platform": "fastmoss"}
    _append(result["videos"], video)
    for row in _extract_rows(data, "list", "product_list", "goods_list"):
        shop = _shop_from_mapping(row)
        product = _product_from_mapping(row, shop=shop)
        _append(result["products"], product)
        _append(result["shops"], shop)
        _append(result["relations"]["video_products"], _video_product_relation(video, product, row))
        _append(result["relations"]["product_shops"], _product_shop_relation(product, shop, row))
        _append_media(result, _product_media(product, row))
    return result


def map_fastmoss_shop_base(
    payload: Mapping[str, Any],
    *,
    seller_id: str = "",
) -> FactMapping:
    data = extract_fastmoss_data(payload)
    result = _empty_mapping()
    shop = _shop_from_mapping(data, fallback_shop_id=seller_id)
    _append(result["shops"], shop)
    _append_media(result, _shop_media(shop, data))
    return result


def map_fastmoss_shop_goods(
    payload: Mapping[str, Any],
    *,
    seller_id: str = "",
    shop_name: str = "",
) -> FactMapping:
    data = extract_fastmoss_data(payload)
    result = _empty_mapping()
    shop = _shop_from_mapping(data, fallback_shop_id=seller_id, fallback_shop_name=shop_name)
    _append(result["shops"], shop)
    for row in _extract_rows(data, "product_list", "list", "goods_list"):
        nested_shop = _as_mapping(row.get("shop_info") or row.get("shop"))
        row_shop = _shop_from_mapping({**shop, **nested_shop, **row}, fallback_shop_id=seller_id, fallback_shop_name=shop_name)
        product = _product_from_mapping(row, shop=row_shop)
        _append(result["products"], product)
        _append(result["shops"], row_shop)
        _append(result["relations"]["product_shops"], _product_shop_relation(product, row_shop, row))
        _append_media(result, _product_media(product, row))
    return result


def map_fastmoss_shop_author(
    payload: Mapping[str, Any],
    *,
    seller_id: str = "",
    shop_name: str = "",
) -> FactMapping:
    data = extract_fastmoss_data(payload)
    result = _empty_mapping()
    shop = _shop_from_mapping(data, fallback_shop_id=seller_id, fallback_shop_name=shop_name)
    _append(result["shops"], shop)
    for row in _extract_rows(data, "list", "author_list", "creator_list"):
        creator = _creator_from_mapping(row)
        _append(result["creators"], creator)
        _append(result["relations"]["shop_creators"], _shop_creator_relation(shop, creator, row))
        _append_media(result, _creator_media(creator, row))
    return result


def extract_fastmoss_data(payload: Mapping[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if isinstance(data, Mapping):
        return dict(data)
    return dict(payload)


def _creator_metric_mapping(
    data: Mapping[str, Any],
    *,
    uid: str,
    creator_id: str = "",
    unique_id: str = "",
    fact_key: str,
) -> FactMapping:
    result = _empty_mapping()
    creator = _creator_from_mapping(
        {**dict(data), "unique_id": _first_non_empty(data.get("unique_id"), unique_id)},
        fallback_uid=uid,
        fallback_creator_id=_first_non_empty(creator_id, unique_id),
    )
    if not creator and uid:
        creator = {"uid": uid, "source_platform": "fastmoss"}
    if creator:
        creator["facts"] = {fact_key: dict(data)}
    _append(result["creators"], creator)
    return result


def _product_from_mapping(
    row: Mapping[str, Any],
    *,
    fallback_product_id: str = "",
    shop: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    nested_product = _as_mapping(row.get("product") or row.get("product_info"))
    payload = {**nested_product, **dict(row)}
    shop_payload = _as_mapping(shop)
    product_id = _first_non_empty(
        payload.get("product_id"),
        payload.get("goods_id"),
        payload.get("id"),
        fallback_product_id,
    )
    if not product_id:
        return {}
    shop_name = _first_non_empty(
        payload.get("shop_name"),
        payload.get("seller_name"),
        shop_payload.get("shop_name"),
        shop_payload.get("name"),
    )
    seller_id = _first_non_empty(
        payload.get("seller_id"),
        payload.get("shop_id"),
        shop_payload.get("seller_id"),
        shop_payload.get("shop_id"),
        shop_payload.get("id"),
    )
    return {
        "product_id": product_id,
        "product_url": _first_non_empty(payload.get("detail_url"), payload.get("product_url"), payload.get("url")),
        "title": _first_non_empty(payload.get("title"), payload.get("product_title"), payload.get("name")),
        "seller_id": seller_id,
        "shop_id": seller_id,
        "shop_name": shop_name,
        "country_region": _first_non_empty(payload.get("region"), shop_payload.get("region")),
        "source_platform": "fastmoss",
        "facts": {"raw": dict(row)},
    }


def _shop_from_mapping(
    row: Mapping[str, Any],
    *,
    fallback_shop_id: str = "",
    fallback_shop_name: str = "",
) -> dict[str, Any]:
    id_value = _first_non_empty(row.get("id"))
    looks_like_product_row = bool(
        _first_non_empty(row.get("product_id"), row.get("goods_id"), row.get("title"), row.get("product_title"))
    )
    shop_id = _first_non_empty(
        row.get("seller_id"),
        row.get("shop_id"),
        "" if looks_like_product_row else id_value,
        fallback_shop_id,
    )
    shop_name = _first_non_empty(
        row.get("shop_name"),
        row.get("seller_name"),
        row.get("name"),
        fallback_shop_name,
    )
    if not (shop_id or shop_name):
        return {}
    return {
        "shop_id": shop_id,
        "seller_id": shop_id,
        "shop_name": shop_name,
        "shop_url": _first_non_empty(row.get("shop_url"), row.get("url")),
        "country_region": _first_non_empty(row.get("region"), row.get("country_region")),
        "source_platform": "fastmoss",
        "facts": {"raw": dict(row)},
    }


def _creator_from_mapping(
    row: Mapping[str, Any],
    *,
    fallback_creator_id: str = "",
    fallback_uid: str = "",
) -> dict[str, Any]:
    nested_author = _as_mapping(row.get("author") or row.get("creator"))
    payload = {**nested_author, **dict(row)}
    uid = _first_non_empty(payload.get("uid"), payload.get("author_uid"), fallback_uid)
    unique_id = _first_non_empty(
        payload.get("unique_id"),
        payload.get("author_unique_id"),
        payload.get("creator_id"),
        payload.get("influencer_id"),
        fallback_creator_id,
    )
    creator_id = _first_non_empty(fallback_creator_id, unique_id)
    if not (creator_id or uid or unique_id):
        return {}
    return {
        "creator_id": creator_id,
        "uid": uid,
        "unique_id": unique_id,
        "nickname": _first_non_empty(
            payload.get("nickname"),
            payload.get("author_nickname"),
            payload.get("author_name"),
            payload.get("name"),
        ),
        "profile_url": _profile_url(unique_id),
        "country_region": _first_non_empty(payload.get("region"), payload.get("country_region")),
        "source_platform": "fastmoss",
        "facts": {"raw": dict(row)},
    }


def _video_from_mapping(
    row: Mapping[str, Any],
    *,
    creator: Mapping[str, Any] | None = None,
    product_id: str = "",
    fallback_video_id: str = "",
) -> dict[str, Any]:
    nested_video = _as_mapping(row.get("video") or row.get("aweme"))
    payload = {**nested_video, **dict(row)}
    video_id = _first_non_empty(payload.get("video_id"), payload.get("aweme_id"), payload.get("id"), fallback_video_id)
    if not video_id:
        return {}
    creator_payload = _as_mapping(creator)
    unique_id = _first_non_empty(payload.get("unique_id"), payload.get("author_unique_id"), creator_payload.get("unique_id"))
    uid = _first_non_empty(payload.get("uid"), payload.get("author_uid"), creator_payload.get("uid"))
    creator_id = _first_non_empty(payload.get("creator_id"), creator_payload.get("creator_id"), unique_id)
    return {
        "video_id": video_id,
        "creator_id": creator_id,
        "uid": uid,
        "unique_id": unique_id,
        "product_id": _first_non_empty(payload.get("product_id"), product_id),
        "title": _first_non_empty(payload.get("video_desc"), payload.get("desc"), payload.get("title")),
        "video_url": _first_non_empty(payload.get("video_url"), payload.get("url")),
        "cover_url": _first_non_empty(payload.get("cover"), payload.get("cover_url")),
        "source_platform": "fastmoss",
        "facts": {"raw": dict(row)},
    }


def _products_from_product_info(product_info: Any) -> list[dict[str, Any]]:
    rows = _as_sequence(product_info)
    products: list[dict[str, Any]] = []
    for row in rows:
        product = _product_from_mapping(row)
        if product:
            products.append(product)
    return products


def _product_shop_relation(
    product: Mapping[str, Any],
    shop: Mapping[str, Any],
    metadata_source: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    product_id = _first_non_empty(product.get("product_id"))
    shop_id = _first_non_empty(shop.get("shop_id"), shop.get("seller_id"), product.get("shop_id"), product.get("seller_id"))
    shop_name = _first_non_empty(shop.get("shop_name"), product.get("shop_name"))
    if not product_id or not (shop_id or shop_name):
        return {}
    return {
        "product_id": product_id,
        "shop_id": shop_id,
        "seller_id": shop_id,
        "shop_name": shop_name,
        "metadata": {"raw": dict(metadata_source or {})},
    }


def _creator_product_relation(
    creator: Mapping[str, Any],
    product_id: Any,
    metadata_source: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_product_id = _first_non_empty(product_id)
    if not creator or not normalized_product_id:
        return {}
    return {
        "creator_key": _creator_key(creator),
        "creator_id": _first_non_empty(creator.get("creator_id"), creator.get("unique_id")),
        "uid": _first_non_empty(creator.get("uid")),
        "unique_id": _first_non_empty(creator.get("unique_id")),
        "product_id": normalized_product_id,
        "sold_count": _as_mapping(metadata_source or {}).get("sold_count") or 0,
        "metadata": {"raw": dict(metadata_source or {})},
    }


def _shop_creator_relation(
    shop: Mapping[str, Any],
    creator: Mapping[str, Any],
    metadata_source: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not shop or not creator:
        return {}
    shop_key = _shop_key(shop)
    creator_key = _creator_key(creator)
    if not shop_key or not creator_key:
        return {}
    return {
        "shop_key": shop_key,
        "shop_id": _first_non_empty(shop.get("shop_id"), shop.get("seller_id")),
        "shop_name": _first_non_empty(shop.get("shop_name")),
        "creator_key": creator_key,
        "creator_id": _first_non_empty(creator.get("creator_id"), creator.get("unique_id")),
        "uid": _first_non_empty(creator.get("uid")),
        "unique_id": _first_non_empty(creator.get("unique_id")),
        "metadata": {"raw": dict(metadata_source or {})},
    }


def _video_product_relation(
    video: Mapping[str, Any],
    product: Mapping[str, Any],
    metadata_source: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    video_id = _first_non_empty(video.get("video_id"))
    product_id = _first_non_empty(product.get("product_id"))
    if not video_id or not product_id:
        return {}
    return {
        "video_id": video_id,
        "product_id": product_id,
        "metadata": {"raw": dict(metadata_source or {})},
    }


def _product_media(product: Mapping[str, Any], row: Mapping[str, Any]) -> list[dict[str, Any]]:
    product_id = _first_non_empty(product.get("product_id"))
    image_urls = _image_urls_from_row(row)
    return [
        {
            "entity_type": "product",
            "entity_external_id": product_id,
            "media_role": "product_image",
            "source_url": image_url,
            "source_platform": "fastmoss",
            "metadata": {"product_id": product_id},
        }
        for image_url in image_urls
        if product_id and image_url
    ]


def _creator_media(creator: Mapping[str, Any], row: Mapping[str, Any]) -> list[dict[str, Any]]:
    creator_key = _creator_key(creator)
    avatar_url = _first_non_empty(
        row.get("avatar"),
        row.get("avatar_url"),
        row.get("author_avatar"),
        row.get("cover"),
    )
    if not creator_key or not avatar_url:
        return []
    return [
        {
            "entity_type": "creator",
            "entity_external_id": creator_key,
            "media_role": "creator_avatar",
            "source_url": avatar_url,
            "source_platform": "fastmoss",
        }
    ]


def _video_media(video: Mapping[str, Any], row: Mapping[str, Any]) -> list[dict[str, Any]]:
    video_id = _first_non_empty(video.get("video_id"))
    cover_url = _first_non_empty(row.get("cover"), row.get("cover_url"), video.get("cover_url"))
    if not video_id or not cover_url:
        return []
    return [
        {
            "entity_type": "video",
            "entity_external_id": f"video:{video_id}",
            "media_role": "video_cover",
            "source_url": cover_url,
            "source_platform": "fastmoss",
        }
    ]


def _shop_media(shop: Mapping[str, Any], row: Mapping[str, Any]) -> list[dict[str, Any]]:
    shop_key = _shop_key(shop)
    image_url = _first_non_empty(row.get("shop_avatar"), row.get("avatar"), row.get("img"), row.get("logo"))
    if not shop_key or not image_url:
        return []
    return [
        {
            "entity_type": "shop",
            "entity_external_id": shop_key,
            "media_role": "shop_image",
            "source_url": image_url,
            "source_platform": "fastmoss",
        }
    ]


def _append_media(result: FactMapping, media_items: Sequence[Mapping[str, Any]]) -> None:
    for media in media_items:
        _append(result["media_assets"], media)


def _extract_rows(mapping: Mapping[str, Any], *keys: str) -> list[dict[str, Any]]:
    for key in keys:
        rows = mapping.get(key)
        if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes, bytearray)):
            return [dict(row) for row in rows if isinstance(row, Mapping)]
    return []


def _as_sequence(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, Mapping):
        return [dict(value)]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [dict(item) for item in value if isinstance(item, Mapping)]
    return []


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _image_urls_from_row(row: Mapping[str, Any]) -> list[str]:
    urls: list[str] = []
    for key in (
        "img",
        "image",
        "image_url",
        "cover",
        "cover_url",
        "product_img",
        "product_image",
        "main_image",
    ):
        value = _first_non_empty(row.get(key))
        if value and value not in urls:
            urls.append(value)
    for key in ("images", "image_list", "imgs"):
        for item in _as_sequence(row.get(key)):
            value = _first_non_empty(item.get("url"), item.get("src"), item.get("image_url"))
            if value and value not in urls:
                urls.append(value)
    return urls


def _join_spec_values(value: Any) -> str:
    values: list[str] = []
    for item in _as_sequence(value):
        text = _first_non_empty(item.get("prop_value"), item.get("value"), item.get("name"))
        if text:
            values.append(text)
    return " / ".join(values)


def _creator_key(creator: Mapping[str, Any]) -> str:
    creator_id = _first_non_empty(creator.get("creator_id"))
    uid = _first_non_empty(creator.get("uid"))
    unique_id = _first_non_empty(creator.get("unique_id"))
    if creator_id:
        return f"creator_id:{creator_id}"
    if uid:
        return f"uid:{uid}"
    if unique_id:
        return f"unique_id:{unique_id}"
    return ""


def _shop_key(shop: Mapping[str, Any]) -> str:
    shop_id = _first_non_empty(shop.get("shop_id"), shop.get("seller_id"))
    shop_name = _first_non_empty(shop.get("shop_name"))
    if shop_id:
        return f"shop_id:{shop_id}"
    if shop_name:
        return f"shop_name:{shop_name}"
    return ""


def _profile_url(unique_id: str) -> str:
    if not unique_id:
        return ""
    return f"https://www.tiktok.com/@{unique_id}"


def _empty_mapping() -> FactMapping:
    return {
        "products": [],
        "product_skus": [],
        "shops": [],
        "creators": [],
        "videos": [],
        "media_assets": [],
        "relations": {
            "product_shops": [],
            "creator_products": [],
            "creator_videos": [],
            "video_products": [],
            "shop_creators": [],
        },
        "raw_entity_links": [],
    }


def _append(target: list[dict[str, Any]], value: Mapping[str, Any] | None) -> None:
    if isinstance(value, Mapping) and value:
        target.append(dict(value))


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""
