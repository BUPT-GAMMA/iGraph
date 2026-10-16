# iGraph "加载数据集" 功能 - 完整搜索索引

## 文档导航

本搜索任务生成了三份完整的文档，满足不同的阅读需求：

### 1. 完整指南文档
**文件**: `LOAD_DATASET_GUIDE.md` (18KB)
**适用人群**: 需要完整理解功能的开发者和架构师

**主要内容**:
- 前端UI位置和操作流程详解
- API接口规范（请求/响应格式）
- 数据格式要求（支持的格式列表）
- 4种数据准备方法
- 后端处理流程（3层架构详解）
- API交互完整流程
- 常见问题Q&A
- 最佳实践建议
- 完整流程图

**章节目录**:
1. 前端UI位置和操作流程
2. API接口规范
3. 数据格式要求
4. 如何准备和加载数据
5. 后端处理流程
6. 相关文件速查
7. 常见问题
8. 最佳实践
9. 完整流程图

---

### 2. 快速参考卡
**文件**: `QUICK_REFERENCE.txt` (5.6KB)
**适用人群**: 需要快速查阅的工程师和集成人员

**主要内容**:
- 快速流程概览
- 关键文件位置速查表
- API接口一览
- 支持格式列表
- 数据集目录结构
- 前端核心函数
- 常见错误处理
- 调试命令集合
- 性能指标
- 最佳实践

**格式特点**:
- 条目清晰，易于快速扫描
- 包含代码片段
- 支持Ctrl+F搜索
- 适合打印或离线查看

---

### 3. 实用示例集
**文件**: `/tmp/practical_examples.md`
**适用人群**: 需要具体示例的开发者和测试人员

**8个完整示例**:
1. 最简单的边列表文件（5个节点）
2. JSON graph_data.v1 格式（4个节点，包含标签）
3. 直接创建数据集（无UI，50个节点）
4. CSV文件加载（4个节点）
5. API调用流程（完整HTTP示例）
6. 自然语言文本转图（7个节点，中文）
7. 版本管理演示
8. 错误处理场景

**示例特点**:
- 从简单到复杂的递进式学习
- 每个示例都有完整的代码
- 包含预期的响应格式
- 覆盖常见使用场景

---

## 快速查询指南

### 我想要...

#### ...快速了解这个功能
→ 读 `QUICK_REFERENCE.txt` 的"快速流程"部分

#### ...了解API如何调用
→ 读 `QUICK_REFERENCE.txt` 的"API接口"部分
→ 或读 `LOAD_DATASET_GUIDE.md` 的"第2章"

#### ...准备一个数据文件
→ 读 `LOAD_DATASET_GUIDE.md` 的"第4章"
→ 或查看 `practical_examples.md` 中的示例1或示例2

#### ...了解数据格式
→ 读 `QUICK_REFERENCE.txt` 的"支持的数据格式"部分
→ 或读 `LOAD_DATASET_GUIDE.md` 的"第3章"

#### ...找前端代码
→ 查 `QUICK_REFERENCE.txt` 的"关键文件位置"
→ 位置: index.html 行 2648, 2795, 4045

#### ...找后端代码
→ 查 `QUICK_REFERENCE.txt` 的"关键文件位置"
→ API: backend/api_gateway/app.py 行 1008
→ Service: graph_data_service/app.py 行 600

#### ...查看完整代码示例
→ 查看 `practical_examples.md` 中对应的示例

#### ...了解错误处理
→ 查 `QUICK_REFERENCE.txt` 的"常见错误"部分
→ 或读 `LOAD_DATASET_GUIDE.md` 的"第7章"

#### ...调试问题
→ 查 `QUICK_REFERENCE.txt` 的"调试命令"部分
→ 或读 `LOAD_DATASET_GUIDE.md` 的"第7章"

#### ...了解性能指标
→ 查 `QUICK_REFERENCE.txt` 的"性能指标"部分

---

## 核心信息速查

### 关键文件位置

| 功能 | 文件 | 行号 |
|------|------|------|
| UI按钮 | index.html | 2648 |
| 加载按钮 | index.html | 2795 |
| 核心函数 | index.html | 4045 |
| API端点 | backend/api_gateway/app.py | 1008 |
| Service | graph_data_service/app.py | 600 |
| Registry | graph_data_service/dataset_registry.py | 34 |

### API端点

```
GET  /api/datasets                       获取列表
POST /api/sessions/{id}/load-dataset     加载
POST /api/sessions/{id}/save-dataset     保存
```

### 数据格式

```
✓ 边列表文本:   A B 1.0
✓ JSON:        schema="graph_data.v1"
✓ NPZ:         NumPy二进制
✓ 自然语言:    自动LLM抽取
```

### 前端函数

```
openDatasetModal()     打开模态框
loadDataset(id)        加载数据集
refreshDatasets()      获取列表
handleSaveDataset()    保存为数据集
```

---

## 完整流程说明

### 用户加载数据集的完整流程

```
1. 用户在UI上点击"数据集"按钮
   └─> openDatasetModal() 触发
   
2. 模态框打开，自动刷新列表
   └─> refreshDatasets() 调用
   └─> GET /api/datasets 请求
   
3. 显示数据集列表
   └─> 每个显示: 名称、节点数、边数、描述
   
4. 用户选择数据集，点击"加载"按钮
   └─> loadDataset(datasetId) 调用
   
5. 发送加载请求到后端
   └─> POST /api/sessions/{sessionId}/load-dataset
   └─> Body: { dataset_id: datasetId }
   
6. 后端处理
   └─> API Gateway 恢复/创建Agent
   └─> GraphData Service 加载图
   └─> Registry 从 datasets/{id}/graph.npz 读取
   
7. 返回成功响应
   └─> { success: true, num_nodes, num_edges, ... }
   
8. 前端更新状态
   └─> 关闭模态框
   └─> 添加消息: "已加载数据集 {name}"
   └─> 更新图信息角标
   
9. 自动开始对话
   └─> doStreamChat() 被调用
   └─> Agent开始分析
```

### 数据准备的完整流程

```
方法一: UI上传（推荐）
  1. 点击"上传"按钮
  2. 选择文件（支持多种格式）
  3. 等待处理
  4. 打开"数据集"模态框
  5. 填写信息
  6. 点击"保存"
  
方法二: 直接创建
  1. mkdir -p datasets/my_dataset/versions
  2. 创建 meta.json
  3. 准备 graph.npz
  4. 应用启动时自动加载
```

---

## 代码导航

### 前端代码结构

```javascript
// 模态框管理
function openDatasetModal()         // 行 4042
function closeDatasetModal()        // 行 4043

// 数据操作
async function refreshDatasets()    // 行 4032
async function loadDataset(id)      // 行 4045
async function handleSaveDataset()  // 行 4066
async function handleDeleteDataset() // 行 4087

// UI渲染相关
state.datasetModalOpen              // 模态框状态
state.datasets                      // 数据集列表
state.datasetLoading                // 加载状态
state.datasetSaving                 // 保存状态
```

### 后端代码结构

```python
# API Gateway
from backend.api_gateway.app import LoadDatasetReq  # 行 78
@app.post("/api/sessions/{session_id}/load-dataset")
async def load_dataset(...)                         # 行 1008

# GraphData Service
def do_load_dataset(session_id, dataset_id, version)  # 行 600

# Dataset Registry
def load_graph(dataset_id, version="latest")        # 行 101
```

---

## 数据格式参考

### 最简单的格式 - 边列表文本

```
Alice Bob 1.0
Bob Charlie 2.5
Charlie Diana 1.2
```

### 标准格式 - JSON graph_data.v1

```json
{
  "schema": "graph_data.v1",
  "num_nodes": 3,
  "directed": true,
  "id2node": {"0": "A", "1": "B", "2": "C"},
  "edge_index": [[0,1,0], [1,2,2]],
  "edge_weight": [1.0, 2.0, 3.0]
}
```

### 数据集元数据 - meta.json

```json
{
  "dataset_id": "ds_xyz",
  "name": "My Dataset",
  "description": "Description",
  "node_count": 100,
  "edge_count": 200,
  "is_directed": true,
  "versions": ["v001"]
}
```

---

## 常见操作命令

### 查看数据集
```bash
ls -la datasets/
cat datasets/ds_xyz/meta.json
```

### 查看NPZ内容
```python
import numpy as np
data = np.load("datasets/ds_xyz/graph.npz", allow_pickle=True)
print(data.files)
print(data['num_nodes'])
```

### 前端调试
```javascript
console.log(state.datasets);
console.log(state.datasetLoading);
loadDataset('ds_xyz');
```

---

## 问题排查

### 数据集不出现
→ 检查 meta.json 是否存在
→ 检查 dataset_id 是否与目录名匹配
→ 重新刷新列表

### 加载失败
→ 查看浏览器控制台错误
→ 查看服务器日志
→ 检查文件是否完整

### 性能问题
→ 改用NPZ格式（比JSON快10-100倍）
→ 检查磁盘空间
→ 查看内存使用

---

## 相关项目文档

- `docs/README.md` - 项目总体文档
- `docs/architecture/GRAPH_DATA_JSON_FORMAT.md` - 详细数据格式说明
- `docs/legacy/RUN_AND_TEST.md` - 运行和测试指南
- `datasets/ReadMe.md` - 数据集使用说明

---

## 总结

本搜索任务提供了关于 iGraph "加载数据集" 功能的完整文档：

1. **LOAD_DATASET_GUIDE.md** - 适合深入学习和理解
2. **QUICK_REFERENCE.txt** - 适合快速查阅和集成
3. **practical_examples.md** - 适合动手实践和调试

三份文档相互补充，覆盖了从入门到精通的所有阶段。

选择适合自己的文档开始阅读，快乐开发！

