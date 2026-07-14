# Amazon Product Detail Ingest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the first usable Amazon US single-product collection path: a Feishu row identified by `record_id` supplies an ASIN, a configured Chrome/fingerprint browser collects and parses the Amazon detail page, raw evidence and media are materialized in the existing object bucket, Amazon-specific facts are persisted idempotently, and observed fields are written back to the same Feishu row.

**Architecture:** Add an `amazon` business domain with a thin formal Task/Workflow and a four-stage runtime (`read_amazon_product_row` → `collect_amazon_product_detail` → `persist_amazon_product_detail` → `ready_for_summary`). The browser worker owns page access and layered extraction, while the API row-persist handler serially invokes the existing media and Feishu capabilities plus a new Amazon-specific fact upsert handler. Runtime DB remains unchanged; the existing Fact DB receives isolated `amazon_*` tables through Alembic; the existing object bucket receives Amazon-specific prefixes.

**Tech Stack:** Python 3.11, automation-framework public runtime contract, Playwright-compatible browser bridge, SQLAlchemy/PostgreSQL, Alembic, MinIO/S3 artifact store, Feishu Bitable handlers, pytest, YAML contracts.

**Global Constraints:** US marketplace only; ASIN is normalized to uppercase and must match `^[A-Z0-9]{10}$`; formal task payload contains business inputs only; no production DDL from workers; no new generic helper/service/manager abstraction; no CAPTCHA or block bypass; missing fields never erase old Feishu values; only the requested ASIN page is visited; batch/search are out of scope; no `.platform/**`, `src/automation_business_scaffold/agent.py`, `src/automation_business_scaffold/registry.py`, or framework package edits.

---

## Task 1: Establish formal business and machine contracts

**Files:**

- Create: `docs/business/requirements/amazon-product-detail-collection.md`
- Create: `docs/domains/amazon-product-detail/README.md`
- Create: `contracts/fields/feishu-amazon-products.yaml`
- Create: `contracts/states/amazon-product-collection-status.yaml`
- Create: `contracts/workflow/refresh_amazon_product_row_by_asin.yaml`
- Modify: `contracts/facts/product-fact-collection.yaml`
- Modify: `contracts/harness/architecture-ownership.yaml`
- Modify: `contracts/harness/code-roadmap.yaml`
- Modify: `contracts/README.md`
- Modify: `docs/arch/workflow-amazon-product-detail-design.md`
- Test: `tests/test_amazon_product_contracts.py`

- [ ] Write failing contract tests that assert the formal task/workflow code, four stable stages, three new handlers, US-only identity rule, Feishu field ownership, Amazon fact tables, object prefixes, owner boundaries, and a roadmap feature named `amazon_single_product_ingest`.
- [ ] Run `uv run --extra dev pytest tests/test_amazon_product_contracts.py -q` and confirm failure because contracts do not yet exist.
- [ ] Add the requirement and domain route documents, then encode fields, statuses, workflow, Fact DB boundaries, artifact prefixes, and handler ownership in YAML.
- [ ] Add an `amazon_single_product_ingest` roadmap item with `requires_architecture_delta_gate: true`, exact allowed paths, source contracts, tests, and done-gate commands; keep status `in_progress` until final verification.
- [ ] Update the design document status to approved/implementing without claiming the capability is complete.
- [ ] Run the contract test and existing contract harness tests; confirm they pass.
- [ ] Commit with `docs: define amazon product ingest contracts`.

## Task 2: Add Amazon identity, normalized capture, and layered HTML parser

**Files:**

- Create: `src/automation_business_scaffold/capabilities/browser/amazon/__init__.py`
- Create: `src/automation_business_scaffold/capabilities/browser/amazon/product_page.py`
- Create: `tests/fixtures/amazon/product_detail_child.html`
- Create: `tests/fixtures/amazon/product_detail_unavailable.html`
- Create: `tests/fixtures/amazon/product_detail_blocked.html`
- Create: `tests/test_amazon_product_page.py`

- [ ] Write failing tests for ASIN normalization, canonical URL creation, Amazon.com URL extraction, marketplace rejection, requested/resolved/parent identity classification, and deterministic extraction of every first-version field from saved HTML.
- [ ] Add fixture HTML containing JSON-LD, embedded state, stable DOM nodes, variant links, offer data, BSR, and technical details; add explicit unavailable and blocked fixtures.
- [ ] Implement `normalize_asin(value)`, `canonical_amazon_url(asin)`, `extract_asin_from_url(url)`, `extract_amazon_product_capture(html, requested_asin, resolved_url, observed_at)`, and typed extraction errors in `product_page.py`.
- [ ] Implement precedence as embedded structured data → embedded product state → stable DOM → controlled text sections, with per-field `status`, `source_kind`, `source_locator`, and `confidence` metadata.
- [ ] Ensure the capture contains only normalized JSON-safe values, media source URLs, compact provenance, and no cookies/profile secrets.
- [ ] Run `uv run --extra dev pytest tests/test_amazon_product_page.py -q` and confirm all parser/identity cases pass.
- [ ] Commit with `feat: add amazon product page parser`.

## Task 3: Add isolated Amazon Fact schema and migration

**Files:**

- Create: `alembic/versions/20260714_0007_amazon_product_facts.py`
- Create: `src/automation_business_scaffold/infrastructure/schemas/amazon_fact_schema.py`
- Modify: `src/automation_business_scaffold/infrastructure/schemas/__init__.py`
- Modify: `docs/arch/fact-db-schema-design.md`
- Create: `tests/test_amazon_fact_schema.py`
- Modify: `tests/conftest.py`

- [ ] Write failing Postgres tests asserting creation of `amazon_products`, `amazon_product_snapshots`, `amazon_offer_snapshots`, `amazon_product_variants`, `amazon_bsr_snapshots`, `amazon_media_assets`, `amazon_product_media_assets`, `amazon_raw_captures`, and `amazon_feishu_bindings`, including their unique keys and indexes.
- [ ] Add `ensure_amazon_fact_schema(connection)` for explicit local/test bootstrap only; do not call it from a store constructor or worker.
- [ ] Add Alembic revision `20260714_0007` after `20260528_0006`, with a reversible downgrade that drops only Amazon tables/indexes.
- [ ] Extend the isolated test bootstrap to invoke `ensure_amazon_fact_schema` so store tests use the same schema contract.
- [ ] Document defaults, old-data compatibility, rollback, and old-worker behavior in the Fact DB design.
- [ ] Run `uv run --extra dev pytest tests/test_amazon_fact_schema.py -q` and the existing migration/store tests.
- [ ] Commit with `feat: add amazon fact schema`.

## Task 4: Implement Amazon Fact Store and strict upsert handler

**Files:**

- Create: `src/automation_business_scaffold/infrastructure/facts/amazon_fact_store.py`
- Create: `src/automation_business_scaffold/capabilities/persistence/database/amazon_product_fact_upsert_handler.py`
- Create: `src/automation_business_scaffold/domains/amazon/jobs/amazon_product_fact_upsert.py`
- Modify: `src/automation_business_scaffold/contracts/handler/allowlist.py`
- Modify: `src/automation_business_scaffold/contracts/handler/api.py`
- Create: `tests/test_amazon_fact_store.py`
- Create: `tests/test_amazon_product_fact_upsert_handler.py`

- [ ] Write failing store tests for idempotent `(marketplace_code, asin)` master upsert, append-only snapshots, deterministic snapshot dedupe, offer/variant/BSR persistence, media relations, raw capture refs, and `(source_table_ref, source_record_id)` binding upsert.
- [ ] Implement `AmazonFactStore` with explicit SQL methods and no DDL; retain the original row identity and return compact mutation counts/IDs.
- [ ] Write failing handler tests for strict Fact DB/object-store configuration, capture artifact loading, invalid capture payload, success, repeated execution, unavailable product, and missing raw-capture evidence.
- [ ] Implement `amazon_product_fact_upsert_handler(context)` using project runtime Fact DB/object-store config or test-only overrides; load the capture from its artifact coordinates, return in-process projection facts plus compact refs/counts, and ensure the enclosing row handler does not persist the full projection/capture in Runtime DB.
- [ ] Add the handler contract, API allowlist entry, binding, and job definition.
- [ ] Run the two new test modules plus registry and architecture tests.
- [ ] Commit with `feat: persist amazon product facts`.

## Task 5: Implement browser collection and artifact materialization

**Files:**

- Create: `src/automation_business_scaffold/capabilities/browser/amazon_product_fetch_handler.py`
- Create: `src/automation_business_scaffold/domains/amazon/jobs/amazon_product_browser_fetch.py`
- Modify: `src/automation_business_scaffold/infrastructure/artifacts/artifact_store.py`
- Modify: `src/automation_business_scaffold/infrastructure/artifacts/minio_store.py`
- Modify: `src/automation_business_scaffold/contracts/handler/allowlist.py`
- Modify: `src/automation_business_scaffold/contracts/handler/browser.py`
- Modify: `src/automation_business_scaffold/control_plane/executor/worker_dispatch.py`
- Create: `tests/test_amazon_product_browser_fetch_handler.py`
- Modify: `tests/test_handler_registry_contract.py`

- [ ] Write failing handler tests with a fake browser page for configured-profile resolution, canonical navigation, HTML capture, parser output, object prefix `amazon/raw/US/{asin}/...`, compact Runtime result, unavailable terminal result, identity mismatch, blocked/access-limited failure, and missing object-storage configuration.
- [ ] Implement the browser handler on `infrastructure/browser/browser_bridge.open_automation_page`, resolving `AMAZON_US_BROWSER_PROFILE_REF` then framework default configuration without accepting profile secrets in formal payloads.
- [ ] Extend the existing Artifact Store protocol and MinIO implementation with byte reads so downstream capability handlers can resolve a capture artifact by bucket/object key; keep this low-level read inside infrastructure ownership.
- [ ] Save HTML, normalized capture JSON, and optional screenshot to temporary files; upload them through the existing artifact store and return `raw_capture_ref`, capture artifact coordinates, artifact metadata, identity, collection status, coverage, and media source refs only.
- [ ] Add `amazon_product_browser_fetch` to the browser job contract, registry binding, and `BROWSER_HANDLER_CODES`-driven browser claim filter; remove the hard-coded two-handler tuple from `worker_dispatch.py`.
- [ ] Index returned artifact records in `artifact_object` from the existing worker-dispatch persistence boundary so the runtime can audit Amazon captures without embedding their contents.
- [ ] Run the browser tests, existing TikTok browser/fallback tests, handler registry tests, and control-plane structure tests.
- [ ] Commit with `feat: collect amazon product detail in browser`.

## Task 6: Add Amazon Feishu source adapter and projection mapper

**Files:**

- Create: `src/automation_business_scaffold/domains/amazon/__init__.py`
- Create: `src/automation_business_scaffold/domains/amazon/mappers/__init__.py`
- Create: `src/automation_business_scaffold/domains/amazon/mappers/feishu_amazon_product_row_mapper.py`
- Create: `src/automation_business_scaffold/domains/amazon/mappers/registry.py`
- Create: `src/automation_business_scaffold/domains/amazon/projections/__init__.py`
- Create: `src/automation_business_scaffold/domains/amazon/projections/feishu_amazon_product_projection.py`
- Create: `src/automation_business_scaffold/domains/amazon/projections/registry.py`
- Modify: `src/automation_business_scaffold/contracts/handler/domain_mapping.py`
- Create: `tests/test_feishu_amazon_product_mapping.py`
- Modify: `tests/test_feishu_common_handlers.py`

- [ ] Write failing adapter tests for explicit `record_id`, ASIN normalization, URL consistency, duplicate rejection, invalid/missing ASIN errors, and compact source context.
- [ ] Implement `amazon_product_table_source_adapter` so a single-row request returns exactly one `source_row` with `source_record_id`, `source_table_ref`, `product_identity`, and compact prior field evidence.
- [ ] Write failing projection tests covering all approved fields and the invariant that `missing` values do not clear existing Feishu fields; verify links, numbers, JSON/text serialization, media attachments, state, timestamps, and raw capture reference.
- [ ] Implement `amazon_product_projection_mapper` and route Amazon adapter/mapper codes from the existing handler domain mapping without changing TikTok mapping behavior.
- [ ] Run the new mapping tests and existing Feishu common-handler tests.
- [ ] Commit with `feat: map amazon feishu product rows`.

## Task 7: Implement the serial row-persist business handler

**Files:**

- Create: `src/automation_business_scaffold/domains/amazon/jobs/__init__.py`
- Create: `src/automation_business_scaffold/domains/amazon/jobs/amazon_product_row_persist.py`
- Create: `src/automation_business_scaffold/domains/amazon/flows/__init__.py`
- Create: `src/automation_business_scaffold/domains/amazon/flows/amazon_product_row_persist/__init__.py`
- Create: `src/automation_business_scaffold/domains/amazon/flows/amazon_product_row_persist/orchestrator.py`
- Modify: `src/automation_business_scaffold/contracts/handler/allowlist.py`
- Modify: `src/automation_business_scaffold/contracts/handler/api.py`
- Create: `tests/test_amazon_product_row_persist.py`

- [ ] Write failing tests using bound fake handlers for the serial order `media_asset_sync` → `amazon_product_fact_upsert` → `feishu_table_write`, compact result storage, and the same source `record_id` writeback.
- [ ] Cover partial/missing field projection, unavailable terminal state, media failure, fact failure, Feishu write failure after facts succeed, and idempotent retry inputs.
- [ ] Implement `run_amazon_product_row_persist_flow(context, dispatch=...)` as the row-level business boundary; construct child `HandlerContext` values while keeping external clients out of the flow.
- [ ] Implement `amazon_product_row_persist_handler`, its API contract/job definition, and registry binding.
- [ ] Ensure every successful image reference has a materialized object key/remote URI before Amazon media facts are upserted.
- [ ] Run the new row-persist tests, media tests, fact handler tests, and registry tests.
- [ ] Commit with `feat: add amazon row persistence flow`.

## Task 8: Add the formal Task, Workflow, and runtime stage orchestration

**Files:**

- Create: `src/automation_business_scaffold/domains/amazon/tasks/__init__.py`
- Create: `src/automation_business_scaffold/domains/amazon/tasks/refresh_amazon_product_row_by_asin.py`
- Create: `src/automation_business_scaffold/domains/amazon/workflows/__init__.py`
- Create: `src/automation_business_scaffold/domains/amazon/workflows/refresh_amazon_product_row_by_asin.py`
- Create: `src/automation_business_scaffold/domains/amazon/flows/refresh_amazon_product_row_by_asin/__init__.py`
- Create: `src/automation_business_scaffold/domains/amazon/flows/refresh_amazon_product_row_by_asin/orchestrator.py`
- Create: `src/automation_business_scaffold/domains/amazon/flows/refresh_amazon_product_row_by_asin/summary.py`
- Create: `src/automation_business_scaffold/domains/amazon/flows/refresh_amazon_product_row_by_asin/stages/__init__.py`
- Create: `src/automation_business_scaffold/domains/amazon/flows/refresh_amazon_product_row_by_asin/stages/read_amazon_product_row.py`
- Create: `src/automation_business_scaffold/domains/amazon/flows/refresh_amazon_product_row_by_asin/stages/collect_amazon_product_detail.py`
- Create: `src/automation_business_scaffold/domains/amazon/flows/refresh_amazon_product_row_by_asin/stages/persist_amazon_product_detail.py`
- Create: `src/automation_business_scaffold/domains/amazon/flows/refresh_amazon_product_row_by_asin/stages/ready_for_summary.py`
- Create: `src/automation_business_scaffold/contracts/workflow/refresh_amazon_product_row_by_asin.yaml`
- Modify: `src/automation_business_scaffold/control_plane/runtime_config/settings.py`
- Modify: `src/automation_business_scaffold/control_plane/executor/runner.py`
- Modify: `src/automation_business_scaffold/control_plane/executor/workflow_registry.py`
- Create: `tests/test_refresh_amazon_product_row_by_asin.py`
- Create: `tests/test_runtime_amazon_product_ingest.py`
- Modify: `tests/test_workflow_architecture_manifests.py`

- [ ] Write failing workflow-definition tests for business-only payload fields, four stable stages, browser primary execution, job contracts, strict persistence flags, timeout/watchdog/idempotency rules, and YAML/Python manifest parity.
- [ ] Implement the thin task/workflow shells and Amazon jobs package exports.
- [ ] Write failing runtime tests that submit one request, dispatch one Feishu read job, enqueue one browser execution, dispatch one row-persist API job with capture refs, and finalize success/partial/failed summaries.
- [ ] Implement stage modules and the package orchestrator using existing RuntimeStore enqueue/list/update APIs; default row concurrency remains one and no new Runtime table is introduced.
- [ ] Register the task code and runtime module, add the runner facade, formal payload sanitization, strict persistence preflight, and initial stage mapping.
- [ ] Run the workflow/runtime tests plus existing workflow registry, task submit, executor, API worker, and browser worker tests.
- [ ] Commit with `feat: add amazon single product workflow`.

## Task 9: Verify end-to-end behavior and close completion gates

**Files:**

- Create: `tests/test_runtime_amazon_product_business_e2e.py`
- Modify: `contracts/harness/code-roadmap.yaml`
- Modify: `docs/arch/workflow-amazon-product-detail-design.md`
- Modify: `docs/README.md`
- Modify: `README.md`

- [ ] Write an E2E test with inline Feishu records, saved Amazon HTML/fake browser output, fake object store, isolated Postgres schema, and fake Feishu write client; assert Runtime completion, Amazon facts, raw refs, media refs, and same-record writeback.
- [ ] Run the E2E test and fix only defects that trace to this feature.
- [ ] Run focused gates: `uv run --extra dev pytest tests/test_amazon_product_contracts.py tests/test_amazon_product_page.py tests/test_amazon_fact_schema.py tests/test_amazon_fact_store.py tests/test_amazon_product_fact_upsert_handler.py tests/test_amazon_product_browser_fetch_handler.py tests/test_feishu_amazon_product_mapping.py tests/test_amazon_product_row_persist.py tests/test_refresh_amazon_product_row_by_asin.py tests/test_runtime_amazon_product_ingest.py tests/test_runtime_amazon_product_business_e2e.py`.
- [ ] Run regression gates: `uv run --extra dev pytest tests/test_handler_registry_contract.py tests/test_workflow_architecture_manifests.py tests/test_project_structure_contract.py tests/test_project_architecture_contract.py tests/test_system_architecture_contract.py tests/test_architecture_ownership.py tests/test_browser_fallback_boundary.py tests/test_feishu_common_handlers.py tests/test_media_asset_sync_handler.py tests/test_tiktok_product_browser_fetch_handler.py tests/test_runtime_store.py`.
- [ ] Run the full test suite if the focused and regression gates pass.
- [ ] Update the design/index/readme to describe the implemented first-version boundary and explicitly retain batch/search as out of scope.
- [ ] Change the roadmap feature status to `complete`, then run `python scripts/harness/claim_done.py amazon_single_product_ingest --run-gates` and require `claim=complete` with no failed checks.
- [ ] Invoke the verification-before-completion and requesting-code-review skills; address only actionable, in-scope findings and rerun affected gates.
- [ ] Confirm `git status --short`, review the final diff for secrets/unrelated changes, and commit with `feat: implement amazon single product ingest`.
