# iGraph - "加载数据集"功能完整指南

## 快速总结

**"加载数据集"功能是什么？**
- 前端UI上的"数据集"按钮，可以浏览和加载已保存的图数据集
- 支持多种数据格式：边列表文本、JSON、NPZ二进制、自然语言
- 完整的CRUD操作：创建、读取、更新、删除数据集
- 支持版本管理：每个数据集可以有多个版本

---

## 1. 前端UI位置和操作流程

### 1.1 按钮位置
**文件**: `/index.html` (行号 2648)
```html
<button id="btn-dataset-modal" class="chip">
  <svg>...</svg>
  数据集
</button>
```

### 1.2 操作流程
```
用户点击"数据集"按钮
    ↓
打开数据集管理模态框 (openDatasetModal)
    ↓
自动刷新数据集列表 (refreshDatasets) → GET /api/datasets
    ↓
显示所有可用的数据集及其信息（节点数、边数）
    ↓
用户选择一个数据集，点击"加载"按钮
    ↓
调用 loadDataset(datasetId) 函数
    ↓
发送 POST /api/sessions/{sessionId}/load-dataset 请求
    ↓
后端加载图数据并返回成功响应
    ↓
前端自动关闭模态框，将加载的数据集名称添加到对话消息中
    ↓
Agent 自动开始分析
```

### 1.3 核心前端函数

**打开/关闭模态框**:
```javascript
function openDatasetModal()  { 
  state.datasetModalOpen = true; 
  refreshDatasets();  // 自动刷新列表
}
function closeDatasetModal() { 
  state.datasetModalOpen = false; 
  render(); 
}
```

**刷新数据集列表** (行号 4032):
```javascript
async function refreshDatasets() {
  state.datasetsLoading = true;
  render();
  try {
    const res = await fetch(`${API_BASE}/api/datasets`);
    const data = await res.json();
    if (res.ok && data.success) {
      state.datasets = Array.isArray(data.datasets) ? data.datasets : [];
    }
  } catch (err) {
    state.error = err.message || '获取数据集列表失败';
  } finally {
    state.datasetsLoading = false;
    render();
  }
}
```

**加载数据集** (行号 4045):
```javascript
async function loadDataset(datasetId) {
  if (!state.sessionId || state.datasetLoading) return;
  
  const dataset = state.datasets.find(ds => String(ds.dataset_id) === String(datasetId));
  const datasetName = String(dataset?.name || datasetId || '').trim() || '该数据集';
  const message = `已加载数据集 ${datasetName}，请进行相应的图分析`;
  
  state.datasetLoading = true;
  state.error = null;
  render();
  
  try {
    const res = await fetch(`${API_BASE}/api/sessions/${state.sessionId}/load-dataset`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dataset_id: datasetId }),
    });
    
    const data = await res.json();
    if (!res.ok || !data.success) throw new Error(data.detail || data.error || '加载数据集失败');
    
    state.hasGraph = true;
    state.datasetModalOpen = false;
    state.messages.push({ role: 'user', content: message });
    state.currentPlan = null;
    state.autoScroll = true;
    state.loading = true;
    state.error = null;
    render();
    patchGraphBadge();
    
    await doStreamChat(message);  // 自动开始对话分析
  } catch (err) {
    state.error = err.message || '加载数据集失败';
  } finally {
    state.datasetLoading = false;
    state.loading = false;
    render();
    patchGraphBadge();
  }
}
```

---

## 2. API接口规范

### 2.1 获取数据集列表

**请求**:
```bash
GET /api/datasets
```

**响应** (200 OK):
```json
{
  "success": true,
  "datasets": [
    {
      "dataset_id": "ds_741d440c40",
      "name": "我的图数据集",
      "description": "PubMed论文引用网络",
      "source_type": "graph",
      "node_count": 19717,
      "edge_count": 44338,
      "is_directed": true,
      "created_at": "2024-05-13T10:30:45Z",
      "updated_at": "2024-05-13T15:22:30Z",
      "versions": ["v001"]
    }
  ]
}
```

### 2.2 加载数据集

**请求**:
```bash
POST /api/sessions/{session_id}/load-dataset
Content-Type: application/json

{
  "dataset_id": "ds_741d440c40",
  "version": "latest"  # 可选，默认"latest"
}
```

**响应** (200 OK):
```json
{
  "success": true,
  "session_id": "session_123",
  "dataset_id": "ds_741d440c40",
  "dataset_version": "v001",
  "graph_version": 42,
  "num_nodes": 19717,
  "num_edges": 44338
}
```

**错误响应** (500):
```json
{
  "success": false,
  "error": "FileNotFoundError: dataset not found"
}
```

### 2.3 保存当前图为新数据集

**请求**:
```bash
POST /api/sessions/{session_id}/save-dataset
Content-Type: application/json

{
  "name": "新数据集",
  "description": "数据集描述（可选）"
}
```

**响应** (200 OK):
```json
{
  "success": true,
  "dataset": {
    "dataset_id": "ds_xyz789",
    "name": "新数据集",
    "description": "数据集描述（可选）",
    "source_type": "graph",
    "node_count": 100,
    "edge_count": 200,
    "is_directed": true,
    "created_at": "2024-05-13T15:45:30Z",
    "updated_at": "2024-05-13T15:45:30Z",
    "versions": ["v001"]
  }
}
```

---

## 3. 数据格式要求

### 3.1 支持的输入格式

| 格式 | 扩展名 | 说明 | 复杂度 |
|------|--------|------|--------|
| 边列表文本 | .txt, .edgelist, .csv, .dat | 每行 `src dst [weight]` | 低 |
| JSON (graph_data.v1) | .json | 完整的图模式定义 | 中 |
| NPZ (NumPy) | .npz | 二进制格式，加载快 | 中 |
| 自然语言 | .txt | 由LLM自动抽取三元组 | 高 |

### 3.2 最简单的格式：边列表文本

**文件**: `edges.txt`
```
Alice Bob 1.0
Bob Charlie 2.5
Charlie Alice 1.5
```

**格式说明**:
- 每行一条边
- 格式：`源节点 目标节点 [权重]`
- 权重可选（默认1.0）
- 节点名称可以是字母、数字、中文

**自动处理**:
- 自动为每个独特的节点创建节点ID
- 自动创建双向映射 `id2node` 和 `node2id`

### 3.3 标准格式：JSON graph_data.v1

**文件**: `graph.json`
```json
{
  "schema": "graph_data.v1",
  "num_nodes": 3,
  "directed": true,
  "name": "demo_graph",
  "graph_desc": "A simple directed graph",
  "id2node": {
    "0": "A",
    "1": "B",
    "2": "C"
  },
  "edge_index": [
    [0, 1, 0],    // 源节点ID数组
    [1, 2, 2]     // 目标节点ID数组
  ],
  "edge_weight": [1.0, 2.0, 3.0],
  "node_feat": [[1, 0], [0, 1], [1, 1]],
  "node_label": [0, 1, 0],
  "node_type": ["user", "item", "item"],
  "edge_type": ["follow", "follow", "click"],
  "graph_label": "demo"
}
```

**必填字段**:
- `schema`: 必须是 `"graph_data.v1"`
- `num_nodes`: 节点总数
- `edge_index`: 形状 `[2, E]` 的数组，其中 E 是边数

**可选字段**:
- `directed`: 是否有向（默认true）
- `id2node`: 节点ID到名称的映射
- `edge_weight`: 边权重数组
- `node_label`, `edge_label`: 标签
- `node_type`, `edge_type`: 类型
- `node_feat`, `edge_feat`: 特征

### 3.4 数据集的存储结构

```
datasets/ds_741d440c40/
├── meta.json                 # 元数据
├── graph.npz                # 当前版本的图数据
└── versions/
    ├── v001.npz             # 版本1
    ├── v002.npz             # 版本2
    └── ...
```

**meta.json 结构**:
```json
{
  "dataset_id": "ds_741d440c40",
  "name": "我的图数据集",
  "description": "PubMed论文引用网络",
  "source_type": "graph",
  "node_count": 19717,
  "edge_count": 44338,
  "is_directed": true,
  "created_at": "2024-05-13T10:30:45Z",
  "updated_at": "2024-05-13T15:22:30Z",
  "versions": ["v001"]
}
```

---

## 4. 如何准备和加载数据

### 方法一：通过UI上传（推荐）

**步骤**:
1. 点击界面上的"上传"按钮
2. 选择你的图文件（支持 .txt, .json, .npz, .csv 等）
3. 等待Agent处理并返回图的基本信息
4. 打开"数据集"模态框（点击"数据集"按钮）
5. 填写"数据集名称"和可选的"描述"
6. 点击"保存"按钮

**优点**:
- 无需手动创建目录结构
- 自动生成 dataset_id 和版本
- 支持多种格式自动识别

### 方法二：直接在文件系统创建

**步骤**:

1. 创建目录:
```bash
mkdir -p datasets/my_dataset/versions
```

2. 创建 `meta.json`:
```json
{
  "dataset_id": "my_dataset",
  "name": "我的数据集",
  "description": "数据集描述",
  "source_type": "graph",
  "node_count": 100,
  "edge_count": 200,
  "is_directed": true,
  "created_at": "2024-05-13T10:00:00Z",
  "updated_at": "2024-05-13T10:00:00Z",
  "versions": ["v001"]
}
```

3. 准备图数据文件 (Python):
```python
from graph_data_service.graph_data import GraphData
import numpy as np

graph = GraphData(
    num_nodes=100,
    edge_index=np.array([[0, 1, 2], [1, 2, 3]], dtype=np.int64),
    name="my_graph",
    directed=True
)

graph.save("datasets/my_dataset/graph.npz")
graph.save("datasets/my_dataset/versions/v001.npz")
```

4. 启动应用，点击"数据集"按钮，会自动出现

---

## 5. 后端处理流程

### 5.1 API Gateway (backend/api_gateway/app.py, 行号 1008)

```python
@app.post("/api/sessions/{session_id}/load-dataset")
async def load_dataset(session_id: str, req: LoadDatasetReq):
    # 1. 恢复或创建 agent
    agent, _restored, _needs_graph_reload = self._restore_agent_if_needed(session_id)
    
    try:
        # 2. 调用图数据服务
        result = await _call_graph_data_tool(
            self, session_id, "load_dataset",
            {"dataset_id": req.dataset_id, "version": req.version}
        )
        
        # 3. 如果成功，更新 agent 的图状态
        if result.get("success"):
            agent._has_graph = True
            agent._graph_node_count = result.get("num_nodes", 0)
            agent._graph_edge_count = result.get("num_edges", 0)
            agent.logger.set_graph_flag(True)
        
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
```

### 5.2 GraphData Service (graph_data_service/app.py, 行号 600)

```python
def do_load_dataset(self, session_id: str, dataset_id: str, version: str = "latest") -> dict:
    session = self.get_or_create(session_id)
    graph = self._dataset_registry.load_graph(dataset_id, version=version)
    session.set_graph(graph)
    
    actual_version = version if version != "latest" else \
                     self._dataset_registry.get(dataset_id).versions[-1]
    session.bind_dataset(dataset_id, actual_version)
    self._compute.invalidate(session_id)
    
    return {
        "success": True,
        "session_id": session_id,
        "dataset_id": dataset_id,
        "dataset_version": actual_version,
        "graph_version": session.graph_version,
        "num_nodes": graph.num_nodes,
        "num_edges": graph.num_edges,
    }
```

### 5.3 数据集注册表 (graph_data_service/dataset_registry.py, 行号 101)

```python
def load_graph(self, dataset_id: str, version: str = "latest") -> GraphData:
    meta = self.get(dataset_id)
    if meta is None:
        raise FileNotFoundError(dataset_id)
    
    if version == "latest":
        return GraphData.load(str(self._graph_path(dataset_id)))
    else:
        version_path = self._dataset_dir(dataset_id) / "versions" / f"{version}.npz"
        if not version_path.exists():
            raise FileNotFoundError(f"dataset version not found: {dataset_id}/{version}")
        return GraphData.load(str(version_path))
```

---

## 6. 相关文件速查

| 功能 | 文件 | 行号 |
|------|------|------|
| 前端UI - 加载按钮 | index.html | 2795 |
| 前端 - loadDataset函数 | index.html | 4045 |
| 前端 - refreshDatasets函数 | index.html | 4032 |
| 前端 - 按钮样式 | index.html | 1062-1103 |
| API - load-dataset端点 | backend/api_gateway/app.py | 1008 |
| API - LoadDatasetReq模型 | backend/api_gateway/app.py | 78 |
| Service - 加载逻辑 | graph_data_service/app.py | 600 |
| 数据集注册表 | graph_data_service/dataset_registry.py | 34 |
| GraphData类 | graph_data_service/graph_data.py | 13 |
| 数据格式文档 | docs/GRAPH_DATA_JSON_FORMAT.md | - |

---

## 7. 常见问题

### Q: 为什么我的数据集没有出现在列表中？

**A**: 检查以下几点：
1. 确认 `datasets/{dataset_id}/meta.json` 存在
2. meta.json 中的 `dataset_id` 必须与目录名匹配
3. 前端已刷新数据集列表（点击"数据集"按钮时自动刷新）
4. 查看浏览器控制台是否有错误

### Q: 加载大数据集很慢，怎么办？

**A**: 
1. 使用NPZ格式而不是JSON（快10-100倍）
2. 确保 graph.npz 在 `datasets/{id}/` 目录中（而不是 versions/）
3. 检查磁盘性能和可用内存

### Q: 如何删除数据集？

**A**: 
1. 打开"数据集"模态框
2. 在想要删除的数据集右侧点击"删除"按钮
3. 确认删除

### Q: 如何版本控制？

**A**: 每次保存数据集都会自动创建新版本：
- 第1次保存：v001
- 第2次保存：v002
- 加载特定版本：`POST /api/sessions/{id}/load-dataset` + `{"version": "v001"}`

---

## 8. 最佳实践

1. **使用有意义的名称**: 避免"test123"，使用"Customer-Network"
2. **添加描述**: 包含数据来源和节点/边的含义
3. **定期备份**: 重要数据集导出到外部存储
4. **选择合适的格式**: 
   - 小图 (<10MB)：JSON
   - 大图：NPZ
5. **确保元数据准确**: node_count 和 edge_count 必须匹配实际数据

---

## 完整流程图

```
┌─────────────────────────┐
│   用户操作              │
├─────────────────────────┤
│ 点击"数据集"按钮         │
└────────────┬────────────┘
             │
             ▼
┌─────────────────────────┐
│  前端 openDatasetModal()│
│  + refreshDatasets()    │
└────────────┬────────────┘
             │
             ▼
┌─────────────────────────────────┐
│  GET /api/datasets              │
│  → 获取数据集列表              │
└────────────┬────────────────────┘
             │
             ▼
┌─────────────────────────────────┐
│  显示数据集列表                 │
│  (每个显示:名称、节点数、边数)   │
└────────────┬────────────────────┘
             │
             ▼
┌─────────────────────────────────┐
│  用户选择数据集，点击"加载"     │
└────────────┬────────────────────┘
             │
             ▼
┌──────────────────────────────────────────────────┐
│  前端 loadDataset(datasetId)                     │
│  POST /api/sessions/{sid}/load-dataset           │
│  Body: { dataset_id: datasetId }                 │
└────────────┬─────────────────────────────────────┘
             │
             ▼
┌──────────────────────────────────────────────────┐
│  API Gateway                                     │
│  1. 恢复或创建 Agent                            │
│  2. 调用 GraphData Service 的 load_dataset      │
└────────────┬─────────────────────────────────────┘
             │
             ▼
┌──────────────────────────────────────────────────┐
│  GraphData Service                               │
│  1. DatasetRegistry.load_graph()                 │
│  2. 从 datasets/{id}/graph.npz 加载              │
│  3. 设置到 GraphSession                         │
│  4. 返回 {success, num_nodes, num_edges}        │
└────────────┬─────────────────────────────────────┘
             │
             ▼
┌──────────────────────────────────────────────────┐
│  前端更新                                        │
│  1. 关闭模态框                                  │
│  2. 添加消息: "已加载数据集 {name}"             │
│  3. 更新图信息角标                              │
│  4. 自动调用 doStreamChat() 开始对话             │
└──────────────────────────────────────────────────┘
```

---

## 总结

**核心概念**:
- 数据集 = 持久化的图数据 + 元数据
- 数据集ID 自动生成，格式 `ds_{uuid[:10]}`
- 支持版本管理和多格式输入
- 完整的CRUD API

**使用流程**:
1. 上传数据 → 2. 保存为数据集 → 3. 从数据集列表加载 → 4. 分析

**推荐格式**:
- 简单场景：边列表 (.txt)
- 标准场景：JSON graph_data.v1
- 大规模：NPZ二进制

**关键文件**:
- UI: `index.html` (行号 2648, 2795, 4032, 4045)
- API: `backend/api_gateway/app.py` (行号 1008)
- Service: `graph_data_service/app.py` (行号 600)
- Registry: `graph_data_service/dataset_registry.py` (行号 34)

