from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_yaml(relative_path: str) -> dict[str, Any]:
    path = REPO_ROOT / relative_path
    assert path.is_file(), f"missing contract: {relative_path}"
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), f"{relative_path} must be a YAML mapping"
    return loaded


def _read(relative_path: str) -> str:
    path = REPO_ROOT / relative_path
    assert path.is_file(), f"missing document: {relative_path}"
    return path.read_text(encoding="utf-8")


def test_formal_requirement_and_domain_route_are_declared() -> None:
    requirement = _read("docs/business/requirements/amazon-product-detail-collection.md")
    domain_route = _read("docs/domains/amazon-product-detail/README.md")

    required_requirement_tokens = (
        "状态: 已批准，实施中",
        "refresh_amazon_product_row_by_asin",
        "美国站",
        "^[A-Z0-9]{10}$",
        "同一飞书 record_id",
        "refresh_current_amazon_product_table",
        "采集标签=T",
        "30天购买人数=500+",
    )
    assert all(token in requirement for token in required_requirement_tokens)

    required_route_refs = (
        "../../business/requirements/amazon-product-detail-collection.md",
        "../../arch/workflow-amazon-product-detail-design.md",
        "../../../contracts/fields/feishu-amazon-products.yaml",
        "../../../contracts/states/amazon-product-collection-status.yaml",
        "../../../contracts/workflow/refresh_amazon_product_row_by_asin.yaml",
        "../../../contracts/workflow/refresh_current_amazon_product_table.yaml",
    )
    assert all(reference in domain_route for reference in required_route_refs)


def test_workflow_contract_freezes_codes_stages_and_handlers() -> None:
    contract = _load_yaml("contracts/workflow/refresh_amazon_product_row_by_asin.yaml")

    assert contract["task_code"] == "refresh_amazon_product_row_by_asin"
    assert contract["workflow_code"] == "refresh_amazon_product_row_by_asin"
    assert [stage["stage_code"] for stage in contract["stages"]] == [
        "read_amazon_product_row",
        "collect_amazon_product_detail",
        "persist_amazon_product_detail",
        "ready_for_summary",
    ]
    assert set(contract["new_handlers"]) == {
        "amazon_product_browser_fetch",
        "amazon_product_row_persist",
        "amazon_product_fact_upsert",
    }
    assert contract["identity"] == {
        "marketplace_code": "US",
        "domain": "amazon.com",
        "asin_normalization": "trim_uppercase",
        "asin_pattern": "^[A-Z0-9]{10}$",
        "canonical_url_template": "https://www.amazon.com/dp/{asin}",
    }
    assert contract["persistence"] == {
        "requires_fact_db": True,
        "requires_object_storage": True,
        "object_storage_policy": {
            "mode": "default_deny_explicit_allowlist",
            "success_required": ["normalized_capture"],
            "success_allowed": ["normalized_capture", "amazon_product_media"],
            "blocked_required": ["blocked_screenshot"],
            "blocked_allowed": ["blocked_screenshot"],
            "forbidden_new_objects": [
                "html",
                "network_data",
                "page_data",
                "success_screenshot",
                "runtime_artifact",
                "log",
                "temporary_file",
            ],
            "durable_reference_required": [
                "bucket",
                "object_key",
                "content_digest",
            ],
            "local_path_as_durable_locator": "forbidden",
            "downstream_read": "minio_only",
        },
        "runtime_result_policy": "compact_references_only",
        "persist_result_identity": {
            "source_record_id": "exact_source_row",
            "requested_asin": "exact_normalized_source_asin",
            "resolved_asin": "exact_validated_browser_resolved_asin",
            "run_id": "exact_stable_collection_run",
            "mismatch": "fail_before_summary",
        },
        "runtime_result_nested_allowlists": {
            "fact_refs": [
                "product_id",
                "snapshot_id",
                "binding_id",
                "raw_capture_ids",
                "normalized_capture_ref",
            ],
            "media_coverage": ["expected", "materialized", "missing", "complete"],
            "writeback": [
                "written_count",
                "skipped_count",
                "failed_count",
                "target_record_ids",
            ],
            "artifact_ref": [
                "capture_kind",
                "bucket",
                "object_key",
                "content_digest",
                "content_type",
                "sanitization_status",
                "request_id",
                "execution_id",
                "run_id",
                "collected_at",
                "created_at",
            ],
            "media_source_ref": [
                "source_url",
                "source_platform",
                "marketplace_code",
                "product_id",
                "media_role",
                "position",
            ],
        },
        "runtime_result_nested_value_constraints": {
            "fact_ids": "lowercase_uuid_hex_32",
            "raw_capture_ids": "list_of_lowercase_uuid_hex_32",
            "capture_ref": "governed_amazon_raw_capture_coordinate_with_bound_provenance",
            "capture_ref_request_and_execution_ids": "lowercase_uuid_hex_32",
            "capture_ref_run_id": "lowercase_sha256_hex_64",
            "media_source_ref": (
                "amazon_us_https_cdn_url_without_query_or_fragment_or_sensitive_path"
            ),
            "media_source_role_position": "unique_or_reject_before_persist_dispatch",
            "same_source_url_distinct_role_position": "preserve_each_relation",
            "writeback": "raw_exact_1_0_0_and_single_source_record_before_compaction",
        },
        "runtime_context": {
            "location": "task_request.stage_cursor.runtime_context",
            "allowed": [
                "browser_target_digest",
                "browser_resource_code",
                "artifact_bucket",
                "artifact_object_prefix",
            ],
            "value_constraints": {
                "browser_target_digest": "lowercase_sha256_hex_64",
                "browser_resource_code": "browser:amazon:{browser_target_digest}",
            },
            "artifact_coordinate_snapshot": "immutable_submit_time_non_secret_policy",
            "missing_artifact_coordinate_snapshot": ("fail_closed_for_legacy_inflight_request"),
            "forbidden": [
                "browser_profile_ref",
                "browser_provider_token",
                "browser_workspace_id",
                "browser_profile_id",
            ],
        },
    }
    assert contract["payload"]["business_inputs_only"] is False
    assert contract["payload"]["required"] == ["table_ref", "source_record_id", "table_refs"]
    assert contract["payload"]["allowed"] == ["table_ref", "source_record_id", "table_refs"]
    assert contract["payload"]["business_input_fields"] == ["table_ref", "source_record_id"]
    assert contract["payload"]["configuration_snapshot"] == {
        "field": "table_refs",
        "source": "amazon_skill_local_env",
        "required": True,
        "secret_free": True,
        "allowed_keys": ["AMAZON_PRODUCTS"],
        "precedence": "task_payload_only",
    }
    assert contract["payload"]["value_constraints"] == {"table_ref": {"const": "AMAZON_PRODUCTS"}}
    assert contract["payload"]["additional_properties"] is False
    top_level_summary = contract["observability"]["top_level_summary"]
    assert top_level_summary["required_fields"] == [
        "final_status",
        "row_total_count",
        "row_status_counts",
        "aggregate_metrics",
        "row_summary",
        "failed_stage",
        "error_code",
    ]
    assert top_level_summary["row_status_count_keys"] == [
        "success",
        "partial_success",
        "unavailable",
        "blocked",
        "failed",
        "skipped",
    ]
    assert top_level_summary["row_status_count_policy"] == "single_row_one_hot_zero_or_one"
    assert top_level_summary["aggregate_metrics"]["required"] == [
        "average_row_duration_ms",
        "max_row_duration_ms",
        "blocked_rate",
        "average_parse_coverage_percentage",
        "media_failure_rate",
        "feishu_failure_rate",
    ]
    assert top_level_summary["aggregate_metrics"]["average_parse_coverage_percentage"] == (
        "recompute((observed+explicitly_unavailable)/total*100); zero_when_total_zero_or_invalid"
    )
    assert (
        contract["observability"]["row_summary"]["terminal_feishu_writeback_duration"]
        == "merge_runtime_finished_at_minus_started_at"
    )
    assert contract["observability"]["runtime_error_codes"] == {
        "browser_retryable": [
            "navigation_timeout",
            "transient_page_failure",
            "rate_limited",
        ],
        "browser_terminal": [
            "invalid_asin",
            "invalid_product_url",
            "unsupported_marketplace",
            "browser_profile_unavailable",
            "access_blocked",
            "captcha_required",
            "identity_mismatch",
            "artifact_size_limit_exceeded",
            "required_failure_evidence_missing",
        ],
        "unknown_child_code": "fixed_safe_fallback",
        "api_non_retryable_or_supervisor_terminal": ("force_terminal_even_before_max_attempts"),
    }


def test_feishu_field_and_state_ownership_is_explicit() -> None:
    fields_contract = _load_yaml("contracts/fields/feishu-amazon-products.yaml")
    states_contract = _load_yaml("contracts/states/amazon-product-collection-status.yaml")

    assert fields_contract["table_alias"] == "AMAZON_PRODUCTS"
    assert fields_contract["table_display_name"] == "Amazon竞品表"
    assert fields_contract["identity"] == {
        "marketplace_code": "US",
        "field": "ASIN",
        "normalization": "trim_uppercase",
        "pattern": "^[A-Z0-9]{10}$",
        "projection_may_write": False,
    }
    fields = {field["name"]: field for field in fields_contract["fields"]}
    assert fields["ASIN"]["owner"] == "business_input"
    assert fields["采集标签"] == {
        "name": "采集标签",
        "canonical_name": "collection_tag",
        "type": "single_select",
        "owner": "business_input",
        "role": "batch_selector",
        "write_policy": "never_write",
        "batch_inclusion_value": "T",
    }
    assert fields["强制刷新"]["owner"] == "business_input"
    assert fields["来源关键词"]["owner"] == "amazon_search_workflow"
    assert fields["采集状态"]["owner"] == "amazon_workflow"
    assert fields["30天购买人数"] == {
        "name": "30天购买人数",
        "canonical_name": "bought_past_month",
        "type": "single_line_text",
        "owner": "amazon_projection",
        "write_policy": "observed_only",
        "fact_source": "commerce.bought_past_month",
        "value_policy": {
            "source_node": "#social-proofing-faceout-title-tk_bought",
            "source_text_pattern": "<display_value> bought in past month",
            "output": "display_value_only",
            "example": "500+",
            "numeric_coercion": "forbidden",
            "missing": "preserve_existing",
        },
    }
    assert fields["侧边栏图片"]["media_resolution"] == (
        "amazon_gallery_item_bound_hires_original_resource"
    )
    assert fields["送达日期"]["value_policy"] == {
        "input": "free_delivery_primary_message_without_destination_or_fastest_delivery",
        "output": "chinese_date_or_date_range_only",
        "weekday": "omit",
        "single_date_format": "M月D号",
        "same_month_range_format": "M月D-D号",
        "cross_month_range_format": "M月D号-M月D号",
        "strip_before_projection": ["free_delivery_label", "order_threshold"],
        "unparseable_observed_value": "preserve_existing",
    }
    promotion_field = fields["促销活动记录"]
    assert promotion_field["write_policy"] == "overwrite_current_snapshot"
    assert promotion_field["allowed_promotion_types"] == [
        "coupon",
        "limited_time_deal",
    ]
    assert promotion_field["value_policy"] == {
        "timestamp_timezone": "Asia/Shanghai",
        "timestamp_format": "M-D HH:mm",
        "timestamp_position": "second_line",
        "coupon": "coupon | 折扣 | 折后价\n采集时间",
        "limited_time_deal": "Limited time deal | 活动价\n采集时间",
        "no_promotion": "当前没有促销活动\n采集时间",
        "multiple_promotions": "consecutive_two_line_blocks_without_blank_lines",
        "coupon_price_basis": "commerce.featured_offer.price_amount",
        "coupon_rounding": "usd_half_up_2_decimals",
        "limited_time_deal_price_basis": "promotion.deal_price",
        "empty_observed_promotions": "write_no_promotion_snapshot",
        "missing_promotions": "preserve_existing",
    }
    projection_fields = {
        "商品链接",
        "标题",
        "品牌",
        "主图",
        "侧边栏图片",
        "30天购买人数",
        "当前价格",
        "库存状态",
        "Parent ASIN",
        "Child ASIN列表",
        "BSR排名",
        "技术参数",
        "送达日期",
        "包装规格",
        "页面ASIN",
        "字段完整度",
    }
    assert all(fields[name]["owner"] == "amazon_projection" for name in projection_fields)
    assert fields_contract["writeback_rules"]["missing"] == "preserve_existing"
    assert fields_contract["writeback_rules"]["record_target"] == ("source_record_id")
    assert fields_contract["writeback_rules"]["active_write_fields"] == [
        "主图",
        "侧边栏图片",
        "30天购买人数",
        "送达日期",
        "包装规格",
        "促销活动记录",
    ]

    states = {state["code"]: state for state in states_contract["states"]}
    assert set(states) == {
        "pending",
        "collecting",
        "persisting",
        "success",
        "partial_success",
        "unavailable",
        "blocked",
        "failed",
    }
    assert {code for code, state in states.items() if state["terminal"]} == {
        "success",
        "partial_success",
        "unavailable",
        "blocked",
        "failed",
    }
    assert states_contract["runtime_boundary"]["blocked_is_runtime_status"] is False
    assert states_contract["transitions"] == {
        "pending": ["collecting", "failed"],
        "collecting": ["persisting", "blocked", "failed"],
        "persisting": ["success", "partial_success", "unavailable", "failed"],
    }


def test_amazon_fact_tables_and_object_prefixes_are_isolated() -> None:
    contract = _load_yaml("contracts/facts/product-fact-collection.yaml")
    amazon = contract["platform_contracts"]["amazon_us"]

    assert amazon["identity_key"] == ["marketplace_code", "asin"]
    assert amazon["marketplace_code"] == "US"
    assert set(amazon["fact_tables"]) == {
        "amazon_products",
        "amazon_product_snapshots",
        "amazon_offer_snapshots",
        "amazon_product_variants",
        "amazon_bsr_snapshots",
        "amazon_media_assets",
        "amazon_product_media_assets",
        "amazon_raw_captures",
        "amazon_feishu_bindings",
    }
    assert amazon["object_prefixes"] == {
        "normalized_capture": "<env>/raw-captures/amazon/us/<asin>/",
        "blocked_evidence": "<env>/raw-captures/amazon/us/<asin>/",
        "product_media": "<env>/product-media/amazon/us/<asin>/",
    }
    assert amazon["object_storage_policy"] == (
        "default_deny_allow_only_normalized_capture_blocked_evidence_and_product_media"
    )
    assert amazon["runtime_db_schema_change"] is False
    assert amazon["cross_platform_foreign_keys"] is False
    assert amazon["fact_bundle_transaction"] == {
        "database_scope": "one_capture_database_transaction",
        "includes": [
            "product_identity_and_master",
            "product_snapshot",
            "raw_capture_indexes",
            "featured_offer",
            "variants",
            "bsr",
            "media_assets_and_relations",
            "feishu_binding",
            "latest_snapshot_pointer",
        ],
        "commit_visibility": "all_or_nothing",
        "latest_snapshot_publish": "final_database_write",
        "excludes": ["object_storage_objects", "feishu_projection_write"],
    }
    assert amazon["raw_capture_object_key"] == {
        "template": (
            "<env>/raw-captures/amazon/us/<asin>/<yyyy>/<mm>/<dd>/<run_id>/<sha256>/<filename>"
        ),
        "content_address": "sha256_of_stored_bytes",
        "exact_retry": "reuse_same_coordinate",
        "changed_bytes": "preserve_new_coordinate_without_overwriting_old_object",
        "same_run_normalized_capture": "reject_divergent_digest",
    }
    assert amazon["required_evidence_scope"] == (
        "success_like_fact_persistence_only"
    )
    assert amazon["required_evidence"] == [
        "normalized_capture_ref",
        "field_coverage",
    ]
    assert amazon["raw_capture_kinds"] == {
        "required_on_success": ["normalized_capture"],
        "optional_on_success": [],
        "required_on_blocked": ["blocked_screenshot"],
        "allowed_on_blocked": ["blocked_screenshot"],
        "normalized_capture_on_blocked": "forbidden",
        "media_materialization_on_blocked": "forbidden",
        "blocked_evidence_binding": "runtime_terminal_business_audit_record",
        "blocked_enters_fact_persistence": False,
        "forbidden_new_persistence": [
            "html",
            "network_data",
            "page_data",
            "success_screenshot",
            "runtime_screenshot",
        ],
    }
    assert amazon["raw_capture_metadata"] == {
        "normalized_capture": {
            "content_type": "application/json",
            "sanitization_status": "normalized",
        },
        "blocked_screenshot": {
            "content_type": "image/png",
            "sanitization_status": "not_applicable",
        },
    }
    assert amazon["raw_capture_key_collision"] == "reject_changed_immutable_evidence"
    assert amazon["field_evidence_policy"] == {
        "contract_revision": 5,
        "accepted_capture_revisions": [1, 2, 3, 4, 5],
        "coverage": "exact_target_field_set",
        "target_fields": [
            "product.title",
            "product.brand",
            "product.category_path",
            "product.bullet_points",
            "product.description",
            "product.technical_details",
            "commerce.availability_status",
            "commerce.rating",
            "commerce.review_count",
            "commerce.bought_past_month",
            "commerce.featured_offer.seller_id",
            "commerce.featured_offer.seller_name",
            "commerce.featured_offer.is_buy_box",
            "commerce.featured_offer.price_amount",
            "commerce.featured_offer.list_price_amount",
            "commerce.featured_offer.currency",
            "commerce.featured_offer.fulfillment_channel",
            "commerce.featured_offer.delivery_text",
            "commerce.featured_offer.coupon_text",
            "commerce.featured_offer.promotions",
            "variants.parent_asin",
            "variants.child_asins",
            "variants.current_attributes",
            "variants.dimensions",
            "rankings",
            "media.main_image",
            "media.gallery_images",
        ],
        "required_keys": [
            "value",
            "status",
            "source_kind",
            "source_locator",
            "confidence",
        ],
        "value_binding": "exact_normalized_capture_field_value",
        "missing_requires_empty_capture_value": True,
        "success_allows_missing": False,
        "collection_status_optional_fields": ["commerce.bought_past_month"],
        "partial_success_requires_missing": False,
    }
    assert amazon["normalized_capture_contract"] == {
        "current_revision": 5,
        "accepted_revisions": [1, 2, 3, 4, 5],
        "new_browser_output_revision": 5,
        "revision_1_promotions": "text_array",
        "revision_2_promotions": {
            "type": "object_array",
            "exact_keys": [
                "promotion_type",
                "label",
                "discount_type",
                "discount_value",
                "deal_price",
                "reference_price",
                "reference_price_type",
                "currency",
                "prime_only",
                "claim_required",
                "raw_text",
            ],
            "promotion_types": [
                "coupon",
                "limited_time_deal",
            ],
            "discount_types": [
                "percentage",
                "amount",
                "price_override",
            ],
            "reference_price_types": [None],
            "observation_time_binding": "parent_capture.captured_at",
            "profile_context_binding": "parent_capture.profile_context_digest",
        },
        "revision_3_media_resolution": {
            "inherits_revision_2_promotions": True,
            "amazon_media_url": ("original_resource_url_without_image_transform_segment"),
            "thumbnail_derivative": "forbidden",
        },
        "revision_4_gallery_binding": {
            "inherits_revision_3_media_url_normalization": True,
            "ordered_source": "ImageBlockATF.colorImages.initial",
            "item_binding": "sidebar_thumbnail_to_same_item_hires_asset",
            "candidate_priority": [
                "hiRes",
                "data-old-hires",
                "largest_dynamic_image",
                "large",
            ],
            "thumbnail_asset_id_inference": "forbidden",
            "video_thumbnail": "excluded",
        },
        "revision_5_bought_past_month": {
            "inherits_revision_4_gallery_binding": True,
            "capture_field": "commerce.bought_past_month",
            "evidence_path": "commerce.bought_past_month",
            "source_node": "#social-proofing-faceout-title-tk_bought",
            "source_text_pattern": "<display_value> bought in past month",
            "normalized_value": "display_value_only",
            "example": "500+",
            "numeric_coercion": "forbidden",
            "collection_status_optional": True,
        },
        "compatibility": {
            "persistence_reads_revision_1": True,
            "persistence_reads_revision_2": True,
            "persistence_reads_revision_3": True,
            "persistence_reads_revision_4": True,
            "revision_1_rewrite_required": False,
            "revision_2_rewrite_required": False,
            "revision_3_rewrite_required": False,
            "revision_4_rewrite_required": False,
            "revision_1_2_media_adapter": (
                "canonicalize_derivative_before_validation_and_materialization"
            ),
            "revision_3_high_resolution_guarantee": "requires_recollection",
            "fact_schema_ddl_required": False,
            "legacy_coupon_text_preserved": True,
        },
        "dom_text_policy": {
            "controlled_offer_regions_only": True,
            "exclude_tags": ["script", "style", "noscript", "template"],
            "forbidden_content": [
                "redeem_parameters",
                "token",
                "cookie",
                "account_or_address_text",
            ],
        },
        "field_semantics": {
            "commerce.bought_past_month": {
                "source": "stable_dom.#social-proofing-faceout-title-tk_bought",
                "required_suffix": "bought in past month",
                "fact_projection": (
                    "amazon_product_snapshots.payload_json.bought_past_month"
                ),
                "projection_field": "30天购买人数",
                "projection_format": "display_value_only",
                "example": "500+",
                "numeric_coercion": "forbidden",
                "missing_projection": "preserve_existing",
                "collection_status_optional": True,
            },
            "commerce.featured_offer.promotions": {
                "allowed_types": ["coupon", "limited_time_deal"],
                "excluded_page_offers": [
                    "checkout_discount",
                    "prime_member_price",
                    "prime_exclusive_price",
                    "prime_day_deal",
                    "subscribe_and_save",
                    "quantity_discount",
                    "qualifying_purchase",
                    "ordinary_strikethrough_price",
                    "other",
                ],
                "limited_time_deal_fields": {
                    "keep": ["label", "deal_price"],
                    "clear": [
                        "discount_value",
                        "reference_price",
                        "reference_price_type",
                    ],
                },
                "projection_field": "促销活动记录",
                "projection_write_policy": "overwrite_current_snapshot",
                "projection_empty_observation": (
                    "write_content_then_timestamp_no_promotion_snapshot"
                ),
                "projection_timestamp_timezone": "Asia/Shanghai",
                "projection_timestamp_format": "M-D HH:mm",
                "projection_timestamp_position": "second_line",
                "projection_multiple_promotions": (
                    "consecutive_two_line_blocks_without_blank_lines"
                ),
                "coupon_calculated_price_source": ("commerce.featured_offer.price_amount"),
            },
            "product.technical_details.Number of Items": {
                "source": "Product information.Item details.Number of Items",
                "projection_field": "包装规格",
                "projection_missing_value": "没有包装规格",
                "forbidden_fallback_sources": [
                    "Unit Count",
                    "quantity_selector",
                    "product_title",
                ],
            },
            "commerce.featured_offer.delivery_text": {
                "required_prefix": "FREE delivery",
                "source": "featured_offer_primary_delivery_message",
                "projection_field": "送达日期",
                "projection_format": "chinese_date_or_date_range_only",
                "single_date_format": "M月D号",
                "same_month_range_format": "M月D-D号",
                "cross_month_range_format": "M月D号-M月D号",
                "weekday": "omit",
                "strip_segments": [
                    "free_delivery_label",
                    "order_threshold",
                    "destination_address_and_postcode",
                    "fastest_delivery",
                    "order_countdown",
                    "account_text",
                ],
            },
        },
    }
    assert amazon["raw_capture_provenance"] == {
        "required_fields": ["request_id", "execution_id", "run_id"],
        "request_binding": "active_runtime_request",
        "execution_binding": "one_origin_browser_execution",
        "run_binding": "active_collection_run",
        "persisted_execution_id": "origin_browser_execution_id",
        "stored_byte_digest_verification": "required_before_fact_write",
        "durable_reference_fields": ["bucket", "object_key", "content_digest"],
        "local_path_read": "forbidden",
        "size_limits": {
            "normalized_capture_bytes": 2097152,
            "blocked_screenshot_bytes": 10485760,
            "materialized_media_bytes": 26214400,
        },
    }
    assert amazon["media_source_url_policy"] == {
        "resolution_owner": "amazon_product_browser_fetch",
        "gallery_binding_owner": "amazon_product_browser_fetch",
        "download_enforcement_owner": "media_asset_sync",
        "source_candidate_priority": [
            "hiRes",
            "data-old-hires",
            "largest_dynamic_image",
            "large",
        ],
        "ordered_gallery_source": "ImageBlockATF.colorImages.initial",
        "thumbnail_to_hires_binding": "required_before_normalized_capture",
        "thumbnail_asset_id_inference": "forbidden",
        "amazon_cdn_derivative_normalization": ("remove_filename_image_transform_segment"),
        "normalized_capture_resolution": ("original_resource_url_without_image_transform_segment"),
        "thumbnail_derivative_upload": "forbidden",
        "unresolved_original_policy": "remove_and_mark_partial_success",
        "compatibility": {
            "payload_shape_change": False,
            "fact_schema_migration_required": False,
            "old_browser_derivative_output": "canonicalized_by_media_sync_adapter",
            "old_capture_revision_1_2": "canonicalized_by_persistence_adapter",
            "old_capture_revision_3": (
                "accepted_but_requires_recollection_for_high_resolution_guarantee"
            ),
            "old_job_terminal_effect": ("unchanged_when_original_resource_materializes"),
            "deployment": "rolling_worker_upgrade_compatible",
        },
        "caller_override_policy": "may_only_tighten",
        "download_size_limit_bytes": 26214400,
        "identity_conflict_policy": "reject",
        "amazon_marketplace_policy": "require_us",
        "caller_materialized_fields_policy": "forbidden",
        "scheme": "https",
        "allowed_host_suffixes": [
            "media-amazon.com",
            "ssl-images-amazon.com",
        ],
        "allowed_ports": ["default", 443],
        "userinfo": "forbidden",
        "redirect_policy": "validate_each_hop_against_same_allowlist_before_request",
        "redirect_response_body_limit": "download_size_limit_bytes_each_hop",
        "query_and_fragment": "strip_before_persistence",
        "invalid_observed_media": "remove_and_mark_partial_success",
    }
    assert (
        amazon["media_source_url_policy"]["download_size_limit_bytes"]
        == (amazon["raw_capture_provenance"]["size_limits"]["materialized_media_bytes"])
    )
    assert amazon["media_cache_policy"] == {
        "candidate_key": "normalized_source_url_sha256",
        "candidate_index": "amazon_media_assets.source_url_digest",
        "url_identity_is_freshness_proof": False,
        "required_object_checks": [
            "current_environment_amazon_prefix",
            "bucket_and_object_key_present",
            "stored_bytes_size_matches",
            "stored_bytes_sha256_matches",
        ],
        "source_revalidation": {
            "validators": ["etag", "last_modified"],
            "not_modified": "reuse_cached_object",
            "changed_content": "write_new_content_addressed_object",
            "missing_validators": "download_and_compare_sha256",
            "validation_failure": "fall_back_to_full_download",
        },
        "preserve_current_capture_coordinates": ["media_role", "position"],
        "download_concurrency_change": False,
    }


def test_amazon_owner_boundaries_are_registered() -> None:
    contract = _load_yaml("contracts/harness/architecture-ownership.yaml")
    owners = contract["owners"]

    helper_paths = {entry["path"] for entry in owners["business_flow"]["allowed_helper_like_paths"]}
    assert {
        "src/automation_business_scaffold/domains/amazon/flows/refresh_amazon_product_row_by_asin/orchestrator.py",
        "src/automation_business_scaffold/domains/amazon/flows/refresh_current_amazon_product_table/orchestrator.py",
        "src/automation_business_scaffold/domains/amazon/flows/amazon_product_row_persist/orchestrator.py",
    } <= helper_paths

    assert owners["amazon_browser_capture"]["paths"] == [
        "src/automation_business_scaffold/capabilities/browser/amazon/**",
        "src/automation_business_scaffold/capabilities/browser/amazon_product_fetch_handler.py",
    ]
    assert "write Fact DB" in owners["amazon_browser_capture"]["forbidden"]
    assert "upload object storage content" in owners["amazon_fact_persistence"]["forbidden"]
    assert owners["object_storage_transport"]["allowed_logical_writers"] == [
        "media_asset_sync",
        "amazon_browser_capture",
    ]
    runtime_projection = owners["amazon_runtime_result_projection"]
    assert runtime_projection["paths"] == [
        "src/automation_business_scaffold/domains/amazon/projections/runtime_result_projection.py",
        "src/automation_business_scaffold/domains/amazon/projections/registry.py",
    ]
    assert runtime_projection["entrypoint"] == [
        "src/automation_business_scaffold/contracts/handler/domain_mapping.py:get_runtime_result_projection"
    ]
    assert runtime_projection["allowed_callers"] == [
        "src/automation_business_scaffold/control_plane/executor/worker_dispatch.py",
        "src/automation_business_scaffold/contracts/handler/domain_mapping.py",
    ]
    assert "access RuntimeStore or a database directly" in runtime_projection["forbidden"]
    assert (
        "execute browser, media, Fact DB, object storage, or Feishu side effects"
        in runtime_projection["forbidden"]
    )
    assert owners["amazon_fact_persistence"]["paths"] == [
        "alembic_fact.ini",
        "alembic_fact/**",
        "alembic/versions/20260714_0007_amazon_product_facts.py",
        "scripts/execution_control/run_fact_alembic_upgrade.sh",
        "src/automation_business_scaffold/infrastructure/schemas/amazon_fact_schema.py",
        "src/automation_business_scaffold/infrastructure/facts/amazon_fact_store.py",
        "src/automation_business_scaffold/capabilities/persistence/database/amazon_product_fact_upsert_handler.py",
    ]
    assert "decide Feishu fields" in owners["amazon_fact_persistence"]["forbidden"]


def test_amazon_roadmap_features_have_expected_status_and_gates() -> None:
    roadmap = _load_yaml("contracts/harness/code-roadmap.yaml")
    features = {feature["feature_code"]: feature for feature in roadmap["features"]}

    schema = features["amazon_product_fact_schema"]
    ingest = features["amazon_single_product_ingest"]
    batch = features["amazon_batch_product_ingest"]
    assert schema["status"] == "complete"
    assert ingest["status"] == "in_progress"
    assert batch["status"] == "complete"
    for feature in (schema, ingest, batch):
        assert feature["requires_architecture_delta_gate"] is True
        assert feature["source_contracts"]
        assert feature["allowed_paths"]
        assert feature["done_gate"]["tests"]
        assert feature["done_gate"]["commands"]

    assert {
        "alembic_fact.ini",
        "alembic_fact/**",
        "scripts/execution_control/run_fact_alembic_upgrade.sh",
        "alembic/versions/20260714_0007_amazon_product_facts.py",
        "src/automation_business_scaffold/infrastructure/schemas/amazon_fact_schema.py",
        "src/automation_business_scaffold/infrastructure/schemas/__init__.py",
        "src/automation_business_scaffold/infrastructure/facts/amazon_fact_store.py",
        "src/automation_business_scaffold/capabilities/persistence/database/amazon_product_fact_upsert_handler.py",
        "tests/conftest.py",
        "scripts/deploy/macos/deploy.local.env.example",
        "scripts/deploy/macos/deploy.sh",
        "scripts/deploy/macos/preflight.sh",
        "scripts/execution_control/executor.local.env.example",
        "skills/mujitask-tiktok-feishu-sync/skill.local.env.example",
        "docs/ops/deployment.md",
        "tests/test_macos_fact_migration_deployment.py",
    } <= set(schema["allowed_paths"])
    fact_owned_paths = {
        "alembic_fact.ini",
        "alembic_fact/**",
        "scripts/execution_control/run_fact_alembic_upgrade.sh",
        "alembic/versions/20260714_0007_amazon_product_facts.py",
        "src/automation_business_scaffold/infrastructure/schemas/amazon_fact_schema.py",
        "src/automation_business_scaffold/infrastructure/facts/amazon_fact_store.py",
        "src/automation_business_scaffold/capabilities/persistence/database/amazon_product_fact_upsert_handler.py",
    }
    assert fact_owned_paths.isdisjoint(ingest["allowed_paths"])
    assert {
        "docs/arch/runtime-control-plane-contract.md",
        "docs/arch/module-ownership-contract.md",
        "contracts/facts/product-fact-collection.yaml",
    } <= set(ingest["source_contracts"])
    assert {
        "src/automation_business_scaffold/capabilities/browser/amazon/product_page.py",
        "src/automation_business_scaffold/capabilities/browser/amazon_product_fetch_handler.py",
        "src/automation_business_scaffold/domains/amazon/tasks/refresh_amazon_product_row_by_asin.py",
        "src/automation_business_scaffold/domains/amazon/workflows/refresh_amazon_product_row_by_asin.py",
        "tests/test_runtime_amazon_product_business_e2e.py",
        "scripts/deploy/macos/deploy.local.env.example",
        "scripts/deploy/macos/deploy.sh",
        "scripts/deploy/macos/preflight.sh",
        "scripts/execution_control/executor.local.env.example",
        "skills/mujitask-tiktok-feishu-sync/skill.local.env.example",
        "tests/test_macos_fact_migration_deployment.py",
    } <= set(ingest["allowed_paths"])

    schema_tests = [
        "tests/test_amazon_product_contracts.py",
        "tests/test_amazon_fact_migration_routing.py",
        "tests/test_amazon_fact_schema.py",
        "tests/test_amazon_fact_store.py",
        "tests/test_amazon_product_fact_upsert_handler.py",
        "tests/test_macos_fact_migration_deployment.py",
    ]
    assert schema["done_gate"]["tests"] == schema_tests
    assert {
        token
        for command in schema["done_gate"]["commands"]
        for token in command.split()
        if token.startswith("tests/")
    } == set(schema_tests)

    ingest_tests = [
        "tests/test_amazon_product_contracts.py",
        "tests/test_config.py",
        "tests/test_amazon_product_page.py",
        "tests/test_amazon_product_browser_fetch_handler.py",
        "tests/test_feishu_amazon_product_mapping.py",
        "tests/test_media_asset_sync_handler.py",
        "tests/test_amazon_product_row_persist.py",
        "tests/test_feishu_common_handlers.py",
        "tests/test_refresh_amazon_product_row_by_asin.py",
        "tests/test_runtime_amazon_product_ingest.py",
        "tests/test_runtime_amazon_product_business_e2e.py",
        "tests/test_runtime_workflow_registry.py",
        "tests/test_handler_registry_contract.py",
        "tests/test_workflow_architecture_manifests.py",
        "tests/test_harness_code_roadmap.py",
        "tests/test_architecture_ownership.py",
        "tests/test_macos_fact_migration_deployment.py",
    ]
    assert ingest["done_gate"]["tests"] == ingest_tests
    assert {
        token
        for command in ingest["done_gate"]["commands"]
        for token in command.split()
        if token.startswith("tests/")
    } == set(ingest_tests)


def test_amazon_handler_entry_and_fact_migration_docs_freeze_runtime_boundaries() -> None:
    handler_doc = (REPO_ROOT / "docs/arch/handler-contract-design.md").read_text(encoding="utf-8")
    entry_doc = (REPO_ROOT / "docs/arch/entry-output-contract-design.md").read_text(
        encoding="utf-8"
    )
    fact_doc = (REPO_ROOT / "docs/arch/fact-db-schema-design.md").read_text(encoding="utf-8")

    assert "### 6.8 Amazon 单商品采集 Handler" in handler_doc
    assert "`amazon_product_browser_fetch`" in handler_doc
    assert "`amazon_product_fact_upsert`" in handler_doc
    assert "`amazon_product_row_persist`" in handler_doc
    assert "Runtime payload/result 不内联完整 normalized capture" in handler_doc

    assert "### 2.2 Amazon 单商品入口" in entry_doc
    assert "### 2.3 Amazon 竞品表批量入口" in entry_doc
    assert '"table_ref": "AMAZON_PRODUCTS"' in entry_doc
    assert '"source_record_id": "recxxxxxxxx"' in entry_doc
    assert "正式入口不得直接传 ASIN" in entry_doc

    assert "`BUSINESS_EXECUTION_CONTROL_FACT_MIGRATION_DB_URL`" in fact_doc
    assert "`fact_alembic_version`" in fact_doc
    assert "Fact migration 图 downgrade 到 `base`" in fact_doc


def test_contract_index_and_design_status_match_implementation_phase() -> None:
    contract_index = _read("contracts/README.md")
    design = _read("docs/arch/workflow-amazon-product-detail-design.md")

    assert "feishu-amazon-products.yaml" in contract_index
    assert "amazon-product-collection-status.yaml" in contract_index
    assert "refresh_amazon_product_row_by_asin.yaml" in contract_index
    assert "refresh_current_amazon_product_table.yaml" in contract_index
    assert "状态: 已批准，实施中，能力尚未完成" in design
    assert "代码、migration 和机器契约完成并通过 completion gate 前" in design


def test_amazon_documentation_routes_and_runtime_projection_boundary_are_indexed() -> None:
    root_readme = _read("README.md")
    business_overview = _read("docs/business/business-requirements.md")
    runtime_contract = _read("docs/arch/runtime-control-plane-contract.md")
    module_contract = _read("docs/arch/module-ownership-contract.md")

    assert "TikTok / FastMoss / Amazon" in root_readme
    assert "`AMAZON_PRODUCTS`" in root_readme
    assert "当前阶段已经明确的业务目标主要有五类" in business_overview
    assert "当前已实时验证的 5 张 TikTok 飞书表" in business_overview
    assert "requirements/amazon-product-detail-collection.md" in business_overview
    assert "domain Runtime result projection" in runtime_contract
    assert "domains/{domain}/projections/*runtime_result_projection.py" in runtime_contract
    assert "Runtime result projection" in module_contract
