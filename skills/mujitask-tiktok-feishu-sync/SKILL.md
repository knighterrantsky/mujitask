# mujitask-tiktok-feishu-sync

读取飞书表中的 TikTok 竞品链接，自动整理链接后抓取竞品数据，并把结果回写到飞书表格。

## 适用场景

- 飞书多维表格中已经维护了 TikTok 竞品链接
- 需要对表中的 TikTok 链接统一整理、去重、规范化
- 需要批量抓取 TikTok 竞品信息并更新回飞书
- 需要持续重复执行同一张飞书竞品表的数据更新

## 执行规则

- 每次执行主流程时，先自动整理飞书表中的 TikTok 链接
- 链接整理完成后，再执行 TikTok 竞品数据抓取
- 抓取完成后，把结果更新回当前飞书表格记录
- 如果浏览器未启动、token 缺失、表格地址错误或本地部署不完整，直接返回阻塞原因

## 主脚本入口

只使用一个主入口脚本：

- macOS: `bash run_feishu_tiktok_sync.sh`
- Windows: `powershell -ExecutionPolicy Bypass -File .\run_feishu_tiktok_sync.ps1`

这个主入口会自动完成：

1. 链接整理和去重
2. TikTok 竞品数据抓取
3. 飞书结果回写

其他脚本属于本地实现细节，不作为主入口说明。

## 固定调用约束

- OpenClaw 只应调用主入口脚本，不要自行拼接内部 task 名、workflow 名或额外参数
- 不要直接调用 `run_cleanup.*`、`run_batch_sync.*` 或 `start_browser_cdp.*`，除非用户明确要求只做单步排查
- 不依赖 stdin 传参；业务配置只从当前 skill 目录下的 `skill.local.env` 读取
- macOS/bash 主入口使用“同步流式输出 + 固定结果尾行”协议
- 主入口脚本启动后，应先看到阶段日志，再看到批量抓取阶段输出的 `run_id`、进度文件路径和心跳日志
- 底层 CLI 原始输出会保存到 `runtime/cli_runs/stdout/<run_id>.log`
- 主入口脚本结束前会输出一行：`__OPENCLAW_RESULT__ <json>`

## 调用实例

下面这些表达可以直接作为调用句式：

- 读取飞书表中的 TikTok 竞品链接，抓取竞品信息并回写结果
- 处理这张飞书 TikTok 竞品表里的链接，并把抓取结果更新回表格
- 对当前飞书 TikTok 竞品表执行链接整理后再批量抓取
- 使用当前飞书表数据做 TikTok 竞品采集和写回
- 读取当前飞书表中的竞品链接，规范化后抓取并更新结果
- 对这张飞书竞品表执行 TikTok 竞品链接清理、抓取和回写
- 根据当前飞书表里的 TikTok 竞品链接批量更新竞品信息

## 命令示例

OpenClaw 在宿主机上应执行固定命令：

```bash
bash run_feishu_tiktok_sync.sh
```

或：

```powershell
powershell -ExecutionPolicy Bypass -File .\run_feishu_tiktok_sync.ps1
```

期望先看到类似输出：

```text
[feishu-tiktok-sync] Step 1/2: normalizing and deduplicating TikTok links in Feishu
[cleanup] Running tiktok_product_link_cleanup with run_mode=canary run_id=openclaw-cleanup-...
[cleanup] Progress files: run_file=... steps_file=...
[feishu-tiktok-sync] Step 2/2: crawling TikTok competitor data and writing results back to Feishu
[batch-sync] Running tiktok_feishu_batch_sync with run_mode=canary max_records=0 run_id=...
[batch-sync] Progress files: run_file=... steps_file=...
[batch-sync] Progress: run_status=running completed_steps=1 last_step=load_records last_status=success
[batch-sync] Heartbeat: run is still active; waiting for the next workflow update
__OPENCLAW_RESULT__ {"status":"success","task_name":"feishu_tiktok_sync",...}
```

如果最终进程被杀掉，可以优先根据 `run_id` 去查看：

- `runtime/cli_runs/<run_id>.json`
- `runtime/cli_runs/steps/<run_id>.json`
- `runtime/cli_runs/signals/<run_id>.json`
- `runtime/cli_runs/stdout/<run_id>.log`

## 返回结果

成功时：

- 会更新飞书表中可正常抓取的 TikTok 竞品记录
- 会返回本次处理的摘要信息
- 会说明成功写回、跳过或失败的记录数量

部分失败时：

- 会返回失败记录或未处理记录的说明
- 会提示哪些记录因为链接异常、页面不可访问、浏览器不可用或权限问题未能完成

完全失败时：

- 会明确返回阻塞原因
- 常见原因包括：浏览器未启动、token 缺失、表格地址错误、本地部署不完整

## 常见错误

### 缺少 `skill.local.env`

- 无法读取本地业务配置
- 处理方式：重新执行部署脚本，或补充当前 skill 目录下的 `skill.local.env`

### 缺少 `INSTALL_DIR`

- 无法定位本地项目安装目录
- 处理方式：修复 `skill.local.env`，或重新执行部署脚本

### 缺少 `TABLE_URL`

- 无法定位目标飞书表
- 处理方式：补充正确的飞书表格地址

### 缺少 `FEISHU_ACCESS_TOKEN`

- 无法读取或回写飞书数据
- 处理方式：补充有效的飞书 token

### 缺少 Chrome 或浏览器未就绪

- 无法进入 TikTok 页面抓取
- 处理方式：先安装 Chrome，并启动本地浏览器调试环境

### 本地部署不完整

- 可能无法找到 CLI、Python 环境或浏览器配置
- 处理方式：重新执行部署脚本，并重新跑部署后验证脚本
