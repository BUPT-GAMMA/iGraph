# Neo4j 原生查询

当前连接使用 Neo4j Cypher，通过参数对象传递值，例如 `MATCH (n:Paper) WHERE n.label = $label RETURN count(n) AS count`，`parameters={"label": 0}`。标签和属性以实际结构为准，示例不证明当前库有 Paper。

已验证 Neo4j 5.26.31。结构发现使用数据库元数据；实际覆盖以 `coverage` 为准。`elementId(n)` 是当前来源内的实体标识，业务 ID 只能来自实际属性。

查询工具将原生 Cypher 编译分类后执行已允许的只读语句。常用读取包含 MATCH、WHERE、WITH、RETURN 和聚合；过程及外部/动态扩展受执行端限制。拒绝或超时后根据错误与预算缩小查询，不切换协议或凭据。

计数与聚合在服务器完成；导出包含原查询、参数、来源及有界结果。预览或导出截断不能推导总体。应用只读不证明账号只读。

## 分组统计与文件导出

对已确认的标签和属性，直接按组聚合，例如：

```cypher
MATCH (n:Paper) RETURN n.label AS label, count(*) AS count ORDER BY label
```

需要百分比 CSV 时，可在该查询的工具顶层设 `save_result=true`，用工作区读取返回的 `artifact_path`，
从未截断的完整分组行计算总和、百分比并发布文件。若分组结果截断，另查总体分母并如实说明输出不全。
Cypher 不使用 SQL 的 `OVER()` 窗口语法；这类后续计算可直接基于已导出的结果完成，无需反复改写聚合查询。
