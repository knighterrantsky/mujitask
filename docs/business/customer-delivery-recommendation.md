# 客户交付建议与部署要求

这份文档用于和客户对齐当前 TikTok → 飞书采集服务的交付方式、部署要求、调用方式和后续扩展建议。

## 1. 推荐交付形态

当前推荐采用：

- 客户 Mac mini 本机部署
- OpenClaw 通过 skill 直接调用本机命令
- 命令同步执行并返回 JSON 结果

不建议当前阶段优先采用：

- 先部署成常驻 HTTP agent 服务
- 把飞书 token 直接写进 skill 文本
- 每新增一个业务就复制一套独立脚本目录

推荐原因：

- 和客户现有 OpenClaw + skill 的使用方式一致
- 部署更轻，维护更简单
- 返回结果清晰，OpenClaw 容易判断成功或失败
- 所有后续自动化业务都可以继续放在同一个工程里演进

## 2. 客户 Mac mini 部署要求

客户机器需要具备：

- `git`
- `uv`
- Python `3.11`
- 可以访问 TikTok 和飞书的网络环境

推荐部署目录：

- `~/apps/mujitask`

推荐安装方式：

```bash
bash examples/macmini/install_local_cli.sh '<repo_url>' '~/apps/mujitask' '<release-tag>'
```

推荐更新方式：

```bash
bash examples/macmini/update_local_cli.sh '~/apps/mujitask' '<release-tag>'
```

更详细的本机部署说明见：

- [macmini-deployment.md](/Users/happyzhao/Work/mujitask/docs/business/macmini-deployment.md#L1)

## 3. OpenClaw 调用方式建议

推荐由 OpenClaw skill 直接执行本机命令：

```bash
cd ~/apps/mujitask
uv run automation-business-scaffold-run run \
  --task tiktok_product_to_feishu \
  --params-json '{"product_url":"https://www.tiktok.com/shop/pdp/1729440407432826887"}'
```

当前 CLI 入口位于：

- [cli.py](/Users/happyzhao/Work/mujitask/src/automation_business_scaffold/cli.py#L34)

命令返回约定：

- 成功时退出码 `0`
- 失败时退出码 `1`
- stdout 返回结构化 JSON

这样 OpenClaw 可以直接判断：

- 退出码是否为 `0`
- JSON 里的 `status` 是否为 `success`
- 失败时读取 `error`
- 成功时读取 `result.data`

## 4. 返回模式建议

当前客户场景推荐使用同步调用。

原因：

- 当前主要是单条 URL 或小批量处理
- OpenClaw 更适合同步拿结果
- 不需要额外设计轮询和回调机制

同步模式下，建议固定返回这些字段：

- `status`
- `run_id`
- `result`
- `error`
- `run_file`
- `steps_file`
- `signals_file`
- `artifacts_dir`

如果以后任务变成大批量长耗时，再考虑切换成 HTTP 异步模式。

## 5. 配置提供方式建议

推荐分成两层：

1. 机密配置：环境变量
2. 业务配置：本地 JSON 文件

建议约定：

- `FEISHU_ACCESS_TOKEN`：通过环境变量提供
- `table_url`、字段映射、批量大小：通过本地 JSON 配置提供

示例配置文件：

- [customer.local.example.json](/Users/happyzhao/Work/mujitask/examples/macmini/customer.local.example.json#L1)

不建议：

- 把 token 写进 skill markdown
- 把 token 提交到 git
- 每次通过对话把 token 传给 OpenClaw

## 6. 当前能力边界

当前已完成：

- 单条 TikTok 商品链接抓取
- 提取主图、价格、销量、店铺名称
- 下载主图到本地文件
- 生成飞书可消费的数据结构
- 支持本地 CLI 调用

当前单条 task：

- `tiktok_product_to_feishu`

代码位置：

- [tiktok_product_to_feishu.py](/Users/happyzhao/Work/mujitask/src/automation_business_scaffold/tasks/tiktok_product_to_feishu.py#L20)

说明文档：

- [tiktok-product-feishu-flow.md](/Users/happyzhao/Work/mujitask/docs/business/tiktok-product-feishu-flow.md#L1)

## 7. 批量 URL 支持建议

当前单条 task 只支持一个 `product_url`，还没有正式实现“批量读飞书表格并回写”的成品 task。

下一步建议新增：

- `tiktok_feishu_batch_sync`

推荐让它支持两种输入：

- 从飞书多维表格读取待处理记录
- 直接传一组 `product_urls`

推荐返回：

- 整体状态
- 成功数 / 失败数
- 每条记录的处理结果
- 每条失败的错误信息

协议草案见：

- [tiktok-feishu-batch-contract.md](/Users/happyzhao/Work/mujitask/docs/business/tiktok-feishu-batch-contract.md#L1)

## 8. 后续扩展建议

这个工程建议继续作为统一自动化工程使用。

后续如果还有别的自动化业务，建议继续在同一个工程里新增：

- `tasks/`
- `workflows/`
- `flows/`
- `extend_script/`

而 OpenClaw 侧只需要新增对应 skill 描述和调用入口，不要把业务实现散落到多个独立脚本仓库里。

推荐模式是：

1. 工程内新增一个可执行 task
2. 为 task 暴露稳定 CLI
3. OpenClaw skill 调这个 CLI
4. 新业务继续复用同一套运行时目录、日志和中间数据机制
