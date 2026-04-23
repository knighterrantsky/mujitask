# OpenClaw Skills

更新时间：`2026-04-14`

状态：开发/集成说明。本文描述 OpenClaw skill 的集成边界、入口脚本和调试口径；客户需求以 `docs/business` 为准，系统架构以 `docs/arch` 为准。

本文件描述当前 OpenClaw skill 的真实职责边界。当前 skill 已不再承担主业务编排，默认边界已经收敛为：

- skill 负责识别用户意图
- skill 负责提取少量参数
- skill 负责提交顶层任务并返回 `request_id`
- `executor_daemon / browser_runloop / outbox_dispatcher` 负责后续执行和通知

## 1. 当前 skill 定位

当前正式 skill 名称：

- `mujitask-tiktok-feishu-sync`

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
- `TABLE_URL`、`FEISHU_ACCESS_TOKEN`、`BROWSER_PROFILE_REF`、`FASTMOSS_PHONE`、`FASTMOSS_PASSWORD` 固定来自 `skill.local.env`
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
- `run_keyword_search_step.sh`
- `run_skill_step.py`
- `lightweight_submit.py`
- `openclaw_result.py`

### 6.2 仅用于人工排障的脚本

下面这些脚本仍保留，但仅用于人工排障，不再作为默认业务入口：

- `run_cleanup_step.sh`
- `run_pending_rows_step.sh`
- `run_fastmoss_login_check_step.sh`

## 7. 与运行时的关系

当前 skill 与后台运行时的关系如下：

1. skill 提交顶层 `task_request`
2. `executor_daemon` 推进顶层阶段
3. `browser_runloop` 消费浏览器叶子任务
4. `outbox_dispatcher` 发送最终通知

所以当前 OpenClaw skill 的职责是“入口层”，不是“编排层”。

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
