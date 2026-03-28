# Business Consumption Contract

这份文档定义业务仓库如何消费 `automation-framework`。

目标不是解释 framework 内部实现，而是明确：

- 业务仓库能做什么
- 业务仓库不能依赖什么
- 业务仓库应该通过什么方式接入

## 1. 职责边界

`automation-framework` 负责：

- agent / run / step / signal / artifact runtime
- browser target / session / provider 抽象
- `BaseWorkflowTask` 所需的通用 step 执行能力
- recorder MVP 与 review-only `workflow_draft`

业务仓库负责：

- 真实业务 task
- 业务 workflow 拆分
- 业务 mapper / validator / flow
- 业务数据模型与业务文档

明确边界：

- framework 不承载真实业务流程
- 业务仓库不应依赖 framework 内部实现路径
- 业务仓库默认通过 scaffold 初始化，而不是直接在 framework 仓库里开发

## 2. 支持的接入模式

当前支持的标准接入模式只有一种：

```text
business repo
  -> TaskRegistry
  -> create_app(...)
  -> BaseWorkflowTask.build_workflow()
  -> WorkflowSpec
  -> framework runtime execute
```

最小示例：

```python
from automation_framework.agent.server import create_app
from automation_framework.core import TaskRegistry

from my_business.tasks import MyBusinessWorkflowTask

registry = TaskRegistry()
registry.register(MyBusinessWorkflowTask())
app = create_app(registry)
```

## 3. 公开 import 面

业务仓库只应依赖 [public-import-surface.md](./public-import-surface.md) 中列出的公开模块。

默认业务接入主入口：

- `create_app`
- `TaskRegistry`
- `BaseWorkflowTask`
- `FrameworkResult`
- `WorkflowSpec`
- `StepDefinition`
- `StepAction`

## 4. Workflow 边界

当前 workflow 的边界必须这样理解：

- `workflow_draft` 是 review-only 中间产物
- `workflow_draft` 不是 runtime executable contract
- 正式执行时，业务仓库当前仍应把自己的业务定义映射成 `WorkflowSpec`

如果业务需要更完整的 workflow step contract，直接看：

- [workflow-runtime-contract.md](./workflow-runtime-contract.md)
- [workflow-draft-contract.md](./workflow-draft-contract.md)

## 5. 配置边界

当前要区分两类配置：

### runtime 配置

由 framework runtime 读取，例如：

- `BROWSER_PROFILES_FILE`
- `DEFAULT_PROFILE_REF`
- `AGENT_HOST`
- `AGENT_PORT`
- `AGENT_RUN_DIR`
- `AGENT_RECORDING_DIR`

### 业务默认配置

由业务仓库自己维护，例如：

- 默认 `run_mode`
- 默认源系统 / 目标系统
- 业务字段映射默认值
- 业务文案、分类、价格处理策略

这两类配置不要混在一起。

## 6. Run Mode / Effects Contract

业务仓库在有副作用的 step 上必须显式声明 `effects`。

当前 runtime 已有这些 effect：

- `write`
- `upload`
- `draft`
- `submit`

当前 `run_mode` 约束：

- `draft` 模式下，`submit` 会被 runtime 阻止
- `canary` / `full_auto` 才允许 `submit`

这条约束是 runtime 的职责，不是业务仓库约定俗成。

## 7. 业务仓库文档责任

业务仓库至少要维护：

- 自己的任务接入说明
- 业务 workflow 定义文档
- mapper / validator 说明
- framework/scaffold 升级记录

业务仓库如果使用 scaffold，建议保留：

- `AGENT.MD`
- `.platform/platform-manifest.yaml`
- `.platform/model-rules.yaml`
- `docs/framework_contract/<framework_version>/...`

## 8. 升级责任

业务仓库不要直接追 framework 最新源码。

推荐方式：

1. pin framework tag/commit
2. 先看 `public-capability-status.md`
3. 再看 `public-migration-guide.md`
4. 最后比对 scaffold 新版本的受保护区

只有这些步骤都完成后，才进入业务仓库升级。
