# 测试目录结构与命名规范

日期: 2026-04-29

## 1. 定位

本文定义 Mujitask 测试代码的目录结构、命名规范、分类规则和迁移策略。

- 本文是测试组织方式的事实来源。
- 测试代码本身仍以 `tests/` 目录为准。
- 本次只定义规范，不要求一次性移动所有旧测试文件。

## 2. 总体原则

测试不完全 1:1 镜像源码目录，而是按测试层级 + 源码 owner 组织。

核心规则：

- 第一层按测试层级分：`contract` / `unit` / `integration` / `e2e`
- 第二层按源码 owner 分：`domains` / `capabilities` / `control_plane` / `infrastructure`

原因：

- 只按源码目录镜像，无法看出测试目的。
- 只按测试类型命名，后期难以定位被保护的 owner。
- 当前项目同时有 Runtime、Fact DB、workflow、handler、projection、contract，继续扁平化会降低可维护性。

## 3. 推荐目录结构

```text
tests/
├── conftest.py
├── support/
│   ├── fixtures/
│   ├── factories/
│   └── helpers/
├── contract/
│   ├── architecture/
│   ├── project_structure/
│   ├── handler_registry/
│   ├── workflow_manifest/
│   └── harness/
├── unit/
│   ├── domains/
│   │   └── tiktok/
│   │       ├── mappers/
│   │       ├── projections/
│   │       ├── policies/
│   │       ├── jobs/
│   │       └── flows/
│   ├── capabilities/
│   │   ├── input_sources/
│   │   ├── fact_sources/
│   │   ├── channels/
│   │   ├── persistence/
│   │   └── media/
│   ├── control_plane/
│   │   ├── executor/
│   │   ├── supervisor/
│   │   ├── watchdog/
│   │   ├── outbox/
│   │   └── runtime_config/
│   └── infrastructure/
│       ├── fastmoss/
│       ├── feishu/
│       ├── runtime/
│       └── storage/
├── integration/
│   ├── runtime/
│   ├── workflows/
│   ├── outbox/
│   └── db/
└── e2e/
    ├── tiktok/
    ├── fastmoss/
    └── feishu/
```

## 4. 测试层级定义

### 4.1 `tests/contract/`

用途：

- 静态契约测试
- 架构护栏
- handler registry / workflow manifest / harness gate 检查
- 不依赖外部服务
- 不真实执行业务流程

示例目标：

- project structure contract
- architecture ownership
- handler registry contract
- workflow manifest contract
- completion claim gate

### 4.2 `tests/unit/`

用途：

- 单个函数、mapper、projection、policy、handler、validator、store 小单元测试
- 默认无网络
- 默认不需要真实账号
- 默认不依赖真实外部服务

适合：

- domains/tiktok/mappers
- domains/tiktok/projections
- capabilities/input_sources/feishu
- capabilities/fact_sources/fastmoss
- control_plane/watchdog
- infrastructure/fastmoss

### 4.3 `tests/integration/`

用途：

- 多个组件协作
- Runtime DB 支持的 workflow 执行链路
- executor / worker / outbox / watchdog 协同
- 可依赖测试数据库
- 不应依赖真实生产账号

适合：

- runtime workflow integration
- executor integration
- outbox integration
- Postgres-backed runtime tests

### 4.4 `tests/e2e/`

用途：

- 真实外部服务
- 真实账号或真实浏览器 profile
- TikTok / FastMoss / Feishu live 验证
- 默认不在普通 `pytest` 中无条件运行

必须用 marker 标注：

```python
@pytest.mark.e2e
@pytest.mark.requires_credentials
```

## 5. 文件命名规范

```text
tests/<level>/<owner>/<subarea>/test_<target>_<behavior>.py
```

示例：

```text
tests/unit/domains/tiktok/projections/test_competitor_table_projection.py
tests/unit/domains/tiktok/mappers/test_selection_table_source_adapter.py
tests/unit/capabilities/input_sources/feishu/test_table_read_handler.py
tests/unit/control_plane/watchdog/test_scanner_timeout.py
tests/integration/workflows/test_refresh_current_competitor_table.py
tests/integration/runtime/test_executor_claim_and_summary.py
tests/contract/project_structure/test_project_structure_contract.py
tests/e2e/fastmoss/test_product_search_live.py
```

## 6. 测试函数命名规范

```text
def test_<target>_<expected_behavior>_when_<condition>():
    ...
```

示例：

```python
def test_competitor_projection_preserves_manual_fields_when_fact_missing():
    ...

def test_watchdog_marks_job_failed_when_heartbeat_expired():
    ...

def test_feishu_table_read_returns_normalized_rows_when_view_has_records():
    ...
```

要求：

- 测试函数名必须表达被测对象、期望行为和触发条件。
- 避免 `test_ok`、`test_basic`、`test_case1`、`test_new_flow`。
- 不用 `v1`、`v2`、`new`、`old` 表达测试身份。

## 7. Fixture / helper 规则

```text
tests/support/fixtures/   可复用 pytest fixtures
tests/support/factories/  构造业务对象、payload、result、row、fact bundle
tests/support/helpers/    测试辅助函数
```

约束：

- 测试 helper 不能成为业务代码依赖。
- 测试 helper 不能复制生产逻辑。
- fixture 名称要表达资源范围，例如 `runtime_store`, `fake_feishu_client`, `sample_competitor_row`。
- e2e 凭证相关 fixture 必须跳过缺少凭证的情况，不能默认失败或读取真实本地私密文件。

## 8. Marker 规则

建议使用以下 marker：

```text
contract
unit
integration
e2e
requires_db
requires_credentials
requires_browser
slow
```

说明：

- `unit` 默认应该最快、最稳定。
- `integration` 可以使用测试 DB。
- `e2e` 必须显式 marker。
- 涉及真实外部账号、真实浏览器 profile、真实网络请求的测试必须标记 `requires_credentials` 或 `requires_browser`。

示例：

```python
@pytest.mark.integration
@pytest.mark.requires_db
def test_executor_claims_pending_job(...):
    ...
```

```python
@pytest.mark.e2e
@pytest.mark.requires_credentials
def test_fastmoss_product_search_live(...):
    ...
```

## 9. 旧测试迁移策略

当前不要求一次性移动所有旧测试文件。

迁移顺序建议：

1. 先迁移 contract 测试到 `tests/contract/`
2. 再迁移 unit 测试到 `tests/unit/`
3. 再迁移 integration 测试到 `tests/integration/`
4. 最后迁移 e2e 测试到 `tests/e2e/`

每次移动测试文件时，必须同步检查并更新：

- `contracts/harness/code-roadmap.yaml` 中引用的测试文件路径
- `docs/test/README.md` 中的分类说明
- `AGENTS.md` 中涉及测试 gate 的说明
- `scripts/harness/claim_done.py` 中的路径假设
- CI / 发布脚本中的 pytest 路径

原因：

- 当前 `contracts/harness/code-roadmap.yaml` 中可能引用具体测试文件路径。
- 直接移动测试文件可能导致 completion gate 失效。
- 所以测试结构迁移必须分批做，每批移动后运行对应 gate。

## 10. 新增测试放置规则

新增测试默认必须按新目录结构放置。旧测试可继续留在 `tests/` 根目录，直到对应模块被迁移。

新增测试选择规则：

| 测试类型 | 放置位置 |
| --- | --- |
| 架构/契约护栏 | `tests/contract/` |
| 纯函数/单 handler/mapper/projection | `tests/unit/` |
| Runtime DB + 多组件协作 | `tests/integration/` |
| 真实外部服务 / 真实账号 / 真实浏览器 | `tests/e2e/` |

## 11. 禁止事项

- 不要继续无限制往 `tests/` 根目录新增测试文件。
- 不要只用 `test_runtime_xxx.py` 这种前缀承载所有集成测试。
- 不要把 e2e 测试混进普通 unit/integration 测试。
- 不要让测试 helper 依赖真实账号、真实 token、真实本地私密配置。
- 不要移动测试文件后忘记更新 `contracts/harness/code-roadmap.yaml`。
- 不要把测试目录结构做成源码目录的机械镜像。
- 不要在测试文件名里使用 `v1`、`v2`、`new`、`old`、`legacy`。
