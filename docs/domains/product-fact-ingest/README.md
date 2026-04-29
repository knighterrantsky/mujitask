# Product Fact Ingest Domain

## 本域覆盖

- `tiktok_fastmoss_product_ingest`
- TikTok 商品 request-first 采集
- FastMoss 商品详情采集
- 媒体同步、Fact DB upsert、可选飞书写回

## 默认上下文

普通实现 / 修复默认只读:

1. [../../dev/rewrite-state.yaml](../../dev/rewrite-state.yaml)
2. [../../arch/project-structure-contract.md](../../arch/project-structure-contract.md)
3. [../../../contracts/codex/task-routing.yaml](../../../contracts/codex/task-routing.yaml)
4. [../../../contracts/workflow/tiktok_fastmoss_product_ingest.yaml](../../../contracts/workflow/tiktok_fastmoss_product_ingest.yaml)
5. 当前相关源码和测试。

## 条件展开

- 修改正式客户需求或验收口径时，才读 `docs/business/**`。
- 修改 Fact DB、Storage、browser fallback 或 workflow 架构边界时，才读对应 `docs/arch/**` 长文档。
- 修改字段、状态或 workflow 机器事实时，必须同步读写 `contracts/**`。
- 旧行为可参考 git history；legacy `business/` 目录已删除。

## 本域不可破坏的不变量

- 商品、店铺、达人、视频、媒体资产属于通用事实，不属于选品分析专有数据。
- TikTok 商品数据采集优先走 request/API 路径。
- Browser 只作为 fallback，用于 request 失效、关键字段缺失或被风控阻断的场景。
- `tiktok_fastmoss_product_ingest` 当前不是已沉淀的正式客户流程需求；不要从设计文档反推业务验收。
- 新实现落在 `src/automation_business_scaffold/domains/tiktok/**`、`capabilities/**` 或 `control_plane/**`，legacy `business/` 目录已删除。
- 完成本任务后遵守 `AGENTS.md` 的 Stop Protocol，不输出无关下一步建议。
