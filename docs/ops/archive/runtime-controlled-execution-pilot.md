# Runtime 受控执行 Pilot 说明

> 状态: Ops archive。本文保留历史 Pilot/runbook；当前 Runtime 和 artifact 设计以 [../../arch/README.md](../../arch/README.md) 为准。

更新时间：`2026-04-12`

## 1. 目的

本文说明当前仓库内已经落地的受控执行 Pilot 能力：

- `feishu_single_row_update` 支持受控执行模式
- 支持 `submit / status / result / execute_next / daemon_once / daemon_loop`
- 支持本地对象目录产物落盘
- 支持 `artifact_object` 本地索引

如果要按“可交付阶段”执行部署、验收和回退，请继续参考
[../runtime-acceptance-and-rollback.md](../runtime-acceptance-and-rollback.md)。

当前实现仍然是 **兼容式升级**：

- 旧的 `feishu_single_row_update` 调用方式仍然保留
- 不带受控参数时，仍走旧逻辑
- 带 `control_action` 或 `execution_control_*` 参数时，切到新链路

## 2. 当前入口

当前仍然只使用原任务名：

```text
feishu_single_row_update
```

不会新增新的公开任务名。

## 3. 受控执行参数

### 3.1 基础控制参数

- `control_action`
- `execution_control_enabled`
- `execution_control_db_url`
- `execution_control_artifact_root`
- `execution_control_artifact_bucket`
- `execution_requested_by`
- `execution_worker_id`
- `execution_lease_seconds`
- `execution_heartbeat_interval_seconds`
- `execution_poll_interval_seconds`
- `execution_wait_timeout_seconds`

### 3.2 daemon 额外参数

- `execution_control_stop_when_idle`
- `execution_control_max_iterations`
- `execution_control_max_idle_cycles`

## 4. 当前支持的 control_action

### 4.1 `submit`

只入队，不直接执行。

适合：

- OpenClaw 先提交请求
- daemon 后续拉取执行

### 4.2 `status`

读取当前请求的控制面状态。

至少支持：

- `request_id`
- `execution_id`

### 4.3 `result`

当前与 `status` 共用同一读取逻辑，但语义上表示读取结果。

### 4.4 `execute_next`

执行单次队列消费：

- 取当前可执行的最早排队任务
- 获取资源租约
- 执行业务逻辑
- 回写状态
- 生成本地产物与索引

### 4.5 `daemon_once`

语义上等价于“执行器单次轮询”。

与 `execute_next` 的区别是：

- 返回里额外强调 daemon 处理结果
- 适合后续独立执行器脚本直接调用

### 4.6 `daemon_loop`

持续轮询执行，直到满足退出条件。

典型退出方式：

- `execution_control_stop_when_idle=true`
- `execution_control_max_idle_cycles=1`

或：

- `execution_control_max_iterations=<n>`

### 4.7 `run`

同步兼容模式：

- 先 submit
- 再尝试 claim
- 再本进程执行
- 直到成功、失败或超时

这主要用于：

- 本地联调
- 尚未切到独立 daemon 的兼容阶段

## 5. 资源锁规则

当前资源编码规则：

- 如果没有 `profile_ref`，默认使用 `browser.tiktok.main`
- 如果有 `profile_ref`，编码为 `browser.tiktok.<profile_ref>`

这意味着：

- 同一 `profile_ref` 的任务会排队
- 不同 `profile_ref` 的任务可以独立并行演进

## 6. 本地产物目录

当前受控执行 Pilot 先使用本地对象目录模拟对象存储。

默认根目录：

```text
runtime/execution_control/object_store
```

单次执行的核心文件：

```text
runs/<run_id>/run.json
runs/<run_id>/steps.json
runs/<run_id>/signals.json
runs/<run_id>/stdout.log
runs/<run_id>/artifacts/execute_controlled_single_row_update/state.json
```

## 7. artifact_object 当前索引内容

当前已索引的 `kind`：

- `run_json`
- `steps_json`
- `signals_json`
- `stdout_log`
- `state_json`

当前字段：

- `artifact_id`
- `run_id`
- `step_id`
- `kind`
- `bucket`
- `object_key`
- `etag`
- `size`
- `content_type`
- `source_path`
- `created_at`

说明：

- 当前 `bucket` 是本地兼容值，默认 `local-runtime`
- `object_key` 已按后续对象存储路径风格组织
- 后续接 MinIO 时优先复用这套 `object_key`

## 8. 返回字段

执行完成后，当前返回会带上这些兼容字段：

- `artifact_count`
- `artifacts`
- `artifact_uri_prefix`
- `run_object_key`
- `steps_object_key`
- `signals_object_key`
- `stdout_object_key`
- `artifacts_dir`

这组字段的目的，是为后续 `__OPENCLAW_RESULT__ v2` 和 MinIO 接入提前预留稳定命名。

## 9. 当前状态

当前实现已完成：

- 排队
- 租约
- 心跳
- 过期回收
- daemon 单次执行
- daemon 循环执行
- 本地产物落盘
- artifact 索引

当前尚未完成：

- 真正的 MinIO 上传
- 独立 CLI 子命令封装
- OpenClaw 侧显式 `submit/status/result` 接线
- 对所有业务任务的全面推广

## 10. 推荐下一步

建议按下面顺序继续：

1. 把这套控制动作接到 OpenClaw skill 或独立脚本
2. 新增 `artifact_sync` 抽象层
3. 把本地对象目录上传到 MinIO
4. 用 `artifact_object` 保存正式对象索引

## 11. 当前 Skill 接入口径

本文件记录的是早期 Pilot。当前 `skills/mujitask-tiktok-feishu-sync/run_skill_step.py` 已经收敛为：

- `refresh-current-competitor-table-submit`：提交顶层刷新 workflow
- `product-url-complete-submit`：提交选品表单商品补全 workflow
- `competitor-row-by-url-submit`：提交竞品表 URL 定位并补全 workflow
- `keyword-search-submit`：提交顶层关键词搜索 workflow
- `influencer-pool-sync-submit`：提交达人池同步 workflow

旧 direct run、status/result、worker、cleanup、seed 等兼容入口已经移除，正式异步路径统一由顶层 workflow 提交后交给 `executor_daemon` 推进。
