from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import automation_business_scaffold.infrastructure.browser.browser_bridge as browser_bridge
from automation_business_scaffold.capabilities.browser import (
    amazon_product_fetch_handler as handler_module,
)
from automation_business_scaffold.capabilities.browser.amazon.product_page import (
    AmazonAccessBlockedError,
    AmazonIdentityMismatchError,
    extract_amazon_product_capture,
)
from automation_business_scaffold.capabilities.browser.amazon_product_fetch_handler import (
    amazon_product_browser_fetch_handler,
)
from automation_business_scaffold.contracts.handler.allowlist import (
    BROWSER_HANDLER_CODES,
    BROWSER_HANDLER_CONTRACTS,
)
from automation_business_scaffold.contracts.handler.browser import BOUND_BROWSER_HANDLERS
from automation_business_scaffold.contracts.handler.contract import (
    HandlerContext,
    HandlerError,
    HandlerResult,
)
from automation_business_scaffold.control_plane.executor import worker_dispatch
from automation_business_scaffold.control_plane.supervisor.execution_supervisor import (
    ExecutionProgressEvent,
    ExecutionSupervisorError,
    ExecutionSupervisorOutcome,
)
from automation_business_scaffold.domains.amazon.jobs.amazon_product_browser_fetch import (
    AMAZON_PRODUCT_BROWSER_FETCH_JOB,
)
from automation_business_scaffold.infrastructure.artifacts.artifact_store import StoredArtifact
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore
from automation_business_scaffold.models import ArtifactObjectRecord


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "amazon"
OBSERVED_AT = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
RUNTIME_REQUEST_ID = "1" * 32
RUNTIME_EXECUTION_ID = "2" * 32
STABLE_CAPTURE_RUN_ID = "3" * 64
RUNTIME_TARGET_DIGEST = "d" * 64


class FakeArtifactStore:
    provider_code = "fake"
    artifact_bucket = "artifacts"
    artifact_object_prefix = "dev"

    def __init__(self, *, fail_upload: bool = False) -> None:
        self.fail_upload = fail_upload
        self.uploads: dict[tuple[str, str], bytes] = {}

    def upload_file(
        self,
        *,
        bucket: str,
        object_key: str,
        local_path: Path,
        content_type: str,
        metadata=None,
    ) -> StoredArtifact:
        if self.fail_upload:
            raise RuntimeError("object store unavailable")
        payload = local_path.read_bytes()
        self.uploads[(bucket, object_key)] = payload
        return StoredArtifact(
            bucket=bucket,
            object_key=object_key,
            etag=hashlib.sha256(payload).hexdigest(),
            size=len(payload),
            content_type=content_type,
            uri=f"s3://{bucket}/{object_key}",
            metadata={"storage_backend": "fake"},
        )

    def build_uri(self, *, bucket: str, object_key: str) -> str:
        return f"s3://{bucket}/{object_key}"

    def read_bytes(
        self,
        *,
        bucket: str,
        object_key: str,
        max_bytes: int | None = None,
    ) -> bytes:
        payload = self.uploads[(bucket, object_key)]
        if max_bytes is not None and len(payload) > max_bytes:
            raise ValueError("object exceeds read limit")
        return payload


def _fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def _capture(
    name: str = "product_detail_child.html",
    *,
    requested_asin: str = "B0CHILD001",
    resolved_url: str = "https://www.amazon.com/dp/B0CHILD001",
) -> dict[str, object]:
    return extract_amazon_product_capture(
        _fixture(name),
        requested_asin=requested_asin,
        resolved_url=resolved_url,
        observed_at=OBSERVED_AT,
    )


def _context(
    *,
    asin: str = " b0child001 ",
    payload_overrides: dict[str, object] | None = None,
    store: FakeArtifactStore | None = None,
) -> HandlerContext:
    payload: dict[str, object] = {
        "requested_asin": asin,
        "source_record_id": "record-1",
        "run_id": "run-1",
        "artifact_bucket": "artifacts",
        "artifact_object_prefix": "dev",
    }
    payload.update(payload_overrides or {})
    metadata: dict[str, object] = {"observed_at": OBSERVED_AT}
    if store is not None:
        metadata["artifact_store"] = store
    return HandlerContext(
        request_id="request-1",
        job_id="execution-1",
        handler_code="amazon_product_browser_fetch",
        worker_type="browser_worker",
        runtime_table="task_execution",
        payload=payload,
        workflow_code="refresh_amazon_product_row_by_asin",
        stage_code="collect_amazon_product_detail",
        item_code="amazon_product_browser_fetch",
        metadata=metadata,
    )


def _success_collection(capture: dict[str, object] | None = None) -> dict[str, object]:
    capture = json.loads(json.dumps(capture or _capture()))
    media = capture.get("media", {})
    if isinstance(media, dict):
        media_items = [media.get("main_image"), *(media.get("gallery_images") or [])]
        for item in media_items:
            if not isinstance(item, dict):
                continue
            source_url = str(item.get("url") or "")
            if source_url.startswith("https://images.example.test/"):
                item["url"] = source_url.replace(
                    "https://images.example.test/",
                    "https://m.media-amazon.com/images/I/",
                    1,
                )
        evidence = capture.get("field_evidence", {})
        if isinstance(evidence, dict):
            if isinstance(evidence.get("media.main_image"), dict):
                evidence["media.main_image"]["value"] = media.get("main_image")
            if isinstance(evidence.get("media.gallery_images"), dict):
                evidence["media.gallery_images"]["value"] = media.get("gallery_images")
    return {
        "capture": capture,
        "html": _fixture("product_detail_child.html"),
        "resolved_url": "https://www.amazon.com/dp/B0CHILD001",
        "browser_target_digest": "target-digest",
        "browser_provider_name": "roxy",
        "stage_durations_ms": {"navigation": 12.5, "parse": 3.25},
        "screenshot_bytes": b"",
    }


def test_handler_is_allowlisted_bound_and_has_compact_job_contract() -> None:
    assert "amazon_product_browser_fetch" in BROWSER_HANDLER_CONTRACTS
    assert BOUND_BROWSER_HANDLERS["amazon_product_browser_fetch"] is (
        amazon_product_browser_fetch_handler
    )
    assert AMAZON_PRODUCT_BROWSER_FETCH_JOB.worker_type == "browser_worker"
    assert AMAZON_PRODUCT_BROWSER_FETCH_JOB.runtime_table == "task_execution"
    assert AMAZON_PRODUCT_BROWSER_FETCH_JOB.payload_contract.field_names(required_only=True) == (
        "requested_asin",
        "source_record_id",
        "run_id",
        "artifact_bucket",
        "artifact_object_prefix",
    )
    assert AMAZON_PRODUCT_BROWSER_FETCH_JOB.business_key_template == (
        "{source_record_id}:{requested_asin}"
    )
    assert "capture" not in AMAZON_PRODUCT_BROWSER_FETCH_JOB.result_contract.field_names()
    assert "html" not in AMAZON_PRODUCT_BROWSER_FETCH_JOB.result_contract.field_names()


def test_amazon_resource_lane_digest_uses_resolved_browser_target_key(monkeypatch) -> None:
    target = object()
    monkeypatch.setattr(
        browser_bridge,
        "resolve_browser_target",
        lambda *, profile_ref: target if profile_ref == "amazon-us" else None,
    )
    monkeypatch.setattr(
        browser_bridge,
        "build_target_key",
        lambda resolved: "roxy:workspace:profile" if resolved is target else "",
    )

    digest = browser_bridge.resolve_automation_browser_target_digest(profile_ref="amazon-us")

    assert digest == hashlib.sha256(b"roxy:workspace:profile").hexdigest()


def test_success_uploads_only_governed_normalized_capture_and_returns_compact_refs(
    monkeypatch,
) -> None:
    store = FakeArtifactStore()
    calls: list[dict[str, object]] = []

    def collect(**kwargs):
        calls.append(kwargs)
        return _success_collection()

    monkeypatch.setenv("AMAZON_US_BROWSER_PROFILE_REF", "amazon-us-profile")
    monkeypatch.setenv("DEFAULT_PROFILE_REF", "default-profile")
    monkeypatch.setattr(handler_module, "_collect_browser_page", collect)

    result = amazon_product_browser_fetch_handler(
        _context(
            store=store,
            payload_overrides={
                    "browser_profile_ref": "payload-must-be-ignored",
                    "browser_workspace_id": "workspace-must-be-ignored",
                    "browser_provider_token": "provider-token-must-be-ignored",
                    "minio_secret_key": "payload-secret-must-be-ignored",
            },
        )
    )

    assert result.status == "success"
    assert calls[0]["profile_ref"] == "amazon-us-profile"
    assert calls[0]["canonical_url"] == "https://www.amazon.com/dp/B0CHILD001"
    assert result.result["requested_asin"] == "B0CHILD001"
    assert result.result["resolved_asin"] == "B0CHILD001"
    assert result.result["browser_target_digest"] == "target-digest"
    assert set(result.result) == {
        "marketplace_code",
        "requested_asin",
        "resolved_asin",
        "canonical_url",
        "collection_status",
        "field_coverage",
        "normalized_capture_ref",
        "raw_capture_refs",
        "artifact_refs",
        "media_source_refs",
        "browser_target_digest",
        "browser_provider_name",
        "stage_durations_ms",
    }
    assert result.result["browser_provider_name"] == "roxy"
    assert result.result["stage_durations_ms"]["navigation"] == 12.5
    assert result.result["stage_durations_ms"]["parse"] == 3.25
    assert result.result["stage_durations_ms"]["artifact"] >= 0
    serialized_result = json.dumps(result.result, sort_keys=True)
    assert "field_evidence" not in serialized_result
    assert "secret-cookie-must-not-leak" not in serialized_result
    assert "payload-must-be-ignored" not in serialized_result
    assert "workspace-must-be-ignored" not in serialized_result
    assert "provider-token-must-be-ignored" not in serialized_result
    assert "payload-secret-must-be-ignored" not in serialized_result

    base = "dev/raw-captures/amazon/us/B0CHILD001/2026/07/14/run-1"
    normalized_ref = result.result["normalized_capture_ref"]
    for artifact_ref in result.result["raw_capture_refs"]:
        assert artifact_ref["request_id"] == "request-1"
        assert artifact_ref["execution_id"] == "execution-1"
        assert artifact_ref["run_id"] == "run-1"
    normalized_key = normalized_ref["object_key"]
    assert normalized_key == (f"{base}/{normalized_ref['content_digest']}/normalized.json")
    assert set(store.uploads) == {("artifacts", normalized_key)}
    normalized_bytes = store.uploads[("artifacts", normalized_key)]
    normalized = json.loads(normalized_bytes)
    assert normalized["profile_context"]["locale"] == "en_US"
    assert normalized["profile_context"]["currency"] == "USD"
    assert normalized["profile_context"]["profile_context_digest"]
    assert (
        hashlib.sha256(normalized_bytes).hexdigest()
        == (result.result["normalized_capture_ref"]["content_digest"])
    )
    assert result.result["raw_capture_refs"] == [normalized_ref]
    assert result.result["artifact_refs"] == [normalized_ref]
    assert result.result["field_coverage"]["percentage"] == 100.0
    assert result.result["media_source_refs"] == [
        {
            "source_url": "https://m.media-amazon.com/images/I/structured-main.jpg",
            "source_platform": "amazon",
            "marketplace_code": "US",
            "product_id": "B0CHILD001",
            "media_role": "main_image",
            "position": 0,
        },
        {
            "source_url": "https://m.media-amazon.com/images/I/structured-main.jpg",
            "source_platform": "amazon",
            "marketplace_code": "US",
            "product_id": "B0CHILD001",
            "media_role": "gallery_image",
            "position": 0,
        },
        {
            "source_url": "https://m.media-amazon.com/images/I/structured-gallery-1.jpg",
            "source_platform": "amazon",
            "marketplace_code": "US",
            "product_id": "B0CHILD001",
            "media_role": "gallery_image",
            "position": 1,
        },
        {
            "source_url": "https://m.media-amazon.com/images/I/structured-gallery-2.jpg",
            "source_platform": "amazon",
            "marketplace_code": "US",
            "product_id": "B0CHILD001",
            "media_role": "gallery_image",
            "position": 2,
        },
    ]


def test_raw_capture_uploads_are_content_addressed_across_same_run_retries(
    monkeypatch,
) -> None:
    store = FakeArtifactStore()
    monkeypatch.setenv("AMAZON_US_BROWSER_PROFILE_REF", "amazon-us-profile")
    monkeypatch.setattr(
        handler_module, "_collect_browser_page", lambda **kwargs: _success_collection()
    )

    first = amazon_product_browser_fetch_handler(_context(store=store))
    exact_retry = amazon_product_browser_fetch_handler(_context(store=store))

    assert first.status == "success"
    assert exact_retry.status == "success"
    first_refs = {ref["capture_kind"]: ref for ref in first.result["raw_capture_refs"]}
    retry_refs = {ref["capture_kind"]: ref for ref in exact_retry.result["raw_capture_refs"]}
    assert {kind: ref["object_key"] for kind, ref in first_refs.items()} == {
        kind: ref["object_key"] for kind, ref in retry_refs.items()
    }
    first_normalized_key = first_refs["normalized_capture"]["object_key"]
    first_normalized_bytes = store.uploads[("artifacts", first_normalized_key)]

    changed_capture = _capture()
    changed_capture["product"]["title"] = "Changed title in the same run"
    monkeypatch.setattr(
        handler_module,
        "_collect_browser_page",
        lambda **kwargs: _success_collection(changed_capture),
    )
    changed = amazon_product_browser_fetch_handler(_context(store=store))

    assert changed.status == "success"
    changed_refs = {ref["capture_kind"]: ref for ref in changed.result["raw_capture_refs"]}
    changed_normalized_key = changed_refs["normalized_capture"]["object_key"]
    assert changed_normalized_key != first_normalized_key
    assert store.uploads[("artifacts", first_normalized_key)] == first_normalized_bytes
    assert (
        json.loads(store.uploads[("artifacts", changed_normalized_key)])["product"]["title"]
        == "Changed title in the same run"
    )


def test_media_source_refs_strip_query_fragment_and_normalize_capture(monkeypatch) -> None:
    store = FakeArtifactStore()
    capture = _success_collection()["capture"]
    source_url = (
        "https://M.MEDIA-AMAZON.COM:443/images/I/main.jpg?tracking=must-not-cross-boundary#fragment"
    )
    capture["media"]["main_image"] = {"url": source_url}
    capture["media"]["gallery_images"] = []
    capture["field_evidence"]["media.main_image"]["value"] = {"url": source_url}
    capture["field_evidence"]["media.gallery_images"]["value"] = []
    monkeypatch.setenv("AMAZON_US_BROWSER_PROFILE_REF", "amazon-us-profile")
    monkeypatch.setattr(
        handler_module,
        "_collect_browser_page",
        lambda **kwargs: _success_collection(capture),
    )

    result = amazon_product_browser_fetch_handler(_context(store=store))

    expected_url = "https://m.media-amazon.com/images/I/main.jpg"
    assert result.status == "success"
    assert result.result["media_source_refs"] == [
        {
            "source_url": expected_url,
            "source_platform": "amazon",
            "marketplace_code": "US",
            "product_id": "B0CHILD001",
            "media_role": "main_image",
            "position": 0,
        }
    ]
    normalized_ref = result.result["normalized_capture_ref"]
    normalized = json.loads(store.uploads[("artifacts", normalized_ref["object_key"])])
    assert normalized["media"]["main_image"]["url"] == expected_url
    assert normalized["field_evidence"]["media.main_image"]["value"]["url"] == expected_url


def test_media_source_refs_preserve_same_asset_across_main_and_gallery_roles() -> None:
    capture = _success_collection()["capture"]
    source_url = "https://m.media-amazon.com/images/I/shared.jpg"
    capture["media"]["main_image"] = {"url": source_url}
    capture["media"]["gallery_images"] = [{"url": source_url}]
    capture["field_evidence"]["media.main_image"]["value"] = {"url": source_url}
    capture["field_evidence"]["media.gallery_images"]["value"] = [{"url": source_url}]

    assert handler_module._media_source_refs(capture) == [
        {
            "source_url": source_url,
            "source_platform": "amazon",
            "marketplace_code": "US",
            "product_id": "B0CHILD001",
            "media_role": "main_image",
            "position": 0,
        },
        {
            "source_url": source_url,
            "source_platform": "amazon",
            "marketplace_code": "US",
            "product_id": "B0CHILD001",
            "media_role": "gallery_image",
            "position": 0,
        },
    ]


def test_media_source_refs_preserve_same_asset_across_gallery_positions() -> None:
    capture = _success_collection()["capture"]
    source_url = "https://m.media-amazon.com/images/I/shared-gallery.jpg"
    capture["media"]["gallery_images"] = [{"url": source_url}, {"url": source_url}]
    capture["field_evidence"]["media.gallery_images"]["value"] = [
        {"url": source_url},
        {"url": source_url},
    ]

    gallery_refs = [
        ref
        for ref in handler_module._media_source_refs(capture)
        if ref["media_role"] == "gallery_image"
    ]

    assert gallery_refs == [
        {
            "source_url": source_url,
            "source_platform": "amazon",
            "marketplace_code": "US",
            "product_id": "B0CHILD001",
            "media_role": "gallery_image",
            "position": 0,
        },
        {
            "source_url": source_url,
            "source_platform": "amazon",
            "marketplace_code": "US",
            "product_id": "B0CHILD001",
            "media_role": "gallery_image",
            "position": 1,
        },
    ]


@pytest.mark.parametrize(
    "source_url",
    [
        "https://images.example.test/image.jpg",
        "https://localhost/image.jpg",
        "http://m.media-amazon.com/image.jpg",
        "https://user@m.media-amazon.com/image.jpg",
        "https://m.media-amazon.com:444/image.jpg",
        "https://media-amazon.com.evil.example/image.jpg",
    ],
)
def test_media_source_refs_reject_ungoverned_or_local_urls(source_url: str) -> None:
    capture = _success_collection()["capture"]
    capture["media"]["main_image"] = {"url": source_url}
    capture["media"]["gallery_images"] = []
    capture["field_evidence"]["media.main_image"]["value"] = {"url": source_url}
    capture["field_evidence"]["media.gallery_images"]["value"] = []

    assert handler_module._media_source_refs(capture) == []


def test_browser_capture_drops_ungoverned_media_and_marks_partial(monkeypatch) -> None:
    store = FakeArtifactStore()
    capture = _success_collection()["capture"]
    source_url = "https://localhost/private-image.jpg"
    valid_gallery_url = "https://m.media-amazon.com/images/I/valid-gallery.jpg"
    capture["media"]["main_image"] = {"url": source_url}
    capture["media"]["gallery_images"] = [
        {"url": valid_gallery_url},
        {"url": source_url},
    ]
    capture["field_evidence"]["media.main_image"]["value"] = {"url": source_url}
    capture["field_evidence"]["media.gallery_images"]["value"] = [
        {"url": valid_gallery_url},
        {"url": source_url},
    ]
    monkeypatch.setenv("AMAZON_US_BROWSER_PROFILE_REF", "amazon-us-profile")
    monkeypatch.setattr(
        handler_module,
        "_collect_browser_page",
        lambda **kwargs: _success_collection(capture),
    )

    result = amazon_product_browser_fetch_handler(_context(store=store))

    assert result.status == "partial_success"
    assert result.result["collection_status"] == "partial_success"
    assert result.result["media_source_refs"] == [
        {
            "source_url": valid_gallery_url,
            "source_platform": "amazon",
            "marketplace_code": "US",
            "product_id": "B0CHILD001",
            "media_role": "gallery_image",
            "position": 0,
        }
    ]
    normalized_ref = result.result["normalized_capture_ref"]
    normalized = json.loads(store.uploads[("artifacts", normalized_ref["object_key"])])
    assert normalized["media"]["main_image"] is None
    assert normalized["field_evidence"]["media.main_image"] == {
        "value": None,
        "status": "missing",
        "source_kind": None,
        "source_locator": None,
        "confidence": 0.0,
    }
    assert normalized["media"]["gallery_images"] == [{"url": valid_gallery_url}]
    assert normalized["field_evidence"]["media.gallery_images"]["value"] == [
        {"url": valid_gallery_url}
    ]
    assert normalized["field_evidence"]["media.gallery_images"]["status"] == "observed"


def test_html_sanitizer_removes_state_and_account_secrets_but_keeps_product_evidence() -> None:
    html = """
    <html><body>
      <div id="nav-link-accountList">Private Customer Name</div>
      <span id="glow-ingress-line2">Private Street Address</span>
      <p id="shippingAddress">Jane Doe, 123 Main St</p>
      <h1 id="productTitle">Safe Product Title</h1>
      <script id="amazon-product-state" type="application/json">
        {
          "asin": "B0CHILD001",
          "product": {"title": "Safe Product Title"},
          "accessToken": "private-access-token",
          "cookie": "private-cookie"
        }
      </script>
    </body></html>
    """

    sanitized = handler_module._sanitize_amazon_html(html)

    assert "Safe Product Title" in sanitized
    assert "B0CHILD001" in sanitized
    assert "Private Customer Name" not in sanitized
    assert "Private Street Address" not in sanitized
    assert "Jane Doe, 123 Main St" not in sanitized
    assert "private-access-token" not in sanitized
    assert "private-cookie" not in sanitized


def test_real_page_collection_uses_browser_bridge_and_does_not_screenshot_success(
    monkeypatch,
) -> None:
    class Page:
        url = "https://www.amazon.com/dp/B0CHILD001"

        def __init__(self) -> None:
            self.goto_calls: list[tuple[str, str, int]] = []
            self.screenshot_calls = 0
            self.ready_selector = ""
            self.evaluate_scripts: list[str] = []

        def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
            self.goto_calls.append((url, wait_until, timeout))

        def wait_for_load_state(self, state: str, *, timeout: int) -> None:
            assert state == "domcontentloaded"
            assert timeout == 5000

        def wait_for_selector(self, selector: str, *, timeout: int) -> None:
            assert "#productTitle" in selector
            assert timeout == 5000
            self.ready_selector = selector

        def evaluate(self, script: str) -> None:
            self.evaluate_scripts.append(script)

        def content(self) -> str:
            return _fixture("product_detail_child.html")

        def screenshot(self, *, full_page: bool) -> bytes:
            self.screenshot_calls += 1
            return b"unexpected"

    page = Page()
    navigate_calls: list[tuple[str, str, int]] = []

    class AutomationPage:
        def navigate(
            self,
            url: str,
            *,
            wait_until: str,
            timeout_ms: int,
        ) -> None:
            navigate_calls.append((url, wait_until, timeout_ms))

    @contextmanager
    def open_page(*, profile_ref: str):
        assert profile_ref == "amazon-us-profile"
        yield SimpleNamespace(
            page=AutomationPage(),
            raw_page=page,
            target_key="private-target-key",
            provider_name="chrome",
        )

    monkeypatch.setattr(handler_module, "open_automation_page", open_page)

    collection = handler_module._collect_browser_page(
        requested_asin="B0CHILD001",
        canonical_url="https://www.amazon.com/dp/B0CHILD001",
        profile_ref="amazon-us-profile",
        observed_at=OBSERVED_AT,
        timeout_ms=5000,
    )

    assert collection["capture"]["resolved_asin"] == "B0CHILD001"
    assert navigate_calls == [("https://www.amazon.com/dp/B0CHILD001", "domcontentloaded", 5000)]
    assert page.goto_calls == []
    assert collection["browser_target_digest"] == hashlib.sha256(b"private-target-key").hexdigest()
    assert collection["browser_provider_name"] == "chrome"
    assert set(collection["stage_durations_ms"]) == {"navigation", "parse"}
    assert all(value >= 0 for value in collection["stage_durations_ms"].values())
    ready_tokens = {token.strip() for token in page.ready_selector.split(",")}
    assert "title" not in ready_tokens
    assert {
        "#productTitle",
        "#availability",
        "#outOfStock",
        "#productDetails_feature_div",
        "form[action*='validateCaptcha']",
        "#captchacharacters",
    } <= ready_tokens
    assert len(page.evaluate_scripts) == 3
    assert all("scrollTo" in script for script in page.evaluate_scripts)
    assert all("while" not in script for script in page.evaluate_scripts)
    assert [
        checkpoint
        for checkpoint in (1200, 2800, 4800)
        if any(str(checkpoint) in script for script in page.evaluate_scripts)
    ] == [1200, 2800, 4800]
    assert "target_key" not in collection
    assert page.screenshot_calls == 0


def test_wait_for_amazon_page_clicks_at_most_one_visible_details_toggle() -> None:
    class Toggle:
        def __init__(self) -> None:
            self.first = self
            self.click_count = 0

        def is_visible(self) -> bool:
            return True

        def click(self, *, timeout: int) -> None:
            assert timeout == 2000
            self.click_count += 1
            raise RuntimeError("detached after click")

    class Page:
        def __init__(self) -> None:
            self.toggle = Toggle()
            self.locator_selector = ""
            self.scroll_count = 0
            self.waits: list[int] = []

        def wait_for_load_state(self, state: str, *, timeout: int) -> None:
            assert state == "domcontentloaded"
            assert timeout == 5000

        def wait_for_selector(self, selector: str, *, timeout: int) -> None:
            del selector, timeout

        def evaluate(self, script: str) -> None:
            assert "scrollTo" in script
            self.scroll_count += 1

        def wait_for_timeout(self, timeout_ms: int) -> None:
            self.waits.append(timeout_ms)

        def locator(self, selector: str) -> Toggle:
            self.locator_selector = selector
            return self.toggle

    page = Page()

    handler_module._wait_for_amazon_page(page, timeout_ms=5000)

    assert page.scroll_count == 3
    assert page.waits == [150, 150, 150]
    assert "#productDetails_feature_div" in page.locator_selector
    assert "#detailBullets_feature_div" in page.locator_selector
    assert all(
        "aria-expanded='false'" in selector
        for selector in page.locator_selector.split(",")
    )
    assert page.toggle.click_count == 1


def test_wait_for_amazon_page_uses_framework_humanized_scroll_when_available() -> None:
    class AutomationPage:
        def __init__(self) -> None:
            self.scrolls: list[int] = []

        def scroll_by(self, delta_y: int) -> None:
            self.scrolls.append(delta_y)

    class Page:
        def __init__(self) -> None:
            self.evaluate_calls = 0

        def wait_for_load_state(self, state: str, *, timeout: int) -> None:
            assert state == "domcontentloaded"
            assert timeout == 5000

        def wait_for_selector(self, selector: str, *, timeout: int) -> None:
            del selector, timeout

        def evaluate(self, script: str) -> None:
            del script
            self.evaluate_calls += 1

        def locator(self, selector: str):
            del selector
            raise RuntimeError("details toggle is unavailable")

    page = Page()
    automation_page = AutomationPage()

    handler_module._wait_for_amazon_page(
        page,
        timeout_ms=5000,
        automation_page=automation_page,
    )

    assert automation_page.scrolls == [1200, 1600, 2000]
    assert page.evaluate_calls == 0


def test_screenshot_capture_fails_closed_when_sensitive_masking_fails() -> None:
    class Page:
        screenshot_calls = 0

        def evaluate(self, script: str) -> None:
            raise RuntimeError("page is no longer scriptable")

        def screenshot(self, *, full_page: bool) -> bytes:
            self.screenshot_calls += 1
            return b"unsafe-full-page"

    page = Page()

    assert handler_module._page_screenshot(page) == b""
    assert page.screenshot_calls == 0


def test_natural_same_origin_json_response_is_used_but_not_persisted_separately(
    monkeypatch,
) -> None:
    class Response:
        def __init__(
            self,
            *,
            url: str,
            payload: dict[str, object],
            content_type: str = "application/json",
            resource_type: str = "xhr",
            content_length: int | None = None,
        ) -> None:
            self.url = url
            self.request = SimpleNamespace(resource_type=resource_type)
            self._body = json.dumps(payload).encode()
            self.headers = {
                "content-type": content_type,
                "content-length": str(
                    len(self._body) if content_length is None else content_length
                ),
                "set-cookie": "must-not-be-persisted",
            }

        def body(self) -> bytes:
            return self._body

    class Page:
        url = "https://www.amazon.com/dp/B0CHILD001"

        def __init__(self) -> None:
            self.events: list[str] = []
            self.response_listener = None

        def on(self, event: str, listener) -> None:
            assert event == "response"
            self.events.append("listener:on")
            self.response_listener = listener

        def remove_listener(self, event: str, listener) -> None:
            assert event == "response"
            assert listener is self.response_listener
            self.events.append("listener:off")
            self.response_listener = None

        def goto(self, *args, **kwargs) -> None:
            self.events.append("goto")
            assert self.response_listener is not None

        def wait_for_timeout(self, timeout_ms: int) -> None:
            if timeout_ms == handler_module._NETWORK_SETTLE_MS:
                self.events.append("network:settle")
            else:
                assert timeout_ms == 150
                self.events.append("scroll:settle")
            for response in (
                Response(
                    url="https://evil.example/product.json",
                    payload={
                        "asin": "B0CHILD001",
                        "product": {"title": "Cross-origin title"},
                    },
                ),
                Response(
                    url="https://www.amazon.com/product.txt",
                    content_type="application/notjson",
                    payload={
                        "asin": "B0CHILD001",
                        "product": {"title": "Non-JSON title"},
                    },
                ),
                Response(
                    url="https://www.amazon.com/too-large.json",
                    content_length=handler_module._MAX_NETWORK_RESPONSE_BYTES + 1,
                    payload={
                        "asin": "B0CHILD001",
                        "product": {"title": "Oversized title"},
                    },
                ),
                Response(
                    url="https://www.amazon.com/wrong.json",
                    payload={
                        "asin": "B0WRONG001",
                        "product": {"title": "Wrong-ASIN title"},
                    },
                ),
                Response(
                    url="https://www.amazon.com/non-finite.json",
                    payload={
                        "asin": "B0CHILD001",
                        "commerce": {"rating": float("nan")},
                    },
                ),
                Response(
                    url="https://www.amazon.com/gp/aod/ajax?token=secret",
                    payload={
                        "asin": "B0CHILD001",
                        "product": {"title": "Network response title"},
                        "commerce": {"featuredOffer": {"priceAmount": "$28.50"}},
                        "media": {
                            "images": [
                                "https://m.media-amazon.com/images/I/network.jpg"
                                "?token=media-query-secret#fragment"
                            ]
                        },
                        "cookie": "secret-cookie",
                        "authorization": "Bearer secret-token",
                    },
                ),
            ):
                self.response_listener(response)

        def content(self) -> str:
            return (
                _fixture("product_detail_child.html")
                .replace(
                    "https://images.example.test/",
                    "https://m.media-amazon.com/images/I/",
                )
                .replace(
                    '<script type="application/ld+json">',
                    '<script type="application/ignored+json">',
                )
                .replace(
                    '<script id="amazon-product-state" type="application/json">',
                    '<script id="ignored-state" type="application/json">',
                )
            )

        def screenshot(self, *, full_page: bool) -> bytes:
            raise AssertionError("successful collection must not take a screenshot")

    page = Page()

    @contextmanager
    def open_page(*, profile_ref: str):
        assert profile_ref == "amazon-us-profile"
        yield SimpleNamespace(
            page=page,
            raw_page=page,
            target_key="target-key",
        )

    store = FakeArtifactStore()
    monkeypatch.setenv("AMAZON_US_BROWSER_PROFILE_REF", "amazon-us-profile")
    monkeypatch.setattr(handler_module, "open_automation_page", open_page)

    result = amazon_product_browser_fetch_handler(_context(store=store))

    assert result.status == "success"
    assert page.events == ["listener:on", "goto", "network:settle", "listener:off"]
    assert [ref["capture_kind"] for ref in result.result["artifact_refs"]] == [
        "normalized_capture",
    ]
    assert result.summary["artifact_count"] == 1
    serialized_result = json.dumps(result.result, sort_keys=True)
    assert "Network response title" not in serialized_result
    assert "secret" not in serialized_result
    assert "media-query-secret" not in serialized_result

    normalized_ref = result.result["artifact_refs"][0]
    normalized = json.loads(store.uploads[("artifacts", normalized_ref["object_key"])])
    assert normalized["product"]["title"] == "Network response title"
    assert normalized["commerce"]["featured_offer"]["price_amount"] == 28.5
    assert normalized["media"]["gallery_images"] == [
        {"url": "https://m.media-amazon.com/images/I/network.jpg"}
    ]
    evidence = normalized["field_evidence"]["product.title"]
    assert evidence["source_kind"] == "same_origin_response"
    assert evidence["source_locator"].startswith("/gp/aod/ajax#sha256=")
    assert len(store.uploads) == 1
    serialized_normalized = json.dumps(normalized, sort_keys=True)
    assert "set-cookie" not in serialized_normalized
    assert "must-not-be-persisted" not in serialized_normalized
    assert "media-query-secret" not in serialized_normalized
    assert "Cross-origin title" not in serialized_normalized
    assert "Non-JSON title" not in serialized_normalized
    assert "Oversized title" not in serialized_normalized
    assert "Wrong-ASIN title" not in serialized_normalized


def test_response_observer_accepts_only_exact_origin_fetch_or_xhr_json() -> None:
    class Response:
        def __init__(
            self,
            *,
            url: str,
            resource_type: str = "xhr",
            content_type: str = "application/json",
        ) -> None:
            self.url = url
            self.request = SimpleNamespace(resource_type=resource_type)
            self.headers = {"content-type": content_type}

        def body(self) -> bytes:
            return b'{"asin":"B0CHILD001","product":{"title":"Accepted"}}'

    class Page:
        def __init__(self) -> None:
            self.listener = None

        def on(self, event: str, listener) -> None:
            assert event == "response"
            self.listener = listener

        def remove_listener(self, event: str, listener) -> None:
            assert event == "response"
            assert listener is self.listener
            self.listener = None

    page = Page()
    observations: list[dict[str, object]] = []
    stop = handler_module._observe_same_origin_product_responses(
        page,
        canonical_url="https://www.amazon.com/dp/B0CHILD001",
        observations=observations,
    )
    assert page.listener is not None

    for response in (
        Response(
            url="https://www.amazon.com:443/fetch.json?token=discarded",
            resource_type="fetch",
            content_type="application/vnd.amazon+json; charset=utf-8",
        ),
        Response(url="http://www.amazon.com/wrong-scheme.json"),
        Response(url="https://www.amazon.com:444/wrong-port.json"),
        Response(url="https://api.amazon.com/wrong-subdomain.json"),
        Response(url="https://www.amazon.com/document.json", resource_type="document"),
        Response(url="https://www.amazon.com/script.json", resource_type="script"),
        Response(url="https://www.amazon.com/not-json", content_type="text/plain"),
    ):
        page.listener(response)

    assert observations == [
        {
            "source_path": "/fetch.json",
            "payload": {"asin": "B0CHILD001", "product": {"title": "Accepted"}},
        }
    ]
    stop()
    assert page.listener is None


def test_response_observer_enforces_count_and_byte_limits() -> None:
    class Response:
        def __init__(
            self,
            *,
            index: int,
            padding_bytes: int = 0,
            content_length: int | None = None,
            payload: dict[str, object] | None = None,
        ) -> None:
            self.url = f"https://www.amazon.com/response-{index}.json"
            self.request = SimpleNamespace(resource_type="xhr")
            self._body = json.dumps(
                payload
                if payload is not None
                else {
                    "asin": "B0CHILD001",
                    "product": {"title": f"Response {index}"},
                    "padding": "x" * padding_bytes,
                }
            ).encode()
            self.headers = {"content-type": "application/json"}
            if content_length is not None:
                self.headers["content-length"] = str(content_length)

        def body(self) -> bytes:
            return self._body

    class Page:
        def __init__(self) -> None:
            self.listener = None

        def on(self, event: str, listener) -> None:
            assert event == "response"
            self.listener = listener

        def remove_listener(self, event: str, listener) -> None:
            assert event == "response"
            assert listener is self.listener
            self.listener = None

    def listen(
        canonical_url: str = "https://www.amazon.com/dp/B0CHILD001",
    ) -> tuple[Page, list[dict[str, object]], object]:
        page = Page()
        observations: list[dict[str, object]] = []
        stop = handler_module._observe_same_origin_product_responses(
            page,
            canonical_url=canonical_url,
            observations=observations,
        )
        return page, observations, stop

    count_page, count_observations, count_stop = listen()
    assert count_page.listener is not None
    for index in range(handler_module._MAX_NETWORK_RESPONSE_COUNT + 1):
        count_page.listener(Response(index=index))
    assert len(count_observations) == handler_module._MAX_NETWORK_RESPONSE_COUNT
    assert count_observations[0]["source_path"] == "/response-1.json"
    assert count_observations[-1]["source_path"] == "/response-8.json"
    count_stop()

    total_page, total_observations, total_stop = listen()
    assert total_page.listener is not None
    for index in range(3):
        total_page.listener(Response(index=index, padding_bytes=200 * 1024))
    assert len(total_observations) == 2
    assert [item["source_path"] for item in total_observations] == [
        "/response-1.json",
        "/response-2.json",
    ]
    total_stop()

    starvation_page, starvation_observations, starvation_stop = listen()
    assert starvation_page.listener is not None
    for index in range(handler_module._MAX_NETWORK_RESPONSE_COUNT):
        starvation_page.listener(
            Response(
                index=index,
                payload={
                    "asin": f"B0WRONG00{index}",
                    "product": {"title": f"Wrong product {index}"},
                },
            )
        )
    starvation_page.listener(
        Response(
            index=99,
            payload={
                "asin": "B0CHILD001",
                "product": {"title": "Latest real product response"},
            },
        )
    )
    assert len(starvation_observations) == 1
    assert (
        handler_module.extract_amazon_network_product_data(
            starvation_observations,
            expected_asin="B0CHILD001",
        )["product"]["title"]
        == "Latest real product response"
    )
    starvation_stop()

    reverse_page, reverse_observations, reverse_stop = listen()
    assert reverse_page.listener is not None
    reverse_page.listener(
        Response(
            index=99,
            payload={
                "asin": "B0CHILD001",
                "product": {"title": "Early real product response"},
            },
        )
    )
    for index in range(handler_module._MAX_NETWORK_RESPONSE_COUNT):
        reverse_page.listener(
            Response(
                index=index,
                payload={
                    "asin": f"B0WRONG00{index}",
                    "product": {"title": f"Wrong product {index}"},
                },
            )
        )
    assert len(reverse_observations) == 1
    assert (
        handler_module.extract_amazon_network_product_data(
            reverse_observations,
            expected_asin="B0CHILD001",
        )["product"]["title"]
        == "Early real product response"
    )
    reverse_stop()

    parent_page, parent_observations, parent_stop = listen("https://www.amazon.com/dp/B0PARENT01")
    parent_page.url = "https://www.amazon.com/dp/B0CHILD001"
    assert parent_page.listener is not None
    parent_page.listener(
        Response(
            index=100,
            payload={
                "asin": "B0CHILD001",
                "product": {"title": "Resolved child response"},
            },
        )
    )
    assert len(parent_observations) == 1
    parent_stop()

    body_page, body_observations, body_stop = listen()
    assert body_page.listener is not None
    body_page.listener(
        Response(
            index=0,
            padding_bytes=handler_module._MAX_NETWORK_RESPONSE_BYTES,
            content_length=1,
        )
    )
    assert body_observations == []
    body_stop()


def test_real_page_collection_screenshots_blocked_redirect(monkeypatch) -> None:
    class Page:
        url = "https://www.amazon.com/errors/validateCaptcha"

        def __init__(self) -> None:
            self.scripts: list[str] = []

        def goto(self, *args, **kwargs) -> None:
            return None

        def content(self) -> str:
            return _fixture("product_detail_blocked.html")

        def evaluate(self, script: str) -> None:
            self.scripts.append(script)

        def screenshot(self, *, full_page: bool) -> bytes:
            assert full_page is True
            return b"blocked-screenshot"

    page = Page()

    @contextmanager
    def open_page(*, profile_ref: str):
        yield SimpleNamespace(page=page, target_key="target-key")

    monkeypatch.setattr(handler_module, "open_automation_page", open_page)

    collection = handler_module._collect_browser_page(
        requested_asin="B0BLOCK001",
        canonical_url="https://www.amazon.com/dp/B0BLOCK001",
        profile_ref="amazon-us-profile",
        observed_at=OBSERVED_AT,
        timeout_ms=5000,
    )

    assert isinstance(collection["error"], AmazonAccessBlockedError)
    assert collection["error"].error_code == "captcha_required"
    assert collection["screenshot_bytes"] == b"blocked-screenshot"
    assert any("nav-link-accountList" in script for script in page.scripts)


@pytest.mark.parametrize(
    ("http_status", "expected_error_code"),
    [(429, "rate_limited"), (503, "transient_page_failure")],
)
def test_navigation_http_failures_are_retryable_and_remain_local_only(
    monkeypatch,
    http_status: int,
    expected_error_code: str,
) -> None:
    class Page:
        url = "https://www.amazon.com/dp/B0CHILD001"

        def goto(self, *args, **kwargs):
            return SimpleNamespace(status=http_status)

        def content(self) -> str:
            return "<html><body><h1>Temporary Amazon response</h1></body></html>"

        def evaluate(self, script: str) -> None:
            assert "visibility = 'hidden'" in script

        def screenshot(self, *, full_page: bool) -> bytes:
            assert full_page is True
            return b"temporary-response-screenshot"

    @contextmanager
    def open_page(*, profile_ref: str):
        assert profile_ref == "amazon-us-profile"
        yield SimpleNamespace(page=Page(), target_key="target-key")

    store = FakeArtifactStore()
    monkeypatch.setenv("AMAZON_US_BROWSER_PROFILE_REF", "amazon-us-profile")
    monkeypatch.setattr(handler_module, "open_automation_page", open_page)

    result = amazon_product_browser_fetch_handler(_context(store=store))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == expected_error_code
    assert result.error.retryable is True
    assert result.result["artifact_refs"] == []
    assert store.uploads == {}


@pytest.mark.parametrize("content_mode", ["empty", "error"])
def test_empty_or_unreadable_page_content_is_a_retryable_technical_failure(
    monkeypatch,
    content_mode: str,
) -> None:
    class Page:
        url = "https://www.amazon.com/dp/B0CHILD001"

        def goto(self, *args, **kwargs) -> None:
            return None

        def content(self) -> str:
            if content_mode == "error":
                raise RuntimeError("content channel closed")
            return ""

        def evaluate(self, script: str) -> None:
            assert "visibility = 'hidden'" in script

        def screenshot(self, *, full_page: bool) -> bytes:
            return b"page-read-failure"

    @contextmanager
    def open_page(*, profile_ref: str):
        yield SimpleNamespace(page=Page(), target_key="target-key")

    store = FakeArtifactStore()
    monkeypatch.setenv("AMAZON_US_BROWSER_PROFILE_REF", "amazon-us-profile")
    monkeypatch.setattr(handler_module, "open_automation_page", open_page)

    result = amazon_product_browser_fetch_handler(_context(store=store))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "transient_page_failure"
    assert result.error.retryable is True
    assert "normalized_capture_ref" not in result.result
    assert result.result["artifact_refs"] == []
    assert store.uploads == {}


def test_default_profile_is_used_and_payload_profile_is_ignored(monkeypatch) -> None:
    store = FakeArtifactStore()
    seen: list[str] = []
    monkeypatch.delenv("AMAZON_US_BROWSER_PROFILE_REF", raising=False)
    monkeypatch.setenv("DEFAULT_PROFILE_REF", "framework-default")
    monkeypatch.setattr(
        handler_module,
        "_collect_browser_page",
        lambda **kwargs: seen.append(kwargs["profile_ref"]) or _success_collection(),
    )

    result = amazon_product_browser_fetch_handler(
        _context(
            store=store,
            payload_overrides={"browser_profile_ref": "untrusted-payload-profile"},
        )
    )

    assert result.status == "success"
    assert seen == ["framework-default"]


def test_missing_profile_or_object_storage_fails_before_opening_browser(monkeypatch) -> None:
    opened: list[bool] = []
    monkeypatch.delenv("AMAZON_US_BROWSER_PROFILE_REF", raising=False)
    monkeypatch.delenv("DEFAULT_PROFILE_REF", raising=False)
    monkeypatch.setattr(
        handler_module,
        "_collect_browser_page",
        lambda **kwargs: opened.append(True) or _success_collection(),
    )

    missing_profile = amazon_product_browser_fetch_handler(_context(store=FakeArtifactStore()))

    assert missing_profile.status == "failed"
    assert missing_profile.error is not None
    assert missing_profile.error.error_code == "browser_profile_unavailable"
    assert opened == []

    monkeypatch.setenv("AMAZON_US_BROWSER_PROFILE_REF", "amazon-us-profile")
    monkeypatch.setenv("BUSINESS_EXECUTION_CONTROL_ARTIFACT_STORE_PROVIDER", "local")
    monkeypatch.setenv("EXECUTION_CONTROL_ARTIFACT_STORE_PROVIDER", "local")
    missing_store = amazon_product_browser_fetch_handler(_context())

    assert missing_store.status == "failed"
    assert missing_store.error is not None
    assert missing_store.error.error_code == "object_storage_required"
    assert opened == []


def test_invalid_asin_fails_without_browser_or_artifact_side_effects(monkeypatch) -> None:
    store = FakeArtifactStore()
    opened: list[bool] = []
    monkeypatch.setenv("AMAZON_US_BROWSER_PROFILE_REF", "amazon-us-profile")
    monkeypatch.setattr(
        handler_module,
        "_collect_browser_page",
        lambda **kwargs: opened.append(True) or _success_collection(),
    )

    result = amazon_product_browser_fetch_handler(_context(asin="not-an-asin", store=store))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "invalid_asin"
    assert opened == []
    assert store.uploads == {}


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_error_code"),
    [
        (
            AmazonAccessBlockedError("robot check", error_code="captcha_required"),
            "blocked",
            "captcha_required",
        ),
        (
            AmazonIdentityMismatchError("unrelated ASIN"),
            "failed",
            "identity_mismatch",
        ),
    ],
)
def test_only_blocked_terminal_page_errors_upload_one_screenshot(
    monkeypatch,
    error,
    expected_status: str,
    expected_error_code: str,
) -> None:
    store = FakeArtifactStore()
    monkeypatch.setenv("AMAZON_US_BROWSER_PROFILE_REF", "amazon-us-profile")
    monkeypatch.setattr(
        handler_module,
        "_collect_browser_page",
        lambda **kwargs: {
            "capture": None,
            "html": _fixture("product_detail_blocked.html")
            + '<div data-token="secret-token-must-not-leak"></div>',
            "resolved_url": "https://www.amazon.com/errors/validateCaptcha",
            "browser_target_digest": RUNTIME_TARGET_DIGEST,
            "screenshot_bytes": b"png-evidence",
            "error": error,
        },
    )

    result = amazon_product_browser_fetch_handler(_context(asin="B0BLOCK001", store=store))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == expected_error_code
    assert result.error.retryable is False
    assert result.summary["collection_status"] == expected_status
    assert "normalized_capture_ref" not in result.result
    expected_kinds = ["screenshot"] if expected_status == "blocked" else []
    assert [ref["capture_kind"] for ref in result.result["artifact_refs"]] == expected_kinds
    for ref in result.result["artifact_refs"]:
        assert ref["object_key"].split("/")[-2] == ref["content_digest"]
        assert ref["request_id"] == "request-1"
        assert ref["execution_id"] == "execution-1"
        assert ref["run_id"] == "run-1"
    if expected_status == "blocked":
        assert len(store.uploads) == 1
        assert next(iter(store.uploads))[1].endswith("page.png")
    else:
        assert store.uploads == {}


def test_terminal_page_error_without_required_screenshot_is_retryable(
    monkeypatch,
) -> None:
    store = FakeArtifactStore()
    monkeypatch.setenv("AMAZON_US_BROWSER_PROFILE_REF", "amazon-us-profile")
    monkeypatch.setattr(
        handler_module,
        "_collect_browser_page",
        lambda **kwargs: {
            "capture": None,
            "html": _fixture("product_detail_blocked.html"),
            "resolved_url": "https://www.amazon.com/errors/validateCaptcha",
            "browser_target_digest": "target-digest",
            "screenshot_bytes": b"",
            "error": AmazonAccessBlockedError(
                "robot check",
                error_code="captcha_required",
            ),
        },
    )

    result = amazon_product_browser_fetch_handler(_context(asin="B0BLOCK001", store=store))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "required_failure_evidence_missing"
    assert result.error.retryable is True
    assert store.uploads == {}


def test_unavailable_is_success_and_parent_redirect_is_partial(monkeypatch) -> None:
    store = FakeArtifactStore()
    monkeypatch.setenv("AMAZON_US_BROWSER_PROFILE_REF", "amazon-us-profile")
    unavailable = _capture(
        "product_detail_unavailable.html",
        requested_asin="B0UNAVL001",
        resolved_url="https://www.amazon.com/dp/B0UNAVL001",
    )
    monkeypatch.setattr(
        handler_module,
        "_collect_browser_page",
        lambda **kwargs: _success_collection(unavailable),
    )

    unavailable_result = amazon_product_browser_fetch_handler(
        _context(asin="B0UNAVL001", store=store)
    )

    assert unavailable_result.status == "success"
    assert unavailable_result.result["collection_status"] == "unavailable"

    parent_capture = _capture(
        requested_asin="B0PARENT01",
        resolved_url="https://www.amazon.com/dp/B0CHILD001",
    )
    monkeypatch.setattr(
        handler_module,
        "_collect_browser_page",
        lambda **kwargs: _success_collection(parent_capture),
    )
    parent_result = amazon_product_browser_fetch_handler(_context(asin="B0PARENT01", store=store))

    assert parent_result.status == "partial_success"
    assert parent_result.result["resolved_asin"] == "B0CHILD001"
    assert parent_result.result["parent_asin"] == "B0PARENT01"
    assert parent_result.result["media_source_refs"] == []


def test_artifact_upload_failure_is_retryable(monkeypatch) -> None:
    monkeypatch.setenv("AMAZON_US_BROWSER_PROFILE_REF", "amazon-us-profile")
    monkeypatch.setattr(
        handler_module,
        "_collect_browser_page",
        lambda **kwargs: _success_collection(),
    )

    result = amazon_product_browser_fetch_handler(
        _context(store=FakeArtifactStore(fail_upload=True))
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "artifact_write_failed"
    assert result.error.retryable is True


def test_remote_verification_failure_is_retryable_without_durable_ref(
    monkeypatch,
) -> None:
    class FailRemoteVerificationStore(FakeArtifactStore):
        def read_bytes(self, **kwargs):
            del kwargs
            return b"tampered"

    store = FailRemoteVerificationStore()
    monkeypatch.setenv("AMAZON_US_BROWSER_PROFILE_REF", "amazon-us-profile")
    monkeypatch.setattr(
        handler_module,
        "_collect_browser_page",
        lambda **kwargs: _success_collection(),
    )

    result = amazon_product_browser_fetch_handler(_context(store=store))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "artifact_write_failed"
    assert result.result["artifact_refs"] == []


def test_oversized_normalized_capture_fails_closed_without_upload(monkeypatch) -> None:
    store = FakeArtifactStore()
    capture = _capture()
    oversized_title = "x" * (2 * 1024 * 1024)
    capture["product"]["title"] = oversized_title
    capture["field_evidence"]["product.title"]["value"] = oversized_title
    monkeypatch.setenv("AMAZON_US_BROWSER_PROFILE_REF", "amazon-us-profile")
    monkeypatch.setattr(
        handler_module,
        "_collect_browser_page",
        lambda **kwargs: _success_collection(capture),
    )

    result = amazon_product_browser_fetch_handler(_context(store=store))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "artifact_size_limit_exceeded"
    assert result.error.retryable is False
    assert store.uploads == {}


def test_oversized_runtime_html_is_not_persisted_or_size_checked(monkeypatch) -> None:
    store = FakeArtifactStore()
    collection = _success_collection()
    collection["html"] = f"<html><body>{'x' * (8 * 1024 * 1024)}</body></html>"
    monkeypatch.setenv("AMAZON_US_BROWSER_PROFILE_REF", "amazon-us-profile")
    monkeypatch.setattr(
        handler_module,
        "_collect_browser_page",
        lambda **kwargs: collection,
    )

    result = amazon_product_browser_fetch_handler(_context(store=store))

    assert result.status == "success"
    assert len(store.uploads) == 1
    assert [ref["capture_kind"] for ref in result.result["artifact_refs"]] == [
        "normalized_capture"
    ]


@pytest.mark.parametrize(
    ("capture_kind", "max_bytes", "content_type"),
    [
        ("normalized_capture", 2 * 1024 * 1024, "application/json"),
        ("screenshot", 10 * 1024 * 1024, "image/png"),
    ],
)
def test_upload_bytes_rejects_each_artifact_above_its_stored_byte_limit(
    capture_kind: str,
    max_bytes: int,
    content_type: str,
) -> None:
    store = FakeArtifactStore()

    with pytest.raises(ValueError, match=f"{capture_kind}.*size limit"):
        handler_module._upload_bytes(
            context=_context(store=store),
            artifact_policy={"store": store, "bucket": "artifacts", "object_prefix": "dev"},
            object_key=f"dev/raw-captures/amazon/us/B0CHILD001/run-1/{capture_kind}.bin",
            payload=b"x" * (max_bytes + 1),
            capture_kind=capture_kind,
            content_type=content_type,
            sanitization_status="test",
            run_id="run-1",
            observed_at=OBSERVED_AT,
        )

    assert store.uploads == {}


def test_browser_worker_claims_the_controlled_handler_allowlist(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    class Store:
        def claim_next_browser_execution(self, **kwargs):
            calls.append(kwargs)
            return None

    monkeypatch.setattr(
        worker_dispatch,
        "build_runtime_settings",
        lambda params: SimpleNamespace(worker_id="worker-1", lease_seconds=30),
    )
    monkeypatch.setattr(worker_dispatch, "create_runtime_store", lambda settings: Store())

    worker_dispatch.execute_browser_once({})

    assert set(calls[0]["item_codes"]) == set(BROWSER_HANDLER_CODES)


class ArtifactIndexStore:
    def __init__(self, *, fail_replace: bool = False) -> None:
        self.fail_replace = fail_replace
        self.existing = [
            ArtifactObjectRecord(
                artifact_id="existing",
                request_id=RUNTIME_REQUEST_ID,
                execution_id="older-execution",
                run_id=STABLE_CAPTURE_RUN_ID,
                step_id="older-step",
                kind="older",
                bucket="artifacts",
                object_key=f"dev/runs/{STABLE_CAPTURE_RUN_ID}/older.json",
                etag="etag",
                size=1,
                content_type="application/json",
                source_path="",
                metadata={},
                created_at=1.0,
            )
        ]
        self.replaced: list[ArtifactObjectRecord] = []
        self.marked: tuple[str, dict[str, object]] | None = None

    def list_artifacts(self, *, run_id: str) -> list[ArtifactObjectRecord]:
        assert run_id == STABLE_CAPTURE_RUN_ID
        return list(self.existing)

    def replace_artifacts(self, *, run_id: str, records: list[ArtifactObjectRecord]) -> None:
        if self.fail_replace:
            raise RuntimeError("runtime artifact index unavailable")
        self.replaced = records

    def mark_browser_execution_success(self, **kwargs):
        self.marked = ("success", kwargs)
        return SimpleNamespace(result_status="success")

    def mark_browser_execution_failed(self, **kwargs):
        self.marked = ("terminal", kwargs)
        return SimpleNamespace(result_status="failed")

    def mark_browser_execution_retry_or_failed(self, **kwargs):
        self.marked = ("failed", kwargs)
        return SimpleNamespace(result_status="waiting")


def _artifact_outcome(*, failed: bool = False) -> ExecutionSupervisorOutcome:
    base_context = _context(
        store=FakeArtifactStore(),
        payload_overrides={"run_id": STABLE_CAPTURE_RUN_ID},
    )
    context = HandlerContext(
        **{
            **base_context.to_dict(),
            "request_id": RUNTIME_REQUEST_ID,
            "job_id": RUNTIME_EXECUTION_ID,
            "resource_code": f"browser:amazon:{RUNTIME_TARGET_DIGEST}",
        }
    )
    normalized_digest = "b" * 64
    normalized_ref = {
        "capture_kind": "normalized_capture",
        "bucket": "artifacts",
        "object_key": (
            "dev/raw-captures/amazon/us/B0CHILD001/2026/07/14/"
            f"{STABLE_CAPTURE_RUN_ID}/{normalized_digest}/normalized.json"
        ),
        "etag": "etag-normalized",
        "size": 321,
        "content_type": "application/json",
        "content_digest": normalized_digest,
        "sanitization_status": "normalized",
        "request_id": context.request_id,
        "execution_id": context.job_id,
        "run_id": STABLE_CAPTURE_RUN_ID,
        "collected_at": "2026-07-14T00:00:00Z",
        "created_at": "2026-07-14T00:00:00Z",
        "created_at_epoch": 2.0,
    }
    screenshot_digest = "c" * 64
    screenshot_ref = {
        "capture_kind": "screenshot",
        "bucket": "artifacts",
        "object_key": (
            "dev/raw-captures/amazon/us/B0CHILD001/2026/07/14/"
            f"{STABLE_CAPTURE_RUN_ID}/{screenshot_digest}/page.png"
        ),
        "etag": "etag-screenshot",
        "size": 456,
        "content_type": "image/png",
        "content_digest": screenshot_digest,
        "sanitization_status": "not_applicable",
        "request_id": context.request_id,
        "execution_id": context.job_id,
        "run_id": STABLE_CAPTURE_RUN_ID,
        "collected_at": "2026-07-14T00:00:00Z",
        "created_at": "2026-07-14T00:00:00Z",
        "created_at_epoch": 2.0,
    }
    artifact_refs = [screenshot_ref] if failed else [normalized_ref]
    result_payload = {
        "marketplace_code": "US",
        "requested_asin": "B0CHILD001",
        "resolved_asin": "B0CHILD001",
        "canonical_url": "https://www.amazon.com/dp/B0CHILD001",
        "collection_status": "blocked" if failed else "success",
        "field_coverage": {"total": 1, "observed": 1},
        "raw_capture_refs": artifact_refs,
        "artifact_refs": artifact_refs,
        "media_source_refs": [],
        "browser_target_digest": RUNTIME_TARGET_DIGEST,
    }
    if not failed:
        result_payload["normalized_capture_ref"] = normalized_ref
    if failed:
        worker_result = HandlerResult.failed(
            context,
            error=HandlerError(
                error_type="browser_failure",
                error_code="captcha_required",
                message="blocked",
                retryable=False,
            ),
            result=result_payload,
        )
        supervisor_error = ExecutionSupervisorError(
            error_type="browser_failure",
            error_code="captcha_required",
            message="blocked",
            retryable=False,
            terminal=True,
        )
    else:
        worker_result = HandlerResult.success(
            context,
            result=result_payload,
        )
        supervisor_error = None
    return ExecutionSupervisorOutcome(
        context=context,
        worker_result=worker_result,
        supervisor_status="handler_completed",
        started_at=1.0,
        finished_at=2.0,
        heartbeat_count=0,
        error=supervisor_error,
    )


def test_browser_worker_once_does_not_return_or_persist_raw_amazon_child_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    valid_outcome = _artifact_outcome()
    secret = "Bearer must-not-cross-runtime-or-log-boundary"
    progress_updates: list[dict[str, object]] = []
    claimed = SimpleNamespace(
        request_id=valid_outcome.context.request_id,
        execution_id=valid_outcome.context.job_id,
        item_code=valid_outcome.context.handler_code,
        payload=valid_outcome.context.payload,
        run_id="claim-run-1",
        workflow_code=valid_outcome.context.workflow_code,
        business_key="amazon:US:B0CHILD001",
        dedupe_key="amazon-browser-1",
        resource_code=f"browser:amazon:{RUNTIME_TARGET_DIGEST}",
        attempt_count=1,
        max_attempts=1,
        max_execution_seconds=30,
    )

    class StoredExecution:
        def __init__(self, kwargs: dict[str, object]) -> None:
            self.result_status = "success"
            self.status = "finished"
            self.summary = kwargs["summary"]
            self.result = kwargs["result"]
            self.error_type = ""
            self.error_code = ""

        def to_dict(self) -> dict[str, object]:
            return {
                "execution_id": claimed.execution_id,
                "request_id": claimed.request_id,
                "item_code": claimed.item_code,
                "status": self.status,
                "result_status": self.result_status,
                "summary": self.summary,
                "result": self.result,
                "error_type": self.error_type,
                "error_code": self.error_code,
            }

    class Store(ArtifactIndexStore):
        def claim_next_browser_execution(self, **_kwargs):
            return claimed

        def update_task_execution_progress(self, **kwargs):
            progress_updates.append(dict(kwargs))
            return claimed

        def heartbeat_browser_execution(self, **_kwargs):
            return True

        def mark_browser_execution_success(self, **kwargs):
            self.marked = ("success", kwargs)
            return StoredExecution(kwargs)

    def supervised(**kwargs):
        runtime_context = kwargs["context"]
        kwargs["callbacks"].on_progress(
            ExecutionProgressEvent(
                progress_stage="xBearer-runtime-secret",
                message=secret,
                details={"cookie": secret},
            )
        )
        return ExecutionSupervisorOutcome(
            context=runtime_context,
            worker_result=HandlerResult.success(
                runtime_context,
                summary={"cookie": secret},
                result={**valid_outcome.worker_result.result, "cookie": secret},
                warnings=(secret,),
            ),
            supervisor_status="handler_completed",
            started_at=1.0,
            finished_at=2.0,
            heartbeat_count=0,
            progress_events=(
                ExecutionProgressEvent(
                    progress_stage="parse",
                    message=secret,
                    details={"cookie": secret},
                ),
            ),
        )

    store = Store()
    monkeypatch.setattr(
        worker_dispatch,
        "build_runtime_settings",
        lambda _params: SimpleNamespace(
            worker_id="browser-worker-1",
            lease_seconds=30,
            heartbeat_interval_seconds=1,
            retry_delay_seconds=5,
        ),
    )
    monkeypatch.setattr(worker_dispatch, "create_runtime_store", lambda _settings: store)
    monkeypatch.setattr(worker_dispatch, "run_supervised_handler", supervised)
    monkeypatch.setattr(
        worker_dispatch,
        "build_runtime_request_payload",
        lambda **_kwargs: {},
    )

    payload = worker_dispatch.execute_browser_once({})

    assert secret not in repr(progress_updates)
    assert secret not in repr(payload)
    assert progress_updates[-1]["progress_stage"] == "handler_progress"
    assert progress_updates[-1]["message"] == "Amazon browser collection progress updated."
    assert payload["worker_result"]["result"]["requested_asin"] == "B0CHILD001"
    assert "progress_events" not in payload["supervisor"]


@pytest.mark.parametrize("failed", [False, True])
def test_browser_outcome_indexes_amazon_artifacts_without_deleting_existing_records(
    failed: bool,
) -> None:
    store = ArtifactIndexStore()

    worker_dispatch.persist_browser_execution_outcome(
        store=store,
        execution_id="execution-1",
        run_id="claim-run-1",
        outcome=_artifact_outcome(failed=failed),
        retry_delay_seconds=5,
    )

    expected_artifact_ids = {"existing"}
    if failed:
        expected_artifact_ids.add(
            hashlib.sha256(
                (
                    f"{STABLE_CAPTURE_RUN_ID}:artifacts:dev/raw-captures/amazon/us/"
                    "B0CHILD001/2026/07/14/"
                    f"{STABLE_CAPTURE_RUN_ID}/{'c' * 64}/page.png"
                ).encode("utf-8")
            ).hexdigest()
        )
    else:
        expected_artifact_ids.add(
            hashlib.sha256(
                (
                    f"{STABLE_CAPTURE_RUN_ID}:artifacts:dev/raw-captures/amazon/us/"
                    "B0CHILD001/2026/07/14/"
                    f"{STABLE_CAPTURE_RUN_ID}/{'b' * 64}/normalized.json"
                ).encode("utf-8")
            ).hexdigest()
        )
    assert {record.artifact_id for record in store.replaced} == expected_artifact_ids
    new_record = next(record for record in store.replaced if record.artifact_id != "existing")
    assert new_record.request_id == RUNTIME_REQUEST_ID
    assert new_record.execution_id == RUNTIME_EXECUTION_ID
    assert new_record.run_id == STABLE_CAPTURE_RUN_ID
    assert new_record.step_id == "collect_amazon_product_detail"
    assert store.marked is not None
    stored_ref = store.marked[1]["result"]["artifact_refs"][0]
    assert set(stored_ref) == {
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
    }
    if failed:
        assert store.marked[0] == "terminal"
        assert store.marked[1]["run_id"] == "claim-run-1"


def test_non_retryable_amazon_failure_finishes_on_the_first_attempt() -> None:
    store = ArtifactIndexStore()

    execution, success_count, failed_count = worker_dispatch.persist_browser_execution_outcome(
        store=store,
        execution_id="execution-1",
        run_id="claim-run-1",
        outcome=_artifact_outcome(failed=True),
        retry_delay_seconds=5,
    )

    assert execution.result_status == "failed"
    assert (success_count, failed_count) == (0, 1)
    assert store.marked is not None
    assert store.marked[0] == "terminal"


def test_browser_storage_rejects_foreign_capture_before_runtime_or_index_write() -> None:
    valid_outcome = _artifact_outcome()
    foreign_ref = {
        **valid_outcome.worker_result.result["artifact_refs"][0],
        "request_id": "foreign-request",
        "access_token": "must-not-cross-runtime-boundary",
    }
    outcome = ExecutionSupervisorOutcome(
        context=valid_outcome.context,
        worker_result=HandlerResult.success(
            valid_outcome.context,
            result={"artifact_refs": [foreign_ref]},
        ),
        supervisor_status="handler_completed",
        started_at=1.0,
        finished_at=2.0,
        heartbeat_count=0,
    )
    store = ArtifactIndexStore()

    execution, success_count, failed_count = worker_dispatch.persist_browser_execution_outcome(
        store=store,
        execution_id="execution-1",
        run_id="claim-run-1",
        outcome=outcome,
        retry_delay_seconds=5,
    )

    assert execution.result_status == "failed"
    assert (success_count, failed_count) == (0, 1)
    assert store.replaced == []
    assert store.marked is not None
    assert store.marked[0] == "terminal"
    assert store.marked[1]["error_code"] == "artifact_validation_failed"
    assert "collection_status" not in store.marked[1]["result"]
    assert "foreign-request" not in repr(store.marked[1]["result"])
    assert "must-not-cross-runtime-boundary" not in repr(store.marked[1]["result"])


def test_browser_storage_projects_nested_values_before_first_runtime_write() -> None:
    valid_outcome = _artifact_outcome()
    secret = "Bearer must-not-cross-runtime-boundary"
    worker_result = HandlerResult.success(
        valid_outcome.context,
        summary={"collection_status": "success", "cookie": secret},
        result={
            **valid_outcome.worker_result.result,
            "marketplace_code": "US",
            "requested_asin": "B0CHILD001",
            "resolved_asin": "B0CHILD001",
            "canonical_url": "https://www.amazon.com/dp/B0CHILD001",
            "collection_status": "success",
            "field_coverage": {"total": 2, "observed": 1, "cookie": secret},
            "media_source_refs": [
                {
                    "source_url": "https://m.media-amazon.com/images/I/main.jpg?token=drop",
                    "source_platform": "amazon",
                    "marketplace_code": "US",
                    "product_id": "B0CHILD001",
                    "media_role": "main_image",
                    "position": 0,
                    "cookie": secret,
                },
                {
                    "source_url": "https://m.media-amazon.com/images/token=secret.jpg",
                    "source_platform": "amazon",
                    "marketplace_code": "US",
                    "product_id": "B0CHILD001",
                    "media_role": "gallery_image",
                    "position": 1,
                },
            ],
            "browser_target_digest": RUNTIME_TARGET_DIGEST,
            "browser_provider_name": "roxy",
            "stage_durations_ms": {"navigation": 1.25, "cookie": secret},
        },
        warnings=(secret,),
    )
    outcome = ExecutionSupervisorOutcome(
        context=valid_outcome.context,
        worker_result=worker_result,
        supervisor_status="handler_completed",
        started_at=1.0,
        finished_at=2.0,
        heartbeat_count=0,
        progress_events=(
            ExecutionProgressEvent(
                progress_stage="parse",
                message=secret,
                details={"cookie": secret},
            ),
        ),
    )
    store = ArtifactIndexStore()

    execution, success_count, failed_count = worker_dispatch.persist_browser_execution_outcome(
        store=store,
        execution_id="execution-1",
        run_id="claim-run-1",
        outcome=outcome,
        retry_delay_seconds=5,
    )

    assert execution.result_status == "success"
    assert (success_count, failed_count) == (1, 0)
    assert store.marked is not None
    stored = store.marked[1]
    assert secret not in repr(stored["result"])
    assert secret not in repr(stored["summary"])
    assert "warnings" not in stored["result"]["handler_result"]
    assert "progress_events" not in stored["result"]["supervisor"]
    assert stored["result"]["handler_result"]["status"] == "partial_success"
    assert stored["result"]["handler_result"]["contract_revision"] == "runtime_contract"
    assert stored["summary"]["handler_status"] == "partial_success"
    assert stored["result"]["collection_status"] == "partial_success"
    assert stored["result"]["field_coverage"] == {
        "total": 2,
        "observed": 1,
        "explicitly_unavailable": 0,
        "missing": 1,
        "percentage": 50.0,
    }
    assert stored["result"]["media_source_refs"] == [
        {
            "source_url": "https://m.media-amazon.com/images/I/main.jpg",
            "source_platform": "amazon",
            "marketplace_code": "US",
            "product_id": "B0CHILD001",
            "media_role": "main_image",
            "position": 0,
        }
    ]
    assert stored["result"]["stage_durations_ms"] == {"navigation": 1.25}


def test_browser_storage_requires_submit_time_artifact_coordinate_snapshot() -> None:
    valid_outcome = _artifact_outcome()
    context = _context(
        store=FakeArtifactStore(),
        payload_overrides={"artifact_object_prefix": None},
    )
    outcome = ExecutionSupervisorOutcome(
        context=context,
        worker_result=HandlerResult.success(
            context,
            result={"artifact_refs": valid_outcome.worker_result.result["artifact_refs"]},
        ),
        supervisor_status="handler_completed",
        started_at=1.0,
        finished_at=2.0,
        heartbeat_count=0,
    )
    store = ArtifactIndexStore()

    execution, success_count, failed_count = worker_dispatch.persist_browser_execution_outcome(
        store=store,
        execution_id="execution-1",
        run_id="claim-run-1",
        outcome=outcome,
        retry_delay_seconds=5,
    )

    assert execution.result_status == "failed"
    assert (success_count, failed_count) == (0, 1)
    assert store.replaced == []
    assert store.marked is not None
    assert store.marked[1]["error_code"] == "artifact_validation_failed"


def test_browser_storage_rejects_incomplete_success_result_before_runtime_write() -> None:
    valid_outcome = _artifact_outcome()
    outcome = ExecutionSupervisorOutcome(
        context=valid_outcome.context,
        worker_result=HandlerResult.success(
            valid_outcome.context,
            result={"collection_status": "success"},
        ),
        supervisor_status="handler_completed",
        started_at=1.0,
        finished_at=2.0,
        heartbeat_count=0,
    )
    store = ArtifactIndexStore()

    execution, success_count, failed_count = worker_dispatch.persist_browser_execution_outcome(
        store=store,
        execution_id="execution-1",
        run_id="claim-run-1",
        outcome=outcome,
        retry_delay_seconds=5,
    )

    assert execution.result_status == "failed"
    assert (success_count, failed_count) == (0, 1)
    assert store.replaced == []
    assert store.marked is not None
    assert store.marked[0] == "terminal"
    assert store.marked[1]["error_code"] == "artifact_validation_failed"


def test_browser_storage_rejects_target_digest_not_bound_to_resource_lane() -> None:
    outcome = _artifact_outcome()
    foreign_digest = "e" * 64
    outcome.worker_result.result["browser_target_digest"] = foreign_digest
    store = ArtifactIndexStore()

    execution, success_count, failed_count = worker_dispatch.persist_browser_execution_outcome(
        store=store,
        execution_id="execution-1",
        run_id="claim-run-1",
        outcome=outcome,
        retry_delay_seconds=5,
    )

    assert execution.result_status == "failed"
    assert (success_count, failed_count) == (0, 1)
    assert store.replaced == []
    assert store.marked is not None
    assert store.marked[1]["error_code"] == "artifact_validation_failed"
    assert foreign_digest not in repr(store.marked[1])


def test_browser_storage_rejects_empty_digest_from_invalid_resource_lane() -> None:
    valid_outcome = _artifact_outcome()
    context = HandlerContext(
        **{
            **valid_outcome.context.to_dict(),
            "resource_code": "browser:amazon:",
        }
    )
    result = dict(valid_outcome.worker_result.result)
    result["browser_target_digest"] = ""
    outcome = ExecutionSupervisorOutcome(
        context=context,
        worker_result=HandlerResult.success(context, result=result),
        supervisor_status="handler_completed",
        started_at=1.0,
        finished_at=2.0,
        heartbeat_count=0,
    )
    store = ArtifactIndexStore()

    execution, success_count, failed_count = worker_dispatch.persist_browser_execution_outcome(
        store=store,
        execution_id="execution-1",
        run_id="claim-run-1",
        outcome=outcome,
        retry_delay_seconds=5,
    )

    assert execution.result_status == "failed"
    assert (success_count, failed_count) == (0, 1)
    assert store.replaced == []
    assert store.marked is not None
    assert store.marked[1]["error_code"] == "artifact_validation_failed"


def test_browser_storage_rejects_blocked_result_without_target_digest() -> None:
    outcome = _artifact_outcome(failed=True)
    outcome.worker_result.result.pop("browser_target_digest")
    store = ArtifactIndexStore()

    execution, success_count, failed_count = worker_dispatch.persist_browser_execution_outcome(
        store=store,
        execution_id="execution-1",
        run_id="claim-run-1",
        outcome=outcome,
        retry_delay_seconds=5,
    )

    assert execution.result_status == "failed"
    assert (success_count, failed_count) == (0, 1)
    assert store.replaced == []
    assert store.marked is not None
    assert store.marked[1]["error_code"] == "artifact_validation_failed"


def test_browser_artifact_index_drops_child_controlled_etag() -> None:
    outcome = _artifact_outcome()
    secret = "Bearer-runtime-index-secret"
    for ref in outcome.worker_result.result["artifact_refs"]:
        ref["etag"] = secret
    store = ArtifactIndexStore()

    execution, success_count, failed_count = worker_dispatch.persist_browser_execution_outcome(
        store=store,
        execution_id="execution-1",
        run_id="claim-run-1",
        outcome=outcome,
        retry_delay_seconds=5,
    )

    assert execution.result_status == "success"
    assert (success_count, failed_count) == (1, 0)
    new_records = [record for record in store.replaced if record.artifact_id != "existing"]
    assert new_records
    assert all(record.etag == "" for record in new_records)
    assert secret not in repr(store.replaced)
    assert secret not in repr(store.marked)


def test_browser_storage_rejects_blocked_failure_without_screenshot_evidence() -> None:
    valid_outcome = _artifact_outcome(failed=True)
    result = dict(valid_outcome.worker_result.result)
    for field in ("normalized_capture_ref", "raw_capture_refs", "artifact_refs"):
        result.pop(field, None)
    outcome = ExecutionSupervisorOutcome(
        context=valid_outcome.context,
        worker_result=HandlerResult.failed(
            valid_outcome.context,
            error=valid_outcome.worker_result.error,
            result=result,
        ),
        supervisor_status=valid_outcome.supervisor_status,
        started_at=valid_outcome.started_at,
        finished_at=valid_outcome.finished_at,
        heartbeat_count=valid_outcome.heartbeat_count,
        error=valid_outcome.error,
    )
    store = ArtifactIndexStore()

    execution, success_count, failed_count = worker_dispatch.persist_browser_execution_outcome(
        store=store,
        execution_id="execution-1",
        run_id="claim-run-1",
        outcome=outcome,
        retry_delay_seconds=5,
    )

    assert execution.result_status == "failed"
    assert (success_count, failed_count) == (0, 1)
    assert store.replaced == []
    assert store.marked is not None
    assert store.marked[0] == "terminal"
    assert store.marked[1]["error_code"] == "artifact_validation_failed"


def test_browser_storage_rejects_foreign_identity_in_blocked_result() -> None:
    outcome = _artifact_outcome(failed=True)
    foreign_asin = "B0OTHER001"
    outcome.worker_result.result.update(
        {
            "requested_asin": foreign_asin,
            "resolved_asin": foreign_asin,
            "canonical_url": f"https://www.amazon.com/dp/{foreign_asin}",
        }
    )
    store = ArtifactIndexStore()

    execution, success_count, failed_count = worker_dispatch.persist_browser_execution_outcome(
        store=store,
        execution_id="execution-1",
        run_id="claim-run-1",
        outcome=outcome,
        retry_delay_seconds=5,
    )

    assert execution.result_status == "failed"
    assert (success_count, failed_count) == (0, 1)
    assert store.replaced == []
    assert store.marked is not None
    assert store.marked[1]["error_code"] == "artifact_validation_failed"
    assert foreign_asin not in repr(store.marked[1])


def test_browser_storage_converges_extreme_capture_timestamp_to_terminal_failure() -> None:
    outcome = _artifact_outcome()
    outcome.worker_result.result["normalized_capture_ref"]["collected_at"] = (
        "0001-01-01T00:00:00+23:59"
    )
    store = ArtifactIndexStore()

    execution, success_count, failed_count = worker_dispatch.persist_browser_execution_outcome(
        store=store,
        execution_id="execution-1",
        run_id="claim-run-1",
        outcome=outcome,
        retry_delay_seconds=5,
    )

    assert execution.result_status == "failed"
    assert (success_count, failed_count) == (0, 1)
    assert store.replaced == []
    assert store.marked is not None
    assert store.marked[0] == "terminal"
    assert store.marked[1]["error_code"] == "artifact_validation_failed"


def test_browser_projection_exception_is_safely_terminalized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "Bearer projection-internal-secret"
    actual_projection = worker_dispatch.get_runtime_result_projection(
        "amazon_product_browser_fetch"
    )
    assert actual_projection is not None

    class ExplodingProjection:
        def project_storage(self, outcome: ExecutionSupervisorOutcome):
            del outcome
            raise OverflowError(secret)

        def projection_failure(
            self,
            outcome: ExecutionSupervisorOutcome,
            error: Exception,
            *,
            phase: str,
        ):
            return actual_projection.projection_failure(
                outcome,
                error,
                phase=phase,
            )

    monkeypatch.setattr(
        worker_dispatch,
        "get_runtime_result_projection",
        lambda handler_code: (
            ExplodingProjection()
            if handler_code == "amazon_product_browser_fetch"
            else None
        ),
    )
    store = ArtifactIndexStore()

    execution, success_count, failed_count = worker_dispatch.persist_browser_execution_outcome(
        store=store,
        execution_id="execution-1",
        run_id="claim-run-1",
        outcome=_artifact_outcome(),
        retry_delay_seconds=5,
    )

    assert execution.result_status == "failed"
    assert (success_count, failed_count) == (0, 1)
    assert store.replaced == []
    assert store.marked is not None
    assert store.marked[1]["error_code"] == "artifact_validation_failed"
    assert secret not in repr(store.marked[1])


def test_browser_storage_allows_pre_navigation_failure_without_evidence() -> None:
    valid_outcome = _artifact_outcome()
    error = HandlerError(
        error_type="amazon_browser_failure",
        error_code="transient_page_failure",
        message="navigation failed",
        retryable=True,
    )
    outcome = ExecutionSupervisorOutcome(
        context=valid_outcome.context,
        worker_result=HandlerResult.failed(
            valid_outcome.context,
            error=error,
            result={
                "collection_status": "failed",
                "browser_target_digest": RUNTIME_TARGET_DIGEST,
            },
        ),
        supervisor_status="handler_failed",
        started_at=1.0,
        finished_at=2.0,
        heartbeat_count=0,
        error=ExecutionSupervisorError(
            error_type=error.error_type,
            error_code=error.error_code,
            message=error.message,
            retryable=True,
            terminal=False,
        ),
    )
    store = ArtifactIndexStore()

    execution, success_count, failed_count = worker_dispatch.persist_browser_execution_outcome(
        store=store,
        execution_id="execution-1",
        run_id="claim-run-1",
        outcome=outcome,
        retry_delay_seconds=5,
    )

    assert execution.result_status == "waiting"
    assert (success_count, failed_count) == (0, 0)
    assert store.replaced == []
    assert store.marked is not None
    assert store.marked[0] == "failed"
    assert store.marked[1]["error_code"] == "transient_page_failure"


@pytest.mark.parametrize("error_code", ["navigation_timeout", "rate_limited"])
def test_browser_storage_preserves_governed_retryable_error_codes(error_code: str) -> None:
    valid_outcome = _artifact_outcome()
    error = HandlerError(
        error_type="amazon_browser_failure",
        error_code=error_code,
        message="temporary navigation failure",
        retryable=True,
    )
    outcome = ExecutionSupervisorOutcome(
        context=valid_outcome.context,
        worker_result=HandlerResult.failed(
            valid_outcome.context,
            error=error,
            result={
                "collection_status": "failed",
                "browser_target_digest": RUNTIME_TARGET_DIGEST,
            },
        ),
        supervisor_status="handler_failed",
        started_at=1.0,
        finished_at=2.0,
        heartbeat_count=0,
        error=ExecutionSupervisorError(
            error_type=error.error_type,
            error_code=error.error_code,
            message=error.message,
            retryable=True,
            terminal=False,
        ),
    )
    store = ArtifactIndexStore()

    execution, success_count, failed_count = worker_dispatch.persist_browser_execution_outcome(
        store=store,
        execution_id="execution-1",
        run_id="claim-run-1",
        outcome=outcome,
        retry_delay_seconds=5,
    )

    assert execution.result_status == "waiting"
    assert (success_count, failed_count) == (0, 0)
    assert store.marked is not None
    assert store.marked[1]["error_code"] == error_code


@pytest.mark.parametrize(
    "identity_overrides",
    [
        {"resolved_asin": "B0OTHER001"},
        {
            "resolved_asin": "B0OTHER001",
            "parent_asin": "B0CHILD001",
            "collection_status": "success",
        },
    ],
)
def test_browser_storage_rejects_unrelated_resolved_asin_before_runtime_write(
    identity_overrides: dict[str, object],
) -> None:
    valid_outcome = _artifact_outcome()
    result = {**valid_outcome.worker_result.result, **identity_overrides}
    outcome = ExecutionSupervisorOutcome(
        context=valid_outcome.context,
        worker_result=HandlerResult.success(valid_outcome.context, result=result),
        supervisor_status="handler_completed",
        started_at=1.0,
        finished_at=2.0,
        heartbeat_count=0,
    )
    store = ArtifactIndexStore()

    execution, _, failed_count = worker_dispatch.persist_browser_execution_outcome(
        store=store,
        execution_id="execution-1",
        run_id="claim-run-1",
        outcome=outcome,
        retry_delay_seconds=5,
    )

    assert execution.result_status == "failed"
    assert failed_count == 1
    assert store.replaced == []
    assert store.marked is not None
    assert store.marked[1]["error_code"] == "artifact_validation_failed"


def test_browser_storage_rejects_noncanonical_bound_runtime_identifier() -> None:
    valid_outcome = _artifact_outcome()
    context = HandlerContext(
        **{
            **valid_outcome.context.to_dict(),
            "request_id": "request-1",
        }
    )
    result = json.loads(json.dumps(valid_outcome.worker_result.result))
    result["normalized_capture_ref"]["request_id"] = "request-1"
    for field in ("raw_capture_refs", "artifact_refs"):
        for ref in result[field]:
            ref["request_id"] = "request-1"
    outcome = ExecutionSupervisorOutcome(
        context=context,
        worker_result=HandlerResult.success(context, result=result),
        supervisor_status="handler_completed",
        started_at=1.0,
        finished_at=2.0,
        heartbeat_count=0,
    )
    store = ArtifactIndexStore()

    execution, success_count, failed_count = worker_dispatch.persist_browser_execution_outcome(
        store=store,
        execution_id="execution-1",
        run_id="claim-run-1",
        outcome=outcome,
        retry_delay_seconds=5,
    )

    assert execution.result_status == "failed"
    assert (success_count, failed_count) == (0, 1)
    assert store.replaced == []
    assert store.marked is not None
    assert store.marked[1]["error_code"] == "artifact_validation_failed"
    assert "request-1" not in repr(store.marked[1]["result"])


def test_artifact_retry_uses_stable_capture_run_id_across_claim_attempts() -> None:
    first_store = ArtifactIndexStore()
    second_store = ArtifactIndexStore()

    for store, claim_run_id in (
        (first_store, "claim-run-1"),
        (second_store, "claim-run-2"),
    ):
        worker_dispatch.persist_browser_execution_outcome(
            store=store,
            execution_id="execution-1",
            run_id=claim_run_id,
            outcome=_artifact_outcome(),
            retry_delay_seconds=5,
        )

    first_new = next(record for record in first_store.replaced if record.artifact_id != "existing")
    second_new = next(
        record for record in second_store.replaced if record.artifact_id != "existing"
    )
    assert first_new.artifact_id == second_new.artifact_id
    assert first_new.run_id == second_new.run_id == STABLE_CAPTURE_RUN_ID


def test_browser_artifact_index_failure_retries_before_marking_success() -> None:
    store = ArtifactIndexStore(fail_replace=True)

    execution, success_count, failed_count = worker_dispatch.persist_browser_execution_outcome(
        store=store,
        execution_id="execution-1",
        run_id="claim-run-1",
        outcome=_artifact_outcome(),
        retry_delay_seconds=5,
    )

    assert execution.result_status == "waiting"
    assert (success_count, failed_count) == (0, 0)
    assert store.marked is not None
    assert store.marked[0] == "failed"
    assert store.marked[1]["error_code"] == "artifact_index_failed"


def test_browser_artifact_index_failure_does_not_store_provider_error_text() -> None:
    secret = "Bearer must-not-cross-runtime-boundary"

    class SecretFailureStore(ArtifactIndexStore):
        def replace_artifacts(
            self,
            *,
            run_id: str,
            records: list[ArtifactObjectRecord],
        ) -> None:
            del run_id, records
            raise RuntimeError(secret)

    store = SecretFailureStore()

    worker_dispatch.persist_browser_execution_outcome(
        store=store,
        execution_id="execution-1",
        run_id="claim-run-1",
        outcome=_artifact_outcome(),
        retry_delay_seconds=5,
    )

    assert store.marked is not None
    assert store.marked[1]["error_code"] == "artifact_index_failed"
    assert secret not in repr(store.marked[1])


@pytest.mark.parametrize(
    "secret_code",
    [
        "xBearer-runtime-secret",
        "sk_live_51ABCxyz",
        "AKIAIOSFODNN7EXAMPLE",
        "ghp_abcdefghijklmnopqrstuvwxyz123456",
    ],
)
def test_browser_failure_sanitizes_error_columns_and_nested_envelope(
    secret_code: str,
) -> None:
    valid_outcome = _artifact_outcome(failed=True)
    worker_result = HandlerResult.failed(
        valid_outcome.context,
        error=HandlerError(
            error_type=secret_code,
            error_code=secret_code,
            message="Bearer must-not-cross-runtime-boundary",
            retryable=False,
        ),
        result=valid_outcome.worker_result.result,
    )
    outcome = ExecutionSupervisorOutcome(
        context=valid_outcome.context,
        worker_result=worker_result,
        supervisor_status="handler_failed",
        started_at=1.0,
        finished_at=2.0,
        heartbeat_count=0,
        error=ExecutionSupervisorError(
            error_type=secret_code,
            error_code=secret_code,
            message="Bearer must-not-cross-runtime-boundary",
            retryable=False,
            terminal=True,
        ),
    )
    store = ArtifactIndexStore()

    worker_dispatch.persist_browser_execution_outcome(
        store=store,
        execution_id="execution-1",
        run_id="claim-run-1",
        outcome=outcome,
        retry_delay_seconds=5,
    )

    assert store.marked is not None
    stored = store.marked[1]
    assert stored["error_type"] == "amazon_browser_failure"
    assert stored["error_code"] == "amazon_browser_collection_failed"
    assert secret_code not in repr(stored)
    assert "must-not-cross-runtime-boundary" not in repr(stored)


def _claimed_runtime_execution(
    runtime_db_url: str,
    *,
    item_code: str,
    payload: dict[str, object],
    resource_code: str,
):
    store = RuntimeStore(db_url=runtime_db_url)
    request = store.submit_task_request(
        project_code="automation-business-scaffold",
        task_code="tiktok_fastmoss_product_ingest",
        payload={"test_case": item_code},
        requested_by="pytest",
    )
    enqueued = store.enqueue_task_executions(
        request_id=request.request_id,
        item_code=item_code,
        workflow_code="refresh_amazon_product_row_by_asin",
        items=[
            {
                "business_key": f"business:{item_code}",
                "dedupe_key": f"{request.request_id}:{item_code}",
                "resource_code": resource_code,
                "max_attempts": 3,
                "payload": payload,
            }
        ],
    )
    execution_id = enqueued["created_records"][0]["execution_id"]
    claimed = store.claim_browser_execution(
        execution_id=execution_id,
        worker_id="browser-worker",
        lease_seconds=30,
    )
    assert claimed is not None
    return store, claimed


def _runtime_failure_outcome(
    claimed,
    *,
    retryable: bool,
    error_code: str,
) -> ExecutionSupervisorOutcome:
    context = HandlerContext(
        request_id=claimed.request_id,
        job_id=claimed.execution_id,
        handler_code=claimed.item_code,
        worker_type="browser_worker",
        runtime_table="task_execution",
        payload=dict(claimed.payload),
        workflow_code=claimed.workflow_code,
        stage_code=str(claimed.payload.get("stage_code") or "browser_stage"),
        item_code=claimed.item_code,
        resource_code=claimed.resource_code,
    )
    result: dict[str, object] = {"artifact_refs": []}
    if claimed.item_code == "amazon_product_browser_fetch" and error_code in {
        "access_blocked",
        "captcha_required",
    }:
        stable_run_id = str(claimed.payload["run_id"])
        requested_asin = str(claimed.payload["requested_asin"])
        content_digest = "c" * 64
        screenshot_ref = {
            "capture_kind": "screenshot",
            "bucket": str(claimed.payload["artifact_bucket"]),
            "object_key": (
                f"{claimed.payload['artifact_object_prefix']}/raw-captures/amazon/us/"
                f"{requested_asin}/2026/07/14/{stable_run_id}/{content_digest}/page.png"
            ),
            "content_digest": content_digest,
            "content_type": "image/png",
            "sanitization_status": "not_applicable",
            "request_id": claimed.request_id,
            "execution_id": claimed.execution_id,
            "run_id": stable_run_id,
            "collected_at": "2026-07-14T00:00:00Z",
            "created_at": "2026-07-14T00:00:00Z",
        }
        result = {
            "collection_status": "blocked",
            "artifact_refs": [screenshot_ref],
            "raw_capture_refs": [screenshot_ref],
            "browser_target_digest": RUNTIME_TARGET_DIGEST,
        }
    worker_result = HandlerResult.failed(
        context,
        error=HandlerError(
            error_type="browser_failure",
            error_code=error_code,
            message=error_code,
            retryable=retryable,
        ),
        result=result,
    )
    return ExecutionSupervisorOutcome(
        context=context,
        worker_result=worker_result,
        supervisor_status="handler_failed",
        started_at=1.0,
        finished_at=2.0,
        heartbeat_count=0,
        error=ExecutionSupervisorError(
            error_type="browser_failure",
            error_code=error_code,
            message=error_code,
            retryable=retryable,
            terminal=not retryable,
        ),
    )


def test_real_runtime_terminal_amazon_failure_preserves_error_audit_and_releases_lease(
    runtime_db_url: str,
) -> None:
    store, claimed = _claimed_runtime_execution(
        runtime_db_url,
        item_code="amazon_product_browser_fetch",
        payload={
            "requested_asin": "B0BLOCK001",
            "source_record_id": "record-1",
            "run_id": STABLE_CAPTURE_RUN_ID,
            "stage_code": "collect_amazon_product_detail",
            "artifact_bucket": "artifacts",
            "artifact_object_prefix": "dev",
        },
        resource_code=f"browser:amazon:{RUNTIME_TARGET_DIGEST}",
    )

    execution, success_count, failed_count = worker_dispatch.persist_browser_execution_outcome(
        store=store,
        execution_id=claimed.execution_id,
        run_id=claimed.run_id,
        outcome=_runtime_failure_outcome(
            claimed,
            retryable=False,
            error_code="captcha_required",
        ),
        retry_delay_seconds=5,
    )

    assert (success_count, failed_count) == (0, 1)
    assert execution.status == "finished"
    assert execution.result_status == "failed"
    assert execution.attempt_count == 1
    assert execution.error_type == "browser_failure"
    assert execution.error_code == "captcha_required"
    assert execution.dead_letter_reason == "terminal_handler_failure"
    with store._engine.connect() as connection:  # noqa: SLF001
        lease_count = connection.execute(
            store._text(  # noqa: SLF001
                "SELECT COUNT(*) FROM resource_lease WHERE execution_id = :execution_id"
            ),
            {"execution_id": claimed.execution_id},
        ).scalar_one()
    assert lease_count == 0


def test_real_runtime_retryable_legacy_browser_failure_remains_pending(
    runtime_db_url: str,
) -> None:
    store, claimed = _claimed_runtime_execution(
        runtime_db_url,
        item_code="tiktok_product_browser_fetch",
        payload={"stage_code": "browser_fallback"},
        resource_code="browser:tiktok-profile",
    )

    execution, success_count, failed_count = worker_dispatch.persist_browser_execution_outcome(
        store=store,
        execution_id=claimed.execution_id,
        run_id=claimed.run_id,
        outcome=_runtime_failure_outcome(
            claimed,
            retryable=True,
            error_code="tiktok_browser_fetch_failed",
        ),
        retry_delay_seconds=5,
    )

    assert (success_count, failed_count) == (0, 0)
    assert execution.status == "pending"
    assert execution.result_status == ""
    assert execution.attempt_count == 1
    assert execution.error_code == "tiktok_browser_fetch_failed"
