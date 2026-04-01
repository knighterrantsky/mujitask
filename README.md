# automation-business-scaffold

`automation-business-scaffold` 是业务侧的起步模板仓库。

它的定位很明确：

- 它不是 framework 源码仓库
- 它不是多个业务长期共用的业务仓库
- 它是一个“拿下来就能开始写业务”的 base project

推荐模式：

1. 平台团队维护 `automation-framework` 与 `automation-business-scaffold`
2. 新业务从 `automation-business-scaffold` 初始化自己的独立仓库
3. 业务团队只在自己的业务仓库里长期开发

## 1. 先读什么

默认阅读顺序：

1. `.platform/platform-manifest.yaml`
2. `.platform/model-rules.yaml`
3. `AGENT.MD`
4. `docs/framework_contract/0.2.1/public-capability-status.md`
5. `docs/framework_contract/0.2.1/business-consumption-contract.md`

这几个文件一起定义：

- 当前 pinned 的 framework 版本
- 允许使用的 framework import 面
- 哪些目录是 platform-managed
- 哪些目录是业务可编辑区
- 当前能力哪些可用、哪些还在规划中

额外约束：

- 这个仓库必须能在“只 clone 自己一个仓库”的情况下工作
- 不要把 `../automation-framework` 视为必然存在的路径
- 同级目录里的本地 framework checkout 只属于平台联调便利，不属于标准安装前提

## 2. 快速启动

### 标准模式

这是默认模式，也是对外必须保证成立的模式。

创建虚拟环境并安装：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
python -m playwright install chromium
```

这一步会通过 `pyproject.toml` 中 pin 的 git 依赖自动安装对应 framework 版本。

在这个模式下，你不需要本地 `automation-framework` 同级目录。

### 平台联调覆盖模式

如果你在同一个 `workspace` 里同时有本地 framework 仓库，平台维护者可以临时覆盖 pinned 依赖：

```bash
pip install -e ../automation-framework
```

这个模式只用于：

- 验证 scaffold 是否兼容未发布的 framework 改动
- 平台侧本地联调

不要把它作为业务开发默认前提写入后续派生业务仓库。

复制运行时配置：

```bash
cp .env.example .env
cp config/browser_profiles.example.json config/browser_profiles.json
```

启动 agent：

```bash
uvicorn automation_business_scaffold.agent:app --app-dir src --host 127.0.0.1 --port 8110
```

查看 task：

```bash
curl http://127.0.0.1:8110/tasks
```

执行 demo workflow：

```bash
curl -X POST http://127.0.0.1:8110/runs \
  -H 'Content-Type: application/json' \
  -d '{
    "task_name": "source_to_target_publish_demo",
    "params": {
      "title": "Demo Vintage Chair",
      "price": 128,
      "run_mode": "draft"
    },
    "wait": true
  }'
```

### 直接脚本执行模式

如果不需要启动 agent，也可以直接执行注册好的 task。

先列出可运行 task：

```bash
automation-business-scaffold-run list-tasks
```

直接运行 TikTok 链接清洗 task：

```bash
cd "$HOME/apps/mujitask"
automation-business-scaffold-run run \
  --task tiktok_product_link_cleanup \
  --params-json '{
    "table_url": "https://my.feishu.cn/base/appXXX?table=tblXXX",
    "access_token_env": "FEISHU_ACCESS_TOKEN",
    "url_field_name": "产品链接",
    "normalized_url_field_name": "标准产品链接",
    "cleanup_status_field_name": "链接整理状态",
    "run_mode": "approval_required"
  }'
```

直接运行 TikTok 表格驱动批量同步 task：

```bash
cd "$HOME/apps/mujitask"
automation-business-scaffold-run run \
  --task tiktok_feishu_batch_sync \
  --params-json '{
    "table_url": "https://my.feishu.cn/base/appXXX?table=tblXXX",
    "access_token_env": "FEISHU_ACCESS_TOKEN",
    "url_field_name": "产品链接",
    "profile_ref": "local-chrome",
    "run_mode": "approval_required"
  }'
```

如果只是单条 URL 调试底层字段构建，可以继续使用：

```bash
python -m automation_business_scaffold.cli run \
  --task tiktok_product_to_feishu \
  --product-url 'https://www.tiktok.com/shop/pdp/1729440407432826887'
```

这个模式会像 agent 一样写入运行记录和中间数据：

- `runtime/cli_runs/*.json`
- `runtime/cli_runs/steps/*.json`
- `runtime/cli_runs/signals/*.json`
- `runtime/artifacts/<run_id>/...`

## 3. 目录边界

### Platform-managed

默认不要在普通业务开发里修改这些区域：

- `.platform/`
- `AGENT.MD`
- `docs/framework_contract/`
- `src/automation_business_scaffold/agent.py`
- `src/automation_business_scaffold/registry.py`
- `tests/test_agent.py`
- `tests/test_contract_pack.py`

### Business-editable

业务开发默认在这些区域工作：

- `src/automation_business_scaffold/config.py`
- `src/automation_business_scaffold/tasks/`
- `src/automation_business_scaffold/workflows/`
- `src/automation_business_scaffold/flows/`
- `src/automation_business_scaffold/models/`
- `src/automation_business_scaffold/mappers/`
- `src/automation_business_scaffold/validators/`
- `docs/business/`
- `tests/test_registry.py`
- `tests/test_workflow_demo.py`

## 4. 公开接入入口

这个 scaffold 对外固定公开两个入口：

- `automation_business_scaffold.agent:app`
- `automation_business_scaffold.registry.build_task_registry()`

内置 demo task：

- task name: `source_to_target_publish_demo`
- workflow builder: `build_source_to_target_publish_workflow(run_mode="draft")`

当前 TikTok 业务主入口：

- `tiktok_product_link_cleanup`
- `tiktok_feishu_batch_sync`
- `tiktok_product_to_feishu`

## 5. 新业务怎么从这里开始

建议流程：

1. 用这个仓库初始化一个新的业务仓库
2. 替换项目名、包名、README 标题
3. 在 `tasks/__init__.py` 中替换默认 task 列表
4. 在 `tasks/`、`workflows/`、`mappers/`、`validators/` 内逐步替换 demo 逻辑
5. 保留 `.platform/*`、`AGENT.MD`、`docs/framework_contract/*`

注意：

- 不建议长期直接在 `automation-business-scaffold` 仓库上写真实业务
- 当前 TikTok 一期为了直接复用 `chrome_cdp` / `roxy` provider，业务实现显式依赖 `automation_framework.browser`
- `workflow_draft.review-only.yaml` 只是审核样例，不是可执行 workflow

## 6. 运行时配置与业务默认配置

### runtime 配置

由 framework runtime 读取：

- `BROWSER_PROFILES_FILE`
- `DEFAULT_PROFILE_REF`
- `AGENT_HOST`
- `AGENT_PORT`
- `AGENT_RUN_DIR`
- `AGENT_RECORDING_DIR`

这些配置写在 `.env.example` 中。

### 业务默认配置

由本仓库自己的 `src/automation_business_scaffold/config.py` 负责：

- `BUSINESS_DEFAULT_RUN_MODE`
- `BUSINESS_SOURCE_SYSTEM`
- `BUSINESS_TARGET_SYSTEM`
- `BUSINESS_DEFAULT_CATEGORY`
- `BUSINESS_DEFAULT_PRICE`
- `BUSINESS_DEFAULT_DESCRIPTION`

这部分是业务级默认值，不属于 framework runtime 配置。

## 7. 如何新增业务 task

推荐步骤：

1. 在 `models/` 补业务模型
2. 在 `mappers/` 补字段映射
3. 在 `validators/` 补业务校验
4. 在 `flows/` 补站点交互骨架或数据整形
5. 在 `workflows/` 用 `WorkflowSpec` 描述 step
6. 在 `tasks/` 继承 `BaseWorkflowTask`
7. 在 `tasks/__init__.py` 把新 task 加入默认列表

## 8. 如何替换 framework 依赖

默认依赖已经 pin 到当前 framework commit：

```text
55e8223a92f562f4053006c55e66fe5491c9be61
```

如果平台发布了新版本，升级顺序固定为：

1. 先看新的 `docs/framework_contract/<framework_version>/...`
2. 再看 `docs/framework_contract/<framework_version>/public-migration-guide.md`
3. 再更新 pinned framework 依赖
4. 最后按需迁移 platform-managed 区域

更完整的业务升级手册见：

- `docs/business/platform-upgrade-playbook.md`
- `docs/business/upgrade-notes.md`

## 9. 验证

运行测试：

```bash
pytest
```

首版至少验证这些场景：

- `GET /tasks` 能看到 demo task
- demo task 能通过 `/runs` 执行并产出 step / signal / artifact
- `draft` 模式下带 `submit` effect 的 step 会被 runtime 阻止
- vendored contract docs 版本与 `.platform/platform-manifest.yaml` 一致
