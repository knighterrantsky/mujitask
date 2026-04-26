# Runtime DB 连接稳定性 Runbook

> 状态: Ops 文档。本文约定用户电脑长期运行时，如何避免 Postgres 连接耗尽导致 worker 无法 claim job。

更新时间：`2026-04-26`

## 1. 背景

Mujitask 的生产形态运行在用户电脑上，Runtime DB 通常是本机 Postgres。长期运行时，如果数据库连接被占满，daemon / worker 会出现类似错误:

```text
FATAL: sorry, too many clients already
```

出现该错误时，`executor_daemon`、`api_worker_daemon`、`browser_runloop` 或 `outbox_dispatcher` 可能无法继续读取任务、claim job、写回结果或发送 outbox。业务数据本身不一定有问题，根因通常是连接资源被耗尽。

## 2. 常见原因

常见触发来源:

- 用户电脑打开 pgAdmin、DBeaver 等数据库 GUI，并长期保留大量 idle 连接。
- 本地测试反复启动 CLI / skill / daemon，旧进程或查询连接未及时退出。
- worker 并发、poll 频率或子任务 fan-out 太高，瞬时连接数超过本机 Postgres 上限。
- 某些事实库或辅助脚本绕过 `RuntimeStore`，单独创建无界连接池。
- Postgres 未设置 idle 连接超时，异常 idle 连接不会自动回收。

## 3. 生产稳定性原则

生产环境必须遵守以下原则:

1. 限制连接产生。
2. 自动释放空闲连接。
3. 提前发现连接接近上限。
4. worker 遇到 DB 连接错误时可以退避、重试或等待 watchdog 修复。
5. 用户电脑上的数据库 GUI 只能作为临时排障工具，不应长期连接生产 Runtime DB。

## 4. Postgres 配置建议

用户电脑本机 Postgres 建议设置:

```sql
ALTER SYSTEM SET max_connections = '100';
ALTER SYSTEM SET idle_session_timeout = '10min';
ALTER SYSTEM SET idle_in_transaction_session_timeout = '60s';
SELECT pg_reload_conf();
```

说明:

- `max_connections` 不宜过低，否则 daemon、worker、排障连接会互相挤占。
- `idle_session_timeout` 用于回收普通 idle 连接，主要防 GUI 和陈旧客户端。
- `idle_in_transaction_session_timeout` 必须更短，避免 idle transaction 长时间持锁。
- 具体数值可按用户机器资源调整，但必须有上限和超时。

## 5. Runtime DB 账号约束

生产应拆分账号:

| 账号 | 用途 | 连接策略 |
| --- | --- | --- |
| `mujitask_runtime_user` | daemon / worker / dispatcher / watchdog | 有连接数上限 |
| `mujitask_migration_user` | migration / schema 变更 | 仅发布时使用 |
| `mujitask_readonly_user` | 排障、只读分析 | 有连接数上限 |

示例:

```sql
ALTER ROLE mujitask_runtime_user CONNECTION LIMIT 30;
ALTER ROLE mujitask_readonly_user CONNECTION LIMIT 5;
```

本地单用户开发环境可以继续使用当前系统用户连接，但用户电脑生产部署不应让 runtime 账号拥有无限连接。

## 6. 应用连接策略

当前 Runtime 控制面主路径使用 `RuntimeStore`。`RuntimeStore` 使用 `NullPool + pool_pre_ping`，每次 DB 操作不长期持有池化连接，适合本机小型长期运行。

约束:

- daemon / worker / dispatcher / watchdog 默认应通过 `RuntimeStore` 访问 Runtime DB。
- 事实库写入如需使用同一个 Postgres，应优先复用 `RuntimeStore` 或同等受限连接策略。
- 不允许在业务 flow、handler 或脚本中创建无界 SQLAlchemy engine pool。
- 若确实需要连接池，必须显式设置 `pool_size`、`max_overflow`、`pool_timeout`、`pool_recycle` 和 `pool_pre_ping`。

建议有界池配置:

```python
create_engine(
    db_url,
    future=True,
    pool_size=2,
    max_overflow=0,
    pool_timeout=10,
    pool_recycle=1800,
    pool_pre_ping=True,
)
```

## 7. Worker 并发与退避

生产默认应保守配置:

- `executor_daemon` 只推进顶层 workflow，不执行大量外部 I/O。
- `api_worker_daemon` 必须限制单机并发和 poll 间隔。
- `browser_runloop` 按浏览器 profile / resource 串行。
- `outbox_dispatcher` 限制批量发送数量，避免故障时快速重试占满连接。

当 worker 遇到 DB 连接错误时:

1. 不应把业务 job 直接标记为永久失败。
2. 应退避后重试。
3. watchdog 应能识别 stuck / retry_wait / running 超时状态。
4. 如果连接健康检查失败，应先恢复 DB 连接池健康，再继续消费 job。

## 8. Watchdog 健康检查

watchdog 应定期执行连接健康检查。最低检查项:

```sql
SELECT count(*) AS total_connections
FROM pg_stat_activity;

SELECT state, count(*)
FROM pg_stat_activity
GROUP BY state
ORDER BY count(*) DESC;

SELECT application_name, state, count(*)
FROM pg_stat_activity
GROUP BY application_name, state
ORDER BY count(*) DESC;
```

建议阈值:

| 指标 | 告警阈值 | 动作 |
| --- | --- | --- |
| 总连接数 | `>= max_connections * 0.8` | 暂停提交大批量任务并告警 |
| idle 连接数 | `>= max_connections * 0.5` | 输出连接来源，提示关闭 GUI |
| idle in transaction | `> 0` 且持续超过 60 秒 | 告警并建议终止连接 |
| runtime pending job 长时间不动 | 超过 workflow timeout / heartbeat 规则 | watchdog 修复或 fail |

## 9. Preflight 要求

OpenClaw skill 或部署 smoke test 在提交真实大任务前，应检查:

- Postgres 可以连接。
- 当前连接数低于阈值。
- `task_request / api_worker_job / notification_outbox` 可读写。
- daemon / worker / outbox 进程在线。
- MinIO 可访问。

如果连接数已接近上限，应返回运行环境异常，不继续提交大量 job。

## 10. 临时排障命令

查看连接:

```bash
psql "$DATABASE_URL" -c "
select pid, application_name, state, wait_event_type, wait_event, left(query, 120) as query
from pg_stat_activity
order by backend_start desc;
"
```

统计连接来源:

```bash
psql "$DATABASE_URL" -c "
select application_name, state, count(*)
from pg_stat_activity
group by application_name, state
order by count(*) desc;
"
```

仅在确认是本机 GUI 或陈旧 idle 连接时，才允许临时清理:

```bash
psql "$DATABASE_URL" -c "
select pg_terminate_backend(pid)
from pg_stat_activity
where pid <> pg_backend_pid()
  and state = 'idle'
  and application_name like 'pgAdmin%';
"
```

禁止无差别终止所有连接。不要终止 active migration、active worker 或当前正在写入的业务连接。

## 11. 验收口径

用户电脑生产环境至少满足:

1. 连续运行 24 小时，daemon 不因连接耗尽退出。
2. 提交真实任务后，Runtime DB 连接数不超过阈值。
3. 打开和关闭 pgAdmin 后，idle 连接会被 Postgres 超时回收。
4. outbox 最终消息可以发送。
5. watchdog 能输出 DB 连接健康摘要。

## 12. 当前实现差距

当前已具备:

- Runtime 主路径 `RuntimeStore` 使用 `NullPool + pool_pre_ping`。
- Runtime 状态、job、outbox 都在 Postgres 中可观测。
- task submit / skill submit 会执行 DB connection health preflight，连接数接近上限时拒绝提交。
- watchdog 扫描结果会附带 DB connection health 摘要，只观测和记录，不自动 kill 连接。
- 默认 submit preflight 只按总连接数阈值拒绝；`idle in transaction` 默认观测不拒绝，需要严格拦截时把 `BUSINESS_EXECUTION_CONTROL_DB_HEALTH_MAX_IDLE_IN_TRANSACTION` 显式设为 `0` 或其他上限。
- `TKFactStore` 独立建连路径使用有界连接池，优先仍建议通过 `runtime_store` 复用连接策略。
- Supervisor 会把 `too many clients already` 等 DB 连接异常归类为 retryable infrastructure error。

仍需补齐:

- 部署脚本写入 Postgres idle timeout / role connection limit。
- preflight / watchdog 健康摘要接入 OpenClaw 可见提示和现场监控。
- 持续检查新增 `create_engine` 路径，确保生产使用有界连接策略或复用 `RuntimeStore`。
