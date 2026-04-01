# TikTok -> 飞书双入口协议

这份文档描述当前已经实现并准备 release 的两个正式入口：

1. `tiktok_product_link_cleanup`
2. `tiktok_feishu_batch_sync`

推荐顺序固定为：

1. 先执行 cleanup，整理并去重 TikTok 商品链接
2. 再执行 stage-1 sync，逐条通过浏览器补齐飞书现有字段

## `tiktok_product_link_cleanup`

### 适用场景

- 飞书表格里已经有一批原始 TikTok 商品链接
- 需要去掉 query 参数，统一成标准 PDP URL
- 需要以规范 URL 为唯一键去重
- 重复记录保留首条，删除其余重复整行

### 输入协议

```json
{
  "table_url": "https://my.feishu.cn/base/appXXX?table=tblXXX&view=vewXXX",
  "access_token_env": "FEISHU_ACCESS_TOKEN",
  "url_field_name": "产品链接",
  "run_mode": "canary"
}
```

### 写入规则

- keeper 行只回写 `产品链接`
- duplicate 行只删除
- 不写 `标准产品链接`
- 不写 `链接整理状态`
- 不写 `删除重复数`

### 返回结构

```json
{
  "summary": {
    "total": 3,
    "counts": {
      "updated": 1,
      "deleted": 1,
      "invalid_url": 1
    }
  },
  "items": [
    {
      "record_id": "recA",
      "source_url": "https://www.tiktok.com/shop/pdp/111?source=product_detail",
      "normalized_url": "https://www.tiktok.com/shop/pdp/111",
      "status": "updated",
      "error": "",
      "deleted_record_ids": ["recB"]
    }
  ],
  "settings": {
    "run_mode": "canary",
    "apply_mutations": true,
    "url_field_name": "产品链接"
  }
}
```

### 典型状态

- `preview`
- `updated`
- `delete_preview`
- `deleted`
- `invalid_url`
- `skipped_empty`

## `tiktok_feishu_batch_sync`

### 适用场景

- 飞书表格已经存在待处理记录
- 链接已经过 cleanup
- 需要读取每行 TikTok 链接
- 通过浏览器打开商品页并完成字段提取
- 只补齐阶段一空缺字段，并写回到同一条记录

### 输入协议

```json
{
  "table_url": "https://my.feishu.cn/base/appXXX?table=tblXXX&view=vewXXX",
  "access_token_env": "FEISHU_ACCESS_TOKEN",
  "url_field_name": "产品链接",
  "profile_ref": "local-chrome",
  "run_mode": "canary",
  "max_records": 0,
  "retry_attempts": 3,
  "retry_delay_sec": 3.0
}
```

### 处理规则

- 只读取当前表格 / 视图中的记录，不再以 `product_urls[]` 为输入
- 只处理 URL 非空且已规范化的记录
- 如果同一张表里仍有重复规范 URL，返回 `skipped_duplicate_needs_cleanup`
- 阶段一只检查这些字段：
  - `SKU-ID`
  - `图片`
  - `标题`
  - `节日`
  - `卖家`
  - `前台截图`
  - `价格`
  - `记录日期`
- 如果上述字段全部已有值，返回 `skipped_completed`
- 如果上述字段存在一个或多个空缺，则执行抓取
- 执行方式固定为“读一条、抓一条、写一条”
- 只补当前缺失字段
- 只要发生写回，就必须同步刷新 `记录日期`
- `draft` / `approval_required` 只产 preview，不执行上传和回写
- `canary` / `full_auto` 才会真正上传附件和更新行

### 写入规则

- 只允许写入现有字段：
  - `SKU-ID`
  - `图片`
  - `标题`
  - `节日`
  - `卖家`
  - `前台截图`
  - `价格`
  - `记录日期`
- 不写 `商品主图`
- 不写 `商品页截图`
- 不写 `采集状态`
- 不写 `采集错误`
- 不写 `采集时间`

### 返回结构

```json
{
  "summary": {
    "total": 4,
    "counts": {
      "updated": 1,
      "skipped_completed": 2,
      "failed": 1
    }
  },
  "items": [
    {
      "record_id": "recA",
      "source_url": "https://www.tiktok.com/shop/pdp/111",
      "normalized_url": "https://www.tiktok.com/shop/pdp/111",
      "status": "updated",
      "error": "",
      "fields": {
        "标题": "Sample Title",
        "记录日期": 1774972800000
      },
      "missing_fields": ["标题"],
      "attempt_count": 1,
      "retry_errors": []
    }
  ],
  "failed_items": [
    {
      "record_id": "recB",
      "source_url": "https://www.tiktok.com/shop/pdp/222",
      "normalized_url": "https://www.tiktok.com/shop/pdp/222",
      "error": "browser failed",
      "attempt_count": 4,
      "retry_errors": [
        {
          "attempt": 1,
          "error": "browser failed"
        }
      ]
    }
  ],
  "settings": {
    "run_mode": "canary",
    "apply_mutations": true,
    "profile_ref": "local-chrome",
    "url_field_name": "产品链接",
    "skip_completed_rows": true,
    "max_records": 0,
    "retry_attempts": 3,
    "retry_delay_sec": 3.0,
    "requires_clean_links": true
  }
}
```

### 典型状态

- `preview`
- `updated`
- `failed`
- `skipped_not_cleaned`
- `skipped_duplicate_needs_cleanup`
- `skipped_completed`
- `skipped_empty`
- `invalid_url`

## 浏览器约束

- 当前阶段直接复用 `automation_framework.browser`
- 默认执行环境是 `chrome_cdp`
- 推荐通过 `profile_ref=local-chrome` 使用本机 Chrome 调试端口
