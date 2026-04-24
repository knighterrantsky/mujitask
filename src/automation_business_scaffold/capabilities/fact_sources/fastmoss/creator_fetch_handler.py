from __future__ import annotations

from automation_business_scaffold.contracts.handler.allowlist import API_HANDLER_CONTRACTS
from automation_business_scaffold.contracts.handler.contract import (
    HandlerContext,
    HandlerResult,
)
from automation_business_scaffold.contracts.handler.shared import (
    build_creator_key,
    build_error,
    bundle_entity_keys,
    coerce_bool,
    coerce_mapping,
    coerce_mapping_list,
    coerce_str,
    compact_dict,
    failed_result,
    first_non_empty,
    merge_fact_bundles,
    new_fact_bundle,
    skipped_result,
    success_result,
)
from automation_business_scaffold.infrastructure.fastmoss.fact_mappers import (
    map_fastmoss_author_bundle,
    map_fastmoss_author_goods_list,
    map_fastmoss_author_video_list,
)
from automation_business_scaffold.infrastructure.fastmoss.http_session import (
    FastMossAuthError,
    FastMossHTTPError,
    FastMossHTTPSession,
)
from collections.abc import (
    Mapping,
    Sequence,
)
from datetime import (
    datetime,
    timezone,
)
from typing import Any

HANDLER_CODE = "fastmoss_creator_fetch"
CONTRACT = API_HANDLER_CONTRACTS[HANDLER_CODE]


def fastmoss_creator_fetch_handler(context: HandlerContext) -> HandlerResult:
    payload = dict(context.payload)
    creator_identity = _normalize_creator_identity(payload)
    detail_level = first_non_empty(payload.get("detail_level"), "profile_metrics")
    required = coerce_bool(payload.get("required"), default=False)

    progress_callback = context.metadata.get("progress_callback")
    if callable(progress_callback):
        progress_callback("fastmoss_creator_fetch", message="fastmoss creator fetch started")

    try:
        raw_bundle = _resolve_fastmoss_creator_bundle(payload, creator_identity=creator_identity)
        if not raw_bundle:
            if required:
                error = build_error(
                    error_type="source_missing",
                    error_code="fastmoss_creator_payload_missing",
                    message="FastMoss creator payload or live session configuration was not provided.",
                    retryable=False,
                    details={"creator_identity": creator_identity},
                )
                return failed_result(
                    context,
                    error=error,
                    summary={"detail_level": detail_level, "creator_key": _creator_business_key(creator_identity)},
                )
            empty_bundle = new_fact_bundle()
            return skipped_result(
                context,
                summary={"detail_level": detail_level, "creator_key": _creator_business_key(creator_identity)},
                result={
                    "entities": _contract_entities_from_fact_bundle(empty_bundle),
                    "relations": [],
                    "observations": [],
                    "media_refs": [],
                    "raw_response_refs": [],
                    "creator_fact_bundle": {},
                    "product_relations": [],
                    "fact_bundle": empty_bundle,
                },
                warnings=("FastMoss creator payload or live session configuration was not provided.",),
            )

        raw_bundle = _normalize_fastmoss_creator_bundle(raw_bundle)
        fact_bundle = _build_fastmoss_creator_fact_bundle(
            raw_bundle,
            creator_identity=creator_identity,
            payload=payload,
        )
    except FastMossAuthError as exc:
        error = build_error(
            error_type="auth_failure",
            error_code="fastmoss_auth_required",
            message=str(exc),
            retryable=True,
            details=exc.to_dict(),
        )
        return failed_result(
            context,
            error=error,
            summary={"detail_level": detail_level, "creator_key": _creator_business_key(creator_identity)},
        )
    except FastMossHTTPError as exc:
        error = build_error(
            error_type="transport_failure",
            error_code="fastmoss_http_failure",
            message=str(exc),
            retryable=True,
            details=exc.to_dict(),
        )
        return failed_result(
            context,
            error=error,
            summary={"detail_level": detail_level, "creator_key": _creator_business_key(creator_identity)},
        )

    entities = _contract_entities_from_fact_bundle(fact_bundle)
    relations = _contract_relations_from_fact_bundle(fact_bundle)
    observations = _build_fastmoss_creator_observations(
        raw_bundle,
        fact_bundle=fact_bundle,
        payload=payload,
    )
    media_refs = _contract_media_refs_from_fact_bundle(fact_bundle)
    raw_response_refs = _raw_response_refs_from_fact_bundle(fact_bundle)
    creator_fact_bundle = _creator_compat_fact_bundle(
        fact_bundle,
        creator_identity=creator_identity,
        media_refs=media_refs,
    )
    product_relations = [
        relation for relation in relations if relation.get("relation_type") == "creator_promotes_product"
    ]
    quality = _creator_fetch_quality(raw_bundle, media_refs=media_refs)

    if callable(progress_callback):
        progress_callback("fastmoss_creator_mapped", message="fastmoss creator facts mapped")

    summary = {
        "detail_level": detail_level,
        "creator_key": first_non_empty(creator_fact_bundle.get("creator_key"), _creator_business_key(creator_identity)),
        "entity_count": len(bundle_entity_keys(fact_bundle)),
        "relation_count": len(relations),
        "observation_count": len(observations),
        "media_ref_count": len(media_refs),
    }
    result = {
        "entities": entities,
        "relations": relations,
        "observations": observations,
        "media_refs": media_refs,
        "raw_response_refs": raw_response_refs,
        "quality": quality,
        "creator_fact_bundle": creator_fact_bundle,
        "product_relations": product_relations,
        "fact_bundle": fact_bundle,
    }
    return success_result(context, summary=summary, result=result)


def _normalize_creator_identity(payload: dict[str, Any]) -> dict[str, Any]:
    creator_identity = coerce_mapping(payload.get("creator_identity"))
    source_context = coerce_mapping(payload.get("source_context"))
    creator_candidate = coerce_mapping(source_context.get("creator_candidate"))
    profile_url = first_non_empty(
        creator_identity.get("profile_url"),
        payload.get("profile_url"),
        creator_candidate.get("profile_url"),
        creator_candidate.get("author_url"),
    )
    uid = first_non_empty(
        creator_identity.get("uid"),
        payload.get("uid"),
        creator_candidate.get("uid"),
        creator_candidate.get("author_uid"),
        _extract_fastmoss_influencer_uid(profile_url),
    )
    unique_id = first_non_empty(
        creator_identity.get("unique_id"),
        payload.get("unique_id"),
        creator_candidate.get("unique_id"),
        creator_candidate.get("author_unique_id"),
    )
    creator_id = first_non_empty(
        creator_identity.get("creator_id"),
        payload.get("creator_id"),
        creator_candidate.get("creator_id"),
        creator_candidate.get("influencer_id"),
        unique_id,
        uid,
    )
    return compact_dict(
        {
            "creator_id": creator_id,
            "uid": uid,
            "unique_id": unique_id,
            "nickname": first_non_empty(
                creator_identity.get("nickname"),
                creator_identity.get("display_name"),
                payload.get("nickname"),
                creator_candidate.get("nickname"),
                creator_candidate.get("display_name"),
            ),
            "profile_url": profile_url,
        }
    )


def _creator_business_key(identity: Mapping[str, Any]) -> str:
    return build_creator_key(
        creator_id=first_non_empty(identity.get("creator_id")),
        uid=first_non_empty(identity.get("uid")),
        unique_id=first_non_empty(identity.get("unique_id")),
    )


def _extract_fastmoss_influencer_uid(profile_url: Any) -> str:
    text = coerce_str(profile_url)
    marker = "/influencer/detail/"
    if marker not in text:
        return ""
    return text.split(marker, 1)[-1].split("?", 1)[0].split("/", 1)[0]


def _resolve_fastmoss_creator_bundle(
    payload: dict[str, Any],
    *,
    creator_identity: dict[str, Any],
) -> dict[str, Any]:
    inline_bundle = _resolve_inline_fastmoss_creator_bundle(payload, creator_identity=creator_identity)
    if inline_bundle:
        return inline_bundle

    fastmoss_settings = coerce_mapping(payload.get("fastmoss"))
    live_fetch = coerce_bool(
        fastmoss_settings.get("live_fetch"),
        default=bool(fastmoss_settings and _creator_business_key(creator_identity)),
    )
    if not live_fetch:
        return {}

    session_policy = coerce_mapping(payload.get("session_policy"))
    session = FastMossHTTPSession(
        phone=first_non_empty(fastmoss_settings.get("phone")),
        password=first_non_empty(fastmoss_settings.get("password")),
        base_url=first_non_empty(fastmoss_settings.get("base_url"), "https://www.fastmoss.com"),
        default_region=first_non_empty(
            payload.get("region"),
            fastmoss_settings.get("region"),
            "US",
        ),
        timeout=float(fastmoss_settings.get("timeout", 30.0) or 30.0),
    )
    with session:
        cookies = fastmoss_settings.get("browser_cookies")
        if isinstance(cookies, list):
            session.replace_browser_cookies(cookies)
        require_login = coerce_bool(
            session_policy.get("require_login"),
            default=coerce_bool(
                fastmoss_settings.get("ensure_logged_in"),
                default=bool(cookies or fastmoss_settings.get("phone")),
            ),
        )
        if require_login:
            session.ensure_logged_in()

        creator_id = first_non_empty(creator_identity.get("creator_id"))
        uid_candidate = first_non_empty(creator_identity.get("uid"))
        if not uid_candidate and creator_id.isdigit():
            uid_candidate = creator_id
        uid = session.resolve_author_uid(
            uid=uid_candidate,
            unique_id=first_non_empty(creator_identity.get("unique_id")),
        )
        return _fetch_live_fastmoss_creator_bundle(session, uid=uid, payload=payload)


def _resolve_inline_fastmoss_creator_bundle(
    payload: dict[str, Any],
    *,
    creator_identity: dict[str, Any],
) -> dict[str, Any]:
    for key in (
        "fastmoss_creator_bundle",
        "creator_bundle",
        "author_bundle",
        "fastmoss_author_bundle",
        "mock_fastmoss_creator_bundle",
        "mock_author_bundle",
        "fastmoss_bundle",
        "mock_fastmoss_bundle",
    ):
        candidate = coerce_mapping(payload.get(key))
        if candidate:
            return candidate

    endpoint_bundle: dict[str, Any] = {}
    for key in (
        "base_info",
        "author_index",
        "stat_info",
        "cargo_summary",
        "author_contact",
        "shop_list",
        "goods_list",
        "video_list",
    ):
        candidate = coerce_mapping(payload.get(key))
        if candidate:
            endpoint_bundle[key] = candidate
    if endpoint_bundle:
        endpoint_bundle.setdefault("uid", first_non_empty(creator_identity.get("uid")))
        endpoint_bundle.setdefault("unique_id", first_non_empty(creator_identity.get("unique_id")))
    return endpoint_bundle


def _normalize_fastmoss_creator_bundle(raw_bundle: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(raw_bundle)
    for key in (
        "base_info",
        "author_index",
        "stat_info",
        "cargo_summary",
        "author_contact",
        "shop_list",
        "goods_list",
        "video_list",
    ):
        payload = coerce_mapping(raw_bundle.get(key))
        data = coerce_mapping(payload.get("data"))
        if data:
            normalized[key] = data
    return normalized


def _fetch_live_fastmoss_creator_bundle(
    session: FastMossHTTPSession,
    *,
    uid: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    endpoints = _creator_fetch_endpoints(payload)
    fetch_plan = coerce_mapping(payload.get("fetch_plan"))
    date_type = first_non_empty(fetch_plan.get("date_type"), fetch_plan.get("d_type"), 28)
    region = first_non_empty(payload.get("region"), session.default_region)
    bundle: dict[str, Any] = {"uid": uid}

    if "base_info" in endpoints:
        bundle["base_info"] = session.get_author_base_info(uid)
        bundle["unique_id"] = first_non_empty(bundle["base_info"].get("unique_id"), payload.get("unique_id"))
    if "author_index" in endpoints:
        bundle["author_index"] = session.get_author_index(uid)
    if "stat_info" in endpoints:
        bundle["stat_info"] = session.get_author_stat_info(uid)
    if "cargo_summary" in endpoints:
        bundle["cargo_summary"] = session.get_author_cargo_summary(uid)
    if "contact" in endpoints or "author_contact" in endpoints:
        bundle["author_contact"] = session.get_author_contact(uid)
    if "shop_list" in endpoints:
        shop_plan = coerce_mapping(fetch_plan.get("shop_list"))
        bundle["shop_list"] = session.get_author_shop_list(
            uid,
            page=_coerce_positive_int(shop_plan.get("page"), default=1),
            page_size=_coerce_positive_int(shop_plan.get("page_size"), default=5),
            region=region,
            order=first_non_empty(shop_plan.get("order"), "sold_count,2"),
        )
    if "goods_list" in endpoints:
        goods_plan = coerce_mapping(fetch_plan.get("goods_list"))
        bundle["goods_list"] = session.list_author_goods(
            uid,
            page=_coerce_positive_int(goods_plan.get("page"), default=1),
            page_size=_coerce_positive_int(goods_plan.get("page_size"), default=5),
            region=region,
            order=first_non_empty(goods_plan.get("order"), "sold_count,2"),
            date_type=first_non_empty(goods_plan.get("date_type"), date_type),
        )
    if "video_list" in endpoints:
        video_plan = coerce_mapping(fetch_plan.get("video_list"))
        bundle["video_list"] = session.get_author_video_list(
            uid,
            page=_coerce_positive_int(video_plan.get("page"), default=1),
            page_size=_coerce_positive_int(video_plan.get("page_size"), default=5),
            region=region,
            order=first_non_empty(video_plan.get("order"), "sold_count,2"),
            date_type=first_non_empty(video_plan.get("date_type"), date_type),
        )
    bundle["session_snapshot"] = session.cookie_snapshot()
    return bundle


def _creator_fetch_endpoints(payload: dict[str, Any]) -> set[str]:
    fetch_plan = coerce_mapping(payload.get("fetch_plan"))
    endpoints = {
        coerce_str(item)
        for item in _coerce_sequence(fetch_plan.get("endpoints"))
        if coerce_str(item)
    }
    if endpoints:
        return endpoints

    detail_level = first_non_empty(payload.get("detail_level"), "default").lower()
    endpoints = {"base_info", "author_index", "cargo_summary", "contact", "shop_list"}
    if "stat" in detail_level:
        endpoints.add("stat_info")
    if "goods" in detail_level or "product" in detail_level:
        endpoints.add("goods_list")
    if "video" in detail_level:
        endpoints.add("video_list")
    return endpoints


def _build_fastmoss_creator_fact_bundle(
    raw_bundle: dict[str, Any],
    *,
    creator_identity: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    source_context = coerce_mapping(payload.get("source_context"))
    relation_policy = coerce_mapping(payload.get("relation_policy"))
    uid = first_non_empty(raw_bundle.get("uid"), creator_identity.get("uid"), creator_identity.get("creator_id"))
    unique_id = first_non_empty(raw_bundle.get("unique_id"), creator_identity.get("unique_id"))
    creator_id = first_non_empty(creator_identity.get("creator_id"), unique_id, uid)
    source_product_id = _source_product_id_for_creator_relation(source_context, relation_policy)

    fact_bundle = merge_fact_bundles(
        new_fact_bundle(),
        map_fastmoss_author_bundle(
            raw_bundle,
            source_product_id=source_product_id,
            source_key=first_non_empty(source_context.get("source_record_id")),
            target_record_id=first_non_empty(source_context.get("target_record_id")),
            table_url=first_non_empty(source_context.get("source_table_ref"), source_context.get("table_url")),
        ),
    )
    video_list = coerce_mapping(raw_bundle.get("video_list"))
    if video_list:
        fact_bundle = merge_fact_bundles(
            fact_bundle,
            map_fastmoss_author_video_list(
                video_list,
                uid=uid,
                creator_id=creator_id,
                unique_id=unique_id,
            ),
        )
    goods_list = coerce_mapping(raw_bundle.get("goods_list"))
    if goods_list:
        fact_bundle = merge_fact_bundles(
            fact_bundle,
            map_fastmoss_author_goods_list(
                goods_list,
                uid=uid,
                creator_id=creator_id,
                unique_id=unique_id,
            ),
        )

    stat_info = coerce_mapping(raw_bundle.get("stat_info"))
    if stat_info:
        creator_items = fact_bundle.get("creators")
        if isinstance(creator_items, list):
            for creator in creator_items:
                if not isinstance(creator, dict):
                    continue
                facts = coerce_mapping(creator.get("facts"))
                facts["stat_info"] = stat_info
                creator["facts"] = facts

    _enrich_creator_source_product_relation(
        fact_bundle,
        source_context=source_context,
        source_product_id=source_product_id,
    )
    _append_fastmoss_creator_raw_responses(
        fact_bundle,
        raw_bundle,
        uid=uid,
        unique_id=unique_id,
    )
    return merge_fact_bundles(fact_bundle)


def _enrich_creator_source_product_relation(
    fact_bundle: dict[str, Any],
    *,
    source_context: Mapping[str, Any],
    source_product_id: str,
) -> None:
    if not source_product_id:
        return
    relations = coerce_mapping(fact_bundle.get("relations"))
    creator_products = relations.get("creator_products")
    if not isinstance(creator_products, list):
        return
    matched_sold_count = _metric_number(source_context.get("matched_product_sold_count"))
    for relation in creator_products:
        if not isinstance(relation, dict):
            continue
        if first_non_empty(relation.get("product_id")) != source_product_id:
            continue
        relation["source_record_id"] = first_non_empty(relation.get("source_record_id"), source_context.get("source_record_id"))
        relation["holiday_name"] = first_non_empty(relation.get("holiday_name"), source_context.get("holiday"))
        if matched_sold_count is not None and _metric_number(relation.get("sold_count")) in (None, 0):
            relation["sold_count"] = matched_sold_count
        metadata = coerce_mapping(relation.get("metadata"))
        raw = coerce_mapping(metadata.get("raw"))
        raw.update(
            compact_dict(
                {
                    "source_record_id": source_context.get("source_record_id"),
                    "holiday": source_context.get("holiday"),
                    "matched_product_sold_count": source_context.get("matched_product_sold_count"),
                }
            )
        )
        metadata["raw"] = raw
        relation["metadata"] = metadata


def _source_product_id_for_creator_relation(
    source_context: dict[str, Any],
    relation_policy: dict[str, Any],
) -> str:
    product_id = first_non_empty(source_context.get("product_id"), source_context.get("fastmoss_product_id"))
    include_relation = coerce_bool(
        relation_policy.get("include_source_product_relation"),
        default=bool(product_id),
    )
    if not include_relation:
        return ""
    minimum = _metric_number(relation_policy.get("min_source_product_sold_count"))
    observed = _metric_number(source_context.get("matched_product_sold_count"))
    if minimum is not None and observed is not None and observed < minimum:
        return ""
    return product_id


def _append_fastmoss_creator_raw_responses(
    fact_bundle: dict[str, Any],
    raw_bundle: dict[str, Any],
    *,
    uid: str,
    unique_id: str,
) -> None:
    endpoint_by_key = {
        "base_info": "author.base_info",
        "author_index": "author.index",
        "stat_info": "author.stat_info",
        "cargo_summary": "author.cargo_summary",
        "author_contact": "author.contact",
        "shop_list": "author.shop_list",
        "goods_list": "author.goods_list",
        "video_list": "author.video_list",
    }
    for key, endpoint in endpoint_by_key.items():
        response_payload = coerce_mapping(raw_bundle.get(key))
        if not response_payload:
            continue
        fact_bundle["raw_api_responses"].append(
            {
                "source_platform": "fastmoss",
                "source_endpoint": endpoint,
                "request_url": "",
                "request_params": compact_dict({"uid": uid, "unique_id": unique_id}),
                "response_payload": response_payload,
                "status_code": 200,
            }
        )


def _contract_entities_from_fact_bundle(fact_bundle: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {
        "creators": [
            _contract_creator_entity(creator)
            for creator in coerce_mapping_list(fact_bundle.get("creators"))
        ],
        "products": [
            _contract_product_entity(product)
            for product in coerce_mapping_list(fact_bundle.get("products"))
        ],
        "shops": [
            _contract_shop_entity(shop)
            for shop in coerce_mapping_list(fact_bundle.get("shops"))
        ],
        "videos": [
            _contract_video_entity(video)
            for video in coerce_mapping_list(fact_bundle.get("videos"))
        ],
    }


def _contract_creator_entity(creator: dict[str, Any]) -> dict[str, Any]:
    facts = coerce_mapping(creator.get("facts"))
    base_info = coerce_mapping(facts.get("base_info"))
    author_index = coerce_mapping(facts.get("author_index"))
    stat_info = coerce_mapping(facts.get("stat_info"))
    cargo_summary = coerce_mapping(facts.get("cargo_summary"))
    author_contact = coerce_mapping(facts.get("author_contact"))
    metrics = _metric_fields(base_info, author_index, stat_info, cargo_summary)
    return compact_dict(
        {
            "entity_key": _contract_entity_key("creator", _creator_entity_ref(creator)),
            "creator_id": first_non_empty(creator.get("creator_id"), creator.get("unique_id")),
            "uid": creator.get("uid"),
            "unique_id": creator.get("unique_id"),
            "nickname": creator.get("nickname"),
            "avatar_url": first_non_empty(base_info.get("avatar"), base_info.get("avatar_url")),
            "region": first_non_empty(creator.get("country_region"), base_info.get("region")),
            "profile_url": creator.get("profile_url"),
            "metrics": metrics,
            "contact": _contract_contact(author_contact),
            "source_platform": creator.get("source_platform"),
        }
    )


def _contract_product_entity(product: dict[str, Any]) -> dict[str, Any]:
    product_id = first_non_empty(product.get("product_id"))
    return compact_dict(
        {
            "entity_key": _contract_entity_key("product", product_id),
            "product_id": product_id,
            "title": product.get("title"),
            "image_url": first_non_empty(product.get("image_url"), product.get("cover_url")),
            "source_platform": product.get("source_platform"),
        }
    )


def _contract_shop_entity(shop: dict[str, Any]) -> dict[str, Any]:
    shop_ref = _shop_entity_ref(shop)
    return compact_dict(
        {
            "entity_key": _contract_entity_key("shop", shop_ref),
            "seller_id": first_non_empty(shop.get("seller_id"), shop.get("shop_id")),
            "shop_id": first_non_empty(shop.get("shop_id"), shop.get("seller_id")),
            "shop_name": shop.get("shop_name"),
            "source_platform": shop.get("source_platform"),
        }
    )


def _contract_video_entity(video: dict[str, Any]) -> dict[str, Any]:
    video_id = first_non_empty(video.get("video_id"))
    return compact_dict(
        {
            "entity_key": _contract_entity_key("video", video_id),
            "video_id": video_id,
            "title": video.get("title"),
            "cover_url": video.get("cover_url"),
            "video_url": video.get("video_url"),
            "source_platform": video.get("source_platform"),
        }
    )


def _contract_relations_from_fact_bundle(fact_bundle: dict[str, Any]) -> list[dict[str, Any]]:
    relations = coerce_mapping(fact_bundle.get("relations"))
    result: list[dict[str, Any]] = []
    for relation in coerce_mapping_list(relations.get("creator_products")):
        result.append(_contract_creator_product_relation(relation))
    for relation in coerce_mapping_list(relations.get("creator_videos")):
        result.append(_contract_creator_video_relation(relation))
    for relation in coerce_mapping_list(relations.get("video_products")):
        result.append(_contract_video_product_relation(relation))
    for relation in coerce_mapping_list(relations.get("shop_creators")):
        result.append(_contract_shop_creator_relation(relation))
    for relation in coerce_mapping_list(relations.get("product_shops")):
        result.append(_contract_product_shop_relation(relation))
    return [relation for relation in result if relation]


def _contract_creator_product_relation(relation: dict[str, Any]) -> dict[str, Any]:
    raw = _relation_raw(relation)
    creator_ref = _creator_ref_from_relation(relation)
    product_id = first_non_empty(relation.get("product_id"))
    return compact_dict(
        {
            "relation_key": f"creator_product:{creator_ref}:{product_id}",
            "relation_type": "creator_promotes_product",
            "from_entity_key": _contract_entity_key("creator", creator_ref),
            "to_entity_key": _contract_entity_key("product", product_id),
            "source": first_non_empty(relation.get("source_platform"), "fastmoss"),
            "metrics": _relation_metric_fields(raw, relation),
            "source_context": _relation_source_context(relation, raw),
        }
    )


def _contract_creator_video_relation(relation: dict[str, Any]) -> dict[str, Any]:
    creator_ref = _creator_ref_from_relation(relation)
    video_id = first_non_empty(relation.get("video_id"), _strip_key_prefix(relation.get("video_key")))
    return compact_dict(
        {
            "relation_key": f"creator_video:{creator_ref}:{video_id}",
            "relation_type": "creator_published_video",
            "from_entity_key": _contract_entity_key("creator", creator_ref),
            "to_entity_key": _contract_entity_key("video", video_id),
            "source": first_non_empty(relation.get("source_platform"), "fastmoss"),
            "source_context": _relation_source_context(relation, _relation_raw(relation)),
        }
    )


def _contract_video_product_relation(relation: dict[str, Any]) -> dict[str, Any]:
    video_id = first_non_empty(relation.get("video_id"), _strip_key_prefix(relation.get("video_key")))
    product_id = first_non_empty(relation.get("product_id"))
    return compact_dict(
        {
            "relation_key": f"video_product:{video_id}:{product_id}",
            "relation_type": "video_mounts_product",
            "from_entity_key": _contract_entity_key("video", video_id),
            "to_entity_key": _contract_entity_key("product", product_id),
            "source": first_non_empty(relation.get("source_platform"), "fastmoss"),
            "source_context": _relation_source_context(relation, _relation_raw(relation)),
        }
    )


def _contract_shop_creator_relation(relation: dict[str, Any]) -> dict[str, Any]:
    shop_ref = _shop_ref_from_relation(relation)
    creator_ref = _creator_ref_from_relation(relation)
    return compact_dict(
        {
            "relation_key": f"shop_creator:{shop_ref}:{creator_ref}",
            "relation_type": "shop_collaborates_with_creator",
            "from_entity_key": _contract_entity_key("shop", shop_ref),
            "to_entity_key": _contract_entity_key("creator", creator_ref),
            "source": first_non_empty(relation.get("source_platform"), "fastmoss"),
            "source_context": _relation_source_context(relation, _relation_raw(relation)),
        }
    )


def _contract_product_shop_relation(relation: dict[str, Any]) -> dict[str, Any]:
    product_id = first_non_empty(relation.get("product_id"))
    shop_ref = _shop_ref_from_relation(relation)
    return compact_dict(
        {
            "relation_key": f"product_shop:{product_id}:{shop_ref}",
            "relation_type": "product_sold_by_shop",
            "from_entity_key": _contract_entity_key("product", product_id),
            "to_entity_key": _contract_entity_key("shop", shop_ref),
            "source": first_non_empty(relation.get("source_platform"), "fastmoss"),
            "source_context": _relation_source_context(relation, _relation_raw(relation)),
        }
    )


def _contract_media_refs_from_fact_bundle(fact_bundle: dict[str, Any]) -> list[dict[str, Any]]:
    media_refs: list[dict[str, Any]] = []
    for asset in coerce_mapping_list(fact_bundle.get("media_assets")):
        entity_type = first_non_empty(asset.get("entity_type"))
        entity_ref = _strip_key_prefix(asset.get("entity_external_id"))
        media_refs.append(
            compact_dict(
                {
                    "entity_key": _contract_entity_key(entity_type, entity_ref),
                    "media_type": first_non_empty(asset.get("media_role"), "media"),
                    "source_url": asset.get("source_url"),
                    "source_platform": asset.get("source_platform"),
                }
            )
        )
    return media_refs


def _raw_response_refs_from_fact_bundle(fact_bundle: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    for raw_response in coerce_mapping_list(fact_bundle.get("raw_api_responses")):
        endpoint = first_non_empty(raw_response.get("source_endpoint"))
        request_params = coerce_mapping(raw_response.get("request_params"))
        uid = first_non_empty(request_params.get("uid"), request_params.get("unique_id"), "unknown")
        if endpoint:
            refs.append(f"fastmoss://creator/{uid}/{endpoint}")
    return refs


def _build_fastmoss_creator_observations(
    raw_bundle: dict[str, Any],
    *,
    fact_bundle: dict[str, Any],
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    creators = coerce_mapping_list(fact_bundle.get("creators"))
    creator_ref = _creator_entity_ref(creators[0]) if creators else _creator_business_key(
        _normalize_creator_identity(payload)
    )
    entity_key = _contract_entity_key("creator", _strip_key_prefix(creator_ref))
    fetch_plan = coerce_mapping(payload.get("fetch_plan"))
    window_days = _coerce_positive_int(fetch_plan.get("date_type"), default=0)
    observed_at = first_non_empty(payload.get("observed_at"), _utc_now_iso())
    observations: list[dict[str, Any]] = []
    endpoint_by_key = {
        "base_info": "author.base_info",
        "author_index": "author.index",
        "stat_info": "author.stat_info",
        "cargo_summary": "author.cargo_summary",
    }
    for bundle_key, endpoint in endpoint_by_key.items():
        for metric_name, metric_value in _iter_metric_values(coerce_mapping(raw_bundle.get(bundle_key))):
            observations.append(
                compact_dict(
                    {
                        "entity_key": entity_key,
                        "metric_name": metric_name,
                        "metric_value": metric_value,
                        "window_days": window_days if bundle_key != "base_info" else 0,
                        "observed_at": observed_at,
                        "source": "fastmoss",
                        "source_endpoint": endpoint,
                    }
                )
            )
    return observations


def _creator_compat_fact_bundle(
    fact_bundle: dict[str, Any],
    *,
    creator_identity: dict[str, Any],
    media_refs: list[dict[str, Any]],
) -> dict[str, Any]:
    creators = coerce_mapping_list(fact_bundle.get("creators"))
    creator = dict(creators[0]) if creators else dict(creator_identity)
    contract_creator = _contract_creator_entity(creator) if creators else {}
    creator_id = first_non_empty(
        creator.get("creator_id"),
        creator_identity.get("creator_id"),
        creator.get("unique_id"),
        creator.get("uid"),
    )
    nickname = first_non_empty(creator.get("nickname"), creator_identity.get("nickname"))
    avatar_url = first_non_empty(
        contract_creator.get("avatar_url"),
        _first_media_ref_url(media_refs, entity_type="creator"),
    )
    return compact_dict(
        {
            "entity_key": first_non_empty(
                contract_creator.get("entity_key"),
                _contract_entity_key("creator", creator_id),
            ),
            "creator_key": build_creator_key(
                creator_id=creator_id,
                uid=first_non_empty(creator.get("uid"), creator_identity.get("uid")),
                unique_id=first_non_empty(creator.get("unique_id"), creator_identity.get("unique_id")),
            ),
            "creator_id": creator_id,
            "uid": first_non_empty(creator.get("uid"), creator_identity.get("uid")),
            "unique_id": first_non_empty(creator.get("unique_id"), creator_identity.get("unique_id")),
            "nickname": nickname,
            "display_name": nickname,
            "profile_url": first_non_empty(creator.get("profile_url"), creator_identity.get("profile_url")),
            "avatar_url": avatar_url,
            "metrics": coerce_mapping(contract_creator.get("metrics")),
            "contact": coerce_mapping(contract_creator.get("contact")),
            "facts": coerce_mapping(creator.get("facts")),
        }
    )


def _creator_fetch_quality(
    raw_bundle: dict[str, Any],
    *,
    media_refs: list[dict[str, Any]],
) -> dict[str, Any]:
    contact = _contract_contact(coerce_mapping(raw_bundle.get("author_contact")))
    missing_optional_fields: list[str] = []
    if not contact.get("available"):
        missing_optional_fields.append("contact.normalized_text")
    if not _first_media_ref_url(media_refs, entity_type="creator"):
        missing_optional_fields.append("creator.avatar_url")
    return {
        "contact_available": bool(contact.get("available")),
        "degraded_preview": _has_fastmoss_auth_preview(raw_bundle),
        "missing_optional_fields": missing_optional_fields,
    }


def _contract_contact(payload: dict[str, Any]) -> dict[str, Any]:
    contact_text = first_non_empty(
        payload.get("email"),
        payload.get("mail"),
        payload.get("whatsapp"),
        payload.get("phone"),
        payload.get("contact"),
        payload.get("contact_info"),
        payload.get("raw"),
    )
    return compact_dict(
        {
            "raw": contact_text,
            "normalized_text": contact_text,
            "available": bool(contact_text),
        }
    )


def _metric_fields(*payloads: Mapping[str, Any]) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for payload in payloads:
        for metric_name, metric_value in _iter_metric_values(payload):
            metrics[metric_name] = metric_value
    return metrics


def _relation_metric_fields(*payloads: Mapping[str, Any]) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for field_name in ("sold_count", "sale_amount", "commission_rate"):
        for payload in payloads:
            metric_value = _metric_number(payload.get(field_name))
            if metric_value is not None:
                metrics[field_name] = metric_value
                break
    return metrics


def _iter_metric_values(payload: Mapping[str, Any], *, prefix: str = "") -> list[tuple[str, Any]]:
    metrics: list[tuple[str, Any]] = []
    ignored_keys = {
        "id",
        "uid",
        "unique_id",
        "creator_id",
        "product_id",
        "video_id",
        "shop_id",
        "seller_id",
        "nickname",
        "name",
        "avatar",
        "avatar_url",
        "region",
        "country_region",
        "update_at",
    }
    for key, value in payload.items():
        key_text = coerce_str(key)
        if not key_text or key_text in ignored_keys:
            continue
        metric_name = f"{prefix}.{key_text}" if prefix else key_text
        if isinstance(value, Mapping):
            metrics.extend(_iter_metric_values(value, prefix=metric_name))
            continue
        metric_value = _metric_number(value)
        if metric_value is not None:
            metrics.append((metric_name, metric_value))
    return metrics


def _metric_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    text = coerce_str(value)
    if not text:
        return None
    normalized = text.replace(",", "").replace("$", "").replace("%", "")
    try:
        parsed = float(normalized)
    except ValueError:
        return None
    return int(parsed) if parsed.is_integer() else parsed


def _relation_raw(relation: dict[str, Any]) -> dict[str, Any]:
    return coerce_mapping(coerce_mapping(relation.get("metadata")).get("raw"))


def _relation_source_context(relation: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    return compact_dict(
        {
            "source_record_id": first_non_empty(relation.get("source_record_id"), raw.get("source_record_id")),
            "target_record_id": relation.get("target_record_id"),
            "holiday": first_non_empty(relation.get("holiday_name"), raw.get("holiday")),
            "table_url": coerce_mapping(relation.get("metadata")).get("table_url"),
        }
    )


def _creator_entity_ref(creator: Mapping[str, Any]) -> str:
    return _strip_key_prefix(
        first_non_empty(
            creator.get("creator_key"),
            creator.get("creator_id"),
            creator.get("uid"),
            creator.get("unique_id"),
        )
    )


def _shop_entity_ref(shop: Mapping[str, Any]) -> str:
    return _strip_key_prefix(
        first_non_empty(
            shop.get("shop_key"),
            shop.get("shop_id"),
            shop.get("seller_id"),
            shop.get("shop_name"),
        )
    )


def _creator_ref_from_relation(relation: Mapping[str, Any]) -> str:
    return _strip_key_prefix(
        first_non_empty(
            relation.get("creator_key"),
            relation.get("creator_id"),
            relation.get("uid"),
            relation.get("unique_id"),
        )
    )


def _shop_ref_from_relation(relation: Mapping[str, Any]) -> str:
    return _strip_key_prefix(
        first_non_empty(
            relation.get("shop_key"),
            relation.get("shop_id"),
            relation.get("seller_id"),
            relation.get("shop_name"),
        )
    )


def _contract_entity_key(entity_type: str, ref: Any) -> str:
    normalized_type = coerce_str(entity_type)
    normalized_ref = _strip_key_prefix(ref)
    if not normalized_type or not normalized_ref:
        return ""
    return f"fastmoss_{normalized_type}:{normalized_ref}"


def _strip_key_prefix(value: Any) -> str:
    text = first_non_empty(value)
    if ":" in text:
        return text.split(":", 1)[1]
    return text


def _first_media_ref_url(media_refs: list[dict[str, Any]], *, entity_type: str) -> str:
    prefix = f"fastmoss_{entity_type}:"
    for media_ref in media_refs:
        if coerce_str(media_ref.get("entity_key")).startswith(prefix):
            return first_non_empty(media_ref.get("source_url"))
    return ""


def _has_fastmoss_auth_preview(raw_bundle: dict[str, Any]) -> bool:
    for value in raw_bundle.values():
        payload = coerce_mapping(value)
        code = coerce_str(payload.get("code"))
        if code.startswith("MAG_AUTH_"):
            return True
    return False


def _coerce_sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return []


def _coerce_positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = ["CONTRACT", "HANDLER_CODE", "fastmoss_creator_fetch_handler"]
