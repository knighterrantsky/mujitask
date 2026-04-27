# OpenClaw / Agent Skills

更新时间：`2026-04-24`

状态：开发/集成说明。本文描述 OpenClaw skill 的集成边界、入口脚本和调试口径；客户需求以 `docs/business` 为准，系统架构以 `docs/arch` 为准。

本文件描述当前 OpenClaw 兼容 skill 的真实职责边界。按项目结构契约，`skills/{skill_code}` 是仓库内的 agent skill bundle 源；部署时会复制到目标 agent workspace/skills 目录，并生成或保留 `skill.local.env`。

当前 skill 已不再承担主业务编排，默认边界已经收敛为：

- skill 负责识别用户意图
- skill 负责提取少量参数
- skill 负责提交顶层任务并返回 `request_id`
- `executor_daemon / browser_runloop / outbox_dispatcher` 负责后续执行和通知

## 1. 当前 skill 定位

当前正式 skill 名称：

- `mujitask-tiktok-feishu-sync`

它是一个 agent artifact，而不是 runtime worker。后续可以有多个业务 skill bundle，例如:

- `mujitask-tiktok-feishu-sync`
- `mujitask-tiktok-selection-analysis`
- `mujitask-creator-discovery`

每个 bundle 都应独立描述触发条件、参数提取、提交入口和首条回执契约；共同复用后台 Runtime DB、executor、worker、outbox 和项目安装配置。

对外暴露两类业务语义：

1. 补全当前 TikTok 竞品表
2. 按关键词搜索 TikTok/FastMoss 商品并写入当前飞书表

## 2. skill 负责什么

当前 skill 只负责：

1. 根据自然语言选择顶层入口
2. 从用户输入中提取必要参数
3. 调用顶层 submit 入口
4. 返回首条受理回执

skill 不再负责：

1. 主流程编排
2. 浏览器任务循环
3. 详情抓取重试
4. 最终通知发送
5. 批次内部的步骤衔接

## 3. 当前正式入口

### 3.1 竞品表刷新入口

用户语义示例：

- `帮我补全 TikTok 竞品表`
- `更新当前竞品表`
- `同步当前飞书竞品表`

对应入口：

```bash
bash skills/mujitask-tiktok-feishu-sync/run_refresh_current_competitor_table_step.sh
```

对应顶层任务：

- `refresh_current_competitor_table`

### 3.2 关键词搜索入口

用户语义示例：

- `帮我抓取关键字 Halloween decoration 销量超过 200 的竞品数据`
- `搜索 Easter Basket Stuffers 的 TK 竞品并写入飞书`

对应入口：

```bash
bash skills/mujitask-tiktok-feishu-sync/run_keyword_search_step.sh \
  --search-keyword "<keyword>" \
  --sales-7d-threshold <number>
```

对应顶层任务：

- `search_keyword_competitor_products`

## 4. 参数提取规则

当前只从用户输入中提取：

- `关键词`
- `7日销量阈值`

规则：

- 如果用户没有明确给出 `7日销量阈值`，默认使用 `200`
- `MUJITASK_FEISHU_BASE_URL`、`MUJITASK_FEISHU_TK_*_TABLE_ID`、`MUJITASK_FEISHU_TK_*_VIEW_ID`、`MUJITASK_FEISHU_ACCESS_TOKEN`、`BROWSER_PROFILE_REF`、`FASTMOSS_PHONE`、`FASTMOSS_PASSWORD` 固定来自 `skill.local.env`
- Runtime DB / MinIO 默认配置来自项目自动加载的 `scripts/execution_control/executor.local.env`
- 不在对话中向用户索取这些部署级配置

## 5. 当前输出契约

这两个正式入口都属于：

- 同步提交
- 异步执行

当前固定契约：

1. skill 必须等待脚本返回 `__OPENCLAW_RESULT__`
2. 首条回执必须显式输出 `request_id`
3. 首条回执不等待浏览器执行完成
4. 最终汇总由后台通知再次发送到飞书

禁止行为：

- 后台启动后只短轮询一次就提前回复
- 输出“还没吐出 request_id”“我先让它继续跑”这类过渡话术
- 在 skill 中手工串旧 leaf steps；skill 只提交顶层 task

## 6. 当前实现边界

### 6.1 skill 内保留的文件

当前 skill 包中应至少包含：

- `SKILL.md`
- `skill.local.env`
- `run_refresh_current_competitor_table_step.sh`
- `run_competitor_row_by_url_step.sh`
- `run_product_url_complete_step.sh`
- `run_keyword_search_step.sh`
- `run_influencer_pool_sync_step.sh`
- `run_skill_step.py`
- `lightweight_submit.py`
- `openclaw_result.py`

这些文件是部署产物源。部署脚本会把它们复制到 `MUJITASK_SKILLS_DIR/mujitask-tiktok-feishu-sync` 或等价 agent skills 目录。

`skill.local.env.example` 是配置模板；`skill.local.env` 是目标 agent workspace 中的本机配置。新增业务 skill 时，不要把生产密钥写进仓库内模板。

### 6.2 已移除的旧 wrapper

skill bundle 不再保留旧 leaf step / 人工排障 wrapper。OpenClaw 只通过顶层 task 提交入口创建 `task_request`；排障应查看 runtime task / job / outbox 状态。

## 7. 与运行时的关系

当前 skill 与后台运行时的关系如下：

1. skill 提交顶层 `task_request`
2. `executor_daemon` 推进顶层阶段
3. `browser_runloop` 消费浏览器叶子任务
4. `outbox_dispatcher` 发送最终通知

所以当前 OpenClaw skill 的职责是“入口层”，不是“编排层”。

Agent workspace 与项目安装目录的边界:

| 位置 | 作用 |
| --- | --- |
| 仓库 `skills/{skill_code}` | skill bundle 源代码和模板 |
| 目标 `MUJITASK_SKILLS_DIR/{skill_code}` | agent 实际读取的 skill bundle |
| 目标 `skill.local.env` | agent skill 的固定输入和本机上下文 |
| 项目安装目录 `executor.local.env` | Runtime DB、对象存储、通知、浏览器和第三方账号等后台运行配置 |

## 8. 当前推荐排障顺序

当用户说“没有回执”或“结果不对”时，排查顺序推荐为：

1. `~/.openclaw/logs/gateway.log`
2. skill 当前 session 日志
3. `task_request`
4. `task_execution`
5. `notification_outbox`
6. `runtime/phase1_daemons`
7. `runtime/execution_control/object_store`

## 9. 当前适用说明

本文件描述的是当前已经落地的 skill 边界，而不是旧版“skill 内部做主编排”的设计目标。

如果后续新增达人链、视频链，也应复用同样的边界：

- skill 负责入口
- executor 负责编排
- browser runloop 负责资源串行
- outbox dispatcher 负责最终通知
