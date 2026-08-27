# 达人建联契约 Hard-cut 发布 Runbook

适用范围：`tiktok_influencer_outreach_sync` 切换到 `contract_revision=2026-08-17`，或从该 revision 回退。

本次变更删除旧候选/窗口输入并固定飞书逻辑路由与 `采集标签=T` 过滤。新旧 revision 不并行消费；发布和回退都必须先停止提交并排空旧运行实例。

## 1. 停止新任务

1. 暂停 `tiktok_influencer_outreach_sync` 的定时调度。
2. 暂停所有手动入口和 `influencer-outreach-sync-submit` skill/CLI 调用。
3. 在排空完成前，不恢复任何提交入口。

## 2. 枚举并取消旧 request

用 migration/运维只读账号查询 Runtime DB：

```sql
SELECT request_id, status, current_stage, created_at
FROM task_request
WHERE task_code = 'tiktok_influencer_outreach_sync'
  AND status NOT IN ('finished', 'cancelled')
ORDER BY created_at;
```

对查询出的每个 `request_id` 执行：

```bash
automation-business-scaffold-run run \
  --task tiktok_influencer_outreach_sync \
  --params-json '{"control_action":"cancel","request_id":"REPLACE_REQUEST_ID"}'
```

取消会终止 pending/waiting 子任务；已经 running 的 API job 或 browser execution 必须等待其自行进入终态。期间保持 executor、api worker 和 browser worker 运行，使取消状态能够收敛。

## 3. Drain 与验空

重复执行以下查询，直到三个查询都返回 0 行：

```sql
SELECT r.request_id, r.status, r.current_stage
FROM task_request AS r
WHERE r.task_code = 'tiktok_influencer_outreach_sync'
  AND r.status NOT IN ('finished', 'cancelled');
```

```sql
SELECT j.request_id, j.job_id, j.status, j.job_code
FROM api_worker_job AS j
JOIN task_request AS r ON r.request_id = j.request_id
WHERE r.task_code = 'tiktok_influencer_outreach_sync'
  AND j.status IN ('pending', 'running', 'waiting');
```

```sql
SELECT e.request_id, e.execution_id, e.status, e.item_code
FROM task_execution AS e
JOIN task_request AS r ON r.request_id = e.request_id
WHERE r.task_code = 'tiktok_influencer_outreach_sync'
  AND e.status IN ('pending', 'running', 'waiting');
```

验空后再部署 workflow contract、业务代码和 skill。不要依赖 `contract_revision` 自动隔离旧 job 或阻止 fallback requeue。

## 4. 部署后 smoke check

恢复调度前，先通过目标 OpenClaw agent 执行一次明确“不写回飞书”的真实试运行。两轮必须使用同一 session；第一轮只生成预览，第二轮确认后才允许创建 request：

```bash
openclaw agent \
  --agent tiktok-ops \
  --session-key agent:tiktok-ops:outreach-cutover-smoke \
  --message "试运行一次达人建联表检查，不要写回飞书" \
  --json

openclaw agent \
  --agent tiktok-ops \
  --session-key agent:tiktok-ops:outreach-cutover-smoke \
  --message "确认提交" \
  --json
```

确认：

- 第一轮没有创建 `task_request`；第二轮只创建一个 request，返回非空 `request_id`。Runtime 拒绝时 OpenClaw 必须返回失败，不能返回空 request ID 的成功回执。
- request payload 的 `writeback_enabled=false`，不存在 `mock_fastmoss_*`、调用方分页上限或凭据变量名；本次试运行会真实读取飞书、调用 FastMoss 并沉淀 Runtime/Fact 数据，但不得更新飞书记录。
- 读表 job 使用部署配置中的 `TK_INFLUENCER_OUTREACH` 表/视图，固定过滤表达式为 `CurrentValue.[采集标签] = "T"`，并开启 schema validation。
- 读取结果和 summary 的“读取飞书行数”只统计返回的 `T` 行。
- SKU job 和达人刷新 job 的 `trigger_date` 等于 request `created_at` 对应的 `Asia/Shanghai` 日期。
- smoke request 进入预期终态、飞书零写回且 outbox 不含敏感配置后，再由发布负责人决定是否执行一次受控的 `writeback_enabled=true` 生产验收；未批准前不得恢复定时、手动和 CLI 提交。

## 5. 回退

回退执行与发布相同的 stop、cancel、drain、验空流程。三个活跃查询全部为 0 后，回退 contract 和代码，再用旧 revision 对应的 payload 做 smoke check；验收通过前不恢复提交入口。
