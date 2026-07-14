from __future__ import annotations

import gzip
import hashlib
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from automation_business_scaffold.capabilities.browser import amazon_product_fetch_handler as handler_module
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
    return {
        "capture": capture or _capture(),
        "html": _fixture("product_detail_child.html"),
        "resolved_url": "https://www.amazon.com/dp/B0CHILD001",
        "browser_target_digest": "target-digest",
        "screenshot_bytes": b"",
    }


def test_handler_is_allowlisted_bound_and_has_compact_job_contract() -> None:
    assert "amazon_product_browser_fetch" in BROWSER_HANDLER_CONTRACTS
    assert BOUND_BROWSER_HANDLERS["amazon_product_browser_fetch"] is (
        amazon_product_browser_fetch_handler
    )
    assert AMAZON_PRODUCT_BROWSER_FETCH_JOB.worker_type == "browser_worker"
    assert AMAZON_PRODUCT_BROWSER_FETCH_JOB.runtime_table == "task_execution"
    assert AMAZON_PRODUCT_BROWSER_FETCH_JOB.payload_contract.field_names(
        required_only=True
    ) == ("requested_asin", "source_record_id", "run_id")
    assert AMAZON_PRODUCT_BROWSER_FETCH_JOB.business_key_template == (
        "{source_record_id}:{requested_asin}"
    )
    assert "capture" not in AMAZON_PRODUCT_BROWSER_FETCH_JOB.result_contract.field_names()
    assert "html" not in AMAZON_PRODUCT_BROWSER_FETCH_JOB.result_contract.field_names()


def test_success_uploads_governed_capture_and_sanitized_html_and_returns_compact_refs(
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
                "artifact_bucket": "payload-bucket-must-be-ignored",
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
    }
    serialized_result = json.dumps(result.result, sort_keys=True)
    assert "field_evidence" not in serialized_result
    assert "secret-cookie-must-not-leak" not in serialized_result
    assert "payload-must-be-ignored" not in serialized_result
    assert "payload-secret-must-be-ignored" not in serialized_result

    base = "dev/raw-captures/amazon/us/B0CHILD001/2026/07/14/run-1"
    normalized_key = f"{base}/normalized.json"
    html_key = f"{base}/page.html.gz"
    assert set(store.uploads) == {
        ("artifacts", normalized_key),
        ("artifacts", html_key),
    }
    normalized_bytes = store.uploads[("artifacts", normalized_key)]
    normalized = json.loads(normalized_bytes)
    assert normalized["profile_context"]["locale"] == "en_US"
    assert normalized["profile_context"]["currency"] == "USD"
    assert normalized["profile_context"]["profile_context_digest"]
    assert hashlib.sha256(normalized_bytes).hexdigest() == (
        result.result["normalized_capture_ref"]["content_digest"]
    )
    sanitized_html = gzip.decompress(store.uploads[("artifacts", html_key)]).decode()
    assert "Structured product title" in sanitized_html
    assert "secret-cookie-must-not-leak" not in sanitized_html
    assert "secret-token-must-not-leak" not in sanitized_html
    assert "secret-workspace-must-not-leak" not in sanitized_html
    assert len(result.result["raw_capture_refs"]) == 2
    assert result.result["raw_capture_refs"][1]["sanitization_status"] == "sanitized"
    assert result.result["field_coverage"]["percentage"] == 100.0
    assert result.result["media_source_refs"] == [
        {
            "source_url": "https://images.example.test/structured-main.jpg",
            "source_platform": "amazon",
            "marketplace_code": "US",
            "product_id": "B0CHILD001",
            "media_role": "main_image",
            "position": 0,
        },
        {
            "source_url": "https://images.example.test/structured-gallery-1.jpg",
            "source_platform": "amazon",
            "marketplace_code": "US",
            "product_id": "B0CHILD001",
            "media_role": "gallery_image",
            "position": 1,
        },
        {
            "source_url": "https://images.example.test/structured-gallery-2.jpg",
            "source_platform": "amazon",
            "marketplace_code": "US",
            "product_id": "B0CHILD001",
            "media_role": "gallery_image",
            "position": 2,
        },
    ]


def test_html_sanitizer_removes_state_and_account_secrets_but_keeps_product_evidence() -> None:
    html = """
    <html><body>
      <div id="nav-link-accountList">Private Customer Name</div>
      <span id="glow-ingress-line2">Private Street Address</span>
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

        def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
            self.goto_calls.append((url, wait_until, timeout))

        def wait_for_load_state(self, state: str, *, timeout: int) -> None:
            assert state == "domcontentloaded"
            assert timeout == 5000

        def wait_for_selector(self, selector: str, *, timeout: int) -> None:
            assert "#productTitle" in selector
            assert timeout == 5000

        def evaluate(self, script: str) -> None:
            assert "scrollTo" in script

        def content(self) -> str:
            return _fixture("product_detail_child.html")

        def screenshot(self, *, full_page: bool) -> bytes:
            self.screenshot_calls += 1
            return b"unexpected"

    page = Page()

    @contextmanager
    def open_page(*, profile_ref: str):
        assert profile_ref == "amazon-us-profile"
        yield SimpleNamespace(page=page, target_key="private-target-key")

    monkeypatch.setattr(handler_module, "open_automation_page", open_page)

    collection = handler_module._collect_browser_page(
        requested_asin="B0CHILD001",
        canonical_url="https://www.amazon.com/dp/B0CHILD001",
        profile_ref="amazon-us-profile",
        observed_at=OBSERVED_AT,
        timeout_ms=5000,
    )

    assert collection["capture"]["resolved_asin"] == "B0CHILD001"
    assert collection["browser_target_digest"] == hashlib.sha256(
        b"private-target-key"
    ).hexdigest()
    assert "target_key" not in collection
    assert page.screenshot_calls == 0


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
    assert [ref["capture_kind"] for ref in result.result["artifact_refs"]] == [
        "screenshot"
    ]


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

    missing_profile = amazon_product_browser_fetch_handler(
        _context(store=FakeArtifactStore())
    )

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
def test_terminal_page_errors_upload_sanitized_html_and_screenshot(
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
            "browser_target_digest": "target-digest",
            "screenshot_bytes": b"png-evidence",
            "error": error,
        },
    )

    result = amazon_product_browser_fetch_handler(
        _context(asin="B0BLOCK001", store=store)
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == expected_error_code
    assert result.error.retryable is False
    assert result.summary["collection_status"] == expected_status
    assert "normalized_capture_ref" not in result.result
    assert {ref["capture_kind"] for ref in result.result["artifact_refs"]} == {
        "html",
        "screenshot",
    }
    uploaded_html = next(
        payload for (_, key), payload in store.uploads.items() if key.endswith("page.html.gz")
    )
    assert "secret-token-must-not-leak" not in gzip.decompress(uploaded_html).decode()
    assert any(key.endswith("page.png") for _, key in store.uploads)


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

    result = amazon_product_browser_fetch_handler(
        _context(asin="B0BLOCK001", store=store)
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "required_failure_evidence_missing"
    assert result.error.retryable is True
    assert len(store.uploads) == 1
    assert next(iter(store.uploads))[1].endswith("page.html.gz")


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
    parent_result = amazon_product_browser_fetch_handler(
        _context(asin="B0PARENT01", store=store)
    )

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
                request_id="request-1",
                execution_id="older-execution",
                run_id="run-1",
                step_id="older-step",
                kind="older",
                bucket="artifacts",
                object_key="dev/runs/run-1/older.json",
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
        assert run_id == "run-1"
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
    context = _context(store=FakeArtifactStore())
    artifact_ref = {
        "capture_kind": "html",
        "bucket": "artifacts",
        "object_key": "dev/raw-captures/amazon/us/B0CHILD001/2026/07/14/run-1/page.html.gz",
        "etag": "etag-new",
        "size": 123,
        "content_type": "application/gzip",
        "content_digest": "digest",
        "sanitization_status": "sanitized",
        "created_at_epoch": 2.0,
        "remote_uri": "s3://artifacts/dev/raw/page.html.gz",
    }
    if failed:
        worker_result = HandlerResult.failed(
            context,
            error=HandlerError(
                error_type="browser_failure",
                error_code="captcha_required",
                message="blocked",
                retryable=False,
            ),
            result={"artifact_refs": [artifact_ref]},
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
            result={"artifact_refs": [artifact_ref]},
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

    assert {record.artifact_id for record in store.replaced} == {
        "existing",
        hashlib.sha256(
            b"run-1:artifacts:dev/raw-captures/amazon/us/B0CHILD001/2026/07/14/run-1/page.html.gz"
        ).hexdigest(),
    }
    new_record = next(record for record in store.replaced if record.artifact_id != "existing")
    assert new_record.request_id == "request-1"
    assert new_record.execution_id == "execution-1"
    assert new_record.run_id == "run-1"
    assert new_record.step_id == "collect_amazon_product_detail"
    if failed:
        assert store.marked is not None
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
    second_new = next(record for record in second_store.replaced if record.artifact_id != "existing")
    assert first_new.artifact_id == second_new.artifact_id
    assert first_new.run_id == second_new.run_id == "run-1"


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
    )
    worker_result = HandlerResult.failed(
        context,
        error=HandlerError(
            error_type="browser_failure",
            error_code=error_code,
            message=error_code,
            retryable=retryable,
        ),
        result={"artifact_refs": []},
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
            "run_id": "capture-run-1",
            "stage_code": "collect_amazon_product_detail",
        },
        resource_code="browser:amazon-us-profile",
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
