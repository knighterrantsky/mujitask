# TikTok 商品字段构建 Flow

这个文档描述 `tiktok_product_to_feishu` 这个底层调试 task。

它的目标是：

1. 输入一个 TikTok Shop 商品链接
2. 抓取商品页中的结构化数据
3. 下载商品主图到本地
4. 产出飞书多维表格可直接消费的字段字典

说明：

- 它不负责真正写入飞书
- 它保留为底层字段构建和调试能力
- OpenClaw 正式主入口应该使用 `tiktok_product_link_cleanup` 或 `tiktok_feishu_batch_sync`

## Task 名称

`tiktok_product_to_feishu`

## 支持参数

- `product_url`：TikTok Shop 商品链接，必填
- `url`：`product_url` 的别名
- `run_mode`：可选，默认 `draft`
- `field_mapping`：可选，自定义字段映射，格式为 `{逻辑字段名: 飞书列名}`

## 默认输出字段

当前默认飞书字段映射是：

- `source_url` -> `产品链接`
- `product_id` -> `SKU-ID`
- `main_image_file` -> `图片`
- `title` -> `标题`
- `holiday` -> `节日`
- `price_amount` -> `价格`

同时会保留完整逻辑字段，包括：

- `source_url`
- `resolved_url`
- `product_id`
- `title`
- `holiday`
- `main_image_url`
- `main_image_local_path`
- `main_image_file_name`
- `main_image_mime_type`
- `price_amount`
- `price_currency`
- `price_text`
- `sales_count`
- `shop_name`
- `shop_url`

其中 `图片` 字段不是远程链接，而是一个本地文件描述对象：

```json
{
  "type": "local_file",
  "path": "runtime/downloads/tiktok_product_images/1729440407432826887-main-image.webp",
  "file_name": "1729440407432826887-main-image.webp",
  "mime_type": "image/webp",
  "source_url": "https://example.com/image.webp"
}
```

默认下载目录：

- `runtime/downloads/tiktok_product_images/`

## 节日字段逻辑

`节日` 会优先匹配这些选项：

- `情人节`
- `复活节`
- `毕业季`
- `万圣节`
- `圣诞节`
- `其他`

如果标题里没有命中已知节日关键词，则回退为 `其他`。

## 调用示例

```bash
automation-business-scaffold-run run \
  --task tiktok_product_to_feishu \
  --product-url 'https://www.tiktok.com/shop/pdp/1729440407432826887' \
  --run-mode draft
```

如果飞书列名不是默认中文列名，可以传 `field_mapping` 覆盖：

```json
{
  "source_url": "商品链接",
  "product_id": "SKU",
  "main_image_file": "图片",
  "title": "商品标题",
  "holiday": "节日",
  "price_amount": "价格"
}
```
