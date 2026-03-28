# Workflow Runtime Contract

## 1. 目的

这份文档给业务仓库使用，用来说明 `automation-framework` 当前 step runtime 的最小接入契约。

适用对象：

- `xianyu-example`
- 后续任何需要把业务 task 从 `run() + flow 串接` 迁移到 workflow step 执行模型的业务仓库

## 2. 当前可用能力

当前 runtime 已支持：

- `BaseWorkflowTask`
- `WorkflowSpec`
- `StepDefinition`
- `StepExecutionContext`
- `WorkflowExecutor`
- `ValidatorEngine`
- `ArtifactManager`
- run 级 `steps` / `signals` / `artifacts` 查询

当前 runtime 还未完成：

- workflow loader / 版本化 workflow 定义加载

所以业务仓库现在可以开始做：

- workflow step 切分
- mapper / validator 的模块边界预留
- 新 task 的 workflow 化重构
- step 级 validation rule 接入
- step 级 artifact payload 输出
- 基于 `run_mode + effects` 标注 side effect step

但还不应依赖：

- 运行时自动 repair

## 3. 业务仓库接入方式

业务 task 应继承 `BaseWorkflowTask`：

```python
from automation_framework.core import BaseWorkflowTask, FrameworkResult
from automation_framework.runtime import StepAction, StepDefinition, WorkflowSpec


class MyWorkflowTask(BaseWorkflowTask):
    name = "my_workflow"

    def build_workflow(self, params):
        return WorkflowSpec(
            workflow_id="my_workflow_v1",
            run_mode="draft",
            steps=[
                StepDefinition(
                    step_id="step_a",
                    action=StepAction(type="step_a"),
                ),
                StepDefinition(
                    step_id="step_b",
                    action=StepAction(type="step_b"),
                ),
            ],
        )

    def execute_workflow_step(self, context):
        if context.step.step_id == "step_a":
            return FrameworkResult.ok(data={"value": "hello"})
        if context.step.step_id == "step_b":
            previous = context.get_step_output("step_a")
            return FrameworkResult.ok(data={"value": previous["value"] + "-done"})
        raise RuntimeError(f"Unknown step: {context.step.step_id}")
```

## 4. 运行时保证

对于 `BaseWorkflowTask`，runtime 当前保证：

- step 会按 `WorkflowSpec.steps` 顺序执行
- 每个 step 都会写入 `StepRecord`
- step 成功时会追加 `step.completed` signal
- step 校验失败时会追加 `validation.failed` signal
- step 运行异常时会追加 `step.failed` signal
- step 因 `run_mode` 被拦截时会追加 `run_mode.blocked` signal
- 任一 step 失败后，workflow 停止，run 标记为 `failed`
- 若 step 配置了 artifact，runtime 会把 artifact 路径写回 `StepRecord.artifacts`

## 4.3 Run Mode 与 Step Effects

业务仓库应在每个有副作用的 step 上显式声明 `effects`。当前支持的 effect 有：

- `write`
- `upload`
- `draft`
- `submit`

当前 runtime 的最小约束矩阵：

- `observe`
  - 不允许任何 side effect
- `draft`
  - 允许 `write / upload / draft`
  - 不允许 `submit`
- `approval_required`
  - 允许 `write / upload / draft`
  - 不允许 `submit`
- `canary`
  - 允许 `write / upload / draft / submit`
- `full_auto`
  - 允许 `write / upload / draft / submit`

如果 step 的 effect 与当前 `run_mode` 冲突，runtime 会在执行前阻止该 step。

## 4.1 当前支持的 built-in validation rule

当前内置支持这些 rule：

- `param_exists:key`
- `param_equals:key=value`
- `step_output_exists:step_id.key`
- `step_output_equals:step_id.key=value`
- `result_data_exists:key`
- `result_data_equals:key=value`
- `metadata_exists:key`
- `metadata_equals:key=value`
- `always_pass`

业务仓库如果需要诸如 `page_is(...)`、`element_visible(...)` 这类站点特定规则，应在 `BaseWorkflowTask.evaluate_workflow_rule(...)` 里自行解释。

## 4.2 Artifact payload 约定

业务 step 可以通过 `FrameworkResult.metadata["artifacts_payload"]` 提供 artifact 内容：

```python
FrameworkResult.ok(
    data={"final_message": "hello-done"},
    metadata={
        "artifacts_payload": {
            "state_dump": {"final_message": "hello-done"},
            "html_snapshot": "<html>...</html>",
            "extra": {"payload": {"final_message": "hello-done"}},
        }
    },
)
```

如果 step 在 `artifacts` 中声明了 `state_dump: true`，即使业务没有显式提供状态内容，runtime 也会生成兜底 state dump。

## 5. 当前的业务侧约束

业务仓库在当前阶段应遵守这些规则：

- 一个 step 只做一个清晰职责
- 不要把跨站导航、抽取、映射、填写混在同一个 step
- step 输出要面向后续 step 消费，而不是直接塞到全局大对象里
- mapper 与 business validator 仍然应独立模块化，不要塞回 workflow executor

## 6. 建议的 step 切分

对于类似 `JdToGoofishPublishTask` 的任务，建议最少拆成：

1. `ensure_jd_login`
2. `search_jd_product`
3. `extract_jd_product`
4. `map_to_publish_payload`
5. `ensure_goofish_login`
6. `fill_publish_form`
7. `validate_publish_page`
8. `submit_or_stop`

## 7. 业务仓库当前必须同步维护的文档

当业务仓库开始基于这个 contract 开发时，至少要同步输出：

- workflow definition 文档或示例 YAML
- mapper 字段映射说明
- business validator 规则说明
- README / usage 中的 task 接入和运行说明

这些文档应与代码一起迭代，而不是最后补。

## 8. 业务仓库现在可以直接依赖的 runtime 输出

当前业务仓库可以直接消费：

- `GET /runs/{run_id}/steps`
- `GET /runs/{run_id}/signals`
- `GET /runs/{run_id}/artifacts`

其中 `/artifacts` 适合用来做调试页面、失败排查、人工复核入口，而不必再从完整 `StepRecord` 里自行筛选。
