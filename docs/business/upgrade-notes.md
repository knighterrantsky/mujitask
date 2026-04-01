# Upgrade Notes

这个文件用于记录业务仓库未来吸收 scaffold / framework 升级时的迁移说明。

完整流程见：

- `docs/business/platform-upgrade-playbook.md`

## 首版初始化建议记录

- 使用的 scaffold 版本
- 使用的 framework version / commit
- 是否替换了 demo task 与 demo workflow
- 是否修改了 platform-managed 区域

## 当前业务额外假设

- TikTok 一期实现直接依赖 `automation_framework.browser`
- 当前默认浏览器 provider 为 `chrome_cdp`
- 如果后续切换到 `roxy`，优先保持 task contract 和返回结构不变

## 后续升级建议模板

### Current Baseline

- scaffold version:
- framework version:
- framework commit:
- contract pack version:

### Target Baseline

- scaffold version:
- framework version:
- framework commit:
- contract pack version:

### Files Synced From Scaffold

- `.platform/`
- `AGENT.MD`
- `docs/framework_contract/`
- platform shell files:

### Business-Owned Areas Kept Local

- `tasks/`
- `workflows/`
- `mappers/`
- `validators/`
- `flows/`

### Verification

- agent startup:
- `/tasks`:
- demo/smoke run:
- `draft` blocks `submit`:
