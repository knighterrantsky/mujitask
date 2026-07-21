from __future__ import annotations

import hashlib
from typing import Any
from urllib.error import HTTPError

import pytest

from automation_business_scaffold.capabilities.media import asset_sync_handler
from automation_business_scaffold.contracts.handler.contract import HandlerContext
from automation_business_scaffold.infrastructure.artifacts.artifact_store import (
    StoredArtifact,
)


class _FakeResponse:
    headers = {"Content-Type": "image/webp"}

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        content = b"fake-webp-bytes"
        return content if size < 0 else content[:size]


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


def test_media_asset_sync_downloads_referenced_source_url_before_upload(
    monkeypatch, tmp_path
) -> None:
    requested_urls: list[str] = []

    def fake_urlopen(request, timeout: int):
        requested_urls.append(request.full_url)
        assert timeout == 11
        return _FakeResponse()

    monkeypatch.setattr(asset_sync_handler, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        asset_sync_handler,
        "build_opener",
        lambda *args: pytest.fail("ungoverned non-Amazon downloads must keep using urlopen"),
    )

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
    assert asset["object_key"].startswith("runs/job-media/assets/")
    assert result.result["artifact_refs"][0]["content_type"] == "image/webp"


def test_media_asset_sync_enforces_configured_download_byte_limit(monkeypatch, tmp_path) -> None:
    redirect_handlers: list[object] = []

    class FakeOpener:
        def open(self, request, *, timeout):
            del request, timeout
            return _FakeResponse()

    def build_opener(handler):
        redirect_handlers.append(handler)
        return FakeOpener()

    monkeypatch.setattr(asset_sync_handler, "build_opener", build_opener)
    monkeypatch.setattr(
        asset_sync_handler,
        "urlopen",
        lambda request, timeout: pytest.fail("governed downloads must not use urlopen directly"),
    )

    result = asset_sync_handler.media_asset_sync_handler(
        _context(
            {
                "artifact_root": str(tmp_path / "artifacts"),
                "artifact_store_provider": "local",
                "artifact_bucket": "local-runtime",
                "sync_referenced_files": True,
                "require_materialized_assets": True,
                "media_download_max_bytes": 4,
                "media_download_allowed_host_suffixes": [
                    "media-amazon.com",
                    "ssl-images-amazon.com",
                ],
                "media_download_dir": str(tmp_path / "downloads"),
                "asset_refs": [
                    {
                        "entity_type": "product",
                        "entity_external_id": "B0ABC12345",
                        "media_role": "main_image",
                        "source_url": "https://m.media-amazon.com/images/I/main.webp",
                        "source_platform": "amazon",
                        "marketplace_code": "US",
                    }
                ],
            }
        )
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "media_asset_materialization_failed"
    assert result.result["synced_assets"][0]["sync_state"] == "referenced"
    assert not list((tmp_path / "downloads").rglob("*"))
    assert len(redirect_handlers) == 1


def test_amazon_invalid_metadata_url_is_removed_before_storage_when_sync_disabled(
    monkeypatch,
    tmp_path,
) -> None:
    source_url = "http://169.254.169.254/latest/meta-data?token=must-not-leak"
    monkeypatch.setattr(
        asset_sync_handler,
        "create_store_from_settings",
        lambda settings: pytest.fail("rejected Amazon media must not initialize storage"),
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
                        "entity_external_id": "B0ABC12345",
                        "media_role": "main_image",
                        "source_url": source_url,
                        "source_platform": "amazon",
                        "marketplace_code": "US",
                    }
                ],
            }
        )
    )

    assert result.status == "partial_success"
    assert result.error is None
    assert result.summary["rejected_count"] == 1
    assert result.result["synced_assets"] == []
    assert result.result["artifact_refs"] == []
    assert result.result["media_fact_bundle"]["media_assets"] == []
    assert result.warnings
    assert all(source_url not in warning for warning in result.warnings)
    assert all("must-not-leak" not in warning for warning in result.warnings)


def test_amazon_invalid_url_is_partial_when_download_enabled_but_not_required(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        asset_sync_handler,
        "create_store_from_settings",
        lambda settings: pytest.fail("rejected Amazon media must not initialize storage"),
    )
    monkeypatch.setattr(
        asset_sync_handler,
        "build_opener",
        lambda *args: pytest.fail("rejected Amazon media must not be downloaded"),
    )

    result = asset_sync_handler.media_asset_sync_handler(
        _context(
            {
                "artifact_root": str(tmp_path / "artifacts"),
                "artifact_store_provider": "local",
                "sync_referenced_files": True,
                "require_materialized_assets": False,
                "asset_refs": [
                    {
                        "entity_type": "product",
                        "entity_external_id": "B0ABC12345",
                        "media_role": "main_image",
                        "source_url": "https://images.example.test/private.jpg",
                        "source_platform": "amazon",
                        "marketplace_code": "US",
                    }
                ],
            }
        )
    )

    assert result.status == "partial_success"
    assert result.error is None
    assert result.result["synced_assets"] == []
    assert result.result["media_fact_bundle"]["media_assets"] == []


def test_amazon_valid_source_url_strips_query_and_fragment_before_output(tmp_path) -> None:
    result = asset_sync_handler.media_asset_sync_handler(
        _context(
            {
                "artifact_root": str(tmp_path / "artifacts"),
                "artifact_store_provider": "local",
                "sync_referenced_files": False,
                "asset_refs": [
                    {
                        "entity_type": "product",
                        "entity_external_id": "B0ABC12345",
                        "media_role": "main_image",
                        "source_url": (
                            "https://m.media-amazon.com/images/I/main.webp"
                            "?signature=secret#tracking"
                        ),
                        "source_platform": "amazon",
                        "marketplace_code": "US",
                    }
                ],
            }
        )
    )

    assert result.status == "success"
    expected_url = "https://m.media-amazon.com/images/I/main.webp"
    assert result.result["synced_assets"][0]["source_url"] == expected_url
    assert result.result["media_fact_bundle"]["media_assets"][0]["source_url"] == expected_url


def test_amazon_thumbnail_derivative_is_canonicalized_before_download(
    monkeypatch,
    tmp_path,
) -> None:
    requested_urls: list[str] = []

    class FakeOpener:
        def open(self, request, *, timeout):
            del timeout
            requested_urls.append(request.full_url)
            return _FakeResponse()

    monkeypatch.setattr(
        asset_sync_handler,
        "build_opener",
        lambda handler: FakeOpener(),
    )

    result = asset_sync_handler.media_asset_sync_handler(
        _context(
            {
                "artifact_root": str(tmp_path / "artifacts"),
                "artifact_store_provider": "local",
                "sync_referenced_files": True,
                "asset_refs": [
                    {
                        "entity_type": "product",
                        "entity_external_id": "B0ABC12345",
                        "media_role": "gallery_image",
                        "source_url": ("https://m.media-amazon.com/images/I/gallery._AC_US40_.jpg"),
                        "source_platform": "amazon",
                        "marketplace_code": "US",
                    }
                ],
            }
        )
    )

    expected_url = "https://m.media-amazon.com/images/I/gallery.jpg"
    assert result.status == "success"
    assert requested_urls == [expected_url]
    assert result.result["synced_assets"][0]["source_url"] == expected_url


def test_non_amazon_asset_identity_overrides_payload_fallback(tmp_path) -> None:
    result = asset_sync_handler.media_asset_sync_handler(
        _context(
            {
                "artifact_root": str(tmp_path / "artifacts"),
                "artifact_store_provider": "local",
                "sync_referenced_files": False,
                "source_platform": "tiktok",
                "marketplace_code": "ID",
                "asset_refs": [
                    {
                        "entity_type": "product",
                        "entity_external_id": "1730964478199763166",
                        "media_role": "product_image",
                        "source_url": "https://cdn.example.com/main.webp",
                        "source_platform": "fastmoss",
                        "marketplace_code": "TH",
                    }
                ],
            }
        )
    )

    assert result.status == "success"
    assert result.result["synced_assets"][0]["source_platform"] == "fastmoss"
    assert result.result["synced_assets"][0]["marketplace_code"] == "TH"


def test_amazon_us_media_enforces_default_cdn_and_download_cap(monkeypatch, tmp_path) -> None:
    read_sizes: list[int] = []
    redirect_handlers: list[object] = []

    class TrackingResponse(_FakeResponse):
        def read(self, size: int = -1) -> bytes:
            read_sizes.append(size)
            return super().read(size)

    class FakeOpener:
        def open(self, request, *, timeout):
            assert request.full_url == "https://m.media-amazon.com/images/I/main.webp"
            assert timeout == 30
            return TrackingResponse()

    def fake_build_opener(handler):
        redirect_handlers.append(handler)
        return FakeOpener()

    monkeypatch.setattr(asset_sync_handler, "build_opener", fake_build_opener)
    monkeypatch.setattr(
        asset_sync_handler,
        "urlopen",
        lambda *args, **kwargs: pytest.fail("Amazon/US downloads must always use governed opener"),
    )

    result = asset_sync_handler.media_asset_sync_handler(
        _context(
            {
                "artifact_root": str(tmp_path / "artifacts"),
                "artifact_store_provider": "local",
                "sync_referenced_files": True,
                "media_download_dir": str(tmp_path / "downloads"),
                "asset_refs": [
                    {
                        "entity_type": "product",
                        "entity_external_id": "B0ABC12345",
                        "media_role": "main_image",
                        "source_url": "https://m.media-amazon.com/images/I/main.webp",
                        "source_platform": "amazon",
                        "marketplace_code": "US",
                    }
                ],
            }
        )
    )

    assert result.status == "success"
    assert read_sizes == [25 * 1024 * 1024 + 1]
    assert len(redirect_handlers) == 1
    assert redirect_handlers[0]._allowed_host_suffixes == (
        "media-amazon.com",
        "ssl-images-amazon.com",
    )


def test_amazon_us_media_caller_cannot_raise_download_cap(monkeypatch, tmp_path) -> None:
    read_sizes: list[int] = []

    class TrackingResponse(_FakeResponse):
        def read(self, size: int = -1) -> bytes:
            read_sizes.append(size)
            return super().read(size)

    class FakeOpener:
        def open(self, request, *, timeout):
            del request, timeout
            return TrackingResponse()

    monkeypatch.setattr(asset_sync_handler, "build_opener", lambda handler: FakeOpener())

    result = asset_sync_handler.media_asset_sync_handler(
        _context(
            {
                "artifact_root": str(tmp_path / "artifacts"),
                "artifact_store_provider": "local",
                "sync_referenced_files": True,
                "source_platform": "amazon",
                "marketplace_code": "US",
                "media_download_max_bytes": 100 * 1024 * 1024,
                "media_download_allowed_host_suffixes": ["media-amazon.com"],
                "media_download_dir": str(tmp_path / "downloads"),
                "asset_refs": [
                    {
                        "entity_type": "product",
                        "entity_external_id": "B0ABC12345",
                        "media_role": "main_image",
                        "source_url": "https://m.media-amazon.com/images/I/main.webp",
                    }
                ],
            }
        )
    )

    assert result.status == "success"
    assert read_sizes == [25 * 1024 * 1024 + 1]


def test_amazon_us_media_caller_cannot_expand_cdn_allowlist(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        asset_sync_handler,
        "build_opener",
        lambda *args: pytest.fail("out-of-policy Amazon media must fail before opening"),
    )
    monkeypatch.setattr(
        asset_sync_handler,
        "urlopen",
        lambda *args, **kwargs: pytest.fail("Amazon/US downloads must never use urlopen"),
    )

    result = asset_sync_handler.media_asset_sync_handler(
        _context(
            {
                "artifact_root": str(tmp_path / "artifacts"),
                "artifact_store_provider": "local",
                "sync_referenced_files": True,
                "require_materialized_assets": True,
                "media_download_allowed_host_suffixes": ["example.test"],
                "media_download_dir": str(tmp_path / "downloads"),
                "asset_refs": [
                    {
                        "entity_type": "product",
                        "entity_external_id": "B0ABC12345",
                        "media_role": "main_image",
                        "source_url": "https://images.example.test/main.webp",
                        "source_platform": "amazon",
                        "marketplace_code": "US",
                    }
                ],
            }
        )
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "invalid_media_asset_input"
    assert result.result["synced_assets"] == []


@pytest.mark.parametrize(
    ("payload_identity", "asset_identity"),
    [
        (
            {"source_platform": "amazon", "marketplace_code": "US"},
            {"source_platform": "tiktok", "marketplace_code": "US"},
        ),
        (
            {"source_platform": "amazon", "marketplace_code": "US"},
            {"source_platform": "amazon", "marketplace_code": "CA"},
        ),
    ],
)
def test_amazon_media_rejects_payload_asset_identity_conflicts_before_storage(
    monkeypatch,
    payload_identity,
    asset_identity,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        asset_sync_handler,
        "create_store_from_settings",
        lambda settings: pytest.fail("invalid Amazon identity must fail before storage setup"),
    )

    result = asset_sync_handler.media_asset_sync_handler(
        _context(
            {
                "artifact_root": str(tmp_path / "artifacts"),
                "artifact_store_provider": "local",
                **payload_identity,
                "asset_refs": [
                    {
                        "entity_type": "product",
                        "entity_external_id": "B0ABC12345",
                        "media_role": "main_image",
                        "source_url": "https://m.media-amazon.com/images/I/main.webp",
                        **asset_identity,
                    }
                ],
            }
        )
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "invalid_media_asset_input"
    assert result.result["artifact_refs"] == []


@pytest.mark.parametrize("marketplace_code", [None, "CA"])
def test_amazon_media_requires_us_marketplace_before_storage(
    monkeypatch,
    marketplace_code,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        asset_sync_handler,
        "create_store_from_settings",
        lambda settings: pytest.fail("invalid Amazon marketplace must fail before storage setup"),
    )
    asset_ref = {
        "entity_type": "product",
        "entity_external_id": "B0ABC12345",
        "media_role": "main_image",
        "source_url": "https://m.media-amazon.com/images/I/main.webp",
        "source_platform": "amazon",
    }
    if marketplace_code is not None:
        asset_ref["marketplace_code"] = marketplace_code

    result = asset_sync_handler.media_asset_sync_handler(
        _context(
            {
                "artifact_root": str(tmp_path / "artifacts"),
                "artifact_store_provider": "local",
                "asset_refs": [asset_ref],
            }
        )
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "invalid_media_asset_input"
    assert result.result["artifact_refs"] == []


@pytest.mark.parametrize(
    ("injected_field", "injected_value"),
    [
        ("local_path", "secret.webp"),
        ("object_key", "attacker-controlled/object.webp"),
        ("source_path", "/private/secret.webp"),
        ("bucket", "attacker-controlled-bucket"),
        ("remote_uri", "file:///private/secret.webp"),
        ("file_token", "attacker-controlled-token"),
    ],
)
def test_amazon_media_rejects_caller_materialized_refs_before_storage(
    monkeypatch,
    injected_field,
    injected_value,
    tmp_path,
) -> None:
    storage_setup_calls: list[dict[str, Any]] = []

    def create_store(settings):
        storage_setup_calls.append(settings)
        return None

    monkeypatch.setattr(asset_sync_handler, "create_store_from_settings", create_store)
    if injected_field == "local_path":
        injected_value = str(tmp_path / injected_value)
        (tmp_path / "secret.webp").write_bytes(b"must-not-upload")

    result = asset_sync_handler.media_asset_sync_handler(
        _context(
            {
                "artifact_root": str(tmp_path / "artifacts"),
                "artifact_store_provider": "local",
                "asset_refs": [
                    {
                        "entity_type": "product",
                        "entity_external_id": "B0ABC12345",
                        "media_role": "main_image",
                        "source_url": "https://m.media-amazon.com/images/I/main.webp",
                        "source_platform": "amazon",
                        "marketplace_code": "US",
                        injected_field: injected_value,
                    }
                ],
            }
        )
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "invalid_media_asset_input"
    assert result.result["artifact_refs"] == []
    assert storage_setup_calls == []


@pytest.mark.parametrize(
    "redirect_url",
    [
        "http://169.254.169.254/latest/meta-data",
        "https://images.example.test/private.jpg",
        "https://media-amazon.com.evil.example/private.jpg",
        "https://user@m.media-amazon.com/private.jpg",
        "https://m.media-amazon.com/images/%253Cscript%253E.jpg",
        "https://m.media-amazon.com/images/%2563ookie%253Dsecret.jpg",
        "https://m.media-amazon.com/images/%250Aheader.jpg",
        "https://m.media-amazon.com/images/I/target._AC_US40_.jpg",
    ],
)
def test_governed_media_redirect_rejects_urls_outside_allowed_cdn(
    redirect_url: str,
) -> None:
    handler = asset_sync_handler._GovernedMediaRedirectHandler(
        ("media-amazon.com", "ssl-images-amazon.com")
    )
    request = asset_sync_handler.Request("https://m.media-amazon.com/images/I/source.jpg")

    with pytest.raises(ValueError, match="governed HTTPS media host"):
        handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            redirect_url,
        )


def test_governed_media_redirect_allows_https_redirect_within_cdn() -> None:
    handler = asset_sync_handler._GovernedMediaRedirectHandler(
        ("media-amazon.com", "ssl-images-amazon.com")
    )
    request = asset_sync_handler.Request("https://m.media-amazon.com/images/I/source.jpg")

    redirected = handler.redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        "https://images-na.ssl-images-amazon.com/images/I/target.jpg?signature=1",
    )

    assert redirected is not None
    assert redirected.full_url == (
        "https://images-na.ssl-images-amazon.com/images/I/target.jpg?signature=1"
    )


def test_governed_media_http_redirect_rejection_closes_response() -> None:
    class CloseTrackingResponse:
        closed = False

        def close(self) -> None:
            self.closed = True

    response = CloseTrackingResponse()
    handler = asset_sync_handler._GovernedMediaRedirectHandler(
        ("media-amazon.com", "ssl-images-amazon.com")
    )
    request = asset_sync_handler.Request("https://m.media-amazon.com/images/I/source.jpg")

    with pytest.raises(ValueError, match="governed HTTPS media host"):
        handler.http_error_302(
            request,
            response,
            302,
            "Found",
            {"location": "http://169.254.169.254/latest/meta-data"},
        )

    assert response.closed is True


@pytest.mark.parametrize("redirect_code", [301, 302, 303, 307, 308])
def test_governed_media_redirect_aliases_close_response_when_location_is_missing(
    redirect_code: int,
) -> None:
    class CloseTrackingResponse:
        close_count = 0

        def close(self) -> None:
            self.close_count += 1

    response = CloseTrackingResponse()
    handler = asset_sync_handler._GovernedMediaRedirectHandler(
        ("media-amazon.com", "ssl-images-amazon.com"),
        max_bytes=4,
    )
    request = asset_sync_handler.Request("https://m.media-amazon.com/images/I/source.jpg")

    result = getattr(handler, f"http_error_{redirect_code}")(
        request,
        response,
        redirect_code,
        "Found",
        {},
    )

    assert result is None
    assert response.close_count >= 1


def test_governed_media_redirect_closes_response_for_forbidden_scheme() -> None:
    class CloseTrackingResponse:
        close_count = 0

        def close(self) -> None:
            self.close_count += 1

    response = CloseTrackingResponse()
    handler = asset_sync_handler._GovernedMediaRedirectHandler(
        ("media-amazon.com", "ssl-images-amazon.com"),
        max_bytes=4,
    )
    request = asset_sync_handler.Request("https://m.media-amazon.com/images/I/source.jpg")

    with pytest.raises(HTTPError, match="Redirection to url"):
        handler.http_error_302(
            request,
            response,
            302,
            "Found",
            {"location": "file:///private/secret.jpg"},
        )

    assert response.close_count >= 1


def test_governed_media_redirect_closes_response_on_in_policy_loop() -> None:
    class CloseTrackingResponse:
        close_count = 0

        def close(self) -> None:
            self.close_count += 1

    target_url = "https://m.media-amazon.com/images/I/target.jpg"
    response = CloseTrackingResponse()
    handler = asset_sync_handler._GovernedMediaRedirectHandler(
        ("media-amazon.com", "ssl-images-amazon.com"),
        max_bytes=4,
    )
    request = asset_sync_handler.Request("https://m.media-amazon.com/images/I/source.jpg")
    request.redirect_dict = {target_url: handler.max_repeats}

    with pytest.raises(HTTPError, match="redirect error"):
        handler.http_error_302(
            request,
            response,
            302,
            "Found",
            {"location": target_url},
        )

    assert response.close_count >= 1


def test_governed_media_redirect_does_not_close_final_response() -> None:
    class IntermediateResponse:
        close_count = 0

        def read(self, size: int = -1) -> bytes:
            del size
            return b""

        def close(self) -> None:
            self.close_count += 1

    class FinalResponse:
        closed = False

        def close(self) -> None:
            self.closed = True

    class ParentOpener:
        def __init__(self, final_response: FinalResponse) -> None:
            self.final_response = final_response

        def open(self, request, *, timeout):
            assert request.full_url == "https://m.media-amazon.com/images/I/target.jpg"
            assert timeout == 7
            return self.final_response

    intermediate_response = IntermediateResponse()
    final_response = FinalResponse()
    handler = asset_sync_handler._GovernedMediaRedirectHandler(
        ("media-amazon.com", "ssl-images-amazon.com"),
        max_bytes=4,
    )
    handler.add_parent(ParentOpener(final_response))
    request = asset_sync_handler.Request("https://m.media-amazon.com/images/I/source.jpg")
    request.timeout = 7

    result = handler.http_error_302(
        request,
        intermediate_response,
        302,
        "Found",
        {"location": "https://m.media-amazon.com/images/I/target.jpg"},
    )

    assert result is final_response
    assert intermediate_response.close_count >= 1
    assert final_response.closed is False


@pytest.mark.parametrize("redirect_code", [301, 302, 303, 307, 308])
def test_governed_media_allowed_redirect_rejects_oversized_body_with_bounded_read(
    redirect_code: int,
) -> None:
    class CloseTrackingResponse:
        closed = False

        def __init__(self) -> None:
            self.read_sizes: list[int] = []

        def read(self, size: int = -1) -> bytes:
            self.read_sizes.append(size)
            return b"x" * size

        def close(self) -> None:
            self.closed = True

    response = CloseTrackingResponse()
    handler = asset_sync_handler._GovernedMediaRedirectHandler(
        ("media-amazon.com", "ssl-images-amazon.com"),
        max_bytes=4,
    )
    request = asset_sync_handler.Request("https://m.media-amazon.com/images/I/source.jpg")

    with pytest.raises(ValueError, match="redirect response exceeds"):
        getattr(handler, f"http_error_{redirect_code}")(
            request,
            response,
            redirect_code,
            "Found",
            {"location": "https://m.media-amazon.com/images/I/target.jpg"},
        )

    assert response.read_sizes == [5]
    assert response.closed is True


def test_media_asset_sync_uses_product_entity_key_for_download_path(monkeypatch, tmp_path) -> None:
    requested_urls: list[str] = []

    def fake_urlopen(request, timeout: int):
        del timeout
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
                        "entity_key": "product:1730964478199763166",
                        "media_role": "product_image",
                        "source_url": "https://cdn.example.com/main.webp",
                        "source_platform": "fastmoss",
                    }
                ],
            }
        )
    )

    assert result.status == "success"
    assert requested_urls == ["https://cdn.example.com/main.webp"]
    asset = result.result["synced_assets"][0]
    assert asset["entity_type"] == "product"
    assert asset["entity_external_id"] == "1730964478199763166"
    assert asset["product_id"] == "1730964478199763166"
    assert "/1730964478199763166/" in asset["local_path"]
    assert "unknown-product" not in asset["local_path"]


def test_media_asset_sync_reuses_duplicate_source_url_within_same_run(
    monkeypatch, tmp_path
) -> None:
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
    assert [asset["sync_state"] for asset in result.result["synced_assets"]] == [
        "linked_local",
        "reused_in_run",
    ]
    assert (
        result.result["synced_assets"][0]["object_key"]
        == result.result["synced_assets"][1]["object_key"]
    )
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


def test_media_asset_sync_can_require_referenced_assets_to_materialize(
    monkeypatch, tmp_path
) -> None:
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


def test_media_asset_sync_fails_when_object_storage_is_required_with_local_provider(
    tmp_path,
) -> None:
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


def test_media_asset_sync_materializes_amazon_media_with_stable_product_keys(
    monkeypatch,
    tmp_path,
) -> None:
    class FakeArtifactStore:
        provider_code = "minio"

        def __init__(self) -> None:
            self.uploads: list[dict[str, Any]] = []

        def upload_file(
            self,
            *,
            bucket,
            object_key,
            local_path,
            content_type,
            metadata=None,
        ):
            payload = local_path.read_bytes()
            self.uploads.append(
                {
                    "bucket": bucket,
                    "object_key": object_key,
                    "payload": payload,
                    "content_type": content_type,
                    "metadata": dict(metadata or {}),
                }
            )
            return StoredArtifact(
                bucket=bucket,
                object_key=object_key,
                etag="minio-etag-is-not-the-content-digest",
                size=len(payload),
                content_type=content_type,
                uri=f"s3://{bucket}/{object_key}",
            )

        def build_uri(self, *, bucket, object_key):
            return f"s3://{bucket}/{object_key}"

    class ForbiddenTKFactStore:
        def __init__(self, **kwargs):
            raise AssertionError(f"Amazon media must not open TKFactStore: {kwargs}")

    store = FakeArtifactStore()
    monkeypatch.setattr(asset_sync_handler, "create_store_from_settings", lambda settings: store)
    monkeypatch.setattr(asset_sync_handler, "TKFactStore", ForbiddenTKFactStore)

    class FakeOpener:
        def open(self, request, *, timeout):
            del request, timeout
            return _FakeResponse()

    monkeypatch.setattr(asset_sync_handler, "build_opener", lambda handler: FakeOpener())
    monkeypatch.setattr(
        asset_sync_handler,
        "urlopen",
        lambda *args, **kwargs: pytest.fail("Amazon/US downloads must use governed opener"),
    )

    source_bytes = b"fake-webp-bytes"
    content_digest = hashlib.sha256(source_bytes).hexdigest()
    common_payload = {
        "artifact_root": str(tmp_path / "artifacts"),
        "artifact_store_provider": "minio",
        "artifact_bucket": "runtime-artifacts",
        "artifact_object_prefix": "dev",
        "fact_db_url": "postgresql+psycopg://facts",
        "sync_referenced_files": True,
        "media_download_dir": str(tmp_path / "downloads"),
        "asset_refs": [
            {
                "entity_type": "product",
                "product_id": "B0CHILD001",
                "media_role": "main_image",
                "position": 0,
                "marketplace_code": "US",
                "source_platform": "amazon",
                "source_url": "https://m.media-amazon.com/images/I/main.webp",
            },
            {
                "entity_type": "product",
                "product_id": "B0CHILD001",
                "media_role": "gallery_image",
                "position": 2,
                "marketplace_code": "US",
                "source_platform": "amazon",
                "source_url": "https://images-na.ssl-images-amazon.com/images/I/gallery.webp",
            },
        ],
    }

    first = asset_sync_handler.media_asset_sync_handler(
        _context({**common_payload, "run_id": "amazon-run-1"})
    )
    second = asset_sync_handler.media_asset_sync_handler(
        _context({**common_payload, "run_id": "amazon-run-2"})
    )

    assert first.status == "success"
    assert second.status == "success"
    expected_keys = [
        (f"dev/product-media/amazon/us/B0CHILD001/main_image/{content_digest}.webp"),
        (f"dev/product-media/amazon/us/B0CHILD001/gallery_image/{content_digest}.webp"),
    ]
    assert [asset["object_key"] for asset in first.result["synced_assets"]] == expected_keys
    assert [asset["object_key"] for asset in second.result["synced_assets"]] == expected_keys
    assert [asset["position"] for asset in first.result["synced_assets"]] == [0, 2]
    assert [asset["source_platform"] for asset in first.result["synced_assets"]] == [
        "amazon",
        "amazon",
    ]
    assert [asset["marketplace_code"] for asset in first.result["synced_assets"]] == [
        "US",
        "US",
    ]
    assert all(
        asset["content_digest"] == content_digest
        and asset["size_bytes"] == len(source_bytes)
        and asset["asset_key"] == f"content_sha256:{content_digest}"
        and asset["remote_uri"] == f"s3://runtime-artifacts/{asset['object_key']}"
        and "artifact_uri_prefix" not in asset
        for asset in first.result["synced_assets"]
    )
    assert [ref["object_key"] for ref in first.result["artifact_refs"]] == expected_keys


def test_media_asset_sync_scopes_duplicate_refs_by_platform_and_amazon_role(
    monkeypatch,
    tmp_path,
) -> None:
    requested_urls: list[str] = []

    def fake_urlopen(request, timeout: int):
        del timeout
        requested_urls.append(request.full_url)
        return _FakeResponse()

    class FakeOpener:
        def open(self, request, *, timeout):
            return fake_urlopen(request, timeout)

    monkeypatch.setattr(asset_sync_handler, "urlopen", fake_urlopen)
    monkeypatch.setattr(asset_sync_handler, "build_opener", lambda handler: FakeOpener())
    monkeypatch.setattr(
        asset_sync_handler,
        "_create_fact_store",
        lambda payload, *, asset_refs, warnings: None,
    )

    shared_url = "https://m.media-amazon.com/images/I/shared.webp"
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
                        "product_id": "1730964478199763166",
                        "media_role": "main_image",
                        "source_platform": "tiktok",
                        "source_url": shared_url,
                    },
                    {
                        "entity_type": "product",
                        "product_id": "B0CHILD001",
                        "media_role": "main_image",
                        "source_platform": "amazon",
                        "marketplace_code": "US",
                        "source_url": shared_url,
                    },
                    {
                        "entity_type": "product",
                        "product_id": "B0CHILD001",
                        "media_role": "gallery_image",
                        "source_platform": "amazon",
                        "marketplace_code": "US",
                        "source_url": shared_url,
                    },
                ],
            }
        )
    )

    assert result.status == "success"
    assert requested_urls == [shared_url, shared_url, shared_url]
    assert len(result.result["artifact_refs"]) == 3
    object_keys = [asset["object_key"] for asset in result.result["synced_assets"]]
    assert object_keys[0].startswith("runs/job-media/assets/")
    assert object_keys[1].startswith("product-media/amazon/us/B0CHILD001/main_image/")
    assert object_keys[2].startswith("product-media/amazon/us/B0CHILD001/gallery_image/")
