# Public Import Surface

这份文档定义 `automation-framework` 当前对业务仓库公开的 Python import 面。

原则只有一条：

- 业务仓库可以依赖这里列出的模块与符号。
- 未列出的模块不应被当作对外 contract，即使安装包源码在本地可见。

## 1. 公开模块

当前只允许业务仓库直接 import 下面三个模块：

- `automation_framework.agent.server`
- `automation_framework.core`
- `automation_framework.runtime`

## 2. 业务接入的主入口

### `automation_framework.agent.server`

允许使用：

- `create_app`

用途：

- 在业务仓库中暴露自己的 agent 入口
- 把业务自己的 `TaskRegistry` 注册给 framework runtime

推荐用法：

```python
from automation_framework.agent.server import create_app
from automation_framework.core import TaskRegistry

registry = TaskRegistry()
app = create_app(registry)
```

### `automation_framework.core`

允许业务仓库直接依赖的主符号：

- `TaskRegistry`
- `BaseWorkflowTask`
- `FrameworkResult`

兼容但不推荐作为新业务主入口：

- `BaseTask`

约束：

- 新业务 workflow 默认应从 `BaseWorkflowTask` 开始。
- 只有不需要 step runtime 的简单任务，才考虑继续使用 `BaseTask.run(...)`。

### `automation_framework.runtime`

允许业务仓库直接依赖的主符号：

- `WorkflowSpec`
- `StepDefinition`
- `StepAction`

允许用于本地验证、测试或排障的辅助符号：

- `WorkflowExecutor`
- `RunRegistry`

约束：

- 业务运行时接入的默认方式是 `BaseWorkflowTask.build_workflow() -> WorkflowSpec`。
- `WorkflowExecutor` 与 `RunRegistry` 可以用于本地测试，但业务生产接入仍应优先走 agent `/runs`。

## 3. 明确不开放的模块

下面这些路径当前不属于业务公开 contract：

- `automation_framework.browser.*`
- `automation_framework.clients.*`
- `automation_framework.runtime.engine`
- `automation_framework.runtime.validators`
- `automation_framework.runtime.artifacts`
- `automation_framework.selftest.*`

这些模块可以重构、重命名、拆分，业务仓库不应直接依赖。

## 4. 正反例

推荐：

```python
from automation_framework.agent.server import create_app
from automation_framework.core import BaseWorkflowTask, FrameworkResult, TaskRegistry
from automation_framework.runtime import StepAction, StepDefinition, WorkflowSpec
```

不推荐：

```python
from automation_framework.browser import build_browser_provider
from automation_framework.runtime.engine import StepExecutionContext
from automation_framework.selftest.tasks import TraceToWorkflowDemoTask
```

## 5. 兼容性承诺

- 这里列出的主入口是业务仓库的默认依赖面。
- 如果这些入口发生破坏兼容变更，必须同步更新：
  - `docs/public-capability-status.md`
  - `docs/public-migration-guide.md`
  - `docs/business-consumption-contract.md`
- 如果一个能力只存在于实现里、没有进入这份文档，就不应被业务仓库视为稳定 contract。
