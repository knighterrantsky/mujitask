# Ops 文档索引

更新时间: 2026-04-24

本目录用于承载部署、运维、验收、回退和 runbook 文档。它不承载客户需求，也不承载系统架构设计。

## 事实来源边界

`docs/ops` 是部署、运行、验收、回退和生产排障动作的事实来源。

它不是以下内容的事实来源:

- 客户需求和业务验收口径: 见 [../business/README.md](../business/README.md)。
- 系统架构、workflow 和数据库设计: 见 [../arch/README.md](../arch/README.md)。
- 开发、调试和 skill 集成说明: 见 [../dev/README.md](../dev/README.md)。
- 外部接口研究: 见 [../reference/README.md](../reference/README.md)。

## 文档

| 文档 | 说明 |
| --- | --- |
| [deployment.md](./deployment.md) | 当前部署说明 |
| [release-flow.md](./release-flow.md) | 提交代码并发布的 MR / PR、tag 和 release 执行规则 |
| [phase1-acceptance-and-rollback.md](./phase1-acceptance-and-rollback.md) | Phase 1 历史验收与回退 |
| [archive/phase1-controlled-execution-pilot.md](./archive/phase1-controlled-execution-pilot.md) | Phase 1 历史 Pilot/runbook |

## 当前状态

`docs/ops` 已承接原 `docs/business` 中的部署、验收、回退和历史 runbook 文档。当前系统架构和 Runtime 设计仍以 [../arch/README.md](../arch/README.md) 为准。
