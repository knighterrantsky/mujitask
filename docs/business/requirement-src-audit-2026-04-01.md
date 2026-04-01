# TikTok 选品表需求符合性审计与 `src/` 逐文件函数说明

审计时间：`2026-04-01`

需求基线：`docs/business/requirement.md`

代码范围：`src/automation_business_scaffold`

证据口径：
- 静态阅读 `src/automation_business_scaffold`
- 参考测试 `tests/test_tiktok_product_flow.py`、`tests/test_tiktok_feishu_batch_sync.py`
- 验证命令：`uv run --extra dev pytest`
- 验证结果：`21 passed`

## 1. 总结结论

当前代码**未完全满足** [requirement.md](./requirement.md) 的最新要求，整体判断为**部分满足**。

已具备的主干能力：
- 已有 TikTok 阶段一浏览器采集链路，能抓取商品信息、主图、本地截图，并在落表前把本地图片上传成飞书附件。
- 已有链接格式化、按规范 URL 去重、删除重复记录的能力。
- 现有自动化测试可以证明主流程代码是可运行的。

仍然不满足的关键点：
- 默认字段映射仍绑定旧表结构，仍会写入 `标准产品链接`、`链接整理状态`、`删除重复数`、`商品主图`、`商品页截图`、`采集状态`、`采集错误`、`采集时间` 等字段。
- 阶段一默认写回逻辑没有按最新需求落到 `图片`、`前台截图`、`记录日期` 这一组真实字段。
- 仓库仍保留 `requests + HTML` 路径作为可执行链路，和“浏览器驱动才是正式方案”的最新要求不完全一致。
- FastMoss 阶段二没有实现代码。

## 2. 需求符合性结论矩阵

### 2.1 第 3 节：当前阶段硬约束

| 需求点 | 状态 | 代码/测试证据 | 结论说明 |
| --- | --- | --- | --- |
| 不新增字段、不删除字段、不修改字段定义 | 部分满足 | `requirement.md:15-20`；`src/automation_business_scaffold/flows/tiktok_feishu_sync_flow.py:34-52` | 代码没有直接修改飞书表结构的能力，但默认写回字段依赖旧字段集合，实质上要求表里存在额外字段。 |
| 阶段一只能写入现有输出字段，不能额外引入状态字段、中间字段、标准化链接字段 | 不满足 | `requirement.md:18`；`tiktok_feishu_sync_flow.py:34-52`；`tiktok_feishu_sync_flow.py:893-921` | cleanup 默认写 `标准产品链接/链接整理状态/删除重复数`；batch sync 默认写 `商品主图/商品页截图/采集状态/采集错误/采集时间`。 |
| 图片类字段必须写入图片附件本身，不能只写链接 | 已满足 | `requirement.md:19`；`src/automation_business_scaffold/flows/tiktok_feishu_sync_flow.py:870-891`；`src/automation_business_scaffold/extend_script/feishu_api.py:190-251`；`tests/test_tiktok_product_flow.py:222`；`tests/test_tiktok_feishu_batch_sync.py:323-325` | 代码先生成本地文件，再调用 `upload_media()`，最后写 `file_token` 到附件字段。 |
| 允许格式化 `产品链接` 并回写原字段 | 部分满足 | `requirement.md:16`；`tiktok_feishu_sync_flow.py:130-220`；`tiktok_feishu_sync_flow.py:893-911` | cleanup 任务可以回写 `产品链接`，但 batch sync 本身不强制执行“先格式化再采集”的完整顺序。 |
| 允许基于格式化后的 `产品链接` 判重并删除重复行 | 已满足 | `requirement.md:20`；`tiktok_feishu_sync_flow.py:130-220`；`tiktok_feishu_sync_flow.py:222-266`；`tests/test_tiktok_feishu_batch_sync.py:40-178` | cleanup 流程会提取 `product_id`、保留首条、删除重复记录。 |

### 2.2 第 4-5 节：当前飞书字段与字段分组

| 需求点 | 状态 | 代码/测试证据 | 结论说明 |
| --- | --- | --- | --- |
| 输入字段以 `产品链接`、`关键词`、`备注` 为主 | 部分满足 | `requirement.md:48-54`；`tiktok_feishu_sync_flow.py:771-808` | 代码只真正依赖 `产品链接`，没有对 `关键词`、`备注` 建立显式读取或落表逻辑。 |
| 阶段一目标字段为 `SKU-ID/图片/标题/节日/卖家/前台截图/价格/记录日期` | 部分满足 | `requirement.md:56-64`；`src/automation_business_scaffold/flows/tiktok_product_flow.py:34-41`；`tiktok_feishu_sync_flow.py:38-52` | `SKU-ID/标题/节日/卖家/价格` 有实现；图片与截图默认写到旧字段 `商品主图/商品页截图`；`记录日期` 未按需求实现。 |
| 阶段二目标字段为 FastMoss 相关字段 | 未实现 | `requirement.md:66-74`；`src/automation_business_scaffold` 全局无 `FastMoss` 采集实现 | 代码里没有阶段二流程、模型、任务、workflow。 |

### 2.3 第 6 节：业务流程

| 需求点 | 状态 | 代码/测试证据 | 结论说明 |
| --- | --- | --- | --- |
| 6.1 总体流程：先格式化 URL，再去重，再阶段一采集，再阶段二采集 | 部分满足 | `requirement.md:78-83`；`src/automation_business_scaffold/tasks/tiktok_product_link_cleanup.py`；`src/automation_business_scaffold/tasks/tiktok_feishu_batch_sync.py` | cleanup、阶段一采集是分开的两个 task，没有被封装成一个必经的串联主流程；阶段二缺失。 |
| 6.2 阶段一必须走浏览器驱动 | 部分满足 | `requirement.md:85-99`；`src/automation_business_scaffold/flows/tiktok_product_flow.py:144-170`；`src/automation_business_scaffold/flows/tiktok_feishu_sync_flow.py:486-536` | batch sync 走浏览器；但 `tiktok_product_to_feishu` 和 `tiktok_feishu_single_sync` 仍走 `fetch_tiktok_product_record()` 的 HTTP/HTML 方案。 |
| 6.2 阶段一采集 `SKU-ID/标题/卖家/价格/主图/前台截图/节日/记录日期` | 部分满足 | `requirement.md:89-99`；`tiktok_product_flow.py:172-232`；`tiktok_product_flow.py:424-461`；`tiktok_feishu_sync_flow.py:914-925` | `SKU-ID/标题/卖家/价格/主图/截图/节日` 有实现；`记录日期` 没有写入现表要求的 `yyyy/MM/dd` 字段。 |
| 6.2 图片和前台截图必须保存为飞书附件 | 已满足 | `requirement.md:99`；`tiktok_feishu_sync_flow.py:870-891`；`tests/test_tiktok_feishu_batch_sync.py:323-325` | 上传附件能力满足要求。 |
| 6.3 FastMoss 阶段二采集 | 未实现 | `requirement.md:101-114`；`src/automation_business_scaffold` 无对应 flow/task/workflow | 代码中没有 FastMoss 页面抓取、截图、销量趋势落表逻辑。 |
| 6.4 链接整理：提取 `product_id`、格式化回写原字段、按格式化 URL 去重、保留首条删其余 | 部分满足 | `requirement.md:116-123`；`src/automation_business_scaffold/flows/tiktok_product_flow.py:81-113`；`tiktok_feishu_sync_flow.py:130-220`；`tests/test_tiktok_feishu_batch_sync.py:40-178` | 核心逻辑成立，但 cleanup 同时写了需求禁止的中间状态字段。 |

### 2.4 第 7 节：可测试范围

| 需求点 | 状态 | 代码/测试证据 | 结论说明 |
| --- | --- | --- | --- |
| 读取飞书表并识别待处理记录 | 已满足 | `requirement.md:127-134`；`tiktok_feishu_sync_flow.py:118-128`；`tiktok_feishu_sync_flow.py:393-403` | cleanup 和 batch sync 都支持读取整表记录。 |
| URL 格式化并回写 | 部分满足 | `tiktok_feishu_sync_flow.py:268-335`；`tests/test_tiktok_feishu_batch_sync.py:112-178` | cleanup 有能力，但默认还会附带写中间状态字段。 |
| 按格式化 URL 判重并删除重复记录 | 已满足 | `tiktok_feishu_sync_flow.py:130-266`；`tests/test_tiktok_feishu_batch_sync.py:40-178` | 逻辑和测试都存在。 |
| 浏览器打开 TikTok 商品页并采集阶段一信息 | 已满足 | `tiktok_product_flow.py:144-170`；`tiktok_feishu_sync_flow.py:486-536`；`tests/test_tiktok_product_flow.py:235-270` | batch sync 主链路满足浏览器采集。 |
| 生成主图和前台截图 | 已满足 | `tiktok_product_flow.py:424-461`；`tests/test_tiktok_product_flow.py:235-270` | 两类本地文件都能生成。 |
| 验证图片字段写入的是附件本身而不是链接文本 | 已满足 | `tiktok_feishu_sync_flow.py:870-891`；`tests/test_tiktok_product_flow.py:222`；`tests/test_tiktok_feishu_batch_sync.py:323-325` | 测试证明写入值是 `file_token` 结构，不是 URL 文本。 |
| 基于已有字段映射写入现有输出字段 | 不满足 | `tiktok_product_flow.py:34-41`；`tiktok_feishu_sync_flow.py:38-52`；`tests/test_tiktok_feishu_batch_sync.py:96-97`、`160-177`、`323-325` | 默认字段映射与 2026-04-01 的真实表字段不一致，测试也在验证旧字段。 |
| 7.2 不可测试目标：不新增字段、不删字段、不改字段定义、FastMoss 阶段二 | 部分满足 | `requirement.md:136-140`；`src/automation_business_scaffold` 无 schema 变更能力、无 FastMoss 代码 | 没有表结构变更逻辑；FastMoss 确实未实现。 |

### 2.5 第 8-9 节：进度陈述与下一步

| 需求点 | 状态 | 代码/测试证据 | 结论说明 |
| --- | --- | --- | --- |
| 8.2 “已完成基于浏览器的 TikTok 阶段一采集链路开发” | 已满足 | `requirement.md:149-152`；`tiktok_product_flow.py:144-170`；`tiktok_feishu_sync_flow.py:486-536` | 这条自述能被代码和测试证明。 |
| 8.2 “已完成表格驱动的阶段一批量任务实现” | 已满足 | `src/automation_business_scaffold/tasks/tiktok_feishu_batch_sync.py`；`src/automation_business_scaffold/workflows/tiktok_feishu_batch_sync_v1.py` | 批量任务和 workflow 都存在。 |
| 8.2 “已完成自动化测试，当前测试集通过” | 已满足 | 命令 `uv run --extra dev pytest` | 当前环境下测试确实通过。 |
| 8.3 “默认字段映射和落表行为仍需按真实表结构收敛” | 已满足 | `requirement.md:154-157`；`tiktok_feishu_sync_flow.py:34-52` | 这条偏差判断与代码现状一致。 |
| 8.4 “FastMoss 第二阶段尚未实现” | 已满足 | `requirement.md:159-161`；全仓库无对应实现 | 代码现状支持该判断。 |
| 9 “下一步需要按真实表结构重新收敛字段映射，并保留产品链接格式化回写与删重能力” | 已满足 | `requirement.md:163-167`；本审计结论同向 | 这是当前代码最直接的收口方向。 |

## 3. 必须明确记录的风险

1. cleanup 默认写入 `标准产品链接`、`链接整理状态`、`删除重复数`，与“只能写现有字段、不能引入中间字段/状态字段”的要求冲突。证据：`src/automation_business_scaffold/flows/tiktok_feishu_sync_flow.py:34-36`、`893-911`。
2. batch sync 默认写入 `商品主图`、`商品页截图`、`采集状态`、`采集错误`、`采集时间`，与现表要求的 `图片`、`前台截图`、`记录日期` 不一致。证据：`src/automation_business_scaffold/flows/tiktok_feishu_sync_flow.py:38-52`、`914-925`。
3. 阶段一默认写回链路没有按需求写 `记录日期`，而是写旧字段 `采集时间`，且格式为 UTC ISO 字符串，不是 `yyyy/MM/dd`。证据：`src/automation_business_scaffold/flows/tiktok_feishu_sync_flow.py:49-52`、`914-925`、`1073`。
4. FastMoss 第二阶段只有需求描述，没有任何 flow/task/workflow/model。证据：`src/automation_business_scaffold` 下无相关实现。
5. 仓库仍保留 `requests + HTML` 采集链路，和“浏览器驱动才是正式方案”的最新口径不完全一致。证据：`src/automation_business_scaffold/flows/tiktok_product_flow.py:116-142`、`src/automation_business_scaffold/tasks/tiktok_product_to_feishu.py`、`src/automation_business_scaffold/flows/tiktok_feishu_sync_flow.py:661-738`。
6. batch sync 默认不会把格式化后的 URL 回写到 `产品链接`；如果没有先单独跑 cleanup，阶段一批量任务并不保证满足“先格式化再采集”的流程约束。证据：`src/automation_business_scaffold/flows/tiktok_feishu_sync_flow.py:486-536` 与 `src/automation_business_scaffold/flows/tiktok_product_flow.py:272-319`。
7. 现有测试是“旧字段口径下通过”，不能证明“当前真实表结构口径下通过”。证据：`tests/test_tiktok_feishu_batch_sync.py:96-97`、`160-177`、`221-230`、`323-325`、`377-378`。

## 4. `src/` 逐文件功能列表与函数 I/O

说明：
- “导出/装配型文件”表示主要负责包导出、任务注册、workflow 组装或 agent 壳层。
- “输入”侧重参数、环境变量、上游依赖。
- “输出”侧重返回值、写文件/网络/浏览器等副作用，以及主要异常语义。

### 4.1 根包与壳层

#### `src/automation_business_scaffold/__init__.py`（导出型）

功能：
- 定义包版本 `__version__ = "0.1.0"`。

函数/方法：
- 无。

#### `src/automation_business_scaffold/agent.py`（装配型）

功能：
- 基于 `build_task_registry()` 创建 HTTP agent 应用。
- 从环境变量读取监听地址并启动 `uvicorn`。

函数/方法：
- `main()`
  - 输入：环境变量 `AGENT_HOST`、`AGENT_PORT`。
  - 输出：无返回值；副作用是启动 Web 服务；异常会由 `uvicorn.run()` 直接抛出。

#### `src/automation_business_scaffold/cli.py`（装配型）

功能：
- 提供命令行入口，列出 task、执行注册 task、写运行记录。

函数/方法：
- `list_registered_tasks()`
  - 输入：无；依赖 `build_task_registry()`。
  - 输出：`list[dict]`，每项含 `name`、`description`。
- `run_registered_task(task_name, params, run_dir=..., run_id=None)`
  - 输入：task 名称、参数字典、运行目录、可选 run_id。
  - 输出：`dict`，包含 `run_id`、运行状态、结果或错误、运行记录路径；副作用是创建 `runtime/cli_runs` 记录。
- `_load_json_object(source, label)`
  - 输入：JSON 字符串、错误标签。
  - 输出：`dict`；非对象或非法 JSON 时抛 `ValueError`。
- `_parse_param_value(raw_value)`
  - 输入：命令行原始字符串。
  - 输出：优先返回 `json.loads()` 结果，失败时返回原字符串。
- `_parse_param_items(items)`
  - 输入：`KEY=VALUE` 字符串列表。
  - 输出：参数字典；格式不合法时抛 `ValueError`。
- `_build_params(args)`
  - 输入：`argparse.Namespace`。
  - 输出：最终任务参数字典；会合并 `--params-file`、`--params-json`、`--param`、快捷参数。
- `_build_parser()`
  - 输入：无。
  - 输出：配置好的 `ArgumentParser`。
- `main(argv=None)`
  - 输入：可选 argv 列表。
  - 输出：退出码 `int`；副作用是向标准输出打印 JSON。

#### `src/automation_business_scaffold/config.py`（配置型）

功能：
- 读取业务默认配置。

函数/方法：
- `_read_int(name, default)`
  - 输入：环境变量名、默认值。
  - 输出：解析后的 `int`；解析失败时回退默认值。
- `BusinessDefaults`
  - 输入：构造时接收默认运行模式、系统名、默认分类、价格、描述。
  - 输出：冻结 dataclass，供 mapper/task 复用。
- `get_business_defaults()`
  - 输入：环境变量 `BUSINESS_*`。
  - 输出：`BusinessDefaults` 实例。

#### `src/automation_business_scaffold/registry.py`（装配型）

功能：
- 把 `tasks.DEFAULT_TASKS` 注册为 `TaskRegistry`。

函数/方法：
- `build_task_registry()`
  - 输入：无。
  - 输出：`TaskRegistry`；副作用是完成任务注册。

### 4.2 飞书 API 封装

#### `src/automation_business_scaffold/extend_script/feishu_api.py`（业务基础设施）

功能：
- 封装飞书多维表格记录读取、写入、删除和附件上传。

函数/方法：
- `FeishuAPIError`
  - 功能：统一包装飞书 API 错误，携带 `code`、`status`。
- `FeishuAPIError.__post_init__()`
  - 输入：dataclass 初始化后的实例。
  - 输出：无；副作用是把 `message` 交给 `Exception` 基类。
- `FeishuAPIError.__str__()`
  - 输入：实例自身。
  - 输出：带 `code/status` 的错误字符串。
- `parse_table_url(table_url)`
  - 输入：飞书 Base 表格 URL。
  - 输出：`{"app_token","table_id","view_id"}`；缺参数时抛 `ValueError`。
- `FeishuBitableClient.__init__(access_token, timeout=30)`
  - 输入：访问令牌、超时秒数。
  - 输出：实例；副作用是创建 `requests.Session()`。
- `FeishuBitableClient._headers()`
  - 输入：实例自身。
  - 输出：飞书鉴权请求头字典。
- `FeishuBitableClient._request(method, path, params=None, payload=None, retries=3)`
  - 输入：HTTP 方法、API 路径、查询参数、JSON 负载、重试次数。
  - 输出：飞书响应 `dict`；副作用是发网络请求；失败时抛 `FeishuAPIError`。
- `FeishuBitableClient.list_records(...)`
  - 输入：`app_token`、`table_id`、分页参数、筛选表达式、`view_id`。
  - 输出：单页飞书响应 `dict`。
- `FeishuBitableClient.list_all_records(...)`
  - 输入：与 `list_records` 类似。
  - 输出：拼接后的记录列表 `list[dict]`。
- `FeishuBitableClient.update_record(app_token, table_id, record_id, fields)`
  - 输入：表标识、记录 ID、字段字典。
  - 输出：飞书响应 `dict`；副作用是更新记录。
- `FeishuBitableClient.create_record(app_token, table_id, fields)`
  - 输入：表标识、字段字典。
  - 输出：飞书响应 `dict`；副作用是新增记录。
- `FeishuBitableClient.delete_record(app_token, table_id, record_id)`
  - 输入：表标识、记录 ID。
  - 输出：飞书响应 `dict`；副作用是删除记录。
- `FeishuBitableClient.upload_media(file_name, file_data, parent_type="bitable_file", parent_node="", extra=None)`
  - 输入：文件名、二进制内容、父节点信息、可选 extra。
  - 输出：`file_token` 字符串；副作用是上传文件；失败时抛 `FeishuAPIError`。

### 4.3 `flows/`

#### `src/automation_business_scaffold/flows/__init__.py`（导出型）

功能：
- 统一导出 demo、TikTok 采集、同步、清洗相关 flow 函数。

函数/方法：
- 无。

#### `src/automation_business_scaffold/flows/browser_bridge.py`（基础设施）

功能：
- 把 `automation_framework.browser` 的 provider/session 能力包装成可用页面上下文。

函数/方法：
- `BrowserPageSession`
  - 输入：provider 名、target key、profile ref、session ref、page 对象。
  - 输出：浏览器会话 dataclass。
- `open_automation_page(profile_ref=None, workspace_id=None, profile_id=None, provider_name=None, headless=False, force_open=False)`
  - 输入：浏览器目标定位参数。
  - 输出：上下文管理器，`yield BrowserPageSession`；副作用是打开并最终关闭浏览器 session。

#### `src/automation_business_scaffold/flows/source_to_target_publish_flow.py`（demo 业务 flow）

功能：
- 为 demo 发布流程生成表单数据和发布结果。

函数/方法：
- `build_draft_form(payload)`
  - 输入：`PublishPayload`。
  - 输出：草稿表单字典，含 `title/price/category/description/status`。
- `build_publish_result(trace_id, draft_form, submitted)`
  - 输入：追踪 ID、草稿表单、是否已提交。
  - 输出：发布结果字典；`submitted=True` 时返回 `publish_id`，否则返回 `draft_id`。

#### `src/automation_business_scaffold/flows/tiktok_product_flow.py`（TikTok 阶段一采集核心）

功能：
- 规范化 TikTok 商品 URL。
- 通过 HTTP/HTML 或浏览器抓取 TikTok 商品信息。
- 生成本地主图、整页截图，并拼装飞书落表字段。

函数/方法：
- `TikTokProductExtractionError`
  - 功能：统一表示 TikTok 采集失败。
- `extract_tiktok_product_id(value)`
  - 输入：任意可能包含 TikTok 商品 URL 的字符串。
  - 输出：提取出的商品 ID；找不到返回空串。
- `normalize_tiktok_product_url(product_url)`
  - 输入：原始商品 URL。
  - 输出：规范化后的 `https://www.tiktok.com/shop/pdp/{product_id}`；无效 URL 抛 `ValueError`。
- `fetch_tiktok_product_record(product_url, timeout=30, session=None)`
  - 输入：商品 URL、超时、可选 HTTP session。
  - 输出：`TikTokProductRecord`；副作用是发 HTTP 请求；失败时抛 `TikTokProductExtractionError`。
- `fetch_tiktok_product_record_via_browser(product_url, profile_ref=None, timeout_ms=30000, capture_page_screenshot=True)`
  - 输入：商品 URL、浏览器配置、超时、是否截整页图。
  - 输出：带本地主图和截图路径的 `TikTokProductRecord`；副作用是打开浏览器、截图、落本地文件。
- `extract_tiktok_product_from_html(html, source_url, resolved_url="")`
  - 输入：页面 HTML、源 URL、解析后的最终 URL。
  - 输出：`TikTokProductRecord`；依赖页面内 `__MODERN_ROUTER_DATA__`；缺关键字段时抛 `TikTokProductExtractionError`。
- `download_tiktok_product_main_image(product, download_dir=..., timeout=30, session=None)`
  - 输入：`TikTokProductRecord`、下载目录、超时、可选 session。
  - 输出：补齐 `main_image_local_path/file_name/mime_type` 的新 `TikTokProductRecord`；副作用是写本地文件。
- `build_feishu_bitable_fields(product, field_mapping=None)`
  - 输入：`TikTokProductRecord`、可选逻辑字段到列名映射。
  - 输出：飞书字段字典；不会上传附件，只生成 `local_file` 预览结构。
- `build_feishu_bitable_record(product, field_mapping=None)`
  - 输入：同上。
  - 输出：`{"logical_fields": ..., "fields": ...}`。
- `infer_tiktok_product_holiday(title, options=...)`
  - 输入：商品标题、节日选项。
  - 输出：匹配到的节日名，默认回退 `其他`。
- `_wait_for_product_page_ready(page, timeout_ms)`
  - 输入：浏览器 page、超时。
  - 输出：DOM 快照字典；副作用是轮询等待页面稳定。
- `_build_record_from_browser_state(html, dom_snapshot, source_url, resolved_url)`
  - 输入：HTML、DOM 快照、源 URL、最终 URL。
  - 输出：`TikTokProductRecord`；会融合 DOM 与 router 数据。
- `_capture_browser_product_artifacts(page, product, dom_snapshot, capture_page_screenshot, timeout_ms)`
  - 输入：page、商品记录、DOM 快照、截图配置。
  - 输出：补齐本地图片路径后的 `TikTokProductRecord`；副作用是生成主图截图和整页截图文件。
- `_wait_for_main_image_loaded(page, selector, timeout_ms)`
  - 输入：page、主图 selector、超时。
  - 输出：无；超时抛 `TikTokProductExtractionError`。
- `_capture_locator_screenshot(page, target_path, selector)`
  - 输入：page、目标路径、selector。
  - 输出：无；副作用是写截图文件；找不到元素时抛 `TikTokProductExtractionError`。
- `_read_dom_product_snapshot(page)`
  - 输入：page。
  - 输出：标题、价格、卖家、主图 URL 等 DOM 快照字典。
- `_read_main_image_load_state(page, selectors)`
  - 输入：page、候选 selector 列表。
  - 输出：`{"selector","loaded"}`。
- `_extract_blocked_message(text, content_type)`
  - 输入：响应文本、内容类型。
  - 输出：被风控/报错时的消息字符串，否则返回 `None`。
- `_extract_json_script(html, script_id)`
  - 输入：HTML、脚本 ID。
  - 输出：解析后的 JSON `dict`；找不到或非法 JSON 时抛 `TikTokProductExtractionError`。
- `_find_product_component_data(router_data)`
  - 输入：router JSON。
  - 输出：商品组件数据 `dict`；缺失时抛 `TikTokProductExtractionError`。
- `_extract_price_node(promotion_model)`
  - 输入：价格模型字典。
  - 输出：最可用的价格节点字典。
- `_pick_main_image_url(product_model)`
  - 输入：商品模型字典。
  - 输出：主图 URL 字符串。
- `_pick_url_from_media(media)`
  - 输入：媒体字典。
  - 输出：可用 URL 字符串。
- `_parse_int(value)`
  - 输入：任意数值类型。
  - 输出：解析后的非负整数或 `0`。
- `_as_dict(value)`
  - 输入：任意对象。
  - 输出：如果是 `dict` 就原样返回，否则返回空字典。
- `_build_local_file_payload(product)`
  - 输入：`TikTokProductRecord`。
  - 输出：主图 `local_file` 预览结构；没有本地文件时返回空字典。
- `_build_product_page_screenshot_payload(product)`
  - 输入：`TikTokProductRecord`。
  - 输出：整页截图 `local_file` 预览结构。
- `_build_link_payload(url)`
  - 输入：URL 字符串。
  - 输出：飞书链接字段结构 `{"text","link"}` 或空串。
- `_guess_image_suffix(image_url, content_type)`
  - 输入：图片 URL、内容类型。
  - 输出：推断出的扩展名。
- `_normalize_mime_type(content_type, file_suffix)`
  - 输入：响应头内容类型、后缀。
  - 输出：规范化后的 MIME type。
- `_normalize_price_amount(price_value)`
  - 输入：价格文本。
  - 输出：只保留数字的小数字符串。
- `_infer_currency_from_price_text(price_text)`
  - 输入：价格文本。
  - 输出：当前仅识别 `$ -> USD`。
- `_coerce_normalized_url(value)`
  - 输入：任意 URL 文本。
  - 输出：规范化 URL；失败返回空串。
- `_page_goto(page, url)`
  - 输入：page、URL。
  - 输出：无；副作用是页面跳转。
- `_wait_for_domcontentloaded(page)`
  - 输入：page。
  - 输出：无；副作用是等待加载状态。
- `_safe_wait_for_timeout(page, timeout_ms)`
  - 输入：page、等待毫秒数。
  - 输出：无；副作用是 sleep 或 page timeout。
- `_safe_page_content(page)`
  - 输入：page。
  - 输出：HTML 字符串。
- `_UrllibResponse.__init__(url, status_code, headers, content)`
  - 输入：HTTP 元信息和响应体。
  - 输出：构造兼容 `requests.Response` 的轻量对象。
- `_UrllibResponse.raise_for_status()`
  - 输入：实例自身。
  - 输出：无；4xx/5xx 时抛 `TikTokProductExtractionError`。
- `_http_get(url, headers, timeout, allow_redirects=True, session=None)`
  - 输入：URL、头、超时、重定向配置、可选 session。
  - 输出：HTTP 响应对象；副作用是网络请求；可能抛 `TikTokProductExtractionError`。

#### `src/automation_business_scaffold/flows/tiktok_feishu_sync_flow.py`（飞书清洗与同步核心）

功能：
- 读取飞书表。
- 执行链接清洗、去重删除、批量 TikTok 阶段一采集。
- 上传附件并把结果写回飞书。

函数/方法：
- `ExistingRecordIndex.__init__(by_url, by_sku)`
  - 输入：URL 索引、SKU 索引。
  - 输出：索引对象。
- `TableTarget.__init__(client, app_token, table_id, view_id)`
  - 输入：飞书客户端和表标识。
  - 输出：目标表对象。
- `run_tiktok_feishu_single_sync(params)`
  - 输入：单条同步参数。
  - 输出：单条同步结果字典；内部会读取现存记录并决定跳过或创建。
- `run_tiktok_product_link_cleanup(params)`
  - 输入：cleanup 参数。
  - 输出：cleanup 汇总字典；内部依次执行读取、规范化、删重、回写、汇总。
- `run_tiktok_feishu_batch_sync(params)`
  - 输入：批量同步参数。
  - 输出：batch sync 汇总字典；内部依次执行读取、筛选、采集、上传、回写、汇总。
- `load_cleanup_records(params)`
  - 输入：`table_url`、访问令牌等。
  - 输出：`{"records": list}`；副作用是读取飞书表。
- `normalize_cleanup_records(records, params)`
  - 输入：原始飞书记录列表、cleanup 参数。
  - 输出：`{"items": list}`；会生成 `keep/delete_duplicate/invalid_url/skipped_empty` 状态。
- `delete_cleanup_duplicates(items, params)`
  - 输入：cleanup item 列表、运行参数。
  - 输出：`{"deletion_results": list}`；`canary/full_auto` 会真实删记录，`draft` 只预览。
- `write_back_cleanup_records(items, deletion_results, params)`
  - 输入：cleanup item、删除结果、参数。
  - 输出：`{"update_results": list}`；真实模式会调用飞书更新记录。
- `build_cleanup_summary(items, deletion_results, update_results, params)`
  - 输入：cleanup 中间结果。
  - 输出：最终汇总，含 `summary/items/settings`。
- `load_batch_sync_records(params)`
  - 输入：batch 参数。
  - 输出：`{"records": list}`；副作用是读取飞书表。
- `filter_batch_sync_rows(records, params)`
  - 输入：原始记录和 batch 参数。
  - 输出：`{"items": list, "target_rows": list}`；会去重、跳过空行、跳过已完成行、限制最大条数。
- `collect_batch_sync_products(target_rows, params)`
  - 输入：待采集行、batch 参数。
  - 输出：`{"items": list}`；副作用是打开浏览器采集、写本地截图文件。
- `upload_batch_sync_artifacts(items, params)`
  - 输入：采集结果 item 列表、batch 参数。
  - 输出：`{"items": list}`；真实模式会上传本地图片到飞书并把 `local_file` 转成 `file_token`。
- `write_back_batch_sync_rows(items, params)`
  - 输入：已准备好的回写 item 列表、batch 参数。
  - 输出：`{"items": list}`；真实模式会更新飞书记录。
- `build_batch_sync_summary(filtered_items, processed_items, params)`
  - 输入：筛选结果、写回结果、batch 参数。
  - 输出：`{"summary","items","settings"}`。
- `sync_single_tiktok_product_url(product_url, target, field_mapping, existing_index, write_back, step_delay_sec, step_delay_jitter_sec)`
  - 输入：单条商品 URL、表目标、字段映射、已存在索引、是否写回、延迟参数。
  - 输出：单条同步结果；副作用可能包括 HTTP 抓取、附件上传、新增飞书记录。
- `_build_single_settings(params)`
  - 输入：单条同步参数。
  - 输出：标准化后的单条配置字典；缺 `product_url` 或 `table_url` 时抛 `ValueError`。
- `_build_cleanup_settings(params)`
  - 输入：cleanup 参数。
  - 输出：cleanup 配置字典；会确定 `run_mode`、字段名、是否真实落表。
- `_build_batch_settings(params)`
  - 输入：batch 参数。
  - 输出：batch 配置字典；会确定 profile、延迟、截图配置、最大条数等。
- `_build_table_target(table_url, access_token)`
  - 输入：表格 URL、访问令牌。
  - 输出：`TableTarget`。
- `_load_existing_record_index(client, app_token, table_id, view_id, field_mapping)`
  - 输入：飞书客户端、表标识、字段映射。
  - 输出：`ExistingRecordIndex`；副作用是读取飞书表。
- `_prepare_writable_fields(client, app_token, preview_fields)`
  - 输入：飞书客户端、app token、预览字段字典。
  - 输出：可直接回写飞书的字段字典；副作用是上传附件；本地文件不存在时抛 `FileNotFoundError`。
- `_build_cleanup_update_fields(source_url, normalized_url, cleanup_status, normalized_url_field_name, cleanup_status_field_name, cleanup_duplicate_count_field_name, url_field_name, deleted_count)`
  - 输入：链接清洗后的值和字段名。
  - 输出：cleanup 回写字段字典。
- `_build_sync_success_fields(product, field_mapping)`
  - 输入：`TikTokProductRecord`、字段映射。
  - 输出：成功回写字段字典；当前会附加 `sync_status/sync_error/synced_at`。
- `_build_sync_error_fields(error, field_mapping)`
  - 输入：错误字符串、字段映射。
  - 输出：失败回写字段字典。
- `_is_stage1_completed(fields, field_mapping)`
  - 输入：当前记录字段、字段映射。
  - 输出：`bool`；用于跳过已完成记录。
- `_field_has_value(value)`
  - 输入：任意字段值。
  - 输出：是否可视为“有值”的布尔值。
- `_summarize_status_counts(items)`
  - 输入：结果 item 列表。
  - 输出：`{"total": int, "counts": dict}`。
- `_extract_record_id(response)`
  - 输入：飞书 API 响应字典。
  - 输出：尽力提取出的 `record_id`。
- `_effective_single_field_mapping(field_mapping)`
  - 输入：可选覆盖映射。
  - 输出：单条同步的最终字段映射。
- `_effective_sync_field_mapping(field_mapping)`
  - 输入：可选覆盖映射。
  - 输出：批量同步的最终字段映射。
- `_parse_field_mapping(raw_mapping)`
  - 输入：任意对象。
  - 输出：`dict[str, str] | None`；非法类型抛 `ValueError`。
- `_resolve_access_token(params)`
  - 输入：参数字典。
  - 输出：访问令牌字符串；缺失时抛 `ValueError`。
- `_normalize_link_value(value)`
  - 输入：飞书 Url 字段值或普通值。
  - 输出：标准字符串 URL。
- `_build_link_value(value)`
  - 输入：URL 字符串。
  - 输出：飞书链接字段结构。
- `_sleep_with_jitter(delay_sec, jitter_sec)`
  - 输入：固定延迟和抖动。
  - 输出：无；副作用是 sleep。
- `_coerce_bool(value, default)`
  - 输入：任意布尔候选值、默认值。
  - 输出：`bool`；非法文本抛 `ValueError`。
- `_coerce_int(value, default)`
  - 输入：任意整数候选值、默认值。
  - 输出：`int`。
- `_coerce_float(value, default)`
  - 输入：任意浮点候选值、默认值。
  - 输出：`float`。
- `_normalize_run_mode(value)`
  - 输入：运行模式。
  - 输出：标准化模式字符串；`live` 会转成 `canary`。
- `_should_apply_mutations(run_mode)`
  - 输入：运行模式。
  - 输出：是否允许真实写操作。
- `_utc_now_iso()`
  - 输入：无。
  - 输出：当前 UTC ISO 时间字符串。

### 4.4 `mappers/`

#### `src/automation_business_scaffold/mappers/__init__.py`（导出型）

功能：
- 导出 `map_source_item_to_publish_payload`。

函数/方法：
- 无。

#### `src/automation_business_scaffold/mappers/source_to_target_publish.py`（demo mapper）

功能：
- 把 `SourceItem` 转成 `PublishPayload`。

函数/方法：
- `map_source_item_to_publish_payload(source_item, defaults)`
  - 输入：`SourceItem`、`BusinessDefaults`。
  - 输出：`PublishPayload`。

### 4.5 `models/`

#### `src/automation_business_scaffold/models/__init__.py`（导出型）

功能：
- 导出 `PublishPayload`、`SourceItem`、`TikTokProductRecord`。

函数/方法：
- 无。

#### `src/automation_business_scaffold/models/publish_models.py`（demo 模型）

功能：
- 定义 demo 发布流程的数据模型。

函数/方法：
- `SourceItem`
  - 输入：`title/price/category/description/source_url`。
  - 输出：源商品 dataclass。
- `SourceItem.to_dict()`
  - 输入：实例自身。
  - 输出：`dict[str, str | int]`。
- `PublishPayload`
  - 输入：标题、价格、分类、描述、来源 URL、来源系统、目标系统。
  - 输出：发布载荷 dataclass。
- `PublishPayload.to_dict()`
  - 输入：实例自身。
  - 输出：`dict[str, str | int]`。

#### `src/automation_business_scaffold/models/tiktok_product.py`（TikTok 领域模型）

功能：
- 定义 TikTok 商品采集结果模型。

函数/方法：
- `TikTokProductRecord`
  - 输入：源/解析/规范化 URL、商品基础信息、主图/截图本地路径等。
  - 输出：TikTok 商品 dataclass。
- `TikTokProductRecord.to_dict()`
  - 输入：实例自身。
  - 输出：完整商品字典。
- `TikTokProductRecord.from_dict(cls, data)`
  - 输入：任意字典。
  - 输出：`TikTokProductRecord`；缺失字段会用空串或 `0` 回填。

### 4.6 `tasks/`

#### `src/automation_business_scaffold/tasks/__init__.py`（装配型）

功能：
- 声明默认注册 task 列表 `DEFAULT_TASKS`。

函数/方法：
- 无。

#### `src/automation_business_scaffold/tasks/source_to_target_publish_demo.py`（demo task）

功能：
- 在 workflow runtime 中执行 demo 发布流程。

函数/方法：
- `SourceToTargetPublishDemoTask.build_workflow(params)`
  - 输入：任务参数。
  - 输出：`WorkflowSpec`。
- `SourceToTargetPublishDemoTask.execute_workflow_step(context)`
  - 输入：workflow step 上下文。
  - 输出：`FrameworkResult`；根据 `step_id` 返回不同 `data` 和 artifacts。
- `SourceToTargetPublishDemoTask._build_source_item(params, defaults)`
  - 输入：任务参数、默认配置。
  - 输出：`SourceItem`。

#### `src/automation_business_scaffold/tasks/tiktok_product_to_feishu.py`（单条 TikTok 预处理 task）

功能：
- 走 HTTP/HTML 路径抓单个 TikTok 商品，下载主图，并生成飞书字段预览。

函数/方法：
- `TikTokProductToFeishuTask.build_workflow(params)`
  - 输入：任务参数。
  - 输出：`WorkflowSpec`。
- `TikTokProductToFeishuTask.execute_workflow_step(context)`
  - 输入：workflow step 上下文。
  - 输出：`FrameworkResult`；按 step 分别产出 `tiktok_product`、`tiktok_product_with_image`、`feishu_record`。

#### `src/automation_business_scaffold/tasks/tiktok_product_link_cleanup.py`（链接清洗 task）

功能：
- 驱动 cleanup flow：读取飞书、规范化 URL、删重、回写、汇总。

函数/方法：
- `TikTokProductLinkCleanupTask.build_workflow(params)`
  - 输入：任务参数。
  - 输出：`WorkflowSpec`。
- `TikTokProductLinkCleanupTask.execute_workflow_step(context)`
  - 输入：workflow step 上下文。
  - 输出：`FrameworkResult`；按 step 产出 `records/items/deletion_results/update_results/summary`。

#### `src/automation_business_scaffold/tasks/tiktok_feishu_batch_sync.py`（批量阶段一 task）

功能：
- 驱动批量阶段一采集：读表、筛选、浏览器抓取、上传附件、回写结果。

函数/方法：
- `TikTokFeishuBatchSyncTask.build_workflow(params)`
  - 输入：任务参数。
  - 输出：`WorkflowSpec`。
- `TikTokFeishuBatchSyncTask.execute_workflow_step(context)`
  - 输入：workflow step 上下文。
  - 输出：`FrameworkResult`；按 step 产出 `records/target_rows/items/summary`。

#### `src/automation_business_scaffold/tasks/tiktok_feishu_single_sync.py`（单条同步 task）

功能：
- 读取飞书现存记录后，把单个 URL 抓取并新增为新记录，遇到 URL 或 SKU 重复则跳过。

函数/方法：
- `TikTokFeishuSingleSyncTask.build_workflow(params)`
  - 输入：任务参数。
  - 输出：`WorkflowSpec`。
- `TikTokFeishuSingleSyncTask.execute_workflow_step(context)`
  - 输入：workflow step 上下文。
  - 输出：`FrameworkResult`；`data` 中含 `status/record_id/product_url/product_id/fields/...`。

### 4.7 `validators/`

#### `src/automation_business_scaffold/validators/__init__.py`（导出型）

功能：
- 导出发布载荷校验与 TikTok 商品校验函数。

函数/方法：
- 无。

#### `src/automation_business_scaffold/validators/publish_payload.py`（demo 校验）

功能：
- 校验 demo 发布载荷是否完整。

函数/方法：
- `validate_publish_payload(payload)`
  - 输入：`PublishPayload`。
  - 输出：无；标题为空或价格小于等于 0 时抛 `ValueError`。

#### `src/automation_business_scaffold/validators/tiktok_product.py`（TikTok 校验）

功能：
- 校验 TikTok URL 和商品记录。

函数/方法：
- `validate_tiktok_product_url(product_url)`
  - 输入：URL 字符串。
  - 输出：无；无效 URL 抛 `ValueError`。
- `validate_tiktok_product_record(product, require_local_image=False)`
  - 输入：`TikTokProductRecord`、是否要求本地主图文件存在。
  - 输出：无；关键字段缺失或文件不存在时抛 `ValueError`。

### 4.8 `workflows/`

#### `src/automation_business_scaffold/workflows/__init__.py`（导出型）

功能：
- 导出所有 workflow builder。

函数/方法：
- 无。

#### `src/automation_business_scaffold/workflows/source_to_target_publish_v1.py`（workflow 装配）

功能：
- 声明 demo 发布流程的 step 顺序、前后置条件和 effect。

函数/方法：
- `build_source_to_target_publish_workflow(run_mode="draft", include_submit=False)`
  - 输入：运行模式、是否包含提交步骤。
  - 输出：`WorkflowSpec`。

#### `src/automation_business_scaffold/workflows/tiktok_product_to_feishu_v1.py`（workflow 装配）

功能：
- 声明单条 TikTok 预处理 workflow。

函数/方法：
- `build_tiktok_product_to_feishu_workflow(run_mode="draft")`
  - 输入：运行模式。
  - 输出：`WorkflowSpec`。

#### `src/automation_business_scaffold/workflows/tiktok_product_link_cleanup_v1.py`（workflow 装配）

功能：
- 声明链接清洗 workflow。

函数/方法：
- `build_tiktok_product_link_cleanup_workflow(run_mode="draft")`
  - 输入：运行模式。
  - 输出：`WorkflowSpec`。

#### `src/automation_business_scaffold/workflows/tiktok_feishu_batch_sync_v1.py`（workflow 装配）

功能：
- 声明批量阶段一同步 workflow。

函数/方法：
- `build_tiktok_feishu_batch_sync_workflow(run_mode="draft")`
  - 输入：运行模式。
  - 输出：`WorkflowSpec`。

#### `src/automation_business_scaffold/workflows/tiktok_feishu_single_sync_v1.py`（workflow 装配）

功能：
- 声明单条同步 workflow。

函数/方法：
- `build_tiktok_feishu_single_sync_workflow(run_mode="draft")`
  - 输入：运行模式。
  - 输出：`WorkflowSpec`。

## 5. 审计结论建议

如果要把当前代码真正收敛到 2026-04-01 的需求文档，优先级建议如下：

1. 先收敛 `tiktok_feishu_sync_flow.py` 默认字段映射，只保留真实表的现有字段。
2. 把阶段一成功回写从 `商品主图/商品页截图/采集状态/采集时间` 改成 `图片/前台截图/记录日期` 等现表字段。
3. 把 cleanup 逻辑改成只回写 `产品链接`，不要默认写 `标准产品链接/链接整理状态/删除重复数`。
4. 明确是否保留 `tiktok_product_to_feishu`、`tiktok_feishu_single_sync` 里的 HTTP/HTML 路径；如果保留，应明确标注为调试工具而不是正式方案。
5. 第二阶段另起 flow/task/workflow，实现 FastMoss 采集和附件落表。
6. 在字段映射收敛后重写测试，确保测试断言的是当前真实表字段，而不是旧字段名。
