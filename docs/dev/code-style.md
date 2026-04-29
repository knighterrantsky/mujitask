# 代码规范

更新时间: 2026-04-29

本文定义 Mujitask 的日常代码风格、命名规则、分层边界和提交前检查。

## 1. 基础规则

- Python 版本: `>=3.11`
- 代码格式与静态检查: 使用 `ruff`（配置见 `pyproject.toml`）
- 单行长度: 100
- 测试框架: `pytest`
- 包管理: `uv`
- 代码目录: `src/automation_business_scaffold/`
- 测试目录: `tests/`

## 2. 命名规则

- 文件名、函数名、变量名统一使用 `snake_case`
- Runtime 稳定 code（`task_code`、`workflow_code`、`stage_code`、`job_code`、`handler_code`、`mapper_code`、`projection_code`、`policy_code`、`channel_code`）使用 `snake_case`
- 不在稳定 code 名称中使用 `v1`、`v2`、`new`、`legacy`、`stage1`、`stage2`
- 新增 task、workflow、job、handler 时，文件名应与稳定 code 对齐
- 不使用 `orchestrate_*`、`run_*_workflow` 作为 handler / job code

## 3. 分层规则

优先遵守:

- Task: 业务入口，定义 `TASK_CODE`、参数校验、submit/status/result
- Workflow: 阶段编排，定义 `WORKFLOW_CODE`、stage DAG、job binding
- Job: Runtime 可执行单元，定义 `JOB_CODE`、`HANDLER_CODE`、payload/result schema
- Handler: worker 执行能力，实现 `HANDLER_CODE`、transport、错误分类
- Mapper: 输入源/事实源到业务对象的纯函数转换
- Projection: 业务结果到外部视图字段的投影
- Policy: 选择、过滤、排序、retry/timeout 业务语义
- Flow: handler 内部复用的业务实现过程

具体结构以 [project-structure-contract.md](../arch/project-structure-contract.md) 为准。

## 4. 设计准则

新增或修改代码时，优先遵守以下五条：

1. **入口只 submit，不解释业务。** Task/CLI/RPC 只负责参数校验和提交，不解析业务字段含义。
2. **Workflow 只声明 stage/transition，不直接做外部系统细节。** 外部 API 调用、字段映射、写回逻辑通过 job → handler → mapper/projection 完成。
3. **Handler 一次只完成一种能力。** 一个 handler 对应一种明确的外部能力（读表、搜索、取详情、写回），不把多个能力揉进一个 handler。
4. **Projection 只负责写回。** 业务结果到外部视图字段的投影逻辑集中在 projection 模块，不散落到 handler、flow 或 workflow。
5. **恢复逻辑只进 watchdog/reconciler/outbox，不进业务流程分支。** 超时重试、异常恢复、状态收敛、通知发送由控制面统一处理，不写在 workflow stage 的业务分支里。

## 5. 禁止事项

- 不要在业务 flow 或 handler 中临时创建 Runtime DB engine
- 不要绕过已有 capability handler 新增旁路 helper/service/manager/coordinator
- 不要把飞书字段映射写进通用 handler
- 不要直接依赖 `automation-framework` 的内部模块
- 不要在普通业务开发中修改 `.platform/**` 或 `AGENTS.md`
- 不要在 handler 中使用 `_implementations` 聚合大文件
- 不要写 facade/shim/re-export 作为 runtime 主路径

## 6. 提交前检查

```bash
# 全部测试
uv run --extra dev pytest

# 单个测试文件
uv run --extra dev pytest tests/test_xxx.py

# 代码检查
uv run --extra dev ruff check src/
```

如果涉及完成声明，需要按 `AGENTS.md` 中的 Completion Claim Gate 执行对应检查:

```bash
python scripts/harness/claim_done.py <feature_code>
```
