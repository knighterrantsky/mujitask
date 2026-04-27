# 项目本地配置

更新时间: `2026-04-24`

本文说明 Mujitask 在本地开发、测试、CLI、daemon、Alembic 和 skill 集成时，如何统一读取项目配置，避免运行时出现“本机明明有 Postgres / MinIO，但进程读不到配置”的问题。

## 1. 当前规则

当前项目会在 Python 包加载时自动尝试读取以下本地配置文件：

1. `scripts/execution_control/executor.local.env`
2. `skills/mujitask-tiktok-feishu-sync/skill.local.env`
3. `.env`

这些文件不会覆盖已经显式传入的进程环境变量。

也就是说，当前优先级是：

1. CLI 参数
2. 当前 shell / launchd / CI 显式导出的环境变量
3. `scripts/execution_control/executor.local.env`
4. `skills/mujitask-tiktok-feishu-sync/skill.local.env`
5. `.env`

## 2. 每个文件应该放什么

### 2.1 `scripts/execution_control/executor.local.env`

这是当前项目级 Runtime 配置的主入口。

应该放：

- `BUSINESS_EXECUTION_CONTROL_DB_URL`
- `BUSINESS_EXECUTION_CONTROL_ARTIFACT_ROOT`
- `BUSINESS_EXECUTION_CONTROL_ARTIFACT_BUCKET`
- `BUSINESS_EXECUTION_CONTROL_ARTIFACT_STORE_PROVIDER`
- `BUSINESS_EXECUTION_CONTROL_ARTIFACT_OBJECT_PREFIX`
- `BUSINESS_EXECUTION_CONTROL_MINIO_ENDPOINT`
- `BUSINESS_EXECUTION_CONTROL_MINIO_ACCESS_KEY`
- `BUSINESS_EXECUTION_CONTROL_MINIO_SECRET_KEY`
- `BUSINESS_EXECUTION_CONTROL_MINIO_REGION`
- `BUSINESS_EXECUTION_CONTROL_MINIO_SECURE`
- `BUSINESS_EXECUTION_CONTROL_MINIO_CREATE_BUCKET`
- `BUSINESS_EXECUTION_CONTROL_SYNC_REFERENCED_FILES`
- `BUSINESS_EXECUTION_CONTROL_REQUESTED_BY`
- `BUSINESS_EXECUTION_CONTROL_WORKER_ID`
- `BUSINESS_EXECUTION_CONTROL_DB_HEALTH_PREFLIGHT_ENABLED`
- `BUSINESS_EXECUTION_CONTROL_DB_HEALTH_MAX_CONNECTION_RATIO`
- `BUSINESS_EXECUTION_CONTROL_DB_HEALTH_MAX_IDLE_IN_TRANSACTION`

这份文件是：

- daemon
- CLI runtime task
- RuntimeStore / TKFactStore
- Alembic
- Postgres 测试

共同依赖的默认来源。

### 2.2 `skills/mujitask-tiktok-feishu-sync/skill.local.env`

这是 skill wrapper 的固定业务输入配置。

应该放：

- `INSTALL_DIR`
- `MUJITASK_FEISHU_BASE_URL`
- `MUJITASK_FEISHU_TK_*_TABLE_ID`
- `MUJITASK_FEISHU_TK_*_VIEW_ID`
- `MUJITASK_FEISHU_ACCESS_TOKEN`
- `BROWSER_PROFILE_REF`
- `FASTMOSS_PHONE`
- `FASTMOSS_PASSWORD`
- `OPENCLAW_*`
- 可选的 `EXECUTION_CONTROL_*` 兼容键

说明：

- skill wrapper 仍然会直接解析这份文件。
- 如果 skill 在项目仓库内运行，运行时代码也会自动读取它。
- 但 Runtime DB / MinIO 的正式默认配置仍建议放在 `executor.local.env`，不要只放 skill 文件里。

### 2.3 `.env`

这是项目根目录的通用本地默认配置。

适合放：

- `BROWSER_PROFILES_FILE`
- `DEFAULT_PROFILE_REF`
- `AGENT_HOST`
- `AGENT_PORT`
- `AGENT_RUN_DIR`
- `AGENT_RECORDING_DIR`
- 本地调试用的通用 `FASTMOSS_*` / `ROXY_*`

不建议把 Runtime DB / MinIO 作为唯一来源只放在 `.env`。

## 3. 当前自动加载覆盖范围

以下入口现在都会自动读取项目配置文件：

- `automation_business_scaffold` Python 包导入
- `automation-business-scaffold-run`
- `automation-business-scaffold-executor`
- `automation-business-scaffold-api-worker`
- `automation-business-scaffold-browser-runloop`
- `automation-business-scaffold-outbox-dispatcher`
- pytest `tests/conftest.py`
- `alembic/env.py`

因此，正常情况下不需要在每次本地运行前手工 `source` 这些文件。

## 4. 推荐本地准备方式

### 4.1 Runtime / MinIO

```bash
cp scripts/execution_control/executor.local.env.example scripts/execution_control/executor.local.env
```

填写：

- Postgres 连接串
- MinIO endpoint / access key / secret key
- artifact bucket / object prefix

### 4.2 Skill

```bash
cp skills/mujitask-tiktok-feishu-sync/skill.local.env.example \
  skills/mujitask-tiktok-feishu-sync/skill.local.env
```

填写：

- 飞书 token
- FastMoss 账号
- 表 URL
- OpenClaw / 浏览器 profile 相关配置

### 4.3 根目录 `.env`

```bash
cp .env.example .env
```

填写浏览器、agent 和通用本地调试配置。

## 5. 诊断口径

当运行时提示：

- 读不到 Postgres
- 读不到 MinIO
- `RuntimeStore requires ... DB_URL`
- `MinIO artifact store requires ...`

先按这个顺序检查：

1. `scripts/execution_control/executor.local.env` 是否存在且字段已填写
2. 当前进程是否传了覆盖性的 CLI 参数或环境变量
3. `skills/mujitask-tiktok-feishu-sync/skill.local.env` 是否只配了 skill 层输入，但没配 Runtime 层连接
4. 本机服务是否真的已启动

## 6. 约束

1. Runtime DB / MinIO 配置以 `executor.local.env` 为准，不再依赖人工每次手工导出。
2. skill 的固定业务输入以 `skill.local.env` 为准，不在对话中动态索取。
3. `.env` 只承载通用本地默认值，不承担完整 Runtime 控制面配置。
4. Runtime DB engine 创建必须收口到 `RuntimeStore` / `create_runtime_store()`；开发时不要在业务 flow 或 handler 中临时 `create_engine()`。
5. 如果新增 Fact DB 或辅助 DB 连接，优先复用已有 `runtime_store`；确实需要独立 engine 时，必须显式设置有界连接池或 `NullPool`。
6. 新增 task submit、watchdog 或 worker 连接健康逻辑时，配置来源仍按本文优先级读取，不允许绕过 `executor.local.env` 私自读取部署脚本变量。
7. `too many clients already` 等 DB 连接错误在开发和测试中应按基础设施错误处理，不应被写成业务失败或字段校验失败。

## 7. DB 连接开发护栏

新增或修改 Runtime / Fact DB 相关代码时，先确认属于哪一类:

| 类型 | 推荐做法 | 禁止事项 |
| --- | --- | --- |
| Runtime 控制面读写 | 使用 `RuntimeStore` | 在 flow / handler 中直接 `create_engine()` |
| Fact DB 与 Runtime DB 共库 | 传入并复用 `runtime_store` | 每个 handler 调用都新建默认 QueuePool |
| 独立 Fact DB | 使用有界连接池或 `NullPool` | 不设置 `pool_size` / `max_overflow` 的默认连接池 |
| preflight / watchdog health check | 通过统一配置解析 DB URL | 从 `skill.local.env` 或部署脚本中私自拼连接串 |

最小有界连接池示例:

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

如果使用 `NullPool`，必须保证每次 DB 操作都通过上下文管理器关闭连接。

开发验证时建议至少检查:

```bash
psql "$DATABASE_URL" -c "select state, count(*) from pg_stat_activity group by state order by count desc;"
```

生产运行的阈值、排障命令和 watchdog 口径见 [../ops/runtime-db-connection-stability.md](../ops/runtime-db-connection-stability.md)。
