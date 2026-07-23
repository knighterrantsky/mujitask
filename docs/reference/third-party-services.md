# 第三方服务接入说明

更新时间: 2026-07-23

本文说明 Mujitask 当前依赖的外部服务、配置项、用途和排障入口。

## 1. 服务总览

| 服务 | 用途 | 配置位置 |
| --- | --- | --- |
| 飞书 Base | 读取和写回业务表 | `skill.local.env` |
| FastMoss | 商品、达人、竞品数据采集 | `skill.local.env` |
| TikTok / TikTok Shop | 商品、达人、视频事实来源 | browser profile / FastMoss / handler |
| OpenClaw | Agent skill 调用和通知上下文 | `skill.local.env` |
| Postgres | Runtime DB / Fact DB | `executor.local.env` |
| MinIO | 白名单长期业务对象存储；不承载通用 Runtime artifact | `executor.local.env` |
| Browser Profile | 浏览器登录态和 Cookie 共享 | `.env` / `skill.local.env` / `config/browser_profiles.json` |

## 2. 飞书接入

配置在 `skills/mujitask-tiktok-feishu-sync/skill.local.env`，使用英文 alias 路由表配置，不维护第二套中文 key。完整 URL 由 Base URL + Table ID + View ID 拼出。

当前 Mujitask 业务会通过飞书 Base 读写若干 TikTok 运营表（例如选品、竞品、达人池等）。具体表结构、字段含义、自动维护字段和验收口径以 `docs/business` 为准。

## 3. FastMoss 接入

配置在 `skill.local.env`，基于 cookie 的 session 认证。

主要采集能力:

- 商品搜索（关键词、条件筛选）
- 商品详情（7/28/90 天多窗口销量数据）
- 达人数据
- 店铺数据
- 视频数据

安全验证兜底: 当 FastMoss 返回 `MSG_SAFE_0001` 时，自动走 browser 安全验证流程。

## 4. TikTok 接入

TikTok 访问依赖浏览器 profile、Cookie 和相关事实采集 handler。数据采集优先走 request/API 路径，browser 只作为 fallback。

配置在项目运行环境或 `.env`，不放在 skill 固定配置中:

```text
BROWSER_PROFILE_REF
DEFAULT_PROFILE_REF
BROWSER_PROFILES_FILE
```

## 5. OpenClaw 接入

配置在 `skill.local.env`，用于 agent skill 触发任务、自动识别 delivery context 和发送最终任务通知。

## 6. Postgres 接入

配置在 `scripts/execution_control/executor.local.env`:

```text
BUSINESS_EXECUTION_CONTROL_DB_URL
TEST_DATABASE_URL
```

Postgres 用于 Runtime DB / Fact DB。具体 Runtime 表结构以 `docs/arch/runtime-db-schema-design.md` 为准，Fact DB 表结构以 `docs/arch/fact-db-schema-design.md` 为准。

## 7. MinIO 接入

配置在 `scripts/execution_control/executor.local.env`:

```text
BUSINESS_EXECUTION_CONTROL_ARTIFACT_STORE_PROVIDER
BUSINESS_EXECUTION_CONTROL_MINIO_ENDPOINT
BUSINESS_EXECUTION_CONTROL_MINIO_ACCESS_KEY
BUSINESS_EXECUTION_CONTROL_MINIO_SECRET_KEY
BUSINESS_EXECUTION_CONTROL_ARTIFACT_BUCKET
```

具体 Storage 架构和 artifact contract 以 `docs/arch/storage-architecture-design.md` 为准。

## 8. 安全规则

- 不提交真实 token、账号、密码
- 真实配置只放本地 `.env` / `*.local.env`
- 示例文件只保留占位值
- 第三方 API 返回样例如含敏感信息，应脱敏后再提交到 `docs/reference`
