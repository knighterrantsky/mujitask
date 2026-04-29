# 文档总览

更新时间: 2026-04-23

本目录是项目文档地图。根目录 `README.md` 只作为项目入口和运行入口；具体文档从这里进入。

## 目录边界

| 目录 | 定位 | 事实来源边界 |
| --- | --- | --- |
| [business](./business/README.md) | 客户需求、业务规则、飞书表口径、验收口径 | 当前业务事实来源 |
| [arch](./arch/README.md) | 系统架构、workflow 设计、Runtime/Fact/Storage 设计 | 当前系统设计事实来源 |
| [domains](./domains/README.md) | 业务域阅读路线和不变量索引 | 路由入口，不承载正文事实 |
| [dev](./dev/README.md) | 开发规范、项目结构、实现模式、本地调试 | 开发工作流事实来源 |
| [test](./test/README.md) | 测试策略、验证流程、测试数据 | 测试策略事实来源 |
| [ops](./ops/README.md) | 部署、运维、验收、回退、runbook | 运维执行事实来源 |
| [reference](./reference/README.md) | 外部接口、采集口径、研究材料 | 参考资料，不作为当前业务或系统设计事实来源 |
| [../contracts](../contracts/README.md) | 字段、状态、workflow 机器契约 | 可索引、可校验的契约事实 |
| `automation-framework` 文档 | framework 公共接口、contract、迁移说明 | 直接从 framework 包或 framework 仓库读取 |

## 阅读顺序

1. 当前项目运行和部署: [../README.md](../README.md)
2. 当前客户需求和正式业务流程: [business/README.md](./business/README.md)
3. 按业务域修改时的精确阅读路线: [domains/README.md](./domains/README.md)
4. 当前系统架构和 workflow 设计: [arch/README.md](./arch/README.md)
5. 字段、状态、workflow 机器契约: [../contracts/README.md](../contracts/README.md)
6. 本地开发、调试和 skill 集成: [dev/README.md](./dev/README.md)
7. 测试策略和验证流程: [test/README.md](./test/README.md)
8. 运维部署和回退材料: [ops/README.md](./ops/README.md)
9. FastMoss / TikTok 等外部接口参考: [reference/README.md](./reference/README.md)
10. framework 接口和 contract: 直接查看 `automation-framework` 对应版本文档。

## README 使用原则

- 根目录 `README.md` 是项目入口。
- `docs/README.md` 是全部文档入口。
- 业务、架构、开发、运维、参考资料这类独立文档域保留目录级 README。
- `docs/domains/**` 只做领域路由和按任务阅读索引，不承载新的业务或架构事实。
- 不为每个小目录机械新增 README，除非该目录已经有多份文档并需要索引。
