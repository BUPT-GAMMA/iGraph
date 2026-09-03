# Skills / Memory / Tools 机制详解（重构前基线）

> 当前 Agent Runtime 已改用 Agno AgentOS 的 LearningMachine、UserProfile、UserMemory、
> SessionContext、LearnedKnowledge、DecisionLog 与 LocalSkills。本文只用于旧数据迁移和
> 行为对照；当前设计请参阅 [`AGENTOS_FOUR_MODULE_REFACTOR.md`](AGENTOS_FOUR_MODULE_REFACTOR.md)。

本文描述 iGraph 重构前的知识层与工具披露机制。设计目标：**事实可控、流程可复用、上下文按需、风险可审计**。

---

## 一、知识三层模型

| 层 | 存放内容 | 典型问题 | 载体 | 注入方式 |
|----|---------|---------|------|---------|
| **Memory** | 稳定事实：用户偏好、环境约束、项目约定 | "以后需要记住什么" | `MemoryEngine` + `MemoryManager` | **全量 frozen snapshot**（session 开始时一次性注入 system prompt，整 session 不变） |
| **Skills** | 可复用流程：任务步骤、工具顺序、踩坑 | "以后遇到同类任务该怎么做" | `SkillRegistry` | **compact index 常驻**（每轮 ≤ 2000 字符）+ `skill_view` 按需加载全文 |
| **Session / Plan** | 过程历史：任务进展、一次性结果 | "之前做到哪里了" | `ContextManager` / `SessionStore` | 会话内消息队列；不写入 Memory |

**Tools 不属于知识层**，但采用相同的"按需披露"思路：LLM sub-agent（`LLMToolSelector`）每 turn 选一次，主 agent 只看入选工具的完整 schema + 核心工具集。

---

## 二、Skills 渐进披露

### 2.1 目录结构

```
backend/agent/skills/
├── models.py             # SkillSpec / SkillCondition 数据类
├── registry.py           # SkillRegistry：扫描 bundled + user + review_queue + frontmatter 解析 + lru 缓存
├── skill_loader.py       # build_skills_system_prompt()（compact index 构建 + 激活过滤）
├── tools.py              # skills_list / skill_view / skill_manage virtual tool handler
├── guard.py              # injection scan（复用 safety.py）+ supporting file 路径保护
├── config_resolver.py    # ~/.igraph/config.yaml 解析 + ${key} 模板替换
├── paths.py              # bundled_skills_dir / user_skills_dir / review_queue_dir / skill_events_path
├── manager.py            # SkillManager：create / write_file / remove_file / approve / reject
├── playbook_reviewer.py  # PlaybookReviewer：validated playbook → review_queue 候选 skill
│
│  ── Bundled skills（11 个，随代码发布）──
├── data_ingestion_table/SKILL.md      # 表格上传、输入格式确认、加载流程
├── data_ingestion_text/SKILL.md       # 文本三元组抽取、实体规范化、建图流程
├── dataset_management/SKILL.md        # 保存/加载/版本/复用 dataset 流程
├── experiment_reporting/SKILL.md      # 分析结果整理为报告流程
├── graph_analysis_centrality/SKILL.md # 中心性算法选择、解释、适用边界流程
├── graph_analysis_community/SKILL.md  # 社区检测算法选择与解释流程
├── graph_ml_inference/SKILL.md        # GraphML 推理配置确认与结果解释流程
├── graph_ml_training/SKILL.md         # GraphML 训练前参数确认与用户确认流程
├── graph_path_connectivity/SKILL.md   # 最短路径、可达性、连通分量分析流程
├── graph_rag/SKILL.md                 # 文档分块、检索、图知识辅助问答流程
└── graph_state_check/SKILL.md         # 图加载状态、规模、属性检查流程
```

**三类 skill 来源**（`SkillRegistry` 统一扫描）：

| 来源 | 路径 | 说明 |
|------|------|------|
| `bundled` | `agent_runtime/resources/skills/*/SKILL.md` | 随代码发布的内置技能（只读，启动时初始化到运行数据目录） |
| `user` | `~/.igraph/skills/user/*/SKILL.md` | 用户自定义技能 |
| `review_queue` | `~/.igraph/skills/.review_queue/*/SKILL.md` | 等待审批的候选技能（playbook 自动生成） |

### 2.2 SKILL.md 标准格式（frontmatter + Markdown）

```yaml
---
name: graph_machine_learning
description: GraphML 任务执行规范。用于 train/infer 参数确认、变更复核与结构可视化。
version: 1.0.0
domain: graph_ml
risk_level: high          # low / medium / high
metadata:
  igraph:
    requires_tools: [train, infer]   # AND 条件
    any_tools: [train, infer, visualize_model_architecture] # OR 条件
    requires_graph_state: none   # none | loaded | weighted | directed
    activates_on_intents: [graph_ml_train, graph_ml_infer]
    config:
      - key: ml.default_epochs      # 对应 ~/.igraph/config.yaml 里的键
        description: 默认训练轮数
---

# Graph Machine Learning Skill
...
```

**激活条件逻辑**：`requires_tools`（全部存在，AND）+ `any_tools`（任一存在，OR）+ `requires_graph_state` + `activates_on_intents`。条件基于**全量可见工具**评估，与 `LLMToolSelector` 入选的子集无关。

**新增技能**：新建目录 + `SKILL.md` 即可，无需修改任何 Python。

### 2.3 系统提示中的 compact index

`build_skills_system_prompt(tool_specs, graph_state=...)` 每轮生成，≤ 2000 字符：

```
## Available Skills
以下是当前可用技能索引。需要完整流程时调用 `skill_view(name)`；
需要查看全部技能或按领域筛选时调用 `skills_list(domain=...)`。

### graph_ml
- **graph_machine_learning**: GraphML 任务执行规范。用于 train/infer 参数确认...

### graph_analysis
- **graph_analysis**: 图状态确认、中心性、社区检测与多步结构分析流程。
```

只列名称和描述，完整内容由 Agent 按需调用 `skill_view`。

### 2.4 Virtual Tools（技能工具）

| 工具 | 用途 |
|------|------|
| `skills_list(domain?)` | 列出可用技能 compact index，可按 domain 过滤 |
| `skill_view(name, file_path?)` | 加载指定 skill 完整 body；`file_path` 非空时返回 supporting file 内容 |
| `skill_manage(action, name, ...)` | 创建/维护/审批用户或候选 skill（不能修改 bundled skills） |

`skills_list` / `skill_view` / `skill_manage` 在 MCP 分发前由 `IGraph._handle_virtual_tool` 截获，**不经过 MCP Server**，不污染图工具目录。两种协议（text / native）下均可用。

**`skill_manage` 支持的 action**：

| action | 说明 |
|--------|------|
| `create` | 在 `user` 或 `review_queue` 目录新建 skill |
| `write_file` | 向已有 skill 写入 supporting file |
| `remove_file` | 删除 supporting file（不能删 SKILL.md） |
| `approve` | 将 review_queue 候选 skill 移至 user skill 目录，并将关联 playbook 标记为 `promoted_to_skill` |
| `reject` | 标记候选 skill 为拒绝（写 REJECTED.json），可选删除文件 |

### 2.5 Skill 配置变量（P1.4）

SKILL.md frontmatter 中的 `config` 字段声明需要的配置键：

```yaml
config:
  - key: ml.default_epochs
  - key: ml.device
```

用户在 `~/.igraph/config.yaml` 中配置：

```yaml
skills:
  config:
    ml.default_epochs: 200
    ml.device: cuda
```

`skill_view` 返回 body 前，自动将 `${ml.default_epochs}` 替换为 `200`。配置文件不存在时变量保留原样。

### 2.6 Playbook → Skill 晋升管道（PlaybookReviewer）

`PlaybookReviewer`（`playbook_reviewer.py`）是 Memory 与 Skills 之间的自动化桥梁，将经验证的 playbook 转化为可复用的候选 skill。

**触发时机**：`MemoryEngine.maybe_update_from_turn()` 在 `_apply_playbook_operations()` 之后，对生命周期状态为 `validated` 且尚未生成候选 skill 的 playbook 自动触发。

**Playbook 生命周期状态**：

```
candidate  →  validated  →  promoted_to_skill
                │
                └─ PlaybookReviewer.review_snapshot()
                      → SkillManager.create(destination="review_queue")
                      → playbook.candidate_skill_name = "<name>"
```

| 状态 | 条件 | 说明 |
|------|------|------|
| `candidate` | 初始状态 | 证据 < 2 次，未验证 |
| `validated` | evidence_count ≥ 2 或 hit_count ≥ 2 | 足够多次验证，可以晋升为候选 skill |
| `promoted_to_skill` | `approve` 后 | 已移入 user skill 目录，playbook 归档 |

**生成的候选 skill 目录**：写入 `~/.igraph/skills/.review_queue/<name>/`，包含：
- `SKILL.md`：从 playbook 的 `problem_type` / `approach` / `tool_sequence` 自动渲染
- `metadata.json`：记录 `playbook_id`、`evidence_sessions`、`tool_sequence` 等来源信息

**用户审批流程**：
1. LLM 或用户通过 `skill_manage(action="approve", name="<candidate>")` 将候选 skill 移至 user 目录
2. 关联 playbook 自动标记为 `promoted_to_skill`，不再出现在记忆快照
3. skill 进入 `SkillRegistry` 可见范围，生效于下次 session

**审计事件**（写入 `~/.igraph/skills/skill_events.jsonl`）：`skill_created` / `skill_file_written` / `skill_approved` / `skill_rejected` / `skill_write_rejected` 等。

### 2.7 安全约束（guard.py）

- **注入扫描**：所有 skill body（包括 `skill_manage` 创建时）通过 `scan_skill_content()`（复用 `safety.py`）检测注入 pattern，命中则拒绝加载。
- **路径保护**：`skill_view(name, file_path=...)` 和 `skill_manage(action="write_file", ...)` 均通过 `validate_supporting_file()` 阻止路径穿越（`../../etc/passwd`）和 SKILL.md 自引用。
- **bundled 保护**：`SkillManager` 只写入 `user` 或 `review_queue` 目录，无法修改 `bundled` 目录下的技能。

---

## 三、Memory Frozen Snapshot

### 3.1 架构层次

```
MemoryManager
  ├── BuiltinMemoryProvider ──→ MemoryEngine（核心读写）
  │     ├── long_term_memory.json    # 长期事实（category: preference/goal/project/…）
  │     ├── user_profile.json        # 用户档案（category: profile，独立存储）
  │     ├── problem_solving_playbooks.json  # 题型经验（lifecycle: candidate/validated/promoted_to_skill）
  │     └── memory_events.jsonl     # 审计日志（原子 append + flock）
  └── OptionalExternalProvider（最多 1 个，失败不阻塞）

MemoryService（REST governance wrapper，供 API Gateway /api/memory/* 端点调用）
  └── MemoryEngine（委托）
```

**关键文件位置**：`backend/agent/memory/`

| 文件 | 职责 |
|------|------|
| `memory_engine.py` | 核心读写引擎，管理三个 JSON 文件；集成 PlaybookReviewer 触发候选 skill 生成 |
| `manager.py` | `MemoryManager`：编排 builtin + extra provider |
| `provider.py` | `MemoryProvider` ABC |
| `builtin_provider.py` | `BuiltinMemoryProvider`，委托 MemoryEngine |
| `scope.py` | `MemoryScope` 数据类（user_id / workspace_id / project_id / dataset_id） |
| `paths.py` | `MemoryPathResolver`：按 scope 解析存储路径（workspace/user/project/dataset 层次） |
| `user_profile.py` | `UserProfile` 数据类 + `load_from_items` + `to_snapshot_lines` |
| `tools.py` | `memory_read` / `memory_delete` virtual tool handler |
| `safety.py` | 注入检测（INJECTION_PATTERNS） |
| `store.py` | `atomic_json_write`（temp+fsync+os.replace）+ `append_jsonl`（flock） |
| `service.py` | `MemoryService`：REST governance 层（list/write/update/delete/events/usage） |

**`MemoryPathResolver`**（`paths.py`）：

```python
class MemoryPathResolver:
    root: Path  # 默认 ~/.igraph/memory（IGRAPH_MEMORY_ROOT 可覆盖）

    def scope_dir(scope)    → root / workspace_id / user_id
    def profile_path(scope) → scope_dir / "profile.json"
    def project_memory_path(scope) → scope_dir / "projects" / project_id / "memory.json"
    def dataset_memory_path(scope) → scope_dir / "datasets" / dataset_id / "memory.json"
    def playbooks_path(scope) → scope_dir / "playbooks.json"
    def events_path(scope)  → scope_dir / "events.jsonl"
```

路径完全由 `MemoryScope` 字段决定，`MemoryEngine` 不再硬编码 `default_user/` 目录。

### 3.2 Frozen Snapshot 模式

```python
# 会话开始时（IGraph.__init__）调用一次：
_memory_prompt_block = memory_manager.load_and_freeze(DEFAULT_SCOPE)
# → 调用 MemoryEngine.load_and_freeze()
# → 构建 _snapshot_block，冻结 _snapshot_frozen=True
```

**整个 session 期间 `_memory_prompt_block` 不变**，保护 prefix cache。后台写入（memory_write 或 sub-agent 更新）只修改内存状态 + 磁盘文件，**不刷新本 session 的 system prompt**，下次 session 生效。

### 3.3 User Profile（用户档案层）

用户档案（`category: profile` 操作）路由到 `user_profile.json`，与长期记忆独立存储：

```json
{
  "items": [
    {"key": "language", "value": "zh-CN"},
    {"key": "expertise_level", "value": "expert"}
  ]
}
```

`UserProfile` 数据类的 `to_snapshot_lines()` 按字段优先级截断（`language/expertise_level` > `communication_style` > `domain_knowledge` > `known_constraints` > `custom_facts`），上限 1500 字符。

在 snapshot 中渲染为：

```
## 用户档案（frozen memory snapshot）
- language: zh-CN
- expertise_level: expert
```

### 3.4 字符预算

| 内容 | 上限（字符） | 溢出策略 |
|------|-----------|---------|
| `user_profile` | 1500 | 按字段优先级截断 |
| `long_term_memory` | 8000 | `confidence × 0.5 + recency × 0.3 + hit_rate × 0.2` 降序裁剪 |
| `playbooks` | 4000 | 同上（只保留 `active` 状态） |
| `skills compact index` | 2000 | 按 domain 折叠超额技能 |

### 3.5 写入安全

- **原子写入**：所有 JSON 文件经 `atomic_json_write()`：temp file → flush → fsync → `os.replace`，崩溃不产生半截文件。
- **JSONL 锁**：`memory_events.jsonl` append 时用 `fcntl.flock` + fsync。
- **注入扫描**：upsert 的 key/value/summary 和 playbook 的 approach/trigger_terms 通过 `scan_memory_content()` 检查，命中 INJECTION_PATTERNS 时写 `memory_reject` 事件，不入快照：

  ```python
  INJECTION_PATTERNS = [
      r"ignore (all )?previous instructions?",
      r"you are now",
      r"disregard (all )?previous",
      r"system prompt",
      r"<\|.*?\|>",
      r"[​-‏﻿]",   # 不可见 Unicode
      r"(curl|wget).*(ANTHROPIC|API_KEY|\.env)",
      r"cat\s+.*\.env",
  ]
  ```

### 3.6 Memory 子代理写入流程

每轮对话结束后，`_schedule_memory_update` 异步调用 `MemoryManager.sync_turn()`，由此触发 `MemoryEngine.maybe_update_from_turn()`：

```
maybe_update_from_turn()
  → _build_sub_agent_payload()   # 包含：recent_messages / existing_memories
                                  # existing_profile / existing_playbooks
  → LLM sub-agent 判断 JSON 决策 {should_write, operations, ...}
  → _apply_operations(current, ops, profile_snap=profile_current)
      ├─ category == "profile" → _apply_profile_upsert → user_profile.json
      └─ 其他 category        → _apply_upsert       → long_term_memory.json
  → _apply_playbook_operations  → problem_solving_playbooks.json
      ├─ _apply_playbook_upsert: 更新 evidence/hit_count，推进 lifecycle_status
      │     candidate → validated（evidence_count≥2 或 hit_count≥2）
      └─ _normalize_playbook_lifecycle: 确保 lifecycle_status 与 status 一致
  → PlaybookReviewer.review_snapshot(playbook_snap)   ← 仅当有 validated 条目时触发
      └─ 对 lifecycle_status=="validated" 且无 candidate_skill_name 的 playbook:
            SkillManager.create(destination="review_queue") → 生成候选 skill
            playbook.candidate_skill_name = "<name>"
            emit "candidate_skill_generated" event
  → _prune_snapshot             # 容量控制（active ≤ 120，playbook ≤ 80）
  → _save_*_snapshot            # 原子写入
  → _append_event               # 审计日志
```

`existing_profile` 单独作为 payload 字段传入，sub-agent 看到已有档案，避免重复写入。

### 3.7 Memory Virtual Tools

| 工具 | 描述 |
|------|------|
| `memory_read(scope, query?, limit?)` | 按关键词评分查询当前 long_term / playbook / user_profile 条目；不依赖 embedding |
| `memory_delete(scope, memory_id, reason?)` | 软删除（status → archived）；持久化 + 写审计事件 |

`memory_read` 是**核心工具集**成员（`CORE_TOOL_NAMES`），每轮自动加载，无需 selector 选中。

这两个工具在 MCP 分发前截获，与 skill tools 同属 **agent-level virtual tools**。

---

## 四、Tools LLM Selector

### 4.1 架构

```
backend/agent/tools/
├── selector.py    # LLMToolSelector：主接口（async select + keyword fallback）
└── core_set.py    # CORE_TOOL_NAMES（5 项，永不进入 selector 裁剪）
```

旧的 `backend/agent/embedding.py`（`ToolSelector` + sentence-transformers）已删除，零向量库依赖。

### 4.2 核心工具集（常驻，不参与 selector 选择）

```python
CORE_TOOL_NAMES = {
    "get_graph_info",   # 图状态确认，几乎每轮必用
    "skills_list",      # 技能发现
    "skill_view",       # 技能全文加载
    "tool_expand",      # 主 agent 逃生通道（加载 selector 漏选的工具 schema）
    "memory_read",      # 记忆查询
}
```

### 4.3 两段披露

```
1. 每 turn 开始：set_turn_id(uuid)  ← 清空 per-turn 缓存
2. LLMToolSelector.select(req, k=8)
     输入：user_input + graph_state + detected_intent + compact tool catalog
     LLM 返回：{selected: [...], reasoning: "..."}
     核心工具合并：CORE_TOOL_NAMES ∪ selected
3. 主 agent 只看：入选工具的完整 schema + 核心工具集
```

**Per-turn 缓存**：`set_turn_id(turn_id)` 在每次 `_chat_stream_impl` 入口调用。同一 turn 内多步 react 循环复用同一次决策；新 turn 清空缓存，重新选择。

**Fallback**：LLM selector 超时/解析失败时回退到关键词分词匹配（`_keyword_select`），**不**返回全量工具，保持 token 可控。

**`tool_expand(name)` 逃生通道**：selector 漏选时，主 agent 可主动加载任意工具的完整 schema，加入本 turn 后续步骤。

### 4.4 审计日志

每次 selector 调用（包括 fallback）写入 `logs/tool_selection_events.jsonl`：

```json
{
  "ts": "...", "user_input_digest": "abc123",
  "graph_state": "graph_loaded=true, nodes=1200, ...",
  "candidate_tools": [...], "selected_tools": [...],
  "reasoning": "...", "latency_ms": 320,
  "model_version": "...", "fallback": false
}
```

---

## 五、Prompt 组装顺序

```
[1] Role system prompt（角色职责 + 输出协议，sessions[0]，整 session 不变）
[2] Reasoning mode rule（staged / direct 规则，每轮动态注入 system msg）
[3] Memory frozen snapshot（user_profile + long_term + playbooks，整 session 不变）
[4] Selected tools schema（text 协议写入 system；native 协议走 tools 参数）
[5] Skills compact index（≤ 2000 字符，基于全量可见工具 + 图状态 + intent）
[6] 对话历史（user / assistant / tool_observation）
```

注意：**[3] 和 [5] 基于不同的来源**。[3] 在 session 开始时冻结，中途不变；[5] 每轮根据当前图状态和可见工具动态计算，但有 LRU 内存缓存。

Skills 激活条件基于 **全量可见工具**（`_visible_tool_specs`），不受 selector 入选子集影响——即便 selector 未选中 `train`，GraphML 技能仍出现在 compact index，主 agent 可调用 `skill_view` → `tool_expand("train")` 完成任务。

---

## 六、存储路径一览

```
~/.igraph/memory/
└── {workspace_id}/               # 默认 default_workspace
    └── {user_id}/                # 默认 default_user
        ├── profile.json          # 用户档案（对应旧路径 user_profile.json）
        ├── playbooks.json        # 题型经验（lifecycle: candidate/validated/promoted_to_skill）
        ├── events.jsonl          # 审计日志（写入/拒绝/迁移等事件）
        ├── projects/
        │   └── {project_id}/memory.json   # 项目级记忆（可选）
        └── datasets/
            └── {dataset_id}/memory.json   # 数据集级记忆（可选）

~/.igraph/skills/
├── user/                         # 用户自定义 skill（skill_manage create destination=user）
│   └── {skill_name}/SKILL.md
├── .review_queue/                # PlaybookReviewer 自动生成的候选 skill
│   └── {skill_name}/
│       ├── SKILL.md
│       └── metadata.json        # 来源：playbook_id / evidence_sessions 等
└── skill_events.jsonl            # Skill Manager 操作审计日志

~/.igraph/
└── config.yaml                   # Skill 配置变量（可选）

logs/tool_selection_events.jsonl  # Tool selector 审计日志

# 遗留路径（自动迁移）
backend/agent/memory/default_user/   ← 首次启动时迁移到 ~/.igraph/memory/
backend/agent/memory/memory/default_user/   ← 同上
```

**环境变量覆盖**：`IGRAPH_MEMORY_ROOT` 可将记忆根目录重定向（`MemoryPathResolver.__init__` 优先读取此变量）；`IGRAPH_HOME` 可重定向 `~/.igraph`。

---

## 七、MemoryService REST API

`MemoryService`（`backend/agent/memory/service.py`）是对 `MemoryEngine` 的治理封装，由 `ApiGatewayServer` 实例化，通过以下 HTTP 端点暴露给外部管理工具（调试面板、运维脚本等）：

| 方法 | 路径 | 功能 |
|------|------|------|
| GET | `/api/memory` | 查询记忆条目（`scope` / `query` / `limit` 参数） |
| POST | `/api/memory` | 手动写入记忆条目（`scope` / `key` / `value` / `category` / `confidence`） |
| PATCH | `/api/memory/{memory_id}` | 更新指定条目 |
| DELETE | `/api/memory/{memory_id}` | 软删除（status → archived） |
| GET | `/api/memory/events` | 查看 Memory 审计日志（最多 `limit` 条） |
| GET | `/api/memory/usage` | 快照使用情况（已用字符数 / frozen 状态 / 已纳入/排除 ID 列表） |

**注意**：这些端点绕过 sub-agent 写入决策，直接操作 `MemoryEngine`，仅用于调试和人工干预，不应在对话流程中调用。SFT 数据导出（`/api/export/sft`）内部也使用 `MemoryService.events()` 读取写入历史。
