# FalkorDB 原生查询

当前连接使用 FalkorDB openCypher，逻辑目标 database 是图名称。执行端负责 GRAPH.RO_QUERY 协议；向 query 传递 Cypher 文本，parameters 传递参数对象，例如 `MATCH (n:Paper) WHERE n.label = $label RETURN count(n) AS count`，`parameters={"label": 0}`。标签和属性以实际结构为准，示例不证明当前图有 Paper。

## 常用查询形状

属性筛选写在 `WHERE` 中；当前适配器不接受模式内的 `{property: value}`。
以下以已确认存在的 `Paper`、`cora_id`、`label` 为例，实际名称来自当前库结构：

```cypher
MATCH (n:Paper) WHERE n.cora_id = $id
RETURN n.cora_id AS cora_id, n.label AS label

MATCH (n:Paper)--(neighbor:Paper) WHERE n.cora_id = $id
RETURN DISTINCT neighbor.cora_id AS cora_id, neighbor.label AS label
ORDER BY cora_id
```

两例的工具参数均可为 `parameters={"id": 0}`。结果为空表示本次条件未匹配，不自动换一个 ID。
若工具拒绝，先根据 `error_message` 修正对应表达式；属性 map 的拒绝不需要改变边方向。

## 支持范围与预算

已验证 FalkorDB 4.20.7 的受限只读子集。MATCH、WHERE、WITH、RETURN、聚合与固定长度路径可组合；常用函数包括 count/sum/avg/min/max/collect/size/id/labels/type/exists/coalesce、abs/ceil/floor/round/sign、toInteger/toFloat/toBoolean。使用 `id(n)` 获取当前来源内实体 ID，不使用 Neo4j 的 elementId。业务 ID 优先使用实际属性。

结构发现由 describe_remote_graph 完成；属性最多采样 32 个节点/关系，coverage.sampled 限制结论范围。仅支持已核实的 db.labels、db.relationshipTypes、db.propertyKeys 过程。range、split、未知函数/过程、列表/模式推导、变量长度路径及外部/动态执行未获准；根据实际任务改用有界普通匹配或工作区处理已导出的结果。

表达式采用保守子集：列表只接受纯字面量（字符串、数字、布尔、null 及嵌套列表），整条查询累计最多 128 个元素/嵌套列表项、4 层、4096 UTF-8 字节的字面量源码。可用 `[1,2][0]`、`n.items[0]` 和 `$items[$index]` 单值索引；数组切片、动态列表构造、所有 map 构造/投影（包括模式内属性 map）不支持。参数过滤使用 `WHERE n.label = $label`。`+` 的操作数须为数字字面量、可证明为数字的括号算式或显式 `toFloat(...)` / `toInteger(...)`，例如 `toFloat(n.x)+toFloat(n.y)`；别名/属性名不能证明类型，字符串/列表拼接不支持。普通 `collect(n.property)` 保留；collect 内外的构造同样遵守上述边界。超出子集时，导出必要数据后在工作区处理。

结果达到服务端行上限（最多 128）即保守标记 truncated；总体数量使用服务器 count 聚合。特殊整数带类型保存以避免精度丢失，未知 map/point 等类型会明确拒绝。期限最多 3 秒，停止会关闭客户端；服务器可能继续运行至期限。不要把客户端停止表述为服务器已立即取消。

应用只读不证明账号只读；收到修改要求只说明查询范围，保留当前数据与本地图。
