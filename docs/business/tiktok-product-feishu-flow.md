# TikTok 商品采集到飞书字段

这个业务 flow 的目标是：

1. 输入一个 TikTok Shop 商品链接
2. 抓取商品页中的结构化数据
3. 下载商品主图到本地
4. 产出飞书多维表格可直接消费的字段字典

当前版本先不负责真正写入飞书，`extend_script` 里的导入脚本可以直接消费最后一步输出的 `fields`。

## Task 名称

`tiktok_product_to_feishu`

## 支持参数

- `product_url`: TikTok Shop 商品链接，必填
- `url`: `product_url` 的别名
- `run_mode`: 可选，默认 `draft`
- `field_mapping`: 可选，自定义字段映射，格式为 `{逻辑字段名: 飞书列名}`

## 默认输出字段

默认的飞书字段映射是：

- `main_image_file` -> `商品主图`
- `price_text` -> `商品价格`
- `sales_count` -> `销量`
- `shop_name` -> `店铺名称`

同时 workflow 第一步也会保留完整的逻辑字段：

- `product_id`
- `title`
- `source_url`
- `resolved_url`
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

其中 `商品主图` 不再是远程链接，而是一个本地文件描述对象：

```json
{
  "type": "local_file",
  "path": "runtime/downloads/tiktok_product_images/1729440407432826887-main-image.webp",
  "file_name": "1729440407432826887-main-image.webp",
  "mime_type": "image/webp",
  "source_url": "https://..."
}
```

默认下载目录是 `runtime/downloads/tiktok_product_images/`。

## 调用示例

```bash
curl -X POST http://127.0.0.1:8110/runs \
  -H 'Content-Type: application/json' \
  -d '{
    "task_name": "tiktok_product_to_feishu",
    "params": {
      "product_url": "https://shop.tiktok.com/us/pdp/putare-remote-control-scroller-for-tiktok-videos-e-books-with-holder/1729732615040962895"
    },
    "wait": true
  }'
```

如果你的飞书多维表格列名不是默认中文列名，可以传 `field_mapping`：

```json
{
  "main_image_url": "主图",
  "price_text": "价格",
  "sales_count": "销量",
  "shop_name": "店铺"
}
```
