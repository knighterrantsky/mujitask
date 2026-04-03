# OpenClaw Skills

更新时间：`2026-04-03`

本文件面向 OpenClaw skill 配置与运行。目标是：把一个可直接复制到 OpenClaw workspace 中的 skill 实例说明清楚，而不是提供一份说明型模板。

## 1. Skill 定位

当前 skill 的定位是：

- 以飞书多维表格为主入口。
- 读取表中的 TikTok 竞品记录。
- 基于 `产品链接` 抓取阶段一商品信息。
- 把抓取结果直接回写当前飞书记录。

当前正式入口有两个：

- `tiktok_product_link_cleanup`
- `tiktok_feishu_batch_sync`

## 2. Skill 包结构

部署到 OpenClaw workspace 后，目录结构固定为：

- `SKILL.md`
- `skill.local.env`
- `skill.local.env.example`
- `run_cleanup.sh`
- `run_cleanup.ps1`
- `run_batch_sync.sh`
- `run_batch_sync.ps1`
- `start_browser_cdp.sh`
- `start_browser_cdp.ps1`

## 3. Skill 本地配置

Skill 本地持久化配置文件固定为：

- `skill.local.env`

配置项只保留：

- `INSTALL_DIR`
- `TABLE_URL`
- `FEISHU_ACCESS_TOKEN`

不作为 skill 配置项的内容：

- OpenClaw workspace 路径
- `url_field_name`
- `profile_ref`
- 默认 `run_mode`

这些值由部署脚本或包装脚本固定：

- `url_field_name = 产品链接`
- `profile_ref = local-chrome`

## 4. Skill 调用方式

### 4.1 cleanup

- macOS:
  - `bash run_cleanup.sh draft`
  - `bash run_cleanup.sh canary`
- Windows:
  - `powershell -ExecutionPolicy Bypass -File .\run_cleanup.ps1 -RunMode draft`
  - `powershell -ExecutionPolicy Bypass -File .\run_cleanup.ps1 -RunMode canary`

### 4.2 batch sync

- macOS:
  - `bash run_batch_sync.sh draft 0`
  - `bash run_batch_sync.sh canary 0`
- Windows:
  - `powershell -ExecutionPolicy Bypass -File .\run_batch_sync.ps1 -RunMode draft -MaxRecords 0`
  - `powershell -ExecutionPolicy Bypass -File .\run_batch_sync.ps1 -RunMode canary -MaxRecords 0`

说明：

- 包装脚本会自动读取 `skill.local.env`。
- 包装脚本会自动设置 `FEISHU_ACCESS_TOKEN`。
- 包装脚本固定传入 `url_field_name=产品链接` 和 `profile_ref=local-chrome`。
- `MaxRecords=0` 表示不限制条数。
- batch sync 会先检查 `http://127.0.0.1:9222`，未就绪时自动尝试启动 Chrome CDP。

## 5. 浏览器启动方式

batch sync 使用 `chrome_cdp`。  
为便于宿主机运行，skill 包中同时提供：

- `start_browser_cdp.sh`
- `start_browser_cdp.ps1`

这些脚本负责：

- 查找本机 Chrome
- 以 `--remote-debugging-port=9222` 启动浏览器

如果本机没有 Chrome：

- 直接报错
- 提示“请先安装 Chrome，然后重新执行部署脚本或重新启动浏览器脚本”

## 6. 错误说明

### 6.1 缺少 `skill.local.env`

- 无法读取本地业务配置
- 处理方式：从 `skill.local.env.example` 复制并填写

### 6.2 缺少 `INSTALL_DIR`

- 无法定位本地项目
- 处理方式：重新执行部署脚本，或修复 `skill.local.env`

### 6.3 缺少 `TABLE_URL`

- 无法解析飞书目标表
- 处理方式：补充正确的飞书表地址

### 6.4 缺少 token

- 飞书读取或写回失败
- 处理方式：确认 `FEISHU_ACCESS_TOKEN` 已写入 `skill.local.env`

### 6.5 缺少 Chrome

- `batch sync` 无法进入 TikTok 页面抓取
- 处理方式：先安装 Chrome，再重新执行部署脚本或启动浏览器脚本

## 7. 可运行判定

只有同时满足下面条件，才能认为这份 skill 已经可运行：

- OpenClaw workspace 中存在 `mujitask-tiktok-feishu-sync`
- skill 目录中存在 `SKILL.md` 和包装脚本
- `skill.local.env` 已生成
- 本地项目 `.venv` 已存在
- `list-tasks` 能看到：
  - `tiktok_product_link_cleanup`
  - `tiktok_feishu_batch_sync`

## 8. 关联文档

- [03-部署文档.md](./03-部署文档.md)
- [../../skills/mujitask-tiktok-feishu-sync/SKILL.md](../../skills/mujitask-tiktok-feishu-sync/SKILL.md)
