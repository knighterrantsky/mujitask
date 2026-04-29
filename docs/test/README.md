# Test 文档索引

日期: 2026-04-29

本目录承载测试策略、验证流程、测试数据和测试报告相关文档。

## 事实来源边界

`docs/test` 是测试策略和验证流程的事实来源。

它不作为客户需求、系统设计、开发规范或部署运维的事实来源:

- 客户需求和验收口径见 [../business/README.md](../business/README.md)。
- 系统架构和 workflow 设计见 [../arch/README.md](../arch/README.md)。
- 开发规范、项目结构和实现模式见 [../dev/README.md](../dev/README.md)。
- 部署、验收、回退和 runbook 见 [../ops/README.md](../ops/README.md)。

测试代码本身以仓库 `tests/` 目录下的文件为准，本文档域用于帮助理解测试策略和运行方式。

## 文档

| 文档 | 说明 |
| --- | --- |
| [test-structure-guidelines.md](./test-structure-guidelines.md) | 测试目录结构、命名规范、marker 规则和旧测试迁移策略 |
| [runtime-watchdog-validation.md](./runtime-watchdog-validation.md) | Watchdog 运行时验证流程：claim、超时检测、原子标记、进程终止、自动重启 |

## 测试运行

```bash
# 全部测试（必须使用 --extra dev）
uv run --extra dev pytest

# 单个测试文件
uv run --extra dev pytest tests/test_fastmoss_fact_mappers.py

# 单个测试函数
uv run --extra dev pytest tests/test_fastmoss_fact_mappers.py::test_map_fastmoss_goods_base_extracts_product_shop_relation_and_media

# Postgres 依赖的测试
bash scripts/execution_control/run_local_postgres_tests.sh
```

测试组织规范见 [test-structure-guidelines.md](./test-structure-guidelines.md)。当前旧测试仍可能保留在 `tests/` 根目录；新增测试应优先按新规范放置。

## 测试分类

| 类别 | 推荐位置 | 旧文件模式 | 说明 |
| --- | --- | --- | --- |
| 架构/契约测试 | `tests/contract/` | `tests/test_*contract*.py`, `tests/test_architecture*.py` | 校验项目结构、模块归属、handler registry、workflow manifest |
| Handler / Mapper / Projection 单元测试 | `tests/unit/` | `tests/test_*handler*.py`, `tests/test_*mapper*.py`, `tests/test_*projection*.py` | 单个 owner 的输入/输出/错误分类 |
| Workflow / Runtime 集成测试 | `tests/integration/` | `tests/test_runtime_*.py`, `tests/test_*integration*.py` | Runtime DB 支持的 workflow 执行链路 |
| Control Plane 测试 | `tests/unit/control_plane/` 或 `tests/integration/runtime/` | `tests/test_*supervisor*.py`, `tests/test_*watchdog*.py`, `tests/test_outbox*.py` | Supervisor、Watchdog、Outbox 的行为验证 |
| E2E 测试 | `tests/e2e/` | `tests/test_*e2e*.py` | 端到端业务流程，需要真实外部服务凭证 |
