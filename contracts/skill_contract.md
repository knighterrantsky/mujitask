# Skill Contract

更新时间: 2026-05-05

本契约定义 Mujitask 仓库内 agent skill bundle 的唯一维护方式。所有 `skills/*/SKILL.md` 都是生成产物，不允许作为人工维护源。

## 1. Source Of Truth

每个 skill bundle 必须包含：

```text
skills/{skill_code}/
  skill.spec.yaml
  examples.eval.yaml
  SKILL.md
```

维护链路固定为：

```text
skill.spec.yaml -> tools/render_skill.py -> SKILL.md -> tools/validate_skill.py -> CI gate
```

规则：

- `skill.spec.yaml` 是人工维护源文件。
- `SKILL.md` 必须由 `tools/render_skill.py` 生成。
- `SKILL.md` 顶部 front matter 后必须包含生成标记。
- 修改 `skill.spec.yaml` 后必须重新生成 `SKILL.md`。
- `tools/validate_skill.py` 必须验证 spec、eval examples 和生成产物一致性。
- CI 必须运行 skill contract gate。

## 2. Required Spec Shape

`skill.spec.yaml` 必须符合 `contracts/skill_spec.schema.json` 的结构要求，并至少表达：

- skill metadata: `name`、`title`、`description`、`short_description`、`owner`、`side_effects`
- source of truth: `business_overview`、`requirements_index`
- formal task codes: 当前 skill 业务域允许暴露的正式 workflow `task_code`
- inputs: 当前 skill 允许从用户请求提取并传给正式入口的业务字段
- supported workflows: 每个 intent 的 `kind`、`task_code` 或 `parent_task_code`、目标表、需求文档、设计文档、入口命令
- execution manual: `workflow`、`intent_precedence`、`output_format`、`guardrails`、`edge_cases`、`final_checks`
- examples: 正例和负例必须覆盖提交、拒绝触发和目标表不明确场景

`mujitask-tiktok-feishu-sync` 的正式 task_code 必须精确为：

```text
refresh_current_competitor_table
search_keyword_competitor_products
sync_tk_influencer_pool
tiktok_influencer_outreach_sync
tiktok_fastmoss_product_ingest
search_keyword_selection_products
```

`mujitask-amazon-feishu-sync` 的正式 task_code 必须精确为：

```text
refresh_amazon_product_row_by_asin
refresh_current_amazon_product_table
```

业务域隔离规则：

- TikTok Skill 的 `metadata.owner` 必须是 `domains/tiktok`，不得列出 Amazon task 或 intent。
- Amazon Skill 的 `metadata.owner` 必须是 `domains/amazon`，不得列出 TikTok 或 FastMoss task/intent。
- 每个业务域使用的 Skill、OpenClaw agent/workspace 和飞书账号/会话路由以 `contracts/agents/business-agent-bindings.yaml` 为准；本地飞书 account ID 必须来自部署配置，不得在 skill 代码中固定为 `default` 等字面量。
- Skill bundle 不保存飞书 App ID、App Secret、token 真值或 OpenClaw 配置文件。

不允许再使用 generic `keyword_search` 同时代表竞品和选品。关键词搜索竞品写入必须使用 `keyword_competitor_search`，关键词搜索选品写入必须使用 `keyword_selection_search`。

## 3. Side-Effect Skills

当 `metadata.side_effects` 为 `true` 时，skill 会写飞书、提交任务、调用外部系统或发送通知，必须额外满足：

- 至少一个 intent 标记 `side_effects`。
- 每个有副作用的 formal workflow 必须有明确的 `command`、`task_code`、`target_tables`、`source_documents.requirements` 和 `source_documents.design`。
- 每个 operational sub-intent 必须有明确的 `command`、`parent_task_code`、`mode` 和 `target_tables`。
- 必须声明输入抽取字段，不能让 agent 自由创造参数。
- 必须提供 `negative_activation_examples`，说明哪些表达不能触发本 skill。
- 必须声明 `guardrails`、`edge_cases` 和 `final_checks`。
- 必须声明固定输出格式，尤其是 submit 型入口的 `request_id` 回执。
- `examples.eval.yaml` 必须覆盖正例、负例和易混淆路由。

## 4. Generated SKILL.md

`SKILL.md` 只服务 agent 读取，不是事实源。它必须：

- 从 spec deterministic render。
- 保留 OpenClaw 可读取的 YAML front matter。
- front matter `description` 只描述明确触发场景，不能放宽为泛 FastMoss / 泛 TK 讨论。
- 标准模式按以下 section 顺序生成：
  `Purpose`、`Source of truth`、`When to use`、`Do not use this skill`、`Required inputs`、`Supported workflows`、`Workflow`、`Intent precedence`、`Commands`、`Output format`、`Guardrails`、`Edge cases`、`Final checks`、`Examples`、`Negative activation examples`。
- 单一入口且详细规则已归档到需求/契约的 Skill 可以设置 `metadata.render_mode: compact`，只生成 `Scope`、`Trigger`、`Input`、`Submit`、`Output`、`Guardrails`；完整结构仍保留在 `skill.spec.yaml`，不得复制到 `SKILL.md`。
- 只作为 routing 和 task-submission 执行手册，不承载凭证、table ID、Runtime DB 排障或部署 runbook。

禁止：

- 直接手写或补丁修改 `SKILL.md` 而不同步 `skill.spec.yaml`。
- 在 `SKILL.md` 中添加 spec 没有记录的新入口、新 intent 或新输出承诺。
- 让有副作用的 skill 只靠自然语言描述入口，缺少命令和任务名。
- 生成旧章节：`生成说明`、`触发条件`、`Intent 路由`、`输入提取规则`、`固定配置`、`默认入口`、`失败处理`、`输出契约`。
- 输出敏感配置词或实现细节，例如 access token、password、secret、table URL、手工 source env、Runtime DB 手工排障。
- 把 `FastMoss`、`TK竞品`、`TikTok竞品`、`写入当前飞书表`、`更新当前表` 作为 standalone trigger。

## 5. Validation Commands

本地和 CI 使用同一组命令：

```bash
uv run --extra dev python tools/render_skill.py --check
uv run --extra dev python tools/validate_skill.py
uv run --extra dev pytest tests/test_skill_contract.py
uv run --extra dev pytest tests/test_business_agent_isolation_contract.py
```

`render_skill.py --check` 只做生成一致性检查，不写文件。需要更新生成产物时运行：

```bash
uv run --extra dev python tools/render_skill.py
```

## 6. CI Gate

`.github/workflows/validate-skills.yml` 必须至少运行：

- `tools/render_skill.py --check`
- `tools/validate_skill.py`

任何新增 skill 或修改 skill spec 的 MR / PR，只有通过该 gate 才允许声明完成。
