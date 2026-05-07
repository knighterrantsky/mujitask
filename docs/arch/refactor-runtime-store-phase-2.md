# RuntimeStore Refactor Phase 2

日期: 2026-05-07

状态: RuntimeStore facade 第二阶段拆分说明

## 保持不变的边界

`RuntimeStore` 的 public class path 保持不变:

```text
automation_business_scaffold.infrastructure.runtime.runtime_store.RuntimeStore
```

本阶段不新增 runtime role、daemon、service 或 worker。Runtime DB 仍由现有 `executor`、`api_worker`、`browser_runloop`、`outbox_dispatcher`、`watchdog` 使用。

## 本阶段抽出的 owner

`RuntimeStore` 继续作为 façade，但以下 table/aggregate ownership 已移到 repository 或 query:

- `repositories/api_worker_job_repo.py`: `api_worker_job` enqueue、claim、heartbeat、progress、success/retry/fail、load/list/summarize。
- `repositories/task_execution_repo.py`: `task_execution` browser execution enqueue、claim、heartbeat、progress、terminal status 和 load。
- `repositories/notification_outbox_repo.py`: `notification_outbox` create/load、claim、heartbeat、progress、sent、retry/fail、expired sending reclaim。
- `repositories/artifact_object_repo.py`: `artifact_object` replace persistence。
- `repositories/influencer_pool_job_repo.py`: `influencer_pool_product_job` 和 `influencer_pool_author_job` table persistence。
- `queries/db_health_query.py`: Postgres connection health read-only query。
- `queries/request_status_query.py`: request status、child executions、outbox、artifact read model。
- `queries/watchdog_query.py`: watchdog scan row primitive query。

`RuntimeStore` methods for these areas now delegate to the repository/query instances created in `RuntimeStore.__init__`.

## Explicit Bootstrap

Runtime schema DDL remains in the explicit bootstrap path:

```text
infrastructure/runtime/bootstrap.py
  -> infrastructure/schemas/runtime_schema.ensure_runtime_schema
```

`RuntimeStore.__init__` does not call bootstrap and does not execute DDL. Runtime claim paths check schema availability with a read-only `to_regclass('task_request')` probe and raise the clear `schema_version.py` message when the schema is missing. This keeps runtime execution fail-fast without silently creating or changing tables.

Local bootstrap remains explicit through `RuntimeStore.bootstrap_schema()` or the existing bootstrap/migration scripts.

## Intentionally Left In RuntimeStore

This phase does not attempt to empty `runtime_store.py`. The following remain intentionally in the façade for a later, smaller pass:

- `task_request` submit/update/claim details that are still tightly coupled to request lifecycle and child count refresh.
- request child aggregation and reconciliation glue shared by executor, watchdog, and child completion.
- watchdog action application across multiple runtime tables.
- FastMoss cookie cache persistence, which should be extracted separately by `fastmoss_session_cookie_cache` table ownership.
- row conversion helpers used by multiple repositories during the transition.

The next extraction should keep the same rule: move one coherent table or read model owner at a time, without changing routing keys or runtime topology.
