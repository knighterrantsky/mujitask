# OpenClaw Skills

更新时间：`2026-04-03`

本文件面向 OpenClaw skill 配置与运行。目标是：把一个可直接复制到 OpenClaw workspace 中的 skill 实例说明清楚，而不是提供一份说明型模板。

## 1. Skill 定位

当前 skill 的定位是：

- 以飞书多维表格为主入口
- 读取表中的 TikTok 竞品链接记录
- 自动完成链接规范化、格式化、去重
- 抓取 TikTok 竞品数据并回写当前飞书记录

对 OpenClaw 而言，当前 skill 只定义一个主业务入口：

- 处理飞书表中的 TikTok 竞品链接，并将抓取结果回写飞书

说明：

- 链接清洗属于主流程中的自动前置步骤
- OpenClaw 不需要感知内部开发 task 名、workflow 名或运行模式名

## 2. Skill 包结构

部署到 OpenClaw workspace 后，目录结构固定为：

- `SKILL.md`
- `skill.local.env`
- `skill.local.env.example`
- `run_feishu_tiktok_sync.sh`
- `run_feishu_tiktok_sync.ps1`
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
- 内部运行模式

这些值由部署脚本或包装脚本固定：

- `url_field_name = 产品链接`
- `profile_ref = local-chrome`

## 4. OpenClaw 调用方式

OpenClaw 侧只保留一个业务语义：

- 读取飞书表中的 TikTok 竞品链接
- 自动完成链接规范化与去重
- 批量抓取竞品信息
- 将结果回写飞书表格

推荐对 OpenClaw 使用的自然语言句式：

- 读取飞书表中的 TikTok 竞品链接，抓取竞品信息并回写结果
- 对当前飞书 TikTok 竞品表执行链接规范化后再批量抓取
- 使用当前飞书表数据做 TikTok 竞品采集和写回
- 处理这张飞书竞品表里的 TikTok 链接，并把抓取结果更新回表格

说明：

- 当前 skill 只暴露一个主入口脚本：
  - macOS: `bash run_feishu_tiktok_sync.sh`
  - Windows: `powershell -ExecutionPolicy Bypass -File .\run_feishu_tiktok_sync.ps1`
- 主入口脚本会自动完成“先整理链接，再抓取并回写”
- OpenClaw 不需要知道 cleanup 与 batch sync 的内部拆分

## 5. 浏览器启动方式

当前抓取流程使用 `chrome_cdp`。  
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

- 无法进入 TikTok 页面抓取
- 处理方式：先安装 Chrome，再重新执行部署脚本或启动浏览器脚本

## 7. 可运行判定

只有同时满足下面条件，才能认为这份 skill 已经可运行：

- OpenClaw workspace 中存在 `mujitask-tiktok-feishu-sync`
- OpenClaw workspace 中不存在旧的 `mujitask-tiktok-feishu-sync.backup-*`
- skill 目录中存在 `SKILL.md` 和包装脚本
- skill 目录中存在统一主入口脚本
- `skill.local.env` 已生成
- 本地项目 `.venv` 已存在
- 项目内部任务检查通过

## 8. 关联文档

- [03-部署文档.md](./03-部署文档.md)
- [../../skills/mujitask-tiktok-feishu-sync/SKILL.md](../../skills/mujitask-tiktok-feishu-sync/SKILL.md)
