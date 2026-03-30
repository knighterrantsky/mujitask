# Mac mini 本机部署与更新

这份说明面向“OpenClaw 在客户 Mac mini 上直接调用本机命令”的交付方式。

## 推荐形态

推荐把这个工程部署为一套本机 CLI 工具，而不是先做成常驻 HTTP 服务。

优点：

- 和现有 OpenClaw skill 的调用方式一致
- 不需要额外维护 daemon 进程
- 返回结果天然是同步的，OpenClaw 更容易消费
- `token` 可以只保留在客户本机

## 安装前提

客户 Mac mini 需要有：

- `git`
- `uv`
- Python `3.11`

## 安装方式

仓库里已经放了一个安装脚本模板：

- [install_local_cli.sh](/Users/happyzhao/Work/mujitask/examples/macmini/install_local_cli.sh)

执行方式：

```bash
bash examples/macmini/install_local_cli.sh '<repo_url>' '~/apps/mujitask' '<release-tag>'
```

如果先不锁 tag，也可以只传前两个参数：

```bash
bash examples/macmini/install_local_cli.sh '<repo_url>' '~/apps/mujitask'
```

安装完成后，建议先验证：

```bash
cd ~/apps/mujitask
uv run automation-business-scaffold-run list-tasks
```

## 更新方式

仓库里也放了更新脚本模板：

- [update_local_cli.sh](/Users/happyzhao/Work/mujitask/examples/macmini/update_local_cli.sh)

更新到最新分支：

```bash
bash examples/macmini/update_local_cli.sh '~/apps/mujitask'
```

更新到指定版本：

```bash
bash examples/macmini/update_local_cli.sh '~/apps/mujitask' '<release-tag>'
```

建议正式交付时优先用 tag 更新，不要直接让客户长期跟主分支。

## 配置建议

推荐把配置拆成两层：

1. 机密信息：环境变量
2. 业务参数：本地 JSON 配置文件

示例配置文件已经提供：

- [customer.local.example.json](/Users/happyzhao/Work/mujitask/examples/macmini/customer.local.example.json)

推荐约定：

- `FEISHU_ACCESS_TOKEN`：放在客户本机环境变量
- `table_url`、字段映射、批量大小：放在本地 JSON 配置

不建议：

- 把飞书 token 写进 skill markdown
- 把 token 写进 git 仓库
- 每次通过自然语言把 token 传给 OpenClaw

## OpenClaw 推荐调用方式

OpenClaw skill 推荐直接执行本机命令：

```bash
cd ~/apps/mujitask
uv run automation-business-scaffold-run run \
  --task tiktok_product_to_feishu \
  --params-json '{"product_url":"https://www.tiktok.com/shop/pdp/1729440407432826887"}'
```

命令返回：

- 成功时退出码 `0`
- 失败时退出码 `1`
- stdout 返回结构化 JSON

这比让 OpenClaw 走 HTTP 服务更简单，也更适合当前这个客户。

## 调试文件位置

CLI 模式下，调试数据默认落在：

- `runtime/cli_runs/`
- `runtime/cli_runs/steps/`
- `runtime/cli_runs/signals/`
- `runtime/artifacts/<run_id>/`

如果以后切换到 agent 模式，再用 `runtime/agent_runs/` 即可。
