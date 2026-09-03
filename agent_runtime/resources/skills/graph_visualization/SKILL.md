---
name: graph_visualization
description: 图可视化与高亮展示技能，支持节点、路径、子图、社区等多种高亮类型
metadata:
  version: 1.0.0
  domain: graph_analysis
  risk_level: low
  igraph:
    activates_on_intents: [visualize_graph, show_graph, highlight_nodes, highlight_path, highlight_subgraph, graph_visualization, display_graph, draw_graph, plot_graph, show_shortest_path, show_community, show_functional_group]
    any_tools: [visualize_subgraph]
    requires_graph_state: loaded
---

# 图可视化

当用户意图涉及图的可视化展示、高亮特定节点/路径/子图/社区时触发此技能。

## 适用场景

- 用户要求"可视化图"、"画出图"、"展示图"
- 用户要求高亮特定节点（如最短路径、关键节点）
- 用户要求高亮路径（如 A 到 B 的最短路径）
- 用户要求高亮子图（如 Ego-graph、社区子图）
- 用户要求高亮社区（如社区检测结果）
- 用户要求展示分子官能团、网络关键结构等

## 操作步骤

1. **确认图已加载**：如果图尚未加载，提示用户先加载图数据。
2. **调用可视化工具**：使用 `visualize_subgraph` 工具获取全图可视化数据。
3. **构建 highlights 参数**：根据用户意图构建 `highlights` 参数（见下方统一格式）。
4. **组合高亮**：如果用户同时要求多种高亮（如最短路径 + 社区），可同时指定 `paths`、`nodes`、`subgraphs`。
5. **社区着色**：如需社区着色，设置 `include_community: true`。

## Highlights 统一格式（Agent 输入格式）

```json
{
  "nodes": [
    {"ids": ["A", "F"], "label": "关键节点"}
  ],
  "paths": [
    {"node_sequence": ["A", "C", "D", "B"], "label": "A→B 最短路径"}
  ],
  "subgraphs": [
    {"nodes": ["C1", "C2", "O"], "label": "官能团"}
  ]
}
```

### 参数说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `nodes` | array | 节点高亮列表 |
| `nodes[].ids` | array | 要高亮的节点 ID 列表 |
| `nodes[].label` | string | 高亮标签（可选） |
| `paths` | array | 路径高亮列表 |
| `paths[].node_sequence` | array | 路径节点序列（按顺序） |
| `paths[].label` | string | 路径标签（可选） |
| `subgraphs` | array | 子图/社区高亮列表 |
| `subgraphs[].nodes` | array | 子图节点列表 |
| `subgraphs[].label` | string | 子图标签（可选） |

### 高亮类型与颜色

| 类型 | 颜色 | 说明 |
|------|------|------|
| `paths` | 黄色 (#f59e0b) | 路径高亮 |
| `nodes` | 绿色 (#10b981) | 节点高亮 |
| `subgraphs` | 紫色 (#8b5cf6) | 子图/社区高亮 |

## 验证

- 可视化面板自动打开并显示图
- 高亮按钮出现，点击可切换对应高亮
- 高亮类型颜色区分：路径(黄)、节点(绿)、子图(紫)、社区(粉)
