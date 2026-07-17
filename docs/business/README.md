# Business 文档索引

更新时间：`2026-07-15`

`docs/business` 只承载客户需求、业务规则、飞书表口径和验收口径。系统架构、Runtime 状态机、数据库 schema、Storage、部署运维、开发调试、外部接口研究不再作为本目录的 active truth source。

## 事实来源边界

`docs/business` 是当前客户需求、业务流程、飞书业务字段含义和验收口径的事实来源。

它不是以下内容的事实来源:

- 系统架构和 workflow 设计: 见 [../arch/README.md](../arch/README.md)。
- 开发、调试和 skill 集成说明: 见 [../dev/README.md](../dev/README.md)。
- 部署、验收、回退和 runbook: 见 [../ops/README.md](../ops/README.md)。
- FastMoss / TikTok 外部接口研究: 见 [../reference/README.md](../reference/README.md)。

## Active 文档

| 文档 | 定位 |
| --- | --- |
| [business-requirements.md](./business-requirements.md) | 客户需求总览、飞书表字段口径、正式流程索引 |
| [requirements/README.md](./requirements/README.md) | 流程需求索引 |
| [requirements/refresh-current-competitor-table.md](./requirements/refresh-current-competitor-table.md) | 竞品采集需求 |
| [requirements/search-keyword-competitor-products.md](./requirements/search-keyword-competitor-products.md) | 关键词搜索竞品写入需求 |
| [requirements/sync-tk-influencer-pool.md](./requirements/sync-tk-influencer-pool.md) | 竞品到达人池同步需求 |
| [requirements/tk-selection-collection.md](./requirements/tk-selection-collection.md) | 选品采集需求 |
| [requirements/search-keyword-selection-products.md](./requirements/search-keyword-selection-products.md) | 关键词搜索选品写入需求 |
| [requirements/amazon-product-detail-collection.md](./requirements/amazon-product-detail-collection.md) | Amazon 美国站单商品详情采集正式需求（实施中） |
| [requirements-backlog.md](./requirements-backlog.md) | 原始待澄清需求和需求候选记录；不能改写原始表述，也不能作为实现事实来源 |
| [feishu-five-table-relationship-analysis.md](./feishu-five-table-relationship-analysis.md) | 飞书业务表结构事实和需求依据 |

## 已迁出或删除的旧文档

旧的架构、数据库、Storage、进程交互、部署、接口研究和历史推进方案已完成整理:

- 当前架构事实来源见 [../arch/README.md](../arch/README.md)。
- 开发和 skill 集成说明见 [../dev/README.md](../dev/README.md)。
- 部署、验收、回退和历史 runbook 见 [../ops/README.md](../ops/README.md)。
- FastMoss 接口和页面研究见 [../reference/README.md](../reference/README.md)。
- 已被 arch 吸收的历史架构文档已从 `docs/business` 删除，不再作为 active truth source。

本轮迁出的文档:

| 原 business 文档 | 新位置 | 当前口径 |
| --- | --- | --- |
| `04-openclaw-skills.md` | [../dev/openclaw-skills.md](../dev/openclaw-skills.md) | 开发/集成说明 |
| `18-TK综合数据表设计.md` | [../arch/future-tk-comprehensive-table-design.md](../arch/future-tk-comprehensive-table-design.md) | 未来数据模型设计草案，不是当前业务表事实 |
| `21-数据采集策略与频率设计.md` | [../arch/data-collection-strategy-design.md](../arch/data-collection-strategy-design.md) | 采集策略设计，不是客户需求口径 |

## 实际可用的 Skill 文件

- Skill 集成说明: [../dev/openclaw-skills.md](../dev/openclaw-skills.md)
- [skills/mujitask-tiktok-feishu-sync/SKILL.md](../../skills/mujitask-tiktok-feishu-sync/SKILL.md)
- [examples/openclaw/deploy-openclaw.sh](../../examples/openclaw/deploy-openclaw.sh)
- [examples/openclaw/update-openclaw.sh](../../examples/openclaw/update-openclaw.sh)
- [examples/openclaw/deploy-openclaw.ps1](../../examples/openclaw/deploy-openclaw.ps1)

## 当前正式业务入口

- `refresh_current_competitor_table`
- `refresh_competitor_row_by_url`
- `search_keyword_competitor_products`
- `sync_tk_influencer_pool`
- `tiktok_fastmoss_product_ingest`
- `search_keyword_selection_products`

已批准、实施中且 completion gate 尚未通过：

- `refresh_amazon_product_row_by_asin`
- `refresh_current_amazon_product_table`

## README 实践

本目录保留 README 是合理的，因为它定义了 business 文档域的边界。不要为每个小目录机械新增 README；只有当目录包含多份文档且需要独立索引时，才维护目录级 README。
