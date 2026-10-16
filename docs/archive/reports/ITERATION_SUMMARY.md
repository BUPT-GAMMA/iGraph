# 图计算工具迭代记录

本文档记录 GraphData Service 图计算工具的迭代开发过程。

## 迭代版本

| 版本 | 日期 | 状态 | 说明 |
|------|------|------|------|
| [v1](./tools/ITERATION_DETAIL_v1.md) | 2026-04-19 | ✅ 完成 | 第一批工具：k_hop_neighbors, ego_graph, subgraph_by_nodes |
| [v4](../../tools/ITERATION_DETAIL_v4.md) | 2026-04-23 | ✅ 完成 | 图可视化：D3.js 力导向图 + 可视化面板 |
| [v5](../../tools/ITERATION_DETAIL_v5.md) | 2026-04-28 | ✅ 完成 | Agent 可调用可视化工具 + 高亮系统 + 统一格式 |
| [v6](../../tools/ITERATION_DETAIL_v5.md#v6-2026-04-29侧边栏展开功能) | 2026-04-29 | ✅ 完成 | 右侧侧边栏展开 + 高亮按钮同步 |
| [v7](../../tools/ITERATION_DETAIL_v7.md) | 2026-05-16 | ✅ 完成 | 图可视化全面修复：布局、侧边栏多图 Tab、节点属性显示、高亮系统、graph_diameter 路径返回 |
| [v8](../../tools/ITERATION_DETAIL_v8.md) | 2026-05-17 | ✅ 完成 | visualize_subgraph 大图上下文优化：节点 cap + BFS 连通采样 |

## 工具分类

### 图计算工具 (compute)
| 工具名 | 功能 | 批次 |
|--------|------|------|
| k_hop_neighbors | K 跳邻居查询 | v1 |
| ego_graph | Ego-graph 子图提取 | v1 |
| subgraph_by_nodes | 按节点列表提取子图 | v1 |

### 数据操作工具 (data)
| 工具名 | 功能 | 批次 |
|--------|------|------|
| load_graph | 从文本加载图 | - |
| load_graph_from_upload | 从上传文件加载图 | - |
| clear_graph | 清空图 | - |
| to_undirected | 转换为无向图 | - |
| get_graph_info | 获取图信息 | - |
| visualize_subgraph | 获取图结构用于可视化（含节点属性 label/type/text，大图 BFS 连通采样） | v4, v7, v8 |
| graph_diameter | 图直径计算（含 diameter_path 路径返回） | v7 |
