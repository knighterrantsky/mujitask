# TikTok → 飞书表格驱动同步协议

这份文档描述当前已经实现的阶段一正式入口：

1. `tiktok_product_link_cleanup`
2. `tiktok_feishu_batch_sync`

推荐顺序固定为：

1. 先执行 cleanup，整理并去重 TikTok 商品链接
2. 再执行 batch sync，逐行通过浏览器完成采集和回写

## `tiktok_product_link_cleanup`

### 适用场景

- 飞书表格里已经有一批原始 TikTok 商品链接
- 需要去掉 query 参数，统一成标准 PDP URL
- 需要以标准化 URL 为唯一键去重
- 重复记录保留最早一行，删除其余重复整行

### 输入协议

```json
{
  "table_url": "https://my.feishu.cn/base/appXXX?table=tblXXX&view=vewXXX",
  "access_token_env": "FEISHU_ACCESS_TOKEN",
  "url_field_name": "产品链接",
  "normalized_url_field_name": "标准产品链接",
  "cleanup_status_field_name": "链接整理状态",
  "run_mode": "approval_required"
}
```

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
    "url_field_name": "产品链接",
    "normalized_url_field_name": "标准产品链接",
    "cleanup_status_field_name": "链接整理状态"
  }
}
```

## `tiktok_feishu_batch_sync`

### 适用场景

- 飞书表格已经存在待处理记录
- 需要读取每行 TikTok 链接
- 通过浏览器打开商品页并完成字段提取
- 截图商品页和主图
- 上传附件并回写到同一条记录

### 输入协议

```json
{
  "table_url": "https://my.feishu.cn/base/appXXX?table=tblXXX&view=vewXXX",
  "access_token_env": "FEISHU_ACCESS_TOKEN",
  "url_field_name": "产品链接",
  "profile_ref": "local-chrome",
  "run_mode": "approval_required",
  "field_mapping": {
    "source_url": "产品链接",
    "normalized_url": "标准产品链接",
    "product_id": "SKU-ID",
    "title": "标题",
    "holiday": "节日",
    "shop_name": "卖家",
    "price_amount": "价格",
    "main_image_file": "商品主图",
    "product_page_screenshot_file": "商品页截图",
    "cleanup_status": "链接整理状态",
    "cleanup_deleted_duplicates": "删除重复数",
    "synced_at": "采集时间",
    "sync_status": "采集状态",
    "sync_error": "采集错误"
  }
}
```

### 处理规则

- 只读取当前表格/视图中的记录，不再以 `product_urls[]` 为主输入
- 只处理 URL 非空且未完成阶段一的记录
- 如果同一张表里仍有重复标准化 URL，只处理最早一行，其余记录返回 `duplicate_blocked`
- `draft` / `approval_required` 只产 preview，不执行上传和回写
- `canary` / `full_auto` 才会真正上传附件和更新行

### 返回结构

```json
{
  "summary": {
    "total": 4,
    "counts": {
      "updated": 1,
      "duplicate_blocked": 1,
      "skipped_completed": 1,
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
        "SKU-ID": "111",
        "标题": "Sample Title",
        "价格": "17.99",
        "商品主图": [
          {
            "file_token": "boxcn-main"
          }
        ],
        "商品页截图": [
          {
            "file_token": "boxcn-page"
          }
        ],
        "采集状态": "success"
      }
    }
  ],
  "settings": {
    "run_mode": "canary",
    "apply_mutations": true,
    "profile_ref": "local-chrome",
    "url_field_name": "产品链接",
    "capture_page_screenshot": true,
    "skip_completed_rows": true,
    "max_records": 0
  }
}
```

## 浏览器约束

- 当前阶段直接复用 `automation_framework.browser`
- 默认执行环境是 `chrome_cdp`
- 如果后续迁移到 `roxy`，任务参数和返回结构不变，只切换 `profile_ref` / provider 配置
