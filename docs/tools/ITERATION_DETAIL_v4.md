# 图可视化工具迭代详情 (v4)

**日期**：2026-04-23  
**状态**：✅ 完成  
**主题**：图可视化功能

---

## 需求确认

| 需求项 | 确认内容 |
|--------|----------|
| 图规模 | < 100 节点（当前），< 1000 节点（预期） |
| 核心功能 | 节点拖拽/布局调整、点击查看节点详情、路径/社区高亮 |
| 触发方式 | UI 专门的"可视化"按钮（方案B） |
| 边权可视化 | 暂不需要 |

---

## 技术方案

### 可视化库选型：D3.js v7 力导向图

| 考量因素 | 评估 |
|----------|------|
| 图规模 < 1000 节点 | ✅ 性能良好 |
| 节点拖拽/布局调整 | ✅ D3 原生支持 |
| 点击查看节点详情 | ✅ 可绑定事件 |
| 路径/社区高亮 | ✅ 数据驱动 |
| CDN 可用 | ✅ |

---

## 实现计划

### Phase 1: 后端工具新增

#### 1.1 新增 `visualize_subgraph` 工具

**文件**：`graph_data_service/tool_catalog.py`

```python
{
    "name": "visualize_subgraph",
    "description": (
        "获取图的完整结构用于可视化渲染。\n\n"
        "参数:\n"
        "- node_names(可选): 指定节点列表；为空则返回全图。\n"
        "- include_community(可选): 是否包含社区信息，默认 false。\n\n"
        "返回:\n"
        "- JSON 字符串，包含节点、边及元数据。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "node_names": {
                "type": "array",
                "items": {"type": "string"},
                "description": "节点名称列表（可选）",
            },
            "include_community": {
                "type": "boolean",
                "description": "是否包含社区信息（默认 false）",
                "default": False,
            },
        },
        "required": [],
    },
    "category": "data",
}
```

**文件**：`graph_data_service/compute.py`

新增方法：
```python
def visualize_subgraph(
    self,
    g: nx.DiGraph,
    gd: GraphData,
    node_names: Optional[List[str]] = None,
    include_community: bool = False,
) -> dict
```

**文件**：`graph_data_service/app.py`

- 在 `_dispatch_graph_tool` 中添加分发逻辑
- 在 `_DATA_TOOLS` 集合中添加 `"visualize_subgraph"`

#### 1.2 返回数据结构

```json
{
  "success": true,
  "directed": true,
  "num_nodes": 50,
  "num_edges": 120,
  "nodes": [
    {
      "id": "A",
      "label": "A",
      "in_degree": 2,
      "out_degree": 3,
      "total_degree": 5,
      "community_id": 0
    }
  ],
  "edges": [
    {
      "source": "A",
      "target": "B",
      "directed": true
    }
  ],
  "communities": [
    {"id": 0, "nodes": ["A", "B", "C"]},
    {"id": 1, "nodes": ["D", "E", "F"]}
  ]
}
```

---

### Phase 2: 前端可视化面板

#### 2.1 面板布局

```
┌──────────────────────────────────────────────────────────────┐
│  图可视化                                          [_] [×]  │
├──────────────────────────────────────────────────────────────┤
│  ┌────────────────────────────────────────────────────────┐  │
│  │                                                        │  │
│  │                   D3.js 画布区域                      │  │
│  │              (力导向图渲染)                            │  │
│  │                                                        │  │
│  │    节点：圆形 + 文字标签                               │  │
│  │    边：带箭头的连线（有向图）                          │  │
│  │    拖拽、缩放、平移                                    │  │
│  │                                                        │  │
│  └────────────────────────────────────────────────────────┘  │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ 控制栏                                                 │  │
│  │ [力导向] [环形]  |  社区→颜色  | [重置视图]            │  │
│  └────────────────────────────────────────────────────────┘  │
├──────────────────────────────────────────────────────────────┤
│  节点详情面板（点击节点时显示）                              │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  节点: A                                                │  │
│  │  入度: 2  出度: 3  总度: 5                              │  │
│  │  邻居: [B, C, D]                                       │  │
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
```

#### 2.2 核心交互

| 交互 | 实现方式 |
|------|----------|
| 拖拽节点 | D3 drag 行为 |
| 缩放/平移 | D3 zoom 行为 |
| 点击节点 | 显示详情面板 |
| 社区着色 | 按 community_id 设置节点颜色 |
| 高亮节点 | 通过 CSS class 控制 |

#### 2.3 D3.js 关键代码结构

```javascript
// 力导向模拟
const simulation = d3.forceSimulation(nodes)
  .force("link", d3.forceLink(edges).id(d => d.id))
  .force("charge", d3.forceManyBody().strength(-300))
  .force("center", d3.forceCenter(width / 2, height / 2));

// 节点渲染
const node = svg.append("g")
  .selectAll("circle")
  .data(nodes)
  .join("circle")
  .attr("r", d => Math.sqrt(d.total_degree + 1) * 4)
  .attr("fill", d => colorScale(d.community_id));

// 边渲染
const link = svg.append("g")
  .selectAll("line")
  .data(edges)
  .join("line")
  .attr("stroke", "#999")
  .attr("stroke-opacity", 0.6);

// 添加箭头标记（处理有向边）
svg.append("defs").append("marker")
  .attr("id", "arrowhead")
  .attr("viewBox", "0 -5 10 10")
  .attr("refX", 20)
  .attr("markerWidth", 6)
  .attr("markerHeight", 6)
  .append("path");
```

---

### Phase 3: 与现有工具联动

| 工具 | 可视化联动方式 |
|------|----------------|
| `community_detection` | 返回 community_id，前端按社区着色 |
| `ego_graph` | 可视化生成的 ego 网络 |
| `subgraph_by_nodes` | 可视化提取的子图 |
| `shortest_path` | 高亮路径上的节点和边 |
| `node_centrality` | 按中心性调整节点大小 |

---

## 文件改动清单

| 文件 | 改动类型 |
|------|----------|
| `graph_data_service/tool_catalog.py` | 新增工具规格 |
| `graph_data_service/compute.py` | 新增 `visualize_subgraph` 方法 |
| `graph_data_service/app.py` | 工具分发 + 数据工具注册 |
| `index.html` | 可视化面板组件 + D3.js 渲染 |
| `docs/archive/reports/ITERATION_SUMMARY.md` | 更新版本表 |
| `docs/tools/ITERATION_DETAIL_v4.md` | 本文档 |

---

## 预期成果

1. ✅ 新增 `visualize_subgraph` 工具，可获取图结构数据
2. ✅ 可视化面板，支持 D3.js 力导向图渲染
3. ✅ 节点拖拽、缩放、平移
4. ✅ 点击节点显示详情
5. ✅ 社区着色支持
6. ✅ 布局切换（力导向/环形）

---

## 实现状态

### Phase 1: 后端 ✅ 已完成

| 任务 | 状态 | 变更文件 |
|------|------|----------|
| 新增 `visualize_subgraph` 工具规格 | ✅ 完成 | tool_catalog.py |
| 实现 `visualize_subgraph` 方法 | ✅ 完成 | compute.py |
| 添加工具分发逻辑 | ✅ 完成 | app.py |
| 注册为数据操作工具 | ✅ 完成 | app.py |
| 新增 `/api/sessions/{id}/graph/visualize` 接口 | ✅ 完成 | backend/api_gateway/app.py |

### Phase 2: 前端 ✅ 已完成

| 任务 | 状态 | 变更文件 |
|------|------|----------|
| D3.js CDN 引入 | ✅ 完成 | index.html |
| 可视化面板 CSS 样式 | ✅ 完成 | index.html |
| 可视化面板 HTML 结构 | ✅ 完成 | index.html |
| 控制栏（力导向/环形布局切换） | ✅ 完成 | index.html |
| 节点详情面板 | ✅ 完成 | index.html |
| D3.js 力导向图渲染 | ✅ 完成 | index.html |
| 节点拖拽/缩放/平移 | ✅ 完成 | index.html |
| 社区着色功能 | ✅ 完成 | index.html |

### Phase 3: 文档更新 ✅ 已完成

| 任务 | 状态 |
|------|------|
| 更新 ITERATION_SUMMARY.md | ✅ 完成 |
