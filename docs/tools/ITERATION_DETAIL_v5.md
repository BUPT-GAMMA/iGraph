# 图可视化工具 v5 迭代详情

## 概述

本次迭代将图可视化功能集成到 Agent 系统中，实现了：
1. **Agent 自动调用可视化**：当用户意图涉及图可视化时，模型自动调用 `visualize_subgraph`
2. **高亮系统**：支持节点、路径、子图、社区四种高亮类型的可视化
3. **统一格式**：所有计算工具的结果可以方便地传入可视化

---

## Phase 1: System Prompt 增强

### 文件：`backend/agent/context/context_manager.py`

在 `_ROLE_PROMPT` 和 `_ROLE_PROMPT_NATIVE` 中新增 **图可视化原则** 章节：

```python
## 图可视化原则

1. 当用户请求涉及图可视化、查看图结构、展示图，或在图分析结果中提到路径、子图、社区、关键节点等结构特征时，应主动调用 `visualize_subgraph` 工具生成可视化数据。
2. 在完成图算法计算后（如最短路径、直径、中心性、社区检测等），若结果包含可可视化的结构信息（路径节点序列、关键节点集合、子图节点等），应紧接着调用 `visualize_subgraph` 并在 highlights 参数中传入这些结构信息，以便用户直观看到结果。
3. 调用 `visualize_subgraph` 时，应充分利用 `highlights` 参数：
   - 路径类结果（最短路径、直径路径等）→ 使用 `highlights.paths`，填入 `node_sequence`
   - 关键节点类结果（中心性 Top-K、关节点等）→ 使用 `highlights.nodes`，填入 `ids`
   - 子图/社区类结果（社区、官能团、ego-graph 等）→ 使用 `highlights.subgraphs`，填入 `nodes`
   - 可同时高亮多种结构，例如同时高亮直径路径和端点节点
4. `visualize_subgraph` 可与计算工具在同一轮函数调用中一起发起。
5. 若用户仅要求可视化而不涉及计算，直接调用 `visualize_subgraph`（不传 node_names 则返回全图）。
6. 社区相关可视化应设置 `include_community: true`。
```

---

## Phase 2: 高亮格式标准化

### 文件：`graph_data_service/compute.py`

`visualize_subgraph` 方法内部实现 `_normalize_highlights` 函数，统一高亮格式：

#### 输入格式（用户友好）

```json
{
  "nodes": [
    {"ids": ["A", "B"], "color": "#ef4444", "label": "关键节点"}
  ],
  "paths": [
    {"node_sequence": ["A", "B", "C"], "color": "#f59e0b", "label": "最短路径"}
  ],
  "subgraphs": [
    {"nodes": ["A", "B", "C", "D"], "color": "#8b5cf6", "label": "官能团"}
  ]
}
```

#### 输出格式（前端标准化）

```json
{
  "highlights": [
    {
      "id": "hl-node-1",
      "type": "node",
      "label": "关键节点",
      "color": "#ef4444",
      "nodes": ["A", "B"],
      "edges": []
    },
    {
      "id": "hl-path-1",
      "type": "path",
      "label": "最短路径",
      "color": "#f59e0b",
      "nodes": ["A", "B", "C"],
      "edges": [
        {"source": "A", "target": "B"},
        {"source": "B", "target": "C"}
      ]
    },
    {
      "id": "hl-subgraph-1",
      "type": "subgraph",
      "label": "官能团",
      "color": "#8b5cf6",
      "nodes": ["A", "B", "C", "D"],
      "edges": []
    }
  ]
}
```

---

## Phase 3: 前端高亮按钮系统

### 文件：`index.html`

#### 高亮按钮渲染

```javascript
function buildInlineGraphCardHtml(graphInfo) {
  // ...
  let hlBtnsHtml = '';
  if (graphInfo.highlights && graphInfo.highlights.length) {
    const typeIcons = { node: '◉', path: '⟶', subgraph: '◭', community: '◎' };
    for (const hl of graphInfo.highlights) {
      const icon = typeIcons[hl.type] || '●';
      const hlColor = hl.color || '';
      const colorAttr = hlColor ? `data-hl-color="${esc(hlColor)}" style="--hl-color:${esc(hlColor)}"` : '';
      const cls = `graph-hl-btn hl-${hl.type}`;
      hlBtnsHtml += `<button class="${cls}" data-graph-id="${esc(gid)}" data-hl-id="${esc(hl.id)}" ${colorAttr} onclick="toggleInlineHighlight('${esc(gid)}','${esc(hl.id)}')">${icon} ${esc(hl.label || hl.type)}</button>`;
    }
    hlBtnsHtml += `<button class="graph-hl-clear" onclick="clearInlineHighlights('${esc(gid)}')">✖</button>`;
  }
  // ...
}
```

#### 高亮应用逻辑

```javascript
function applyInlineHighlights(gid) {
  // Build highlight sets for nodes and edges
  const hlNodes = {}, hlEdges = {};
  for (const hl of graphInfo.highlights) {
    if (!graphInfo.activeHighlights?.[hl.id]) continue;
    const type = hl.type || 'node';
    const color = hl.color || '';
    
    // Add nodes
    if (hl.nodes) {
      for (const nid of hl.nodes) {
        if (!hlNodes[nid]) hlNodes[nid] = { types: new Set(), color: '' };
        hlNodes[nid].types.add(type);
        if (color) hlNodes[nid].color = color;
      }
    }
    
    // Add edges (for paths, also add reverse edges for undirected display)
    if (hl.type === 'path' && hl.nodes && hl.nodes.length > 1) {
      for (let i = 0; i < hl.nodes.length - 1; i++) {
        const k = `${hl.nodes[i]}→${hl.nodes[i + 1]}`;
        // Add both directions for undirected display
        // ...
      }
    }
  }
  
  // Apply CSS classes and custom colors
  svg.selectAll('.graph-node').each(function(d) {
    const info = hlNodes[d.id];
    if (info && info.types.size) {
      const el = d3.select(this);
      for (const tp of info.types) el.classed(`hl-${tp}`, true);
      if (info.color) el.attr('data-hl-color', info.color).style('--hl-color', info.color);
    }
  });
  // Similar for edges...
}
```

#### 高亮样式（CSS）

```css
/* Highlight type styles */
.graph-node.hl-path circle { stroke: #f59e0b; stroke-width: 3px; fill: #fef3c7; }
.graph-node.hl-node circle { stroke: #10b981; stroke-width: 3px; fill: #d1fae5; }
.graph-node.hl-subgraph circle { stroke: #8b5cf6; stroke-width: 3px; fill: #ede9fe; }
.graph-node.hl-community circle { stroke: #ec4899; stroke-width: 3px; fill: #fce7f3; }

.graph-link.hl-path { stroke: #f59e0b; stroke-opacity: 1; stroke-width: 2.5px; }
.graph-link.hl-subgraph { stroke: #8b5cf6; stroke-opacity: 1; stroke-width: 2.5px; }
.graph-link.hl-community { stroke: #ec4899; stroke-opacity: 0.9; stroke-width: 2px; }

/* Button styles */
.graph-hl-btn.hl-path.active { background: #f59e0b; border-color: #f59e0b; }
.graph-hl-btn.hl-node.active { background: #10b981; border-color: #10b981; }
.graph-hl-btn.hl-subgraph.active { background: #8b5cf6; border-color: #8b5cf6; }
.graph-hl-btn.hl-community.active { background: #ec4899; border-color: #ec4899; }
```

---

## 使用示例

### 场景 1：最短路径可视化

**用户输入**：找出从 A 到 C 的最短路径并可视化

**Agent 行为**：
1. 调用 `find_all_paths` 计算路径
2. 调用 `visualize_subgraph` 并传入：
   ```json
   {
     "highlights": {
       "paths": [
         {"node_sequence": ["A", "B", "C"], "label": "最短路径"}
       ]
     }
   }
   ```

### 场景 2：中心性 Top-K 可视化

**用户输入**：找出度数中心性最高的 5 个节点并展示

**Agent 行为**：
1. 调用 `degree_centrality` 获取中心性排序
2. 调用 `visualize_subgraph` 并传入：
   ```json
   {
     "highlights": {
       "nodes": [
         {"ids": ["node1", "node2", "node3", "node4", "node5"], "label": "Top-5 中心节点"}
       ]
     }
   }
   ```

### 场景 3：社区可视化

**用户输入**：展示图的社区结构

**Agent 行为**：
1. 调用 `community_detection` 获取社区划分
2. 调用 `visualize_subgraph` 并传入：
   ```json
   {
     "include_community": true,
     "highlights": {
       "subgraphs": [
         {"nodes": ["A", "B", "C"], "label": "社区 1"},
         {"nodes": ["D", "E", "F"], "label": "社区 2"}
       ]
     }
   }
   ```

---

## 文件改动清单

| 文件 | 改动类型 | 说明 |
|------|----------|------|
| `backend/agent/context/context_manager.py` | 新增 | 图可视化原则章节 |
| `graph_data_service/compute.py` | 完善 | `_normalize_highlights` 高亮标准化 |
| `index.html` | 新增 | 高亮按钮系统、样式、交互逻辑 |
| `docs/archive/reports/ITERATION_SUMMARY.md` | 更新 | 新增 v5 版本记录 |
| `docs/tools/ITERATION_DETAIL_v5.md` | 新增 | 本文档 |

---

## 实现状态

| 任务 | 状态 | 说明 |
|------|------|------|
| System prompt 图可视化原则 | ✅ 完成 | 已在 `_ROLE_PROMPT` 和 `_ROLE_PROMPT_NATIVE` 中添加 |
| 高亮格式标准化 | ✅ 完成 | `_normalize_highlights` 实现 |
| 前端高亮按钮 | ✅ 完成 | `buildInlineGraphCardHtml` |
| 前端高亮交互 | ✅ 完成 | `toggleInlineHighlight`, `applyInlineHighlights` |
| 前端高亮样式 | ✅ 完成 | CSS 样式定义 |
| 文档更新 | ✅ 完成 | ITERATION_SUMMARY.md, ITERATION_DETAIL_v5.md |

---

## v6 (2026-04-29)：侧边栏展开功能

### 功能说明

在 inline-graph-card 中添加了展开按钮（⛶），点击后在**右侧侧边栏**打开全图查看，支持：
- 右侧滑入面板（宽度 50%，最小 500px，最大 900px）
- 力导向/环形布局切换
- 缩放控制（+ / - / 重置）
- 高亮按钮同步（路径、子图、社区、节点）
- 点击节点显示详情

### 文件改动

| 文件 | 改动类型 | 说明 |
|------|----------|------|
| `index.html` | 修改 | `expandInlineGraph` 改为右侧侧边栏，新增 `toggleExpandHighlight`, `clearExpandHighlights`, `applyExpandHighlights` |
| `docs/archive/reports/ITERATION_SUMMARY.md` | 更新 | 新增 v6 版本记录 |

### 关键代码

```javascript
// 展开按钮
<button class="ig-ctrl-btn" onclick="expandInlineGraph('${esc(gid)}')" title="全屏查看">⛶</button>

// 右侧侧边栏展开函数
function expandInlineGraph(gid) {
  // 查找 graphInfo
  // 创建右侧面板（position:fixed; right:0; top:0; bottom:0）
  // 渲染全图（与 inline 相同的 D3 逻辑）
  // 支持 highlights 同步高亮
}

function closeExpandModal() {
  // 清理面板和全局状态
  document.getElementById('expand-panel')?.remove();
  window._expandSim = null;
  window._expandSvg = null;
  window._expandGraphInfo = null;
}
```

### 实现状态

| 任务 | 状态 | 说明 |
|------|------|------|
| 展开按钮 | ✅ 完成 | inline-graph-card 头部 |
| 右侧侧边栏 | ✅ 完成 | 固定定位，从右侧滑入 |
| 布局控制 | ✅ 完成 | 力导向/环形切换 |
| 缩放控制 | ✅ 完成 | + / - / 重置 |
| 高亮按钮 | ✅ 完成 | 路径/子图/社区/节点 |
| 高亮同步 | ✅ 完成 | applyExpandHighlights |
| 节点详情 | ✅ 完成 | 点击节点显示邻居等信息 |
| 关闭功能 | ✅ 完成 | 点击 × 关闭 |
