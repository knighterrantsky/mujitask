from __future__ import annotations

from typing import Any

from automation_business_scaffold.capabilities.media import asset_sync_handler
from automation_business_scaffold.contracts.handler.contract import HandlerContext


class _FakeResponse:
    headers = {"Content-Type": "image/webp"}

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def read(self) -> bytes:
        return b"fake-webp-bytes"


def _context(payload: dict[str, Any]) -> HandlerContext:
    return HandlerContext(
        request_id="req-media",
        job_id="job-media",
        handler_code="media_asset_sync",
        worker_type="api_worker",
        runtime_table="api_worker_job",
        job_code="media_asset_sync",
        payload=payload,
    )


def test_media_asset_sync_downloads_referenced_source_url_before_upload(monkeypatch, tmp_path) -> None:
    requested_urls: list[str] = []

    def fake_urlopen(request, timeout: int):
        requested_urls.append(request.full_url)
        assert timeout == 11
        return _FakeResponse()

    monkeypatch.setattr(asset_sync_handler, "urlopen", fake_urlopen)

    result = asset_sync_handler.media_asset_sync_handler(
        _context(
            {
                "artifact_root": str(tmp_path / "artifacts"),
                "artifact_store_provider": "local",
                "artifact_bucket": "local-runtime",
                "sync_referenced_files": True,
                "media_download_timeout_seconds": 11,
                "media_download_dir": str(tmp_path / "downloads"),
                "asset_refs": [
                    {
                        "entity_type": "product",
                        "entity_external_id": "1730964478199763166",
                        "media_role": "product_gallery_image",
                        "source_url": "https://cdn.example.com/gallery.webp?from=2378011839",
                        "source_platform": "tiktok",
                    }
                ],
            }
        )
    )

    assert result.status == "success"
    assert requested_urls == ["https://cdn.example.com/gallery.webp?from=2378011839"]
    asset = result.result["synced_assets"][0]
    assert asset["sync_state"] == "linked_local"
    assert asset["media_role"] == "product_gallery_image"
    assert asset["source_url"] == "https://cdn.example.com/gallery.webp?from=2378011839"
    assert asset["local_path"].endswith(".webp")
    assert asset["object_key"].endswith(".webp")
    assert result.result["artifact_refs"][0]["content_type"] == "image/webp"


def test_media_asset_sync_reuses_duplicate_source_url_within_same_run(monkeypatch, tmp_path) -> None:
    requested_urls: list[str] = []

    def fake_urlopen(request, timeout: int):
        requested_urls.append(request.full_url)
        return _FakeResponse()

    monkeypatch.setattr(asset_sync_handler, "urlopen", fake_urlopen)

    result = asset_sync_handler.media_asset_sync_handler(
        _context(
            {
                "artifact_root": str(tmp_path / "artifacts"),
                "artifact_store_provider": "local",
                "artifact_bucket": "local-runtime",
                "sync_referenced_files": True,
                "media_download_dir": str(tmp_path / "downloads"),
                "asset_refs": [
                    {
                        "entity_type": "product",
                        "entity_external_id": "1730964478199763166",
                        "media_role": "product_gallery_image",
                        "source_url": "https://cdn.example.com/shared.webp",
                    },
                    {
                        "entity_type": "product",
                        "entity_external_id": "1730964478199763166",
                        "media_role": "product_sku_image",
                        "source_url": "https://cdn.example.com/shared.webp",
                    },
                ],
            }
        )
    )

    assert result.status == "success"
    assert requested_urls == ["https://cdn.example.com/shared.webp"]
    assert len(result.result["artifact_refs"]) == 1
    assert [asset["sync_state"] for asset in result.result["synced_assets"]] == ["linked_local", "reused_in_run"]
    assert result.result["synced_assets"][0]["object_key"] == result.result["synced_assets"][1]["object_key"]
    assert result.result["synced_assets"][1]["media_role"] == "product_sku_image"


def test_media_asset_sync_can_leave_referenced_url_unmaterialized(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        asset_sync_handler,
        "urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not download")),
    )

    result = asset_sync_handler.media_asset_sync_handler(
        _context(
            {
                "artifact_root": str(tmp_path / "artifacts"),
                "artifact_store_provider": "local",
                "sync_referenced_files": False,
                "asset_refs": [
                    {
                        "entity_type": "product",
                        "entity_external_id": "1730964478199763166",
                        "media_role": "product_gallery_image",
                        "source_url": "https://cdn.example.com/gallery.webp",
                    }
                ],
            }
        )
    )

    assert result.status == "success"
    assert result.result["synced_assets"][0]["sync_state"] == "referenced"
    assert result.result["artifact_refs"] == []


def test_media_asset_sync_can_require_referenced_assets_to_materialize(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        asset_sync_handler,
        "urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not download")),
    )

    result = asset_sync_handler.media_asset_sync_handler(
        _context(
            {
                "artifact_root": str(tmp_path / "artifacts"),
                "artifact_store_provider": "local",
                "sync_referenced_files": False,
                "require_materialized_assets": True,
                "asset_refs": [
                    {
                        "entity_type": "product",
                        "entity_external_id": "1730964478199763166",
                        "media_role": "product_gallery_image",
                        "source_url": "https://cdn.example.com/gallery.webp",
                    }
                ],
            }
        )
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "media_asset_materialization_failed"
    assert result.result["synced_assets"][0]["sync_state"] == "referenced"


def test_media_asset_sync_fails_when_object_storage_is_required_with_local_provider(tmp_path) -> None:
    media_file = tmp_path / "main.webp"
    media_file.write_bytes(b"fake-webp")

    result = asset_sync_handler.media_asset_sync_handler(
        _context(
            {
                "artifact_root": str(tmp_path / "artifacts"),
                "artifact_store_provider": "local",
                "artifact_bucket": "local-runtime",
                "require_object_storage": True,
                "asset_refs": [
                    {
                        "entity_type": "product",
                        "entity_external_id": "1730964478199763166",
                        "media_role": "product_main_image",
                        "local_path": str(media_file),
                    }
                ],
            }
        )
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "object_storage_required"
    assert result.summary["artifact_store_provider"] == "local"
    assert result.result["artifact_refs"] == []


def test_media_asset_sync_reuses_cached_fact_asset_without_download(monkeypatch, tmp_path) -> None:
    class FakeFactStore:
        def __init__(self, *, db_url: str):
            assert db_url == "postgresql+psycopg://facts"

        def find_media_asset(self, **kwargs: Any) -> dict[str, Any]:
            assert kwargs["source_url"] == "https://cdn.example.com/gallery.webp"
            return {
                "asset_id": "asset-cached",
                "asset_key": "source_url:https://cdn.example.com/gallery.webp",
                "source_url": "https://cdn.example.com/gallery.webp",
                "object_key": "runtime/media/gallery.webp",
                "file_name": "gallery.webp",
                "mime_type": "image/webp",
                "source_platform": "tiktok",
            }

    monkeypatch.setattr(asset_sync_handler, "TKFactStore", FakeFactStore)
    monkeypatch.setattr(
        asset_sync_handler,
        "urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not download")),
    )

    result = asset_sync_handler.media_asset_sync_handler(
        _context(
            {
                "artifact_root": str(tmp_path / "artifacts"),
                "artifact_store_provider": "local",
                "fact_db_url": "postgresql+psycopg://facts",
                "sync_referenced_files": True,
                "asset_refs": [
                    {
                        "entity_type": "product",
                        "entity_external_id": "1730964478199763166",
                        "media_role": "product_gallery_image",
                        "source_url": "https://cdn.example.com/gallery.webp",
                    }
                ],
            }
        )
    )

    assert result.status == "success"
    assert result.result["artifact_refs"] == []
    asset = result.result["synced_assets"][0]
    assert asset["sync_state"] == "reused"
    assert asset["asset_id"] == "asset-cached"
    assert asset["object_key"] == "runtime/media/gallery.webp"
