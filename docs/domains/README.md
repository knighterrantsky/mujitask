# Domain Routes

更新时间: 2026-04-25

本目录只做业务域阅读路线，不承载新的业务或架构事实。修改某个业务域前，先读对应 README，再按里面列出的 business、arch 和 contract 入口继续展开。

| 领域 | 覆盖范围 |
| --- | --- |
| [tk-competitor](./tk-competitor/README.md) | `TK竞品收集`、竞品刷新、关键词新增、商品状态 |
| [tk-influencer-pool](./tk-influencer-pool/README.md) | `TK达人池`、竞品到达人池同步、达人查找状态 |
| [product-fact-ingest](./product-fact-ingest/README.md) | TikTok / FastMoss 商品事实采集、媒体同步、Fact DB 沉淀 |

## 约束

- 领域 README 只回答“改这个域时先读什么”和“不可破坏的不变量是什么”。
- 字段、状态、workflow 的可校验定义放在 `contracts/**`。
- 正文事实仍回到 `docs/business/**` 和 `docs/arch/**`。
