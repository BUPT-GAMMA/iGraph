---
name: data_ingestion_text
description: 从用户直接粘贴的自然语言或其他已支持文档中抽取关系；SQL/CSV/TXT 上传文件必须转至 graph_construction 数据集流程。
metadata:
  version: 3.0.0
  domain: data_ingestion
  risk_level: medium
  igraph:
    requires_tools: [load_graph_from_upload, load_graph_from_text]
    any_tools: [load_graph_from_triples, get_graph_info, job_status, job_result, search_node]
    requires_graph_state: none
    activates_on_intents: [data_ingestion, graph_rag, knowledge_extraction]
---

# 文本数据导入

SQL、CSV、TXT 上传文件属于数据集构图流程，优先读取 `graph_construction` Skill。
ready 数据集可直接复用保存图；构图算法尚未接入时必须明确告知，不调用下述旧上传处理流程，
也不得复制源文件内容到内联文本/三元组工具绕过该限制。

## 适用场景

| 用户输入类型 | 推荐工具 |
|------------|---------|
| SQL、CSV、TXT 上传文件 | `graph_construction` — 检查数据集状态并复用保存图 |
| 其他已支持上传文件 | `load_graph_from_upload` — 自动检测格式 |
| 用户直接粘贴的内联文本 | `load_graph_from_text(text=...)` |
| 用户直接给出的三元组（JSON / 手填） | `load_graph_from_triples` |
| 其他已支持结构化表格 | 转至 data_ingestion_table skill |

**关键原则：SQL/CSV/TXT 使用数据集构图；其他已支持上传文件才使用 `load_graph_from_upload`，不要把文件内容复制到 `text`。**

---

## Step 1 — 上传文件路径

确认不是 SQL/CSV/TXT 数据源后，调用 `load_graph_from_upload(filename=...)`。

工具内部会先经过 LLM 预检（FileFormatPreCheckSubAgent），自动识别：
- **binary / JSON**（.npz / .pt / .json）→ 结构化直接加载，同步返回
- **edge_list**（每行 src dst [weight]）→ 直接解析，同步返回
- **triple**（每行 source relation target）→ 直接解析，同步返回
- **freetext**（散文、文章、文档）→ LLM 预检同时推断 `extraction_goal`，提交异步 text2graph 作业，返回 `job_id`

LLM 预检不可用时自动降级为启发式格式检测（无 extraction_goal 推断）。

若用户还未上传文件，提示上传后再继续。

---

## Step 2 — 处理返回值

### 同步格式（binary / edge_list / triple）

直接获得成功结果，调用 `get_graph_info` 查看规模与结构摘要，向用户汇报。

### 异步格式（freetext）

返回示例：
```json
{ "async": true, "job_id": "xxx", "status": "queued", "format_detected": "freetext" }
```

需轮询直到完成：
```
job_status(job_id="xxx")   → 查看进度（status: queued / running / done / failed）
job_result(job_id="xxx")   → 作业完成后取完整结果
```

轮询间隔建议 2–3 秒，超时约 5 分钟（大文件）。完成后同样调用 `get_graph_info`。

---

## Step 3 — 内联文本路径

仅当用户直接粘贴文本时使用 `load_graph_from_text`：

```
load_graph_from_text(
    text="<用户粘贴的文本>",
    extraction_goal="只抽取公司和人物的投资、任职关系",
    directed=true,
    append=false
)
```

`extraction_goal` 是最重要的质量控制参数，不填时按通用模式抽取，噪声较高。

| 场景 | extraction_goal 示例 |
|------|---------------------|
| 企业关系 | `"只抽取公司、人物之间的投资、任职、合并关系"` |
| 论文知识图 | `"只抽取方法、数据集、评测指标之间的使用和对比关系"` |
| 供应链 | `"抽取供应商、产品、工厂之间的供货和生产关系"` |
| 事件时间线 | `"只抽取事件节点和时序先后关系，忽略人物描述"` |
| 通用（不确定） | 留空，但告知用户噪声可能偏高 |

若用户未给出明确目标，**先询问关注的实体类型和关系类型**，再调用工具。

---

## Step 4 — 合并模式

**append**（默认 `false`）
- 首次建图 → 使用默认 `false`，覆盖当前会话图
- 追加新文档到已有图 → `append=true`，新图与现有图合并去重，不删除已有节点
- 属性图追加限制：当前图为构图属性图时，追加返回 `UNSUPPORTED_PROPERTY_GRAPH_APPEND`，原图保持不变。向用户解释该限制；覆盖或清空图需要用户明确要求，禁止自动切换为 `append=false` 重试。

---

## Step 5 — 解读结果与质量判断

工具返回字段含义：

| 字段 | 含义 | 质量信号 |
|------|------|---------|
| `chunks_processed` | 分块数量 | 长文本应 > 1 |
| `raw_triple_count` | 抽取的原始三元组数 | — |
| `deduped_triple_count` | 去重后三元组数 | 比 raw 低很多说明文本有大量重复叙述 |
| `skipped_count` | 跳过的三元组数 | 持续偏高说明噪声大或 extraction_goal 太宽泛 |
| `num_nodes` / `num_edges` | 最终图规模 | — |
| `sample_triples` | 样本三元组 | **必须查看**，判断抽取语义是否正确 |
| `warnings` | 抽取器警告 | 有警告时向用户说明 |
| `format_detected` | 检测到的格式 | 告知用户文件被识别为何种格式 |

**质量阈值参考**
- `deduped / raw < 0.3`：噪声很高，建议收窄 `extraction_goal` 后重试
- `skipped / raw > 0.5`：格式匹配失败率高，可能是文本语言混杂或结构特殊
- `num_nodes < 3`：抽取失败或文本信息量太少，询问用户是否补充文本

---

## Step 6 — 失败处理

### 抽取结果为空（num_edges = 0）
1. 检查文本是否包含可抽取的关系（纯描述性文字无关系）
2. 尝试提供更具体的 `extraction_goal`（仅内联文本路径有效；上传路径由预检自动推断）
3. 若文件是表格或代码，切换到 `load_graph_from_triples` 或其他 skill

### 样本三元组语义明显错误
1. 向用户展示错误样本，说明问题
2. 建议细化 `extraction_goal`
3. 重新调用，使用 `append=false` 覆盖

### 异步作业失败（status: failed）
- 调用 `job_result` 查看错误详情
- 文本过长：建议用户拆分后分批上传，每批使用 `append=true`
- LLM 错误：告知用户稍后重试

### 格式误判
- 若 `format_detected` 与实际不符（如边列表被误识别为 freetext）
- 说明文件内容，建议转换为标准边列表格式后重新上传

### 想修正部分三元组
引导用户使用 `load_graph_from_triples` 手动补充或覆盖特定关系。

---

## Step 7 — 验证与汇报

调用成功后**必须执行**：
1. 告知 `format_detected`，说明文件被识别为何种格式
2. 汇报 `chunks_processed`、`raw_triple_count`、`deduped_triple_count`、`num_nodes`、`num_edges`
3. 展示 2–3 条 `sample_triples`，让用户确认语义正确
4. 如有 `warnings`，逐条说明原因
5. 若 `append=true`，说明合并后总规模变化

汇报模板（中文）：
> 文件识别为 {format}，已处理 {chunks} 个文本块，原始抽取 {raw} 条三元组，去重后保留 {dedup} 条，建图完成：{nodes} 个节点，{edges} 条边。
> 样本关系：{source} —[{relation}]→ {target}…
> {如有警告，说明}
