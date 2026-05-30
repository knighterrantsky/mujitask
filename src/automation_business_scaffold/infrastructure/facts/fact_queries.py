from __future__ import annotations

from typing import Any, Mapping

"""Read-side facts access owned by contracts/facts/product-fact-collection.yaml."""


class TKFactQueryAccess:
    def find_media_asset(
        self,
        *,
        source_url: str = "",
        file_token: str = "",
        local_path: str = "",
        object_key: str = "",
    ) -> dict[str, Any]:
        candidates: list[dict[str, Any]] = []
        seen_asset_ids: set[str] = set()
        asset_key = self.build_asset_key(
            source_url=source_url,
            file_token=file_token,
            local_path=local_path,
            object_key=object_key,
        )
        if asset_key:
            asset = self._get_by_unique("tk_media_assets", "asset_key", asset_key)
            if asset:
                candidates.append(asset)
                seen_asset_ids.add(str(asset.get("asset_id") or ""))
        for column_name, value in (
            ("file_token", file_token),
            ("object_key", object_key),
            ("local_path", local_path),
            ("source_url", source_url),
        ):
            cleaned = _clean_text(value)
            if cleaned:
                for asset in self._get_media_assets_by_column(column_name, cleaned):
                    asset_id = str(asset.get("asset_id") or "")
                    if asset_id and asset_id in seen_asset_ids:
                        continue
                    candidates.append(asset)
                    seen_asset_ids.add(asset_id)
        for asset in candidates:
            if _media_asset_has_locator(asset):
                return asset
        return candidates[0] if candidates else {}

    def creator_has_product(self, *, creator_id: str = "", uid: str = "", unique_id: str = "", product_id: str) -> bool:
        creator_key = self.build_creator_key(creator_id=creator_id, uid=uid, unique_id=unique_id)
        product_id = _clean_text(product_id)
        if not creator_key or not product_id:
            return False
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    self._text(
                        """
                        SELECT 1
                        FROM tk_creator_product_relations
                        WHERE creator_key = :creator_key
                          AND product_id = :product_id
                        LIMIT 1
                        """
                    ),
                    {"creator_key": creator_key, "product_id": product_id},
                )
                .first()
            )
        return row is not None

    def get_product(self, *, product_id: str) -> dict[str, Any]:
        return self._get_by_unique("tk_products", "product_id", _clean_text(product_id))

    def get_creator(self, *, creator_key: str) -> dict[str, Any]:
        return self._get_by_unique("tk_creators", "creator_key", _clean_text(creator_key))

    def get_video(self, *, video_key: str = "", video_id: str = "") -> dict[str, Any]:
        resolved_video_key = _clean_text(video_key) or (f"video:{_clean_text(video_id)}" if _clean_text(video_id) else "")
        return self._get_by_unique("tk_videos", "video_key", resolved_video_key)

    def list_videos_by_product_and_creator(
        self,
        *,
        product_id: str,
        creator_unique_id: str,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        product_id = _clean_text(product_id)
        creator_unique_id = _clean_text(creator_unique_id)
        if not product_id or not creator_unique_id:
            return []
        limit_clause = "LIMIT :limit" if limit is not None and int(limit or 0) > 0 else ""
        params: dict[str, Any] = {
            "product_id": product_id,
            "creator_unique_id": creator_unique_id,
        }
        if limit_clause:
            params["limit"] = int(limit or 0)
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    self._text(
                        f"""
                        SELECT v.*
                        FROM tk_video_product_relations AS rel
                        JOIN tk_videos AS v
                          ON v.video_key = rel.video_key
                        WHERE rel.product_id = :product_id
                          AND v.creator_unique_id = :creator_unique_id
                        ORDER BY v.video_id ASC
                        {limit_clause}
                        """
                    ),
                    params,
                )
                .mappings()
                .all()
            )
        return [_with_video_fact_fields(self._row_to_dict(row)) for row in rows]

    def get_raw_api_response(self, *, raw_response_id: str) -> dict[str, Any]:
        return self._get_by_unique("tk_raw_api_responses", "raw_response_id", _clean_text(raw_response_id))

    def _get_by_unique(self, table_name: str, unique_column: str, unique_value: str) -> dict[str, Any]:
        if not unique_value:
            return {}
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    self._text(
                        f"""
                        SELECT *
                        FROM {table_name}
                        WHERE {unique_column} = :unique_value
                        LIMIT 1
                        """
                    ),
                    {"unique_value": unique_value},
                )
                .mappings()
                .first()
            )
        return self._row_to_dict(row) if row is not None else {}

    def _get_media_assets_by_column(self, column_name: str, value: str) -> list[dict[str, Any]]:
        if column_name not in {"asset_key", "source_url", "file_token", "local_path", "object_key"}:
            return []
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    self._text(
                        f"""
                        SELECT *
                        FROM tk_media_assets
                        WHERE {column_name} = :value
                        ORDER BY updated_at DESC
                        LIMIT 20
                        """
                    ),
                    {"value": value},
                )
                .mappings()
                .all()
            )
        return [self._row_to_dict(row) for row in rows]


class TKFactQuery(TKFactQueryAccess):
    def __init__(self, store: Any):
        self._store = store
        self._engine = store._engine  # noqa: SLF001
        self._text = store._text  # noqa: SLF001
        self._row_to_dict = store._row_to_dict  # noqa: SLF001
        self.build_asset_key = store.build_asset_key
        self.build_creator_key = store.build_creator_key


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _media_asset_has_locator(asset: Mapping[str, Any]) -> bool:
    return any(_clean_text(asset.get(key)) for key in ("object_key", "file_token", "local_path"))


def _with_video_fact_fields(video: dict[str, Any]) -> dict[str, Any]:
    facts = video.get("facts") if isinstance(video.get("facts"), Mapping) else {}
    for target_key, fact_keys in {
        "published_date": ("published_date", "create_date", "publish_time"),
        "create_date": ("create_date", "published_date", "publish_time"),
    }.items():
        if _clean_text(video.get(target_key)):
            continue
        for fact_key in fact_keys:
            value = _clean_text(facts.get(fact_key))
            if value:
                video[target_key] = value
                break
    return video
