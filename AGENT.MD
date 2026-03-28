# automation-business-scaffold agent rules

这个仓库是业务模板仓库，默认给业务侧 Codex 使用。

## Start Here

按这个顺序读取：

1. `.platform/platform-manifest.yaml`
2. `.platform/model-rules.yaml`
3. `AGENT.MD`
4. `docs/framework_contract/0.2.1/public-capability-status.md`
5. `docs/framework_contract/0.2.1/business-consumption-contract.md`

## Repo Role

- 这个仓库是业务模板，不是 framework 源码仓库
- 业务侧不应把 framework 源码当作事实 contract
- 新业务应从这个仓库初始化为独立业务仓库后再长期开发
- 标准安装模式下，不应假设本机存在 `../automation-framework`
- 同级目录本地 framework checkout 只属于平台联调覆盖模式

## Default Working Mode

默认工作模式：`business_feature`

这意味着：

- 只在业务可编辑区工作
- 不修改 platform-managed 区域
- 不修改 vendored framework contract docs
- 不导入非白名单 framework 模块

如果任务明确涉及 framework 升级或 scaffold 基线升级，才进入 `platform_upgrade` 模式。

默认假设：

- 只存在当前 scaffold 仓库
- framework 通过 `pyproject.toml` 中 pin 的 git 依赖安装

## Protected Paths

普通业务开发不要修改：

- `.platform/**`
- `AGENT.MD`
- `docs/framework_contract/**`
- `src/automation_business_scaffold/agent.py`
- `src/automation_business_scaffold/registry.py`
- `tests/test_agent.py`
- `tests/test_contract_pack.py`

## Business Editable Paths

默认只在这些区域工作：

- `src/automation_business_scaffold/config.py`
- `src/automation_business_scaffold/tasks/**`
- `src/automation_business_scaffold/workflows/**`
- `src/automation_business_scaffold/flows/**`
- `src/automation_business_scaffold/models/**`
- `src/automation_business_scaffold/mappers/**`
- `src/automation_business_scaffold/validators/**`
- `docs/business/**`
- `tests/test_registry.py`
- `tests/test_workflow_demo.py`

## Allowed Framework Imports

只允许业务代码直接依赖这些模块：

- `automation_framework.agent.server`
- `automation_framework.core`
- `automation_framework.runtime`

默认使用这些符号：

- `create_app`
- `TaskRegistry`
- `BaseWorkflowTask`
- `FrameworkResult`
- `WorkflowSpec`
- `StepDefinition`
- `StepAction`

不要导入：

- `automation_framework.browser.*`
- `automation_framework.clients.*`
- `automation_framework.runtime.engine`
- `automation_framework.runtime.validators`
- `automation_framework.selftest.*`

## Workflow Rules

- 新业务默认走 `BaseWorkflowTask`
- `workflow_draft.review-only.yaml` 只是审核样例，不是可执行 workflow
- `run_mode` / `effects` 的解释以 vendored contract docs 为准
- 业务逻辑应放在 `tasks/`、`workflows/`、`mappers/`、`validators/`、`flows/`

## Escalation

如果需求必须修改 platform-managed 区域，先停止直接实现，再把任务明确标记为 `platform_upgrade`。
