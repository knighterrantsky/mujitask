# Interactive Staged Workflow Runtime 设计草案

日期: 2026-05-13

状态: 设计目标草案，待后续 contract、schema、migration 和实现分阶段落地。本文不代表当前
Runtime DB 已经具备这些表或能力。

## 1. 设计目标

Mujitask 的 workflow 应从“固定代码流程”演进为“标准化能力节点组成的可编排链条”。

核心目标:

- 流程节点化: workflow 由一组稳定节点组成，而不是一整段不可拆业务代码。
- 节点通用化: 节点可以被多个 workflow 复用。
- 输入输出标准化: 每个节点声明标准输入、标准输出、artifact 引用和错误外壳。
- 能力目录开放给 Agent: Agent 只能基于公开的节点能力目录生成组合计划。
- Runtime 负责执行: Agent 不直接执行副作用，Runtime 负责任务推进、暂停、恢复、重试、取消和审计。
- 输出组合化: workflow 最终结果由多个标准节点输出组合而成。

一句话目标:

```text
Mujitask workflow 应演进为由标准化能力节点组成的 typed chain；
每个节点声明输入、输出、副作用和可交互策略；
Agent 只能基于公开的节点能力目录组合 WorkflowPlan；
Runtime 负责执行、暂停、恢复和审计。
```

## 2. 非目标

MVP 阶段不做以下能力:

- 不开放任意 DAG。先支持链式流程，再考虑分支、合并和循环。
- 不把每个 API call 都节点化。节点粒度由可恢复、可重试、可审计边界决定。
- 不让 Agent 直接调用 handler 或执行外部副作用。
- 不把大对象塞进 Runtime DB result。大对象进入 artifact / Fact DB / Object Store。
- 不把 Outbox 当流程状态源。Outbox 只负责触达。
- 不先做 workflow 定义数据库化。现有代码和 contract YAML 可以先作为 catalog 来源。

## 3. 最小能力总览

最小闭环需要 8 个能力:

| 能力 | 目的 | 为什么是最小能力 |
| --- | --- | --- |
| Node Spec | 描述节点能做什么、需要什么输入、产出什么输出 | 没有它，Agent 和 Runtime 都无法可靠理解节点边界 |
| Result Envelope | 统一节点输出外壳 | 没有它，下游无法稳定消费不同节点的结果 |
| Artifact Ref | 用引用传递大结果 | 没有它，Runtime DB 会被大 JSON 撑爆，结果也难以复用和审计 |
| WorkflowPlanSpec | Agent 生成的可执行计划草案 | 没有它，Agent 会变成直接执行者，缺少审核和校验边界 |
| Compiler / Validator | 校验计划是否合法 | 没有它，Agent 可能组合出不可执行或危险的流程 |
| Node Run State | 持久化节点实例状态 | 没有它，系统无法恢复、重试和审计单个节点 |
| Approval Request | 持久化用户决策 | 没有它，Outbox 消息会被误用为状态源 |
| Idempotency / Side-effect Contract | 定义副作用重试边界 | 没有它，失败重试会重复写库、上传或写飞书 |

## 4. 能力一: Node Spec

### 4.1 目的

Node Spec 是节点能力的机器可读描述。它告诉 Agent 和 Runtime:

- 这个节点是什么。
- 需要什么输入。
- 产出什么输出。
- 由什么类型 worker 执行。
- 是否有外部副作用。
- 如何重试、超时和幂等。

### 4.2 为什么是最小能力

节点通用化的前提是节点边界可描述。如果只有 Markdown 或代码函数，Agent 无法稳定组合，
Runtime 也无法在提交前校验输入输出是否匹配。

Node Spec 直接匹配这些设计目标:

- 流程节点化: 每个节点有独立 `node_code`。
- 节点通用化: 多个 workflow 可以引用同一个 `node_code`。
- 输入输出标准化: 每个节点声明 `input_schema` 和 `output_schema`。
- 能力目录开放给 Agent: Node Spec 是 Capability Catalog 的基础数据。

### 4.3 必要字段

| 字段 | 必须 | 说明 | 必要性 |
| --- | --- | --- | --- |
| `node_code` | 是 | 稳定节点编码 | Agent、Compiler、Runtime 都通过它引用节点 |
| `version` | 是 | 节点契约版本 | 支持后续兼容演进，避免旧 plan 被新语义破坏 |
| `display_name` | 是 | 面向用户或 Agent 展示的名称 | 用于 proposal、审批和日志展示 |
| `description` | 否 | 节点能力说明 | 帮助 Agent 解释能力边界 |
| `input_schema` | 是 | 节点输入结构 | Compiler 校验上游输出是否能接入 |
| `output_schema` | 是 | 节点输出结构 | 下游节点消费和最终 summary 聚合依赖它 |
| `worker_type` | 是 | `api` / `browser` / `system` / `outbox` | Runtime 选择执行 lane |
| `side_effects` | 是 | 外部副作用列表 | 决定是否需要审批、幂等和补偿 |
| `idempotency_policy` | 是 | 幂等键生成规则 | 支撑安全重试 |
| `timeout_policy` | 是 | 超时规则 | Watchdog 和 supervisor 判断卡住 |

### 4.4 Demo

```yaml
node_code: fastmoss_product_search
version: "2026-05-13"
display_name: FastMoss 商品搜索
description: 根据关键词和筛选条件搜索商品候选。
worker_type: api

input_schema:
  type: FastMossProductSearchInput
  required:
    - search_query
  properties:
    search_query:
      type: string
    filters:
      type: object
    max_candidates:
      type: integer

output_schema:
  type: ProductCandidateSetRef
  required:
    - candidate_set_ref
    - candidate_count
  properties:
    candidate_set_ref:
      type: artifact_ref
      artifact_type: product_candidate_set
    candidate_count:
      type: integer
    preview_rows:
      type: array

side_effects:
  - external_api_read
  - runtime_artifact_write

idempotency_policy:
  key_template: "{request_id}:fastmoss_product_search:{search_digest}"

timeout_policy:
  max_execution_seconds: 300
```

## 5. 能力二: Result Envelope

### 5.1 目的

Result Envelope 是所有节点的统一输出外壳。节点内部可以不同，但对 Runtime 和下游节点
必须输出同一种 envelope。

### 5.2 为什么是最小能力

如果每个节点各自返回 JSON，下游无法通用消费，summary 也无法通用聚合。标准 envelope
是“标准化输出组合”的基础。

它直接匹配这些设计目标:

- 输入输出标准化: 下游读取统一外壳。
- 输出组合化: workflow result 可以聚合多个 envelope。
- Runtime 负责执行: Runtime 可统一处理 success、failed、waiting 和 next_action。

### 5.3 必要字段

| 字段 | 必须 | 说明 | 必要性 |
| --- | --- | --- | --- |
| `status` | 是 | 节点业务结果状态 | Runtime 决定是否进入下一节点或失败分支 |
| `output` | 是 | 小型结构化输出 | 下游直接消费的最小数据 |
| `artifact_refs` | 是 | 大对象和数据集引用 | 避免 Runtime DB 保存大 JSON |
| `summary` | 是 | 面向用户或 summary 的摘要 | Outbox、审批和最终结果展示需要 |
| `error` | 否 | 标准错误对象 | 失败、重试和排障需要 |
| `next_action` | 否 | 需要 Runtime 执行的后续动作 | 支持 browser fallback、approval request 等 |

### 5.4 Demo

```json
{
  "status": "success",
  "output": {
    "candidate_set_ref": "artifact://req_123/stage_001/candidates",
    "candidate_count": 86,
    "qualified_count": 37
  },
  "artifact_refs": [
    {
      "artifact_ref": "artifact://req_123/stage_001/candidates",
      "artifact_type": "product_candidate_set",
      "schema_ref": "ProductCandidateSetRef"
    }
  ],
  "summary": {
    "title": "FastMoss 搜索完成",
    "text": "搜索到 86 个候选，37 个满足筛选条件。"
  },
  "error": null,
  "next_action": {
    "type": "approval_request",
    "approval_type": "search_candidate_review"
  }
}
```

## 6. 能力三: Artifact Ref

### 6.1 目的

Artifact Ref 是节点之间传递大结果的引用协议。Runtime DB 只保存引用、摘要和 preview，
完整候选集、详情包、图表文件和报告内容放到 Object Store、Fact DB 或专用 artifact 存储。

### 6.2 为什么是最小能力

关键词搜索、商品详情、图表、报告都可能很大。如果直接放进上游 result 或下游 input，
会导致 Runtime DB 膨胀、重试成本高、审计困难。

它直接匹配这些设计目标:

- 输入输出标准化: 节点之间传标准引用。
- 输出组合化: final result 聚合多个 artifact ref。
- 能力目录开放给 Agent: Agent 可以基于 `artifact_type` 判断哪些可视化或写回能力可用。

### 6.3 必要字段

| 字段 | 必须 | 说明 | 必要性 |
| --- | --- | --- | --- |
| `artifact_ref` | 是 | 稳定引用 URI | 下游节点和审批使用它读取结果 |
| `artifact_type` | 是 | 产物类型 | Compiler 判断 input/output 是否兼容 |
| `schema_ref` | 是 | 产物结构引用 | 下游节点知道如何解析 |
| `preview_json` | 是 | 小型预览 | Outbox 和 Agent 展示需要，避免读取大对象 |
| `storage_location` | 是 | 真实存储位置 | Runtime 或节点执行时读取完整内容 |
| `metadata_json` | 否 | 扩展元数据 | 保存统计、来源、digest 等审计信息 |

### 6.4 Demo

```json
{
  "artifact_ref": "artifact://req_123/stage_001/product_candidate_set",
  "artifact_type": "product_candidate_set",
  "schema_ref": "ProductCandidateSetRef",
  "preview_json": {
    "candidate_count": 86,
    "qualified_count": 37,
    "preview_rows": [
      {
        "candidate_id": "cand_001",
        "title": "Egg Tray",
        "sales_7d": 1200,
        "price": 15.99
      }
    ]
  },
  "storage_location": {
    "bucket": "mujitask-runtime",
    "object_key": "runtime/req_123/stage_001/product_candidate_set.json"
  },
  "metadata_json": {
    "input_digest": "sha256:abc",
    "source": "fastmoss_product_search"
  }
}
```

## 7. 能力四: WorkflowPlanSpec

### 7.1 目的

WorkflowPlanSpec 是 Agent 生成的流程计划草案。Agent 只负责生成 plan，不直接执行节点。
Runtime 只执行经过 Compiler 校验的 plan。

### 7.2 为什么是最小能力

没有 plan，Agent 就会变成“边想边执行”，系统无法在执行前做权限、副作用、输入输出和审批校验。
Plan 是 Agent 和 Runtime 之间的安全边界。

它直接匹配这些设计目标:

- 能力目录开放给 Agent: Agent 基于 Catalog 生成 plan。
- Runtime 负责执行: Runtime 接收 plan 后编译、持久化、执行。
- 流程节点化: plan 明确列出节点链条。

### 7.3 必要字段

| 字段 | 必须 | 说明 | 必要性 |
| --- | --- | --- | --- |
| `plan_id` | 是 | 计划 ID | 支持 proposal、确认和幂等提交 |
| `task_code` | 是 | 目标顶层任务 | 关联 `task_request` |
| `orchestration_mode` | 是 | `automatic` / `interactive` | 决定是否需要审批节点 |
| `nodes` | 是 | 节点列表 | 定义流程链条 |
| `edges` | 是 | 节点连接关系 | Compiler 校验 output 到 input 的连接 |
| `initial_input` | 是 | 用户原始结构化输入 | 第一个节点的输入来源 |
| `interaction_policy` | 是 | 交互策略 | 决定哪些节点后暂停等待用户 |
| `write_policy` | 是 | 写库、写飞书策略 | 控制危险副作用 |
| `cancel_policy` | 是 | 取消策略 | 定义 safe cancellation point |

### 7.4 Demo

```json
{
  "plan_id": "plan_001",
  "task_code": "search_keyword_selection_products",
  "orchestration_mode": "interactive",
  "nodes": [
    {
      "node_alias": "search",
      "node_code": "fastmoss_product_search"
    },
    {
      "node_alias": "review_candidates",
      "node_code": "candidate_review"
    },
    {
      "node_alias": "collect_details",
      "node_code": "selected_product_detail_collect"
    },
    {
      "node_alias": "write_selection_rows",
      "node_code": "feishu_selection_writeback"
    }
  ],
  "edges": [
    {
      "from": "search.output.candidate_set_ref",
      "to": "review_candidates.input.candidate_set_ref"
    },
    {
      "from": "review_candidates.output.selected_candidate_set_ref",
      "to": "collect_details.input.selected_candidate_set_ref"
    },
    {
      "from": "collect_details.output.product_detail_bundle_ref",
      "to": "write_selection_rows.input.product_detail_bundle_ref"
    }
  ],
  "initial_input": {
    "search_query": "egg tray",
    "filters": {
      "min_sales_7d": 500
    }
  },
  "interaction_policy": {
    "after_nodes": ["search", "collect_details"],
    "before_side_effects": ["write_selection_rows"]
  },
  "write_policy": {
    "allow_fact_db_write": "ask",
    "allow_feishu_writeback": "ask"
  },
  "cancel_policy": {
    "allowed_before_each_node": true,
    "running_cancel_mode": "cooperative"
  }
}
```

## 8. 能力五: Compiler / Validator

### 8.1 目的

Compiler / Validator 接收 WorkflowPlanSpec，输出可执行 WorkflowRunSpec，或拒绝 plan。

它校验:

- 节点是否存在。
- 上游 output 是否匹配下游 input。
- worker 类型是否可用。
- 副作用是否有审批策略。
- 写入字段是否允许。
- 是否存在非法跳转。
- 是否超过数据量、权限或运行边界。

### 8.2 为什么是最小能力

Agent 生成的 plan 不能直接执行。没有 Compiler，系统无法证明一个组合方案是安全、可执行、
可恢复的。

它直接匹配这些设计目标:

- 能力目录开放给 Agent: Agent 只生成候选。
- Runtime 负责执行: Compiler 是进入 Runtime 前的门禁。
- 输入输出标准化: Compiler 依赖 schema 做类型匹配。

### 8.3 必要字段

Compiler 输出的最小字段:

| 字段 | 必须 | 说明 | 必要性 |
| --- | --- | --- | --- |
| `compiled_plan_id` | 是 | 编译后计划 ID | Runtime 执行时引用 |
| `source_plan_id` | 是 | 原始 plan ID | 审计 Agent 生成内容 |
| `task_code` | 是 | 顶层任务 | 创建 task_request |
| `compiled_nodes` | 是 | 已解析节点 | 固化 node version、worker_type 和 policy |
| `compiled_edges` | 是 | 已校验边 | Runtime 按边传递输入 |
| `required_approvals` | 是 | 必需审批点 | 防止危险副作用绕过人工确认 |
| `validation_report` | 是 | 校验报告 | proposal 展示和排障 |

### 8.4 Demo

```json
{
  "compiled_plan_id": "cplan_001",
  "source_plan_id": "plan_001",
  "task_code": "search_keyword_selection_products",
  "compiled_nodes": [
    {
      "node_alias": "search",
      "node_code": "fastmoss_product_search",
      "node_version": "2026-05-13",
      "worker_type": "api"
    },
    {
      "node_alias": "write_selection_rows",
      "node_code": "feishu_selection_writeback",
      "node_version": "2026-05-13",
      "worker_type": "api"
    }
  ],
  "compiled_edges": [
    {
      "from": "search.output.candidate_set_ref",
      "to": "review_candidates.input.candidate_set_ref",
      "type_check": "passed"
    }
  ],
  "required_approvals": [
    {
      "before_node": "write_selection_rows",
      "approval_type": "feishu_writeback_confirmation"
    }
  ],
  "validation_report": {
    "status": "accepted",
    "warnings": [
      "feishu writeback requires approval because it mutates TK_SELECTION"
    ]
  }
}
```

## 9. 能力六: Node Run State

### 9.1 目的

Node Run State 是某个 request 中某个节点的一次执行实例。它保存节点运行状态、输入引用、
结果引用和时间信息。

### 9.2 为什么是最小能力

只有 `task_request.current_stage` 不够。系统需要知道每个节点是否执行过、执行了几次、
产出了什么、是否需要恢复或重试。

它直接匹配这些设计目标:

- 流程节点化: 每个节点有独立 run。
- Runtime 负责执行: Runtime 通过 node run 推进、恢复和重试。
- 输出组合化: workflow final result 聚合多个 node run 的结果。

### 9.3 必要字段

| 字段 | 必须 | 说明 | 必要性 |
| --- | --- | --- | --- |
| `node_run_id` | 是 | 节点实例 ID | Runtime 追踪单个节点执行 |
| `request_id` | 是 | 顶层 request | 关联 task_request |
| `node_code` | 是 | 执行节点 | 关联 Node Spec |
| `node_alias` | 是 | plan 内别名 | 一个节点可在同一 plan 中多次出现 |
| `status` | 是 | 生命周期状态 | pending/running/waiting/finished/cancelled |
| `input_ref` | 是 | 输入引用 | 支持恢复和审计 |
| `result_ref` | 否 | 输出引用 | 下游节点消费 |
| `attempt_count` | 是 | 尝试次数 | 支撑 retry |
| `created_at` | 是 | 创建时间 | 审计 |
| `updated_at` | 是 | 更新时间 | Watchdog 判断卡住 |
| `started_at` | 否 | 开始时间 | 运行耗时 |
| `finished_at` | 否 | 结束时间 | 运行耗时和 summary |

### 9.4 Demo

```json
{
  "node_run_id": "nr_001",
  "request_id": "req_123",
  "node_code": "fastmoss_product_search",
  "node_alias": "search",
  "status": "finished",
  "input_ref": {
    "type": "inline",
    "value": {
      "search_query": "egg tray",
      "filters": {
        "min_sales_7d": 500
      }
    }
  },
  "result_ref": {
    "artifact_ref": "artifact://req_123/stage_001/product_candidate_set"
  },
  "attempt_count": 1,
  "created_at": "2026-05-13T10:00:00+08:00",
  "updated_at": "2026-05-13T10:02:00+08:00",
  "started_at": "2026-05-13T10:00:03+08:00",
  "finished_at": "2026-05-13T10:02:00+08:00"
}
```

## 10. 能力七: Approval Request

### 10.1 目的

Approval Request 是用户交互决策的状态源。Outbox 只负责把审批请求送到飞书、OpenClaw
或其他渠道。

### 10.2 为什么是最小能力

交互式流程必须能暂停、等待用户、记录用户选择，然后恢复。只靠 outbox 消息无法表达
pending、approved、rejected、expired、cancelled 等决策状态。

它直接匹配这些设计目标:

- Runtime 负责暂停和恢复。
- Outbox 只负责触达。
- 输出组合化: approval decision 本身也是 workflow 输出的一部分。

### 10.3 必要字段

| 字段 | 必须 | 说明 | 必要性 |
| --- | --- | --- | --- |
| `approval_id` | 是 | 审批 ID | Outbox 和 callback 都引用它 |
| `request_id` | 是 | 顶层 request | 关联 task_request |
| `node_run_id` | 是 | 触发审批的节点 run | 让 Runtime 知道审批后恢复哪里 |
| `approval_type` | 是 | 审批类型 | 决定卡片、表单和 decision schema |
| `status` | 是 | 审批状态 | pending/approved/rejected/expired/cancelled |
| `input_artifact_ref` | 是 | 审批对象 | 用户看到的候选集、详情包或图表 |
| `allowed_decisions` | 是 | 允许的决策 | 限制用户和 Agent 可选动作 |
| `decision_schema` | 是 | 决策结构 | 校验用户提交内容 |
| `decision` | 否 | 用户最终决策 | Runtime 恢复下游输入 |
| `decided_by` | 否 | 决策人 | 审计 |
| `decided_at` | 否 | 决策时间 | 审计和超时 |

### 10.4 Demo

```json
{
  "approval_id": "appr_001",
  "request_id": "req_123",
  "node_run_id": "nr_002",
  "approval_type": "search_candidate_review",
  "status": "pending",
  "input_artifact_ref": "artifact://req_123/stage_001/product_candidate_set",
  "allowed_decisions": [
    "continue_selected",
    "edit_filters_and_rescan",
    "generate_visualization",
    "cancel_workflow"
  ],
  "decision_schema": {
    "type": "object",
    "required": ["decision"],
    "properties": {
      "decision": {
        "enum": [
          "continue_selected",
          "edit_filters_and_rescan",
          "generate_visualization",
          "cancel_workflow"
        ]
      },
      "selected_candidate_ids": {
        "type": "array",
        "items": {"type": "string"}
      }
    }
  },
  "decision": null,
  "decided_by": null,
  "decided_at": null
}
```

## 11. 能力八: Idempotency / Side-effect Contract

### 11.1 目的

Idempotency / Side-effect Contract 声明节点的外部副作用和重试安全边界。

### 11.2 为什么是最小能力

很多节点会写 Fact DB、上传媒体、写飞书、生成图表或触发 browser fallback。失败重试时如果
没有幂等键，会重复写入、重复上传或覆盖错误数据。

它直接匹配这些设计目标:

- Runtime 负责重试和恢复。
- 节点通用化: 通用节点必须知道自己的幂等边界。
- 输出组合化: 副作用结果也要以标准引用进入 final result。

### 11.3 必要字段

| 字段 | 必须 | 说明 | 必要性 |
| --- | --- | --- | --- |
| `side_effect_type` | 是 | 副作用类型 | 区分 external read、writeback、artifact write、fact upsert |
| `idempotency_key_template` | 是 | 幂等键模板 | Runtime 和 handler 生成稳定键 |
| `retry_policy` | 是 | 重试规则 | 决定哪些失败可重试 |
| `requires_approval` | 是 | 是否需要审批 | 写库、写飞书等危险副作用需要控制 |
| `compensation_policy` | 是 | 补偿策略 | 已产生副作用后取消或失败如何处理 |
| `result_ref_type` | 是 | 副作用结果引用类型 | 让下游和 summary 读取结果 |

### 11.4 Demo

```yaml
node_code: feishu_selection_writeback
side_effect_contract:
  side_effect_type: feishu_writeback
  idempotency_key_template: "{request_id}:feishu_writeback:{target_table}:{record_id_or_product_id}"
  retry_policy:
    retryable_errors:
      - rate_limited
      - timeout
      - server_error
    max_attempts: 3
  requires_approval: true
  compensation_policy:
    mode: no_automatic_rollback
    on_cancel_after_write: create_manual_review_summary
  result_ref_type: feishu_write_result_ref
```

## 12. 示例: 关键词搜索选品写入的链式组合

MVP 可以先把 `search_keyword_selection_products` 拆成链式节点:

```text
prepare_search_spec
-> fastmoss_product_search
-> candidate_review
-> selected_product_detail_collect
-> detail_review
-> fact_bundle_upsert
-> feishu_selection_writeback
-> task_summary
```

每个节点输出标准引用:

| 节点 | 标准输出 |
| --- | --- |
| `prepare_search_spec` | `canonical_search_spec_ref` |
| `fastmoss_product_search` | `candidate_set_ref` |
| `candidate_review` | `selected_candidate_set_ref`, `approval_decision_ref` |
| `selected_product_detail_collect` | `product_detail_bundle_ref` |
| `detail_review` | `approved_product_detail_bundle_ref`, `approval_decision_ref` |
| `fact_bundle_upsert` | `fact_write_result_ref` |
| `feishu_selection_writeback` | `feishu_write_result_ref` |
| `task_summary` | `workflow_summary_ref` |

最终 `task_request.result_json` 不保存所有明细，只保存标准输出组合:

```json
{
  "outcome": "success",
  "outputs": {
    "candidate_set_ref": "artifact://req_123/stage_001/product_candidate_set",
    "selected_candidate_set_ref": "artifact://req_123/stage_002/selected_candidates",
    "product_detail_bundle_ref": "artifact://req_123/stage_003/detail_bundle",
    "fact_write_result_ref": "artifact://req_123/stage_005/fact_write_result",
    "feishu_write_result_ref": "artifact://req_123/stage_006/feishu_write_result",
    "workflow_summary_ref": "artifact://req_123/stage_007/summary"
  },
  "summary": {
    "candidate_count": 86,
    "selected_count": 12,
    "written_count": 12,
    "failed_count": 0
  }
}
```

这个例子体现设计目标:

- 流程节点化: 每个业务阶段都是节点。
- 节点通用化: `fastmoss_product_search`、`candidate_review`、`fact_bundle_upsert`
  可以被其他 workflow 复用。
- 输入输出标准化: 每个节点通过 `*_ref` 传递结果。
- Agent 可编排: Agent 只需要知道节点 catalog 和 ref 类型兼容关系。
- Runtime 可恢复: 每个节点 run 独立记录状态。
- 副作用可控: 写 Fact DB 和飞书前可以要求 approval。

## 13. 当前 Runtime DB 的结合方式

当前实现可以作为基础，不需要推倒重来。

推荐映射:

| 新抽象 | 当前承接方式 | MVP 处理 |
| --- | --- | --- |
| Workflow Run | `task_request` | 继续使用 `task_request` 作为顶层 run |
| Node Run | 新增概念，后续可落 `stage_run` 或 `node_run` | MVP 先明确 contract，再设计表 |
| Work Item | `api_worker_job` / `task_execution` | 继续保留 API/browser 分表 |
| Approval Request | 当前缺失 | 需要新增状态源，Outbox 不能替代 |
| Artifact Ref | `artifact_object` + metadata | 短期可扩展 metadata，后续补字段 |
| Outbox | `notification_outbox` | 用 `ref_type/ref_id` 指向 approval 或 node run |

最小落地顺序:

```text
1. 定义 Node Spec / Result Envelope / Artifact Ref contract
2. 定义 WorkflowPlanSpec 和 Compiler 校验规则
3. 增加 Node Run / Stage Run 状态源
4. 增加 Approval Request 状态源
5. 扩展 artifact / outbox 与 node run、approval 的关联
```

## 14. 判断标准

一个能力节点必须满足以下标准，才允许进入 Catalog:

- 有稳定 `node_code`。
- 有机器可读输入 schema。
- 有机器可读输出 schema。
- 输出使用 Result Envelope。
- 大结果通过 Artifact Ref 传递。
- 声明 worker 类型。
- 声明副作用。
- 声明幂等键。
- 声明超时和重试策略。
- 若有危险副作用，声明审批策略。

一个 workflow plan 必须满足以下标准，才允许执行:

- 所有节点都存在于 Catalog。
- 所有边的 input/output 类型兼容。
- 所有副作用都有明确写入策略。
- 所有需要人工确认的节点都有 approval policy。
- 所有大对象通过 artifact ref 传递。
- Runtime 能为每个节点创建可恢复的 node run。
