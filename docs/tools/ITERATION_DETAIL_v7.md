# 图可视化工具 v7 迭代详情

## 概述

本次迭代对图可视化功能进行了全面修复与完善，涵盖前端布局、侧边栏多图管理、高亮系统、节点卡片、以及后端节点属性传递等多个方面，使图可视化功能达到完整可用状态。

---

## Phase 1：前端布局修复

### 文件：`index.html`

**问题**：`.main-content` 的 `flex-direction` 默认为 `column`，导致图可视化区域与聊天区域上下叠放，图无法正常展示。

**修复**：

```css
.main-content {
  flex-direction: row;  /* 原为 column */
}
```

---

## Phase 2：可视化侧边栏重构

### 文件：`index.html`

#### 2.1 toggleGraphSidebar 重写

原函数逻辑混乱，重写后正确切换侧边栏的 `open` 类与宽度。

#### 2.2 侧边栏图卡撑满

侧边栏内部图容器使用 `flex: 1; display: flex; flex-direction: column`，使图渲染区域撑满整个侧边栏可用空间。

#### 2.3 DOM id `-sb` 后缀

侧边栏与 inline 图共用同一套 D3 渲染逻辑，原来 id 直接使用 `gid` 导致 DOM 冲突。修复：侧边栏所有 DOM id 统一加 `-sb` 后缀（如 `graph-svg-${gid}-sb`）。

#### 2.4 多图 Tab 切换

新增函数实现侧边栏多图管理：

```javascript
// 收集所有侧边栏图信息
function _allSidebarGraphs() { ... }

// 切换侧边栏 Tab
function switchSidebarTab(gid) { ... }

// 更新 Tab 标题栏 DOM
function patchSidebarTabs() { ... }

// SSE 收到新图后追加 Tab（不覆盖旧图）
function onNewSidebarGraph(gid) { ... }
```

SSE `graph_visualize` 事件处理后自动调用 `onNewSidebarGraph(gid)`，新图追加为新 Tab，不覆盖已有图。

---

## Phase 3：性能优化——节点引用栏局部更新

### 文件：`index.html`

**问题**：`addNodeRef` / `removeNodeRef` / `clearNodeRefs` 每次都调用全局 `render()`，导致图重绘闪烁。

**修复**：抽取 `_nodeRefBarHtml()` 函数，新增 `patchNodeRefBar()` 只局部更新节点引用栏 DOM，不触发全局渲染。`renderChat` 也复用同一函数。

```javascript
function _nodeRefBarHtml() { /* 生成节点引用栏 HTML 字符串 */ }
function patchNodeRefBar() {
  const el = document.getElementById('node-ref-bar');
  if (el) el.innerHTML = _nodeRefBarHtml();
}
```

---

## Phase 4：高亮系统修复

### 文件：`index.html`

#### 4.1 边格式兼容

`_edgeKey(e)` 函数同时兼容数组 `[u, v]` 和对象 `{source, target}` 两种格式：

```javascript
function _edgeKey(e) {
  if (Array.isArray(e)) return `${e[0]}→${e[1]}`;
  return `${e.source}→${e.target}`;
}
```

#### 4.2 跨数据源查找图信息

`_findGraphInfo(gid)` 同时在 `messages`（inline 图）和 `graphSidebarGraphInfo`（侧边栏图）中查找，避免高亮跨上下文失效。

#### 4.3 路径隐式边高亮

`applyInlineHighlights` / `applyExpandHighlights` 对 `path` 类型高亮，自动将路径节点序列相邻对生成为边高亮（同时写正向与反向 key），支持有向/无向图。

---

## Phase 5：graph_diameter 新增返回字段

### 文件：`graph_data_service/compute.py`

`graph_diameter` 工具结果新增 `diameter_path` 字段，返回直径对应的实际节点路径序列：

```python
result = {
  "diameter": diameter_value,
  "diameter_path": ["node_A", "node_B", "node_C"],  # 新增
  ...
}
```

### 文件：`graph_data_service/tool_catalog.py`

更新 `graph_diameter` 工具描述，说明返回值包含 `diameter_path`，Agent 可直接将其传入 `visualize_subgraph` 的 `highlights.paths`。

---

## Phase 6：节点卡片改进

### 文件：`index.html`

点击节点弹出的节点详情卡片（`showNodeCard`）改进：

- **显示节点 id**：当节点 id 与 label 不同时，同时展示两者
- **邻居带权重展示**：最多展示 20 个邻居，每个邻居显示方向（→ / ←）和边权重
- **邻居列表移入 attrs 区域**：布局更紧凑统一

传入 `showNodeCard` 的数据新增 `nbEdges` 字段（含权重和方向的邻居边信息）：

```javascript
// edgesCopy 加入 weight 字段
const edgesCopy = graphInfo.edges.map(e => ({
  src: e.src, dst: e.dst, weight: e.weight ?? 1
}));
// 节点点击时传入 nbEdges
showNodeCard(node, nbEdges);
```

---

## Phase 7：节点属性显示修复（后端）

### 文件：`graph_data_service/compute.py`

**问题根因**：`visualize_subgraph()` 中构建节点属性时：
1. `gd.name_to_id(node)` 没有 try/except 保护，若节点名不在 `_node2id` 中会抛出 `KeyError` 导致整个函数崩溃
2. 即使 `gd` 接口正常，也没有利用 `adapters.py` 已存入 NetworkX 节点属性的数据作为 fallback

**修复**（`compute.py:1174`）：

```python
for node in target_nodes:
    # 1. KeyError 保护
    try:
        nid = gd.name_to_id(node) if gd.has_node_names else None
    except KeyError:
        nid = None

    attrs: dict = {}
    # 2. 优先通过 GraphData 接口查询（按 id 索引，最准确）
    if nid is not None:
        if gd.has_node_type:
            v = gd.get_node_type_by_id(nid)
            if v is not None:
                attrs["type"] = str(v)
        if gd.has_node_label:
            try:
                v = self._serialize_label(gd.get_node_label_by_id(nid))
                if v is not None:
                    attrs["label"] = v
            except Exception:
                pass
        if gd.has_node_text:
            try:
                v = gd.get_node_text_by_id(nid)
                if v:
                    attrs["text"] = str(v)
            except Exception:
                pass

    # 3. Fallback：从 NetworkX 节点属性读取（adapters.py 已将属性存入节点）
    nx_attrs = g.nodes[node]
    if not attrs.get("type") and nx_attrs.get("node_type"):
        attrs["type"] = str(nx_attrs["node_type"])
    if not attrs.get("label") and nx_attrs.get("node_label") is not None:
        attrs["label"] = str(nx_attrs["node_label"])
    if not attrs.get("text") and nx_attrs.get("text"):
        attrs["text"] = str(nx_attrs["text"])
```

**背景**：`adapters.py` 的 `graph_to_networkx()` 已将 `node_label`、`node_type`、`text` 存入 NetworkX 节点属性（第 66-71 行），但 `visualize_subgraph` 此前完全未利用这些数据。

**验证**（`ds_labeled_test` 数据集）：

```
id='node_0'  attrs={'type': 'TypeA', 'label': 'Label_0', 'text': 'This is the description text for node 0.'}
id='node_1'  attrs={'type': 'TypeB', 'label': 'Label_1', 'text': 'This is the description text for node 1.'}
id='node_2'  attrs={'type': 'TypeA', 'label': 'Label_2', 'text': 'This is the description text for node 2.'}
```

---

## 文件改动清单

| 文件 | 改动类型 | 说明 |
|------|----------|------|
| `index.html` | 修改 | 布局修复、侧边栏重构、多图 Tab、节点引用栏局部更新、高亮系统修复、节点卡片改进 |
| `graph_data_service/compute.py` | 修改 | `visualize_subgraph` 节点属性修复（KeyError 保护 + NetworkX fallback）；`graph_diameter` 新增 `diameter_path` |
| `graph_data_service/tool_catalog.py` | 修改 | `graph_diameter` 工具描述更新 |

---

## 实现状态

| 任务 | 状态 | 说明 |
|------|------|------|
| 布局修复（main-content row） | ✅ 完成 | CSS flex-direction: row |
| 可视化侧边栏按钮正常开关 | ✅ 完成 | toggleGraphSidebar 重写 |
| 侧边栏图卡撑满 | ✅ 完成 | flex: 1 布局 |
| 侧边栏 `-sb` id 后缀避免冲突 | ✅ 完成 | 所有侧边栏 DOM id 加后缀 |
| 多图 Tab 切换 | ✅ 完成 | 新图追加 Tab，不覆盖旧图 |
| 节点引用栏局部更新（无闪烁） | ✅ 完成 | patchNodeRefBar() |
| 高亮边格式兼容 | ✅ 完成 | _edgeKey 兼容数组和对象 |
| 高亮跨数据源查找 | ✅ 完成 | _findGraphInfo 查两处 |
| 路径隐式边高亮 | ✅ 完成 | 正向+反向 key |
| graph_diameter 返回 diameter_path | ✅ 完成 | compute.py + tool_catalog.py |
| 节点卡片显示 id、邻居权重 | ✅ 完成 | showNodeCard 改进 |
| 节点属性（label/type/text）显示 | ✅ 完成 | KeyError 保护 + NetworkX fallback |
