# Mac mini 本机部署与更新

这份说明面向“OpenClaw 在客户 Mac mini 上直接调用本机命令”的交付方式。

## 推荐形态

推荐把这个工程部署为一套本机 CLI 工具，而不是先做成常驻 HTTP 服务。

优点：

- 和 OpenClaw skill 的调用方式一致
- 不需要额外维护 daemon 进程
- 返回结果天然是同步 JSON，OpenClaw 更容易消费
- `FEISHU_ACCESS_TOKEN` 可以只保留在客户本机环境变量

## 环境要求

客户 Mac mini 需要具备：

- `git`
- `uv`
- Python `3.11`
- 可以访问 TikTok 和飞书的网络环境

推荐安装目录：

- `$HOME/apps/mujitask`

常见问题排查见：

- [macmini-deployment-troubleshooting.md](/Users/happyzhao/Work/mujitask/docs/business/macmini-deployment-troubleshooting.md)

## 安装方式

业务仓库：

- `https://github.com/knighterrantsky/mujitask.git`

framework 仓库：

- `https://github.com/knighterrantsky/automation-framework.git`

仓库内已经提供安装脚本模板：

- [install_local_cli.sh](/Users/happyzhao/Work/mujitask/examples/macmini/install_local_cli.sh)

直接在仓库工作目录执行：

```bash
bash examples/macmini/install_local_cli.sh \
  'https://github.com/knighterrantsky/mujitask.git' \
  "$HOME/apps/mujitask" \
  '<release-tag>'
```

也可以让 OpenClaw 直接执行 GitHub Raw 安装脚本：

```bash
curl -fsSL \
  'https://raw.githubusercontent.com/knighterrantsky/mujitask/<release-tag>/examples/macmini/install_local_cli.sh' \
  | bash -s -- \
    'https://github.com/knighterrantsky/mujitask.git' \
    "$HOME/apps/mujitask" \
    '<release-tag>'
```

脚本支持重复执行：

- 首次执行时 clone 到目标目录
- 目录已存在时自动 `fetch` / `pull`
- 安装完成后自动补齐 Chromium 浏览器依赖

标准安装路径已经默认从 GitHub 拉取 pinned 的 framework 依赖，不再需要把 `FRAMEWORK_REPO_URL` 当成主路径。

如果客户环境后续需要切到别的镜像源，仍然可以兼容覆盖：

```bash
FRAMEWORK_REPO_URL='https://github.com/knighterrantsky/automation-framework.git' \
FRAMEWORK_GIT_REF='<framework-tag-or-commit>' \
bash examples/macmini/install_local_cli.sh \
  'https://github.com/knighterrantsky/mujitask.git' \
  "$HOME/apps/mujitask" \
  '<release-tag>'
```

安装完成后建议先验证：

```bash
cd "$HOME/apps/mujitask"
.venv/bin/automation-business-scaffold-run list-tasks
```

预期至少能看到：

- `tiktok_product_to_feishu`
- `tiktok_product_link_cleanup`
- `tiktok_feishu_batch_sync`

## 更新方式

仓库内已经提供更新脚本模板：

- [update_local_cli.sh](/Users/happyzhao/Work/mujitask/examples/macmini/update_local_cli.sh)

更新到指定版本：

```bash
bash examples/macmini/update_local_cli.sh "$HOME/apps/mujitask" '<release-tag>'
```

或者通过 GitHub Raw 调用：

```bash
curl -fsSL \
  'https://raw.githubusercontent.com/knighterrantsky/mujitask/<release-tag>/examples/macmini/update_local_cli.sh' \
  | bash -s -- \
    "$HOME/apps/mujitask" \
    '<release-tag>'
```

建议正式交付时优先使用固定 tag，不要让客户长期跟随主分支。

## 配置建议

推荐把配置拆成两层：

1. 机密信息：环境变量
2. 业务参数：OpenClaw 调用参数

示例配置文件保留在：

- [customer.local.example.json](/Users/happyzhao/Work/mujitask/examples/macmini/customer.local.example.json)

推荐约定：

- `FEISHU_ACCESS_TOKEN`：放在客户本机环境变量
- `table_url`：作为 OpenClaw 调用参数显式传入
- `field_mapping`：如需覆盖列名时显式传入

注意：

- 当前 CLI 不会自动读取 `customer.local.example.json`
- 这个 JSON 文件更适合作为客户本地维护样例，不是正式调用入口
- 不要把真实 token 写进仓库或 skill markdown

标准环境变量示例：

```bash
export FEISHU_ACCESS_TOKEN='your-feishu-access-token'
```

## OpenClaw 正式调用入口

当前对 OpenClaw 正式交付两个 task：

- `tiktok_product_link_cleanup`
- `tiktok_feishu_batch_sync`

`tiktok_product_to_feishu` 保留为底层调试能力，只负责抓取并组装飞书字段，不作为正式 skill 主入口。

### 链接整理 / 去重

读取飞书表中的 `产品链接`，执行“格式化 -> 回写 `产品链接` -> 删除重复整行”。

```bash
cd "$HOME/apps/mujitask"
.venv/bin/automation-business-scaffold-run run \
  --task tiktok_product_link_cleanup \
  --params-json '{
    "table_url": "https://my.feishu.cn/base/appXXX?table=tblXXX",
    "access_token_env": "FEISHU_ACCESS_TOKEN",
    "url_field_name": "产品链接",
    "run_mode": "canary"
  }'
```

cleanup 任务支持的关键参数：

- `table_url`
- `access_token_env`
- `url_field_name`
- `run_mode`
- `trace_id`

### 阶段一表驱动补录

读取飞书表中现有记录；如果阶段一字段存在空缺，就逐条执行“抓取 -> 上传附件 -> 写回当前行”。

```bash
cd "$HOME/apps/mujitask"
.venv/bin/automation-business-scaffold-run run \
  --task tiktok_feishu_batch_sync \
  --params-json '{
    "table_url": "https://my.feishu.cn/base/appXXX?table=tblXXX",
    "access_token_env": "FEISHU_ACCESS_TOKEN",
    "url_field_name": "产品链接",
    "profile_ref": "local-chrome",
    "run_mode": "canary"
  }'
```

批量任务默认节流参数：

- `step_delay_sec = 1.0`
- `step_delay_jitter_sec = 1.0`
- `record_delay_sec = 2.0`
- `record_delay_jitter_sec = 2.0`
- `pause_every = 5`
- `pause_sec = 8.0`
- `retry_attempts = 3`
- `retry_delay_sec = 3.0`

节流位置：

- 单条流程里，抓取后到下载前
- 下载后到飞书附件上传前
- 上传后到新建记录前
- 批量模式里，每条记录之间

## 返回字段

CLI 顶层固定返回这些字段：

- `status`
- `run_id`
- `result`
- `error`
- `run_file`
- `steps_file`
- `signals_file`
- `artifacts_dir`

OpenClaw 读取建议：

- 先看顶层 `status`
- 失败时读取顶层 `error`
- 成功时继续读取 `result.data`

批量任务的 `result.data` 重点字段：

- `summary`
- `items`
- `failed_items`
- `settings`

其中 `summary` 至少包含：

- `total`
- `updated`
- `skipped_completed`
- `failed`

## 去重与中间数据

正式链路建议固定为：

1. 先跑 `tiktok_product_link_cleanup`
2. 再跑 `tiktok_feishu_batch_sync`
3. 阶段一只补缺失字段
4. 只要发生写回，就同步刷新 `记录日期`

CLI 调试数据默认落在：

- `runtime/cli_runs/`
- `runtime/cli_runs/steps/`
- `runtime/cli_runs/signals/`
- `runtime/artifacts/<run_id>/`

排障顺序建议：

1. 看 `run_file` 里的顶层状态和错误
2. 看 `steps_file` 确认执行到了哪一步
3. 看 `signals_file` 判断是业务失败还是运行时拦截
4. 看 `artifacts_dir` 里的 `state_dump`，核对中间字段和落盘图片

如果是部署期常见报错，优先参考：

- [macmini-deployment-troubleshooting.md](/Users/happyzhao/Work/mujitask/docs/business/macmini-deployment-troubleshooting.md)
