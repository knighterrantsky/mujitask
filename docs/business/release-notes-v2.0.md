# v2.0 Release Notes

发布日期：`2026-04-01`

版本目标：收敛 TikTok 飞书一期正式入口、补齐 release 文档，并统一 OpenClaw / CLI / 表驱动阶段一协议。

## 本次发布内容

### 1. 正式入口收敛为两个

- `tiktok_product_link_cleanup`
  - 读取飞书表中的 `产品链接`
  - 规范化为标准 TikTok PDP URL
  - 回写到原字段 `产品链接`
  - 删除重复整行
- `tiktok_feishu_batch_sync`
  - 读取飞书表中的现有记录
  - 只对阶段一字段存在空缺的记录执行浏览器抓取
  - 按“读一条、抓一条、写一条”逐条写回

### 2. 阶段一字段口径收敛

阶段一只允许写入现有字段：

- `SKU-ID`
- `图片`
- `标题`
- `节日`
- `卖家`
- `前台截图`
- `价格`
- `记录日期`

明确不再写入旧字段：

- `商品主图`
- `商品页截图`
- `采集状态`
- `采集错误`
- `采集时间`
- `标准产品链接`
- `链接整理状态`
- `删除重复数`

### 3. 阶段一执行规则更新

- 不是整批 collect 后统一写回
- 改为逐条执行：
  - 抓取
  - 上传附件
  - 直接更新当前飞书行
- 只补当前缺失字段
- 只要该条记录发生写回，就必须同步刷新 `记录日期`
- 如果阶段一字段全部已有值，则跳过
- 如果链接未 cleanup 或仍有重复记录，则跳过并提示先整理链接

### 4. 浏览器链路增强

- 进入商品页后增加等待登录 toast 自动消失
- 单条记录失败时增加最多 3 次重试
- 最终失败时返回失败记录、失败原因、尝试次数和每次重试错误
- 主图抓取增加 DOM 截图失败时的图片下载回退逻辑
- 卖家字段优先使用页面路由数据，降低错误提取概率

### 5. release 文档同步更新

已同步更新：

- `README.md`
- `docs/business/openclaw-skill-template.md`
- `docs/business/delivery-and-cli.md`
- `docs/business/tiktok-feishu-batch-contract.md`
- `docs/business/customer-delivery-recommendation.md`
- `docs/business/macmini-deployment.md`
- `docs/business/macmini-deployment-troubleshooting.md`
- `docs/business/requirement.md`
- `docs/business/tiktok-two-entry-design.md`
- `docs/business/requirement-src-audit-2026-04-01.md`

## 验证情况

已执行：

```bash
uv run --extra dev pytest tests/test_tiktok_feishu_batch_sync.py tests/test_workflow_demo.py tests/test_tiktok_product_flow.py tests/test_registry.py tests/test_agent.py
```

结果：

- `23 passed`

## 升级影响

### 对 OpenClaw / CLI 调用方

- `tiktok_feishu_batch_sync` 不再是 `product_urls[]` 批量插入接口
- 正式调用顺序改为：
  1. `tiktok_product_link_cleanup`
  2. `tiktok_feishu_batch_sync`
- 真实写入应使用 `run_mode=canary` 或 `run_mode=full_auto`
- `draft` / `approval_required` 只做预览，不落表

### 对飞书表结构

- 不要求新增字段
- 不要求调整现有字段定义
- 所有写回行为都收敛到现有表结构内

## 后续计划

- 第二阶段 FastMoss 采集单独排期实现
- 基于当前 `v2.0` 稳定口径继续补齐二期设计和测试
