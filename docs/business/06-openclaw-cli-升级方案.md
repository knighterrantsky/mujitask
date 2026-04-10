# OpenClaw CLI 升级方案

更新时间：`2026-04-10`

## 1. 目标

当前项目继续以 OpenClaw skill 调用本地 CLI 的方式交付，但补齐安装与升级生命周期，避免现场升级时重复删目录重装。

本方案面向一期落地，优先解决：

- 升级不删除 `install_dir`
- `skill.local.env` 中的扩展本地配置不丢失
- `runtime/*` 与 `config/browser_profiles.json` 不被覆盖

## 2. 最终交付形态

现场交付继续拆成两层：

1. OpenClaw skill 层：
   - OpenClaw 继续通过 CLI 方式调用 `automation-business-scaffold-run`
   - 不修改现有 skill 协议和 step 入口
2. 生命周期脚本层：
   - `deploy-openclaw.sh` / `deploy-openclaw.ps1` 负责首装
   - `update-openclaw.sh` 负责 macOS 原地升级
   - `verify-openclaw.sh` / `verify-openclaw.ps1` 负责环境验收

这意味着业务执行入口保持稳定，安装与升级逻辑从“同一个脚本兼任”改为“首装 / 更新分离”。

## 3. 一期升级策略

一期采用“原目录原地升级 + 保留本地状态”的方式：

- 继续使用原 `install_dir`
- 同步新版本代码时保留：
  - `.venv/`
  - `runtime/`
  - `.env`
  - `config/browser_profiles.json`
- 刷新 OpenClaw workspace skill 时 merge 保留已有 `skill.local.env`
- 部署状态文件增加：
  - `INSTALL_LAYOUT_VERSION=1`
  - `UPDATE_SUPPORTED=1`

这样可以先解决现场最痛的升级问题，同时不引入 `current/releases/shared` 目录切换模型。

## 4. 现有工程需要的改造

为了支持上面的交付方式，一期工程改造集中在以下几个点：

1. 部署脚本拆分：
   - `deploy-openclaw.sh` 只做首装
   - 已受管安装再次执行时直接失败并提示改用 `update-openclaw.sh`
2. 公共逻辑抽取：
   - 共用 shell 能力沉淀到 `examples/openclaw/openclaw_deploy_common.sh`
   - kv merge、部署状态写入、目录同步等逻辑沉淀到 `examples/openclaw/openclaw_deploy_utils.py`
3. 配置保留策略收敛：
   - `skill.local.env` 改成 merge 回写
   - `config/browser_profiles.json` 改成仅缺失时生成
4. 校验脚本增强：
   - `verify-openclaw.sh` 增加对升级兼容标记的检查
   - 继续验证 CLI、任务列表、浏览器配置和 skill 包完整性

## 5. 与长期方案的关系

一期不是最终状态。

更长期的推荐方向仍然是：

- 配置与运行时目录进一步外置或共享
- 引入 `current/releases/shared` 目录布局
- 增加 rollback 与更稳妥的切换机制
- 视需要让 OpenClaw skill 从“纯 CLI”演进到“CLI 外壳 + 本地服务内核”

但在进入这些改造之前，一期先把“可升级、不删目录、少丢配置”落地，优先解决现场交付问题。
