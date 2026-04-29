# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Mujitask is a TikTok / FastMoss / Feishu e-commerce automation platform. It runs long-running workflows that scrape product data from TikTok and FastMoss, persist facts to a database, and write back to Feishu tables.

## Environment & startup

This project uses `uv` with Python 3.11+. The virtualenv is at `.venv/`.

```bash
source .venv/bin/activate
uv pip install -e ".[dev]"
```

Three env files are auto-loaded by the runtime (priority order):
1. `scripts/execution_control/executor.local.env` — Runtime DB, MinIO, worker config
2. `skills/mujitask-tiktok-feishu-sync/skill.local.env` — Feishu/FastMoss credentials
3. `.env` — browser profiles, general debug defaults

**Local Postgres** runs via socket at `/tmp`:
```bash
psql -h /tmp -U happyzhao -d automation_business_scaffold
```
Key tables: `task_request`, `api_worker_job`, `task_execution`, `notification_outbox`.

## Commands

```bash
# Run all tests (must use --extra dev to avoid picking up global pytest)
uv run --extra dev pytest

# Run a specific test file
uv run --extra dev pytest tests/test_fastmoss_fact_mappers.py

# Run a single test
uv run --extra dev pytest tests/test_fastmoss_fact_mappers.py::test_map_fastmoss_goods_base_extracts_product_shop_relation_and_media

# Postgres-dependent tests
bash scripts/execution_control/run_local_postgres_tests.sh

# Lint
uv run --extra dev ruff check src/

# Run daemons (for local debugging, --once exits after one poll cycle)
automation-business-scaffold-executor --once
automation-business-scaffold-api-worker --once
automation-business-scaffold-browser-runloop --once
automation-business-scaffold-outbox-dispatcher --once
automation-business-scaffold-watchdog --once

# Submit a task
automation-business-scaffold-run run --task refresh_current_competitor_table --params-json '{"control_action":"submit",...}'

# Check task status
automation-business-scaffold-run run --task refresh_current_competitor_table --params-json '{"control_action":"status","request_id":"..."}'

# List registered tasks
automation-business-scaffold-run list-tasks

# DB migration
alembic upgrade head
```

## Architecture

### Layered structure

```
apps/          — Process entry points (daemon mains, CLI, RPC server). No business logic.
control_plane/ — Task lifecycle, executor, supervisor, reconciler, watchdog, outbox. No business logic.
domains/       — Business task, workflow, job, mapper, projection, policy, flow. ALL new business code goes here.
capabilities/  — Reusable handlers: input_sources, fact_sources, persistence, channels, browser, media.
infrastructure/— External system clients, stores, schemas, rate_limit. No task/workflow references.
contracts/     — Stable contracts for workflow, handler, runtime, config, outbox.
```

### The 5 workflows (task codes)

| Task code | What it does |
|---|---|
| `search_keyword_competitor_products` | Keyword search → FastMoss → seed rows → detail enrich → writeback to **TK竞品收集** |
| `refresh_current_competitor_table` | Read competitor table → fan out `competitor_row_refresh` per row → collect results |
| `sync_tk_influencer_pool` | Discover creators from competitor products → sync to influencer pool |
| `tiktok_fastmoss_product_ingest` | Single product ingest: TikTok + FastMoss → facts → writeback to **TK选品收集** |
| `refresh_competitor_row_by_url` | Single-row refresh by product URL |

### The 5 daemons

| Daemon | Role |
|---|---|
| executor | Advances workflow stages, enqueues jobs, aggregates child results |
| api-worker | Claims and executes API/HTTP/Feishu/FastMoss jobs |
| browser-runloop | Serial consumer for browser/CDP-requiring tasks |
| outbox-dispatcher | Sends final notifications via configured channels |
| watchdog | Scans for stale leases, timeouts, stuck parents |

### Two Feishu tables

- **TK竞品收集** (Competitor Collection) — The main operational table. 12 auto-maintained fields including `近90天销量`. Fully automated.
- **TK选品收集** (Selection Collection) — The "selection" table. Only 3 auto-maintained fields. 14 fields marked `not_written_by_current_ingest`. Largely manual.

### Key flow: competitor_row_refresh (per-product pipeline)

```
TikTok request fetch → [browser fallback if needed] → media sync → FastMoss fetch (d_type=7,28,90) → fact DB upsert → Feishu writeback
```

## Code boundaries

- **Protected paths** (require explicit user approval to modify): `.platform/**`, `AGENTS.md`, `src/automation_business_scaffold/agent.py`, `src/automation_business_scaffold/registry.py`
- **New business code**: always goes in `domains/tiktok/**`, `capabilities/**`, or `control_plane/**`
- **Contracts**: `contracts/fields/` for Feishu field definitions, `contracts/workflow/` for workflow manifests, `contracts/states/` for state machines

## Conventions

- Use `git switch` to create and switch branches, not `git checkout`. Example: `git switch -c codex/new-feature`
- Handler registration uses `@handler_registry.register("handler_code")` — check `capabilities/contracts/handler/allowlist.py` for the registry
- Workflow stages advance via `advance_stage(store, request, workflow, stage_code)` pattern returning `{"action": "advance", "next_stage": "..."}` or `{"action": "waiting"}`
- Feishu writeback uses `projection_mapper` codes like `competitor_table_projection_mapper`, `competitor_seed_projection_mapper`, `selection_table_projection_mapper`
- Write modes: `insert_if_absent` for seed rows, `upsert` for detail writeback, `fill_missing_only` for field-level policy
- FastMoss API sessions require cookie-based auth; check `fastmoss_settings_from_payload()` and `_has_fastmoss_live_config()` before live fetch
- The `d_type` parameter in FastMoss overview endpoints controls the time window (7, 28, 90 days)
- DB URL format is `postgresql+psycopg://` in Python/SQLAlchemy but `postgresql://` for `psql` CLI
- Tests use `TEST_DATABASE_URL` env var and create temporary schemas per test to avoid polluting the runtime DB

## Completion gates

Feature completion requires passing the claim gate:
```bash
python scripts/harness/claim_done.py <feature_code>
```
Features are defined in `contracts/harness/code-roadmap.yaml`. Without a passing gate, status is `not complete` or `blocked` — never claim "done" speculatively.

## Release flow

1. Feature branch: `codex/<topic>`
2. Merge to `main` via MR
3. Tag on `main`: `git tag -a v3.3.X`
4. Create GitLab release via API (`/api/v4/projects/11/releases`)
5. Follow `docs/ops/release-flow.md` for the full process
