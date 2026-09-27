# FalkorDB 原生查询

当前连接使用 FalkorDB openCypher，逻辑目标 database 是图名称。执行端负责 GRAPH.RO_QUERY 协议；向 query 传递 Cypher 文本，parameters 传递参数对象，例如 `MATCH (n:Paper) WHERE n.label = $label RETURN count(n) AS count`，`parameters={"label": 0}`。标签和属性以实际结构为准，示例不证明当前图有 Paper。

已验证 FalkorDB 4.20.7 的受限只读子集。MATCH、WHERE、WITH、RETURN、聚合与固定长度路径可组合；常用函数包括 count/sum/avg/min/max/collect/size/id/labels/type/exists/coalesce、abs/ceil/floor/round/sign、toInteger/toFloat/toBoolean。使用 `id(n)` 获取当前来源内实体 ID，不使用 Neo4j 的 elementId。业务 ID 优先使用实际属性。

结构发现由 describe_remote_graph 完成；属性最多采样 32 个节点/关系，coverage.sampled 限制结论范围。仅支持已核实的 db.labels、db.relationshipTypes、db.propertyKeys 过程。range、split、未知函数/过程、列表/模式推导、变量长度路径及外部/动态执行未获准；根据实际任务改用有界普通匹配或工作区处理已导出的结果。

结果达到服务端行上限（最多 128）即保守标记 truncated；总体数量使用服务器 count 聚合。特殊整数带类型保存以避免精度丢失，未知 map/point 等类型会明确拒绝。期限最多 3 秒，停止会关闭客户端；服务器可能继续运行至期限。不要把客户端停止表述为服务器已立即取消。

应用只读不证明账号只读；收到修改要求只说明查询范围，保留当前数据与本地图。
