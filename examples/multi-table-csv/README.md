# 多表 CSV 构图示例

在构图页面一次选择本目录中的 4 个 CSV 文件，不要上传 README。

建议的主键：

- `customers.customer_id`
- `products.product_id`
- `orders.order_id`
- `order_items.(order_id, line_no)`（复合主键，需要同时勾选两列）

建议的外键：

- `orders.customer_id` → `customers.customer_id`
- `order_items.order_id` → `orders.order_id`
- `order_items.product_id` → `products.product_id`

界面会按同名列自动建议上述外键。请重点检查 `order_items`，并手动把
`order_id` 和 `line_no` 都选为主键。

完成构图后，预期产生 16 个节点（3 位客户、3 个商品、4 个订单、6 个订单项）
以及 16 条外键边（4 条订单到客户、6 条订单项到订单、6 条订单项到商品）。
