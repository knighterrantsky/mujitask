# Migration State

日期: 2026-04-25

状态: migration_state

本文只记录“当前仍存在但不是正式结构”的迁移状态，避免把历史兼容事实误读为正式 workflow contract。

## 当前仍存在但不是目标的结构

- `influencer_pool_product_job`
- `influencer_pool_author_job`

这些结构如果在旧代码、旧讨论或历史数据中出现，只能作为迁移来源、兼容事实或清理对象理解。

## 正式结构

- 通用 `api_worker_job`
- 稳定 `job_code`
- 稳定 `stage`
- `business_key`
- `dedupe_key`
- 需要父子收敛时优先使用通用 `parent_job_id`、`job_group`、`entity_type`、`entity_key`

## 允许做什么

- 读取旧表或旧字段做兼容迁移。
- 写迁移脚本或清理脚本。
- 删除旧引用前补充回归测试。
- 在迁移说明中显式引用旧结构作为来源事实。

## 禁止做什么

- 为新 workflow 新增类似业务专用 job 表。
- 在目标 workflow contract 中继续引用旧专用表作为目标 Runtime 结构。
- 因为历史表存在，就把 `influencer_pool_product_job` 或 `influencer_pool_author_job` 当作新增实现的设计依据。
