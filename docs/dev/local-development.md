# 本地开发与启动说明

更新时间: 2026-04-29

本文说明如何在本机搭建 Mujitask 开发环境、启动本地服务、运行测试和排障。

## 1. 前置要求

- Python >= 3.11
- Git
- Postgres，本地或远程均可
- MinIO（可选，推荐用于完整 Runtime 流程）
- Chromium / Playwright
- 可访问 FastMoss、TikTok、飞书相关账号和配置

## 2. 虚拟环境

```bash
python3 -m venv .venv
source .venv/bin/activate
```

或使用 `uv`:

```bash
uv sync --extra dev
source .venv/bin/activate
```

## 3. 安装依赖

```bash
uv pip install -e ".[dev]"
python -m playwright install chromium
```

## 4. 复制配置文件

```bash
cp .env.example .env
cp config/browser_profiles.example.json config/browser_profiles.json
cp scripts/execution_control/executor.local.env.example scripts/execution_control/executor.local.env
cp skills/mujitask-tiktok-feishu-sync/skill.local.env.example \
  skills/mujitask-tiktok-feishu-sync/skill.local.env
```

至少需要确认这些变量:

- `BUSINESS_EXECUTION_CONTROL_DB_URL`（Runtime DB 连接）
- `BUSINESS_EXECUTION_CONTROL_FACT_DB_URL`（Fact DB 连接；本地可和 Runtime DB 指向同一个 Postgres database；旧 `TK_FACT_DB_URL` 仅作兼容回退）
- `BUSINESS_EXECUTION_CONTROL_ARTIFACT_ROOT`（产物根目录）
- `BUSINESS_EXECUTION_CONTROL_ARTIFACT_BUCKET`（MinIO bucket）
- `BUSINESS_EXECUTION_CONTROL_WORKER_ID`（Worker 标识）

## 5. 数据库准备

```bash
# 创建测试库
createdb automation_business_scaffold_test

# 执行 migration
alembic upgrade head
```

推荐使用 `scripts/execution_control/executor.local.env` 中的 `BUSINESS_EXECUTION_CONTROL_DB_URL`。本地连接示例:

```bash
psql "postgresql://<user>:<password>@127.0.0.1:5432/automation_business_scaffold"
```

如果使用 socket 连接:

```bash
psql -h /tmp -U <postgres_user> -d automation_business_scaffold
```

## 6. 启动本地 Agent API

```bash
uvicorn automation_business_scaffold.apps.rpc_agent.server:app \
  --app-dir src \
  --host 127.0.0.1 \
  --port 8110
```

## 7. 查看已注册任务

```bash
automation-business-scaffold-run list-tasks
```

## 8. 提交任务

```bash
automation-business-scaffold-run run \
  --task refresh_current_competitor_table \
  --params-json '{"control_action":"submit","profile_ref":"main"}'
```

## 9. 本地运行 Daemon（调试模式）

```bash
# 单次轮询
automation-business-scaffold-executor --once
automation-business-scaffold-api-worker --once
automation-business-scaffold-browser-runloop --once
automation-business-scaffold-outbox-dispatcher --once
automation-business-scaffold-watchdog --once
```

## 10. 本地运行测试

```bash
# 全部测试
uv run --extra dev pytest

# Postgres 依赖的测试
bash scripts/execution_control/run_local_postgres_tests.sh
```

不要裸跑 `uv run pytest`（如果当前虚拟环境没有安装 dev 依赖，可能误用系统全局 pytest）。

## 11. 常见问题

### 读不到 Runtime DB

检查 `scripts/execution_control/executor.local.env`，确认配置了 `BUSINESS_EXECUTION_CONTROL_DB_URL`。

### 读不到浏览器 profile

检查 `.env` 和 `config/browser_profiles.json`。

### Skill 配置不生效

检查 `skills/mujitask-tiktok-feishu-sync/skill.local.env`。

### 数据库测试被跳过

确认 `TEST_DATABASE_URL` 已配置或 `createdb automation_business_scaffold_test` 已执行。
