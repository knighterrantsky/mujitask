# 依赖说明

更新时间: 2026-04-29

本文说明 Mujitask 的 Python 依赖、开发依赖、外部运行依赖和升级规则。

## 1. Python 依赖

依赖来源 `pyproject.toml`，当前要求 Python >= 3.11。

## 2. 核心运行依赖

| 依赖 | 用途 |
| --- | --- |
| `automation-framework[captcha]` | 自动化框架 |
| `SQLAlchemy` | Runtime DB / Fact DB ORM 与连接 |
| `psycopg[binary]` | Postgres 驱动 |
| `alembic` | 数据库 migration |
| `minio` | Artifact object store |
| `requests` | HTTP 请求 |
| `uvicorn` | 本地 Agent API 服务 |

## 3. 开发依赖

| 依赖 | 用途 |
| --- | --- |
| `pytest` | 自动化测试 |
| `ruff` | 代码检查 |
| `httpx` | 测试 HTTP 调用 |
| `PyYAML` | YAML 配置和 contract 读取 |

## 4. Framework 依赖

当前项目依赖 `automation-framework`。升级规则:

1. 先查看 `automation-framework` 对应版本的公开接口、contract 和迁移说明
2. 更新 `pyproject.toml`
3. 重新安装依赖: `uv sync --extra dev`
4. 运行测试
5. 只在必要时调整 framework 接入代码

## 5. 外部运行依赖

| 服务 | 用途 | 说明 |
| --- | --- | --- |
| Postgres | Runtime DB / Fact DB | 本地开发可通过 socket 连接 |
| MinIO | Artifact object store | 用于存储运行时产物 |
| Chromium | 浏览器自动化 | 通过 Playwright 安装 |

## 6. 不要做的事

- 不要在本仓库复制维护 framework contract
- 不要直接依赖 framework 内部模块（如 `automation_framework.browser.*`、`automation_framework.runtime.engine`）
- 不要绕过 `pyproject.toml` 手工安装隐式依赖
- DB URL 格式在 Python/SQLAlchemy 中是 `postgresql+psycopg://`，psql CLI 中是 `postgresql://`
