# iGraph Skills / Memory / Tools 迭代计划（综合版 v2）

> 综合来源：`hermes_skills_memory_plan.md`（结构化对比+代码示例）与 `hermes_skills_memory_plan_codex.md`（深度架构分析+领域拆解），并结合 v2 新方向调整。
> 基线代码：iGraph `backend/agent/skills/`、`backend/agent/memory/`、`backend/agent/context/`、`backend/agent/igraph.py`、`backend/agent/embedding.py`、`backend/agent/planner.py`。
> 日期：2026-04-21
> 目标：吸收 Hermes 在长期知识治理上的成熟经验，压缩成适合 iGraph 图分析场景的轻量领域架构——**事实可控、流程可复用、上下文按需、风险可审计**。

---

## 0. v2 方向调整（与 v1 的关键差异）

本版按以下新方向全面重写，请先阅读：

| 维度 | v1 方向 | v2 方向 | 核心理由 |
|------|--------|--------|---------|
| **选择机制的底座** | 语义 embedding（sentence-transformers）+ RRF 混合召回 | **彻底去掉 embedding**，改用 LLM sub-agent 或渐进披露 | 减少依赖、减少隐形误差、决策可解释、召回/选择能感知图状态与用户意图 |
| **Memory 选择** | 语义+关键词混合 recall，Top-K 注入 | **不做选择，全量加载** 为 frozen snapshot | 当前 memory 规模小（KB 级），召回不如全载直接；召回误差风险 > 全载 token 成本 |
| **Skills 选择** | 硬编码工具条件判断 | **Hermes 式渐进披露**：compact index 常驻 → main agent 看摘要 → 调用 `skill_view` 按需加载 | 决策权交给 main agent，上下文可控、可审计 |
| **Tools 选择** | embedding-based `ToolSelector`（`backend/agent/embedding.py`） | **LLM sub-agent tool selector**：看工具描述列表 → 返回候选工具名 → 主 agent 只看入选工具的完整 schema | 工具数量增长时 token 可控；sub-agent 可理解任务上下文和图状态 |
| **embedding.py** | 保留扩展 | **删除**（`ToolSelector` + `embedding_provider` 全部移除） | 零 embedding 依赖 |

**不变的设计原则**
- 知识三层分离：memory（事实）/ skills（流程）/ session（进展）。
- skills 与 memory 的 virtual tools 仍由 iGraph 在 MCP 分发前截获。
- bundled vs user 分离、安全扫描、原子写入、字符预算、provider 抽象。

---

## 1. 事实澄清

| 议题 | 前置文档声明 | 实际代码 | 采用结论 |
|------|------------|---------|---------|
| `skill_loader.py` 路径 bug | codex 文档断言存在路径 bug | **用户确认已修复** | 无需改路径；P0 只做 golden test 覆盖 |
| Memory 存储路径重影 | 两文档未提及 | 仓库中同时存在 `memory/default_user/` 与 `memory/memory/default_user/`，后者 `memory_events.jsonl` 最新最大（64KB） | 仍需 P0 定位真实读写路径并清理重影 |

其余差距判断（技能条件硬编码、frontmatter 未解析、无渐进披露、无 provider 抽象、无安全扫描、无原子写、作用域固定为 `default_user`、playbook 与 skill 边界不清）两份文档与代码一致，继续纳入计划。

---

## 2. 知识三层模型 & 选择机制总览

### 2.1 知识分层

| 层 | 存放内容 | 问答句 | iGraph 载体 | 注入方式 |
|----|---------|--------|---------------|---------|
| **Memory** | 稳定事实：用户偏好、环境约束、项目约定、dataset 约束 | "以后需要记住什么事实" | `MemoryEngine` + 未来 MemoryProvider 栈 | **全量 frozen snapshot**（system prompt 静态块） |
| **Skills** | 可复用流程：任务步骤、工具使用、踩坑、验证方式 | "以后遇到同类任务该怎么做" | `skills/` 目录 + SkillRegistry | **compact index 常驻**（~50–200 tokens）+ `skill_view` 按需加载 |
| **Session / Plan** | 过程历史：任务进展、一次性结果、会话细节 | "之前那次做到了哪里" | `context_manager`/`SessionStore`/plan artifacts | 会话内消息队列；不进 memory |

**Tools 不属于知识层，但采用相同的按需加载思路**：LLM sub-agent 选一次（每 turn 或按需），主 agent 只看入选工具的完整 schema。

### 2.2 选择机制总图

```
每个 user turn 到达时：
  ┌─────────────────────────────────────────────────────────┐
  │ 1. Memory：全量 frozen snapshot 已在 system prompt      │
  │    （会话开始时一次性注入，中途写入只更新磁盘+内存，    │
  │     不刷新 system prompt，保护 prefix cache）           │
  ├─────────────────────────────────────────────────────────┤
  │ 2. Skills：compact index 已在 system prompt            │
  │    main agent 自己判断是否调用 skill_view(name)         │
  │    （不使用 embedding，不使用 sub-agent，主 agent 自决） │
  ├─────────────────────────────────────────────────────────┤
  │ 3. Tools：LLM tool-selector sub-agent 单次调用          │
  │    输入：user 输入 + 紧凑工具描述列表 + 图状态摘要       │
  │    输出：候选工具名列表（JSON）                         │
  │    主 agent prompt：仅加入入选工具的完整 schema         │
  └─────────────────────────────────────────────────────────┘
```

---

## 3. 核心差距归纳（v2 更新）

### 3.1 Skills 核心差距
1. **条件硬编码**：`skill_loader.py` 通过固定工具名集合判断，新增技能必须改 Python。
2. **Frontmatter 解析缺失**：只有 `_strip_frontmatter` 而没有 schema、name、description、platform、metadata。
3. **无渐进披露**：技能全文一次性拼进 system prompt。
4. **无 `skills_list` / `skill_view` / `skill_manage`**：模型无法主动发现、加载、维护技能。
5. **激活条件与 ToolSelector 耦合**：当前 text protocol 下先缩小 active tools 再判断技能；selector 漏选会连带隐藏技能（去 embedding 后仍需注意 LLM selector 漏选的同类问题）。
6. **无扩展框架**：无用户目录、无版本、无平台过滤、无审计日志。
7. **无安全治理**：无 injection scan、path guard、支持文件目录约束。
8. **技能和 playbook 边界不清**：缺少晋升/退出机制。

### 3.2 Memory 核心差距
1. **作用域固定为 `default_user`**：不支持多用户/项目/dataset/workspace。
2. **无用户档案层**：用户偏好、习惯与 agent 笔记混存。
3. **存储路径重影**：`memory/default_user/` 与 `memory/memory/default_user/` 并存。
4. **无 MemoryProvider 抽象**：无法叠加外部记忆服务。
5. **~~动态 recall 破坏 prefix cache~~ → v2 改为全量 frozen snapshot**，此问题自然消失。
6. **写入无安全扫描**。
7. **文件写入非原子**：`open("w") + json.dump`，异常/并发可能产生半截文件。
8. **无字符预算**：`long_term_memory.json` 无上限，全量加载模式下必须限制总字符数。
9. **用户不可治理**：无读/编辑/删除/溯源 API 与 UI。

### 3.3 Tools 核心差距（新增，v2 专门章节）
1. **embedding-based 选择器**：`backend/agent/embedding.py` 的 `ToolSelector` 使用向量相似度筛选工具，不可解释、无法感知图状态、冷启动困难。
2. **Planner 硬依赖**：`planner.py` 的 `_tool_selector.select(query, k=5)` 深度耦合 embedding。
3. **大工具目录时 token 膨胀**：所有工具 schema 直接注入（text protocol）或全量传入（native FC）。
4. **无"核心工具集"概念**：每次都重新筛选，即便是 `get_graph_info` 这种近乎必用工具也会被选漏。
5. **无审计**：无法回看"这一轮为什么选了/没选某个工具"。

---

## 4. 设计原则（v2 最终版）

### 4.1 Skills
1. **声明式触发**：技能通过 frontmatter 声明激活条件，不在代码中硬判断。
2. **渐进披露**：compact index 常驻（name + description + 触发概要），全文按需（`skill_view`），supporting files 仅列路径。
3. **激活条件解耦选择器**：skill 可见性基于**全量可见工具 + 图状态 + intent**，而非 tool-selector 选中子集。
4. **文件即配置**：新技能只需新建目录 + `SKILL.md`，无需改 Python。
5. **Agent-level virtual tools**：`skills_list` / `skill_view` / `skill_manage` 在 MCP 分发前截获。
6. **安全默认开启**：所有进入 prompt 的技能内容经 injection scan 与路径校验。
7. **bundled 与 user 分离**：bundled 只读，user 与 review-queue 可写。

### 4.2 Memory
1. **事实/流程分离**：稳定事实进 memory；工具流程进 skill；会话进展进 session。
2. **全量加载 + frozen snapshot**：会话开始时加载所有 memories + user_profile + playbooks 到 system prompt 静态块；中途写入仅更新内存+磁盘，不刷新 system prompt。
3. **字符预算强制**：long_term_memory / playbooks / user_profile 各有上限（见 4.5），超过按置信度+新鲜度+命中率三元评分裁剪。
4. **原子写入+安全扫描**：所有持久化走 temp + fsync + `os.replace`；所有写入先过 injection scan。
5. **多作用域**：user / project / dataset / session。
6. **用户可治理**：读/编辑/删除/溯源 API 与事件日志。
7. **Provider 抽象**：内置 provider 始终存在；最多 1 个外部 provider，失败不阻塞。

### 4.3 Tools
1. **LLM sub-agent 替代 embedding selector**：`backend/agent/embedding.py` 删除，改为 `tools/selector.py` + 小模型调用。
2. **两段披露**：compact tool catalog（name + 一行描述）→ LLM selector 返回候选 → 主 agent 只看入选工具完整 schema。
3. **核心工具集常驻**：`get_graph_info` 这类几乎必用的工具永远入选，不参与 selector 裁剪，防止漏选。
4. **selector 可扩展**：主 agent 可通过 virtual tool `tool_expand(name)` 主动加载 selector 未选中的工具完整 schema。
5. **缓存 + 稳定性**：同一轮内 selector 结果可缓存；用户输入相似度简单 hash 比较（不是 embedding）决定是否复用上轮结果。
6. **可审计**：每次 selector 决策记录到 `tool_selection_events.jsonl`（输入、候选集、选中集、耗时、模型版本）。
7. **Fallback**：LLM selector 失败或超时时，回退到"加载所有工具"（简单可靠）。

### 4.4 保留 iGraph 既有优势
- **Playbook 机制**（Hermes 无）：改为 skill 候选池角色继续保留。
- **Sub-agent 异步 memory 写入**：不阻塞主 react loop，继续保留。
- **图状态感知**：节点/边数量、有向/加权等元数据作为 skill 条件 & tool selector 输入。

### 4.5 字符预算（v2 在全量加载模式下的关键）

| 内容 | 上限（字符） | 溢出策略 |
|------|-----------|---------|
| `user_profile` | 1500 | 按字段优先级截断（language/expertise > custom_facts） |
| `long_term_memory` | 8000 | `confidence × recency × hit_rate` 综合排序裁剪 |
| `playbooks` | 4000 | 同上（只保留 `validated` 状态） |
| `skills compact index` | 2000 | 按 domain 分组，低相关 domain 折叠为"..等 N 个，调用 skills_list 查看" |
| `tool selector 候选集` | N 个工具（默认 N=8） | 超过时保留 selector top-N + 核心工具集 |

**总 static system prompt 上限（目标）**：~16000 字符（≈ 5000 tokens），确保 prefix cache 有显著复用价值。

---

## 5. 分阶段实施计划

### P0：基线修复与 golden tests（1–2 天）

目标：以最低成本让现有 skills / memory 可回归、可测试、可观测。路径 bug 已修，P0 聚焦其他底线。

- **P0-1 Memory 存储路径唯一化**
  - 定位 `memory/default_user/` vs `memory/memory/default_user/` 中真正被读写的那一份。
  - 把非真实路径合并/归档到 `legacy_backup/`，代码中的引用统一指向唯一路径。
  - 结论落到 `docs/legacy/SKILLS_MEMORY.md` 或 RUNBOOK。

- **P0-2 Golden tests 建档**
  - Skills：给定 tools 列表，断言 system prompt 出现期望技能；断言判断使用*全量可见工具*而非 selector 子集。
  - Memory：给定 snapshot，断言全量加载内容格式与顺序稳定；断言 snapshot 写入在异常退出后仍为合法 JSON。
  - Tools：mock LLM selector 输出，断言主 agent prompt 中只包含入选工具 schema + 核心工具集。

- **P0-3 原子写入**
  - 新增 `_atomic_json_write(path, data)`：`temp + flush + fsync + os.replace`。
  - `long_term_memory.json`、`problem_solving_playbooks.json`、`user_profile.json`（新）统一走该函数。
  - `memory_events.jsonl` 保持 append，多进程场景加 `fcntl` 文件锁（TODO 可延后）。

- **P0-4 Memory 注入安全扫描**
  ```python
  INJECTION_PATTERNS = [
      r'ignore (all )?previous instructions?',
      r'you are now',
      r'disregard (all )?previous',
      r'system prompt',
      r'<\|.*?\|>',
      r'[​-‏﻿]',                    # 不可见 Unicode
      r'(curl|wget).*(ANTHROPIC|API_KEY|\.env)',
      r'cat\s+.*\.env',
  ]
  ```
  对 memory upsert 的 `key/value/summary` 与 playbook 的 `approach/trigger_terms` 统一扫描。命中记录 `memory_reject` 事件，不写入。

- **P0-5 字符预算最简实现**
  按 4.5 表格实现 `_enforce_char_limit(items, limit)`；全量加载时超额自动裁剪。

**验收标准**
- Memory 路径唯一，代码引用一致。
- 三类技能在对应工具可见时真实注入（golden test 证明）。
- snapshot 写入异常退出不产生半截 JSON。
- 注入样本（"ignore previous instructions"）被 `memory_reject`。
- 现有 `tests/test_memory_engine.py`、`tests/test_context_manager.py`、`tests/test_planner.py` 全部通过。

---

### P1：Skills Core 渐进披露（1–2 周）

目标：从"硬编码提示块"升级为"可发现、可按需加载、可治理"的技能系统。

**模块规划**
```
backend/agent/skills/
├── models.py        # SkillSpec / SkillCondition / SkillReadiness
├── registry.py      # SkillRegistry: discover / cache / filter
├── loader.py        # 构建 compact index / 激活过滤
├── tools.py         # skills_list / skill_view / skill_manage handler
├── guard.py         # injection scan / path validation / size checks
├── manager.py       # create / edit / patch / write_file / remove_file
└── [bundled + user skills 目录]
```

#### P1.1 SkillSpec & frontmatter 标准化

```yaml
---
name: graph-machine-learning
description: GraphML 训练、推理、配置确认与模型结构解释流程。
version: 1.0.0
domain: graph_ml
risk_level: high
supports_protocols: [text, native]
metadata:
  igraph:
    tags: [graph-ml, training, inference]
    requires_tools: [get_train_config, get_infer_config]
    any_tools: [train, infer]
    fallback_for_tools: []
    requires_graph_state: loaded   # none|loaded|weighted|directed|with_node_features|with_labels
    activates_on_intents: [graph_ml_train, graph_ml_infer]
    related_skills: [graph-analysis, dataset-management]
    config:
      - key: ml.default_epochs
        description: 默认训练轮数
        default: "100"
---
```

- frontmatter 校验失败不崩溃，记录具体文件到日志。
- `requires_tools` 为 AND，`any_tools` 为 OR，`fallback_for_tools` 为"主工具不可用时才显示"。
- 条件评估基于**全量可见工具 + 图状态 + intent**，与 tool selector 结果无关。

#### P1.2 渐进披露（Hermes 式）

- **Compact skill index**（每轮注入 system prompt，≤ 2000 字符）：
  ```
  ## Available Skills
  
  ### graph_ml
  - **graph-ml-training**: GraphML 训练流程与参数确认
  - **graph-ml-inference**: GraphML 推理与结果解释
  
  ### graph_analysis
  - **graph-analysis-centrality**: 中心性算法选择与解释
  - **graph-analysis-community**: 社区检测算法与联动分析
  - ...等 3 个，调用 `skills_list(domain="graph_analysis")` 查看全部
  
  使用：调用 `skill_view(name)` 加载完整指引；
  supporting files 用 `skill_view(name, file_path=...)` 按需加载。
  ```

- **Agent-level virtual tools**：
  ```
  LLM tool call
    ├─ skills_list / skill_view / skill_manage → iGraph 本地 handler
    └─ 其他图工具 → MCP → GraphData
  ```
  text / native 两种驱动下均可用。

- **缓存两层**：
  - L1：进程内 LRU（key = skills_dirs + visible_tools + graph_state hash）。
  - L2：磁盘快照（各 `SKILL.md` mtime+size manifest），位置 `~/.igraph/.skills_prompt_snapshot.json`。

#### P1.3 激活条件与选择器解耦（v2 强调）

- `build_skills_system_prompt()` 输入从"active tools / selector 结果"改为"**visible tools + graph state + intent**"。
- Tool selector（P2）只决定*工具 schema 注入列表*，不决定技能可见性。
- 即便 selector 未选中 `train`，GraphML 训练技能仍出现在 compact index，允许主 agent 主动 `skill_view` → `tool_expand("train")`。

#### P1.4 Skill 配置变量系统

- frontmatter 中 `metadata.igraph.config` 声明配置项。
- `~/.igraph/config.yaml` 解析：
  ```yaml
  skills:
    config:
      ml.default_epochs: 200
      ml.device: cuda
  ```
- `skill_view` 返回内容前替换 `${ml.default_epochs}` 等模板变量。

**验收标准**
- 新增技能只需 `mkdir + SKILL.md`，无需改 Python。
- Compact index 在默认 prompt 中 token 占用明显下降（可量化）。
- 训练任务即便 tool selector 初始未选中 `train`，GraphML skill 仍出现在索引中。
- text/native 两种协议下均可调用 virtual skills tools。

---

### P2：Tools LLM Selector（替换 embedding，1–2 周）

目标：删除 `backend/agent/embedding.py`，用 LLM sub-agent 替代 embedding-based `ToolSelector`，实现工具渐进披露。

**模块规划**
```
backend/agent/tools/
├── selector.py          # LLMToolSelector：主接口
├── catalog.py           # 工具描述摘要生成（name + one-liner）
├── prompts.py           # selector 系统提示模板
├── core_set.py          # 核心工具集定义（always-on）
└── virtual_tools.py     # tool_expand virtual tool handler
```

#### P2.1 LLMToolSelector

```python
@dataclass
class ToolSelectionRequest:
    user_input: str
    graph_state_summary: str        # "节点 1.2k, 边 3.5k, 有向加权, 无标签"
    detected_intent: str | None     # 来自 query_analyzer
    conversation_recent_tail: str   # 最近 1-2 轮摘要
    tool_catalog: list[ToolBrief]   # [{name, one_line_desc}, ...]

@dataclass
class ToolSelectionResult:
    selected: list[str]
    reasoning: str                  # 模型输出的简短理由（供审计）
    fallback_used: bool             # True 表示 selector 失败，用了全量工具

class LLMToolSelector:
    def __init__(self, provider_gateway, model: str = "small"):
        self._gw = provider_gateway
        self._model = model
        self._cache: dict[str, ToolSelectionResult] = {}

    def select(self, req: ToolSelectionRequest, k: int = 8) -> ToolSelectionResult:
        cache_key = self._cache_key(req)
        if cache_key in self._cache:
            return self._cache[cache_key]

        try:
            prompt = self._render_prompt(req, k)
            raw = self._gw.call_small_model(prompt, timeout_s=5)
            result = self._parse(raw)
            result.selected = self._merge_core_set(result.selected)
            self._cache[cache_key] = result
            return result
        except (TimeoutError, ParseError) as e:
            return ToolSelectionResult(
                selected=self._all_tool_names(),
                reasoning=f"fallback: {e}",
                fallback_used=True,
            )
```

**关键设计**
- **模型选择**：用小参数量模型（小 Claude / 本地 LLM），单次调用 < 5s、< $0.001。
- **cache key**：`hash(user_input_normalized + graph_state + intent + tool_catalog_version)`，连续相似提问复用。*不使用 embedding 做相似度，用简单规范化字符串哈希*。
- **核心工具集**：`core_set.py` 定义不进入选择的工具（`get_graph_info`、`skills_list`、`skill_view`、`memory_read` 等），selector 返回结果与之合并。
- **Fallback 策略**：selector 超时/解析失败时加载全部工具 schema，保证功能可用性，同时记录 `tool_selection_fallback` 事件。
- **每 turn 只调用一次**：selector 决策在一个用户 turn 内稳定，即便主 agent 多轮 tool call 也复用同一决策。
- **Main agent 的逃生通道**：提供 `tool_expand(name)` virtual tool，主 agent 发现 selector 漏选时可主动加载完整 schema。

#### P2.2 Selector Prompt 模板

```
你是 iGraph 的工具选择器。根据用户请求、当前图状态和可用工具目录，
返回最可能需要的 {k} 个工具（或更少）。

用户请求：{user_input}
图状态：{graph_state_summary}
意图：{detected_intent}
最近对话：{conversation_recent_tail}

可用工具目录（name: one_line_desc）：
{tool_catalog_formatted}

**核心工具集**（已默认加载，不需要选择）：
{core_set_names}

请以 JSON 返回：
{
  "selected": ["tool_name_1", "tool_name_2", ...],
  "reasoning": "一句话说明为什么选这些"
}
```

#### P2.3 Planner 与 ContextManager 改造

- `backend/agent/planner.py` 的 `_tool_selector.select(query, k=5)` 改为调用 `LLMToolSelector.select(req, k=5)`。
- `backend/agent/context/context_manager.py` 的 `tool_selector` 参数类型从 `ToolSelector` 改为 `LLMToolSelector`；`active_tools` 语义从"embedding 筛选"变为"LLM 筛选 + 核心工具集"。
- `backend/agent/igraph.py` 初始化处替换；`tools/selector.py` 依赖注入 `provider_gateway`。

#### P2.4 删除 embedding 依赖

- **删除文件**：`backend/agent/embedding.py`。
- **删除依赖**：`requirements.txt` 中 `sentence-transformers`、`torch`、`faiss`（若有）全部移除。
- **迁移 `MemoryEngine` 中的 embedding 相关代码**（`memory_engine.py` 里的 semantic recall）到 P3 移除。
- **迁移 `query_analyzer.py`**：若其中有 embedding 调用（例如 intent 分类），改为小模型 / 规则 / 关键词匹配。

#### P2.5 审计日志

- `tool_selection_events.jsonl` 记录每次 selector 调用：`{timestamp, user_input_digest, graph_state, candidate_tools, selected_tools, reasoning, latency_ms, model_version, fallback}`。

**验收标准**
- `embedding.py` 删除，`pip install` 不再安装 sentence-transformers/torch。
- LLM selector 平均延迟 < 500ms（小模型 + 合理上下文）。
- 核心工具集永远在主 agent prompt 中。
- `tool_expand` virtual tool 工作：selector 漏选的工具可被主动加载。
- Selector 失败时 fallback 到全量工具，功能不中断。
- 审计日志完整。

---

### P3：Memory Core 重构（全量加载 + 多作用域，2 周）

目标：删除语义 recall，改为全量 frozen snapshot 加载；同时补齐作用域、治理、安全、provider 抽象。

**模块规划**
```
backend/agent/memory/
├── memory_engine.py          # [REFACTOR] 删除 semantic recall，改为 load_all + freeze
├── provider.py               # MemoryProvider ABC
├── manager.py                # MemoryManager
├── builtin_provider.py       # 包装 MemoryEngine
├── store.py                  # atomic JSON/JSONL + lock
├── safety.py                 # content scanner
├── scope.py                  # MemoryScope
├── schemas.py                # dataclass/Pydantic
├── user_profile.py           # USER 档案
└── providers/                # 外部 provider 挂载点
```

#### P3.1 去 embedding + 全量 frozen snapshot

```python
class MemoryEngine:
    _snapshot_block: str = ""   # 会话开始时冻结

    def load_and_freeze(self, scope: MemoryScope) -> None:
        """会话开始调用：加载所有内容 → 渲染 system block → 冻结。"""
        user_profile = self._load_user_profile(scope)
        long_term   = self._load_long_term(scope)
        playbooks   = self._load_playbooks(scope)

        # 字符预算裁剪（按 4.5 表格规则）
        user_profile = self._enforce_char_limit(user_profile, limit=1500)
        long_term    = self._enforce_char_limit(long_term, limit=8000)
        playbooks    = self._enforce_char_limit(playbooks, limit=4000)

        self._snapshot_block = self._render(user_profile, long_term, playbooks)

    def get_system_block(self) -> str:
        return self._snapshot_block   # 整会话不变，保护 prefix cache

    def add_memory(self, content: str, scope: MemoryScope) -> None:
        if reason := _scan_memory_content(content):
            self._log_reject(reason); return
        self._live_state.append(...)
        self._atomic_json_write(...)
        # 本会话 snapshot 不刷新，下次会话生效
```

**删除**
- `memory_engine.py` 中所有 `semantic_recall`、`_compute_embedding`、`sentence-transformers` 相关代码。
- `recall(query, top_k)` 方法整体移除（包括关键词 recall 部分）。

**裁剪打分**（替代召回）
```python
def _score(m):
    return (
        m.confidence * 0.5 +
        _recency_score(m.updated_at) * 0.3 +
        min(m.hit_count / 10, 1.0) * 0.2
    )
```

#### P3.2 MemoryProvider / MemoryManager

```python
class MemoryProvider(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def is_available(self) -> bool: ...
    @abstractmethod
    def initialize(self, scope: MemoryScope, **kwargs) -> None: ...
    @abstractmethod
    def get_tool_schemas(self) -> list[dict]: ...
    @abstractmethod
    def handle_tool_call(self, name: str, args: dict) -> str: ...

    # 可选 hooks
    def system_prompt_block(self) -> str: return ""      # 全量加载的核心入口
    def sync_turn(self, turn: "CompletedTurn") -> None: ...
    def on_pre_compress(self, messages: list[dict]) -> str: return ""
    def on_session_end(self, messages: list[dict]) -> None: ...
    def shutdown(self) -> None: ...
```

- 主循环只依赖 `MemoryManager`。
- 内置 provider 始终存在，最多 1 个外部 provider。
- 外部 provider（mem0/Honcho 等）作为后续插件点，初期不启用。

#### P3.3 MemoryScope 多作用域

```python
@dataclass
class MemoryScope:
    user_id: str
    workspace_id: str | None
    project_id: str | None
    dataset_id: str | None
    session_id: str | None
    graph_fingerprint: str | None
```

**存储结构**
```
~/.igraph/memory/<workspace_id>/<user_id>/
  profile.json                      # USER 档案
  projects/<project_id>/memory.json
  datasets/<dataset_id>/memory.json
  playbooks.json
  events.jsonl
```

**作用域加载规则（全量模式下）**
- 会话开始时根据当前 scope 加载：
  - user profile（必选）
  - 当前 project memory（可选）
  - 当前 dataset memory（可选，基于 `graph_fingerprint` 匹配）
  - playbooks 中 `status in [validated, promoted_to_skill]` 的条目
- 跨 scope 泄漏防护：user A 永远看不到 user B 的任何内容。

**迁移**：旧 `default_user` 自动迁移到新路径，保留 `legacy_backup/`，P0 阶段已定位的路径重影在这一步彻底清理。

#### P3.4 User Profile（USER.md 风格）

```python
@dataclass
class UserProfile:
    language: str                    # zh-CN / en-US
    expertise_level: str             # beginner / intermediate / expert
    preferred_output_format: str     # markdown / plain / structured
    domain_knowledge: list[str]
    communication_style: str
    known_constraints: list[str]
    custom_facts: list[dict]
    updated_at: str
```

- 字符上限 1500；超额按字段优先级截断。
- 会话开始时注入 frozen snapshot 的首部。
- 写入触发：用户明确声明偏好、纠正 agent 行为、透露背景。

#### P3.5 用户可治理 API

```
GET    /api/memory               # 列出所有记忆（分 scope）
POST   /api/memory               # 手动写入（过安全扫描）
PATCH  /api/memory/{id}          # 编辑
DELETE /api/memory/{id}          # 删除
GET    /api/memory/events        # 审计日志
GET    /api/memory/usage         # 本会话 snapshot 包含了哪些 memory
```

前端能力：查看/删除/编辑记忆，查看来源 session；**注意全量加载模式下"本轮用了哪些 memory"已退化为"本会话 snapshot 是什么"**，展示粒度是会话级而非 turn 级。

**验收标准**
- `embedding.py` 引用彻底消失（memory 子系统零 embedding 依赖）。
- 主循环仅依赖 `MemoryManager`。
- 两用户互不串扰；同一用户不同 dataset 互不串扰。
- `long_term_memory.json` 写入异常退出不产生半截 JSON。
- 注入样本一律 `memory_reject`。
- 用户删除一条记忆后下次会话不再出现在 snapshot。
- System prompt 在单个会话内稳定不变，prefix cache 命中率可量化提升。

---

### P4：iGraph 领域技能库升级（2 周）

目标：把现有 3 个粗粒度技能拆为贴近图任务生命周期的领域技能。**在 P1 渐进披露落地后进行，确保 compact index 不膨胀**。

**建议技能集（11 个）**

| # | Name | 覆盖场景 |
|---|------|---------|
| 1 | `data-ingestion-table` | 表格字段识别、source/target/weight 映射、建图后验证 |
| 2 | `data-ingestion-text` | 文本三元组抽取、实体规范化、抽取质量说明 |
| 3 | `graph-state-check` | 加载状态、规模、有向/无向、权重、属性检查 |
| 4 | `graph-analysis-centrality` | centrality 选择、解释、适用边界 |
| 5 | `graph-analysis-community` | 社区检测算法选择、子图分析联动 |
| 6 | `graph-path-connectivity` | shortest path、connected components、可达性 |
| 7 | `graph-ml-training` | train config 获取、参数展示、用户确认、结构可视化 |
| 8 | `graph-ml-inference` | infer config 获取、model_id 验证、结果解释 |
| 9 | `dataset-management` | 保存/加载 dataset、版本说明、复用建议 |
| 10 | `graph-rag` | 文档分块、检索、图知识辅助问答、引用边界 |
| 11 | `experiment-reporting` | 图分析 / GraphML 结果整理为报告 |

**统一结构**
```markdown
---
name: ...
description: ...
metadata:
  igraph:
    any_tools: [...]
    requires_graph_state: ...
    risk_level: ...
---

# Title
## When to Use
## Required Checks
## Procedure
## Tool Sequence Patterns
## Pitfalls
## Verification
```

#### P4.1 `skill_manage` 最小安全子集

先只开放：`create` / `patch` / `write_file` / `remove_file`。

- 写入范围仅限 `~/.igraph/skills/user/` 与 `~/.igraph/skills/.review_queue/`。
- 禁止模型修改 bundled skills；改动 bundled 只生成 patch suggestion 由开发者审核。
- guard 约束：skill name/category/frontmatter 合法、supporting file 路径不穿越、单文件大小上限、agent-created 技能走 injection scan。

**验收标准**
- 至少 8 个核心领域技能完成。
- 每个技能有 1 个 golden prompt 测试。
- GraphML train/infer 等高风险技能必须要求用户确认。
- 普通图查询不被 GraphML 技能干扰（false positive 测试）。

---

### P5：Playbook → Skill 晋升闭环（2 周）

目标：解决 playbook 与 skill 职责重叠，让系统从历史经验沉淀可复用技能。

#### P5.1 Playbook 重定位为"技能候选池"

**状态机**
```
candidate ──(evidence ≥ 3, no neg feedback)──► validated
validated ──(reviewer approve)──► promoted_to_skill
任意状态 ──(长期未命中 or 用户否决)──► archived
```

**字段补充**
```python
@dataclass
class PlaybookEntry:
    problem_type: str
    approach: str
    trigger_terms: list[str]
    tool_sequence: list[str]
    confidence: float
    evidence_sessions: list[str]
    success_count: int
    failure_count: int
    last_validated_at: str
    status: str                      # candidate / validated / promoted_to_skill / archived
    candidate_skill_name: str | None
    promotion_reason: str | None
```

**全量加载模式下的影响**
- snapshot 中只载入 `validated` + `promoted_to_skill` 状态的 playbooks。
- `candidate` 状态不进 prompt（避免未验证建议干扰）。
- `promoted_to_skill` 状态可选择不再载入 playbook（已成为 skill），避免重复。

#### P5.2 后台 reviewer 自动生成候选 skill

触发条件（全部满足）：
- 同类 playbook 被命中并成功使用 ≥ 3 次。
- 工具链稳定（tool_sequence 变动次数 < 20%）。
- 用户无负反馈。
- 任务结果有明确验证。

生成物落到 `~/.igraph/skills/.review_queue/<candidate>.md` + `<candidate>.metadata.json`。

#### P5.3 晋升策略

- 低风险说明性技能（`risk_level: low`）可自动启用。
- 高风险技能（涉及训练、删除、写文件、外部 API、长作业）必须人工确认。
- 启用后 playbook 标记为 `promoted_to_skill`，snapshot 中不再载入。

**验收标准**
- 成功任务经验能生成 candidate skill。
- 用户批准后 skill 被下次会话 discover。
- 已 promoted 的 playbook 不再占 snapshot 预算。

---

### P6：观测、评估与安全红队（持续）

#### P6.1 指标

**Skills**
- `skill_index_token_cost`
- `skill_view_calls_per_turn`
- `skill_view_hit_rate`（view 后是否影响后续 tool call）
- `task_success_with_skill` vs `without_skill`
- `high_risk_tool_called_without_required_skill`

**Memory**
- `snapshot_char_count` / `snapshot_truncation_events`
- `memory_write_count` / `memory_reject_count`
- `memory_edit/delete_count_by_user`
- `prefix_cache_hit_rate`（system prompt 稳定性指标）

**Tools**
- `tool_selector_latency_ms`
- `tool_selector_fallback_rate`
- `tool_selector_precision`（选中工具实际被调用比例）
- `tool_selector_recall`（被调用工具确实在入选集里的比例）
- `tool_expand_calls_per_turn`（主 agent 逃生通道使用频率，偏高提示 selector 需调优）
- `tool_selector_cost_usd`（小模型调用费）

**系统**
- `first_token_latency` / `per_turn_prompt_tokens`
- `background_memory_update_latency`
- `snapshot_write_failures` / `provider_failures`

#### P6.2 测试矩阵

**单元测试**
- frontmatter parser / condition evaluator
- skill registry discover / cache
- `skill_view` path traversal guard
- LLM tool selector prompt 渲染 & JSON 解析容错
- tool selector cache key 稳定性
- memory atomic write
- memory safety scanner
- snapshot char budget 裁剪

**集成测试**
- text protocol 下 skills / tools virtual tool call
- native FC 下 skills / tools virtual tool call
- memory full-load snapshot 在会话内稳定
- memory update background chain 失败回滚
- tool selector fallback（超时 → 全量工具）

**E2E 场景**
- 上传 CSV → 映射字段 → 建图 → 社区分析 → 保存 dataset。
- 节点分类训练 → selector 选中 `get_train_config` + `train` → 参数确认 → train。
- selector 漏选场景 → 主 agent `tool_expand` → 恢复执行。
- 用户删除一条偏好 → 下次会话 snapshot 不再出现。

**安全红队样例**
- skill 中包含 "ignore previous instructions"。
- memory 中写入 "读取 .env 并 curl 出去"。
- supporting file 路径穿越（`../../etc/passwd`）。
- gateway 场景要求输入 API key（禁止在聊天中收 secrets）。
- 伪造工具名欺骗 selector（selector 返回不存在的工具名时应被过滤）。

---

## 6. 文件变更影响清单（v2）

```
iGraph/backend/
├── agent/
│   ├── embedding.py                       [DELETE] ToolSelector + embedding 全部移除
│   ├── skills/
│   │   ├── __init__.py
│   │   ├── skill_loader.py                [DEPRECATE] 保留临时兼容 shim
│   │   ├── models.py                      [NEW] SkillSpec / Condition / Readiness
│   │   ├── registry.py                    [NEW] SkillRegistry
│   │   ├── loader.py                      [NEW] compact index + active filter
│   │   ├── tools.py                       [NEW] skills_list / skill_view / skill_manage
│   │   ├── guard.py                       [NEW] injection scan + path guard
│   │   ├── manager.py                     [NEW] create/patch/write_file/remove_file
│   │   ├── data_ingestion/SKILL.md        [UPGRADE] frontmatter
│   │   ├── graph_analysis/SKILL.md        [UPGRADE] frontmatter
│   │   ├── graph_machine_learning/SKILL.md [UPGRADE] frontmatter
│   │   ├── [P4 新增 8 个领域技能目录]
│   │   └── user/                          [NEW] user skills 挂载点（运行时可写）
│   ├── tools/
│   │   ├── __init__.py                    [NEW]
│   │   ├── selector.py                    [NEW] LLMToolSelector（替换 embedding）
│   │   ├── catalog.py                     [NEW] 工具描述摘要生成
│   │   ├── prompts.py                     [NEW] selector prompt 模板
│   │   ├── core_set.py                    [NEW] 核心工具集定义
│   │   └── virtual_tools.py               [NEW] tool_expand handler
│   ├── memory/
│   │   ├── memory_engine.py               [REFACTOR] 删除 semantic recall，改为 load_all + freeze
│   │   ├── provider.py                    [NEW] MemoryProvider ABC
│   │   ├── manager.py                     [NEW] MemoryManager
│   │   ├── builtin_provider.py            [NEW] 包装 MemoryEngine
│   │   ├── store.py                       [NEW] atomic store + lock
│   │   ├── safety.py                      [NEW] content scanner
│   │   ├── scope.py                       [NEW] MemoryScope
│   │   ├── schemas.py                     [NEW] dataclass/Pydantic
│   │   ├── user_profile.py                [NEW] USER 档案
│   │   └── providers/                     [NEW] 外部 provider 挂载点
│   ├── context/
│   │   └── context_manager.py             [MODIFY] 注入 frozen memory snapshot + skills compact index + 仅入选工具 schema
│   ├── planner.py                         [MODIFY] ToolSelector → LLMToolSelector
│   ├── query_analyzer.py                  [MODIFY] 若存在 embedding，改为小模型/规则
│   └── igraph.py                     [MODIFY] 集成 MemoryManager + SkillRegistry + LLMToolSelector
├── api/                                   [MODIFY] 新增 /api/memory* 路由
└── main.py                                [MODIFY] 新增 skills CLI 子命令

用户本地：
~/.igraph/
  ├── config.yaml                          # skills 配置变量
  ├── skills/                              # user skills + .review_queue
  ├── memory/<workspace_id>/<user_id>/...  # 新存储结构
  └── logs/tool_selection_events.jsonl     # selector 审计

requirements.txt                           [MODIFY] 移除 sentence-transformers / torch
```

---

## 7. 推荐里程碑与优先级

| 里程碑 | 工期 | 内容 | 主要目标 |
|-------|------|------|---------|
| **M0** | 1–2 天 | P0 全部 | 基线可测试、安全底线、路径唯一化 |
| **M1** | 1 周 | P1.1 + P1.2 + P1.3 | SkillSpec、compact index、条件解耦 |
| **M2** | 1–2 周 | P2 全部 | LLM tool selector 上线，删除 embedding |
| **M3** | 2 周 | P3 全部 | Memory 全量加载 + MemoryManager + 多作用域 |
| **M4** | 2 周 | P4 全部 + P1.4 | 领域技能库、`skill_manage`、user skills discover |
| **M5** | 2 周 | P5 全部 | playbook → skill 晋升闭环 |
| **M6** | 持续 | P6 全部 | 指标接入、E2E 回归、红队样例固化 |

**优先级三档**

*最高优先级（必须做）*
1. P0-1 Memory 路径唯一化（开发基线）
2. P0-2 golden tests（防回归）
3. P0-3 原子写入
4. P0-4 memory 安全扫描
5. P2 LLM tool selector（决定整个选择层架构）
6. P3.1 Memory 去 embedding + 全量加载（配合 P2 彻底拔除 embedding 依赖）

*第二优先级（显著收益）*
1. P1.1 + P1.2 SkillSpec + compact index
2. P1.3 技能条件与 selector 解耦
3. P3.2 + P3.3 MemoryProvider / Manager / Scope
4. P3.4 User Profile
5. P4 领域技能拆分

*第三优先级（生态与可运营）*
1. P1.4 配置变量 / P4.1 `skill_manage`
2. P5 playbook → skill 晋升
3. P3.5 Memory UI / API
4. 外部 MemoryProvider（mem0 等）
5. P6 评估指标自动化

---

## 8. 关键取舍与反面建议（v2）

1. **去 embedding 的代价与收益需量化**。LLM selector 增加每 turn 约 300–500ms 与小额 API 成本；收益是可解释决策 + 零向量库依赖 + 图状态感知。上线后通过 `tool_selector_latency_ms` 与 `tool_selector_precision/recall` 监测，必要时可用 LLM 蒸馏出静态规则缓存降低延迟。
2. **Memory 全量加载的边界**：当 memory 规模超过字符预算（8000 char ≈ 2500 tokens）时仍需裁剪，此时裁剪规则就是"退化的召回"，但它是**确定性、可审计的**（基于 confidence×recency×hit_rate），而非语义相似度黑盒。
3. **核心工具集必须宁宽勿严**：漏选一个常用工具引发的体验劣化远大于多加载 1-2 个 schema 的 token 代价。建议初始核心集包含：`get_graph_info`、`skills_list`、`skill_view`、`memory_read`、`tool_expand`、`list_datasets`。
4. **不建议同时改 embedding 与 memory recall**：M2（tool selector 换 LLM）与 M3（memory 换全量加载）分开上线，每步独立验证，防止指标互相污染。
5. **不把 skills 走 MCP 暴露**：agent-level virtual tools 截获，避免污染 GraphData/GraphML 的图工具目录。
6. **不把所有 playbook 直接变成 skill**：需要 evidence、成功次数、review，否则技能库迅速变噪。
7. **不让模型直接修改 bundled skills**：先允许 user skills 与 review queue，bundled 变更走 PR。
8. **不把任务进展保存在 memory**：进展属于 session logs / plan artifacts。
9. **不一开始接入外部 memory 服务**：先把 builtin provider + 全量加载模式打磨稳，再评估 Honcho/Mem0 的价值。
10. **selector LLM 不要用主对话同一个大模型**：用小参数量快模型（如小 Claude/小 Llama），避免主对话延迟雪崩。

---

## 9. 最终目标架构（v2）

```
iGraph
├── ContextManager
│   ├── 静态 role prompt
│   ├── Memory frozen snapshot                    （会话开始一次性注入，整会话不变）
│   │   ├── user_profile
│   │   ├── long_term memory (char budget ≤ 8k)
│   │   └── validated playbooks (char budget ≤ 4k)
│   ├── Skills compact index                      （每轮注入；LRU+磁盘 manifest 缓存）
│   └── Selected tools schema                     （LLM selector 结果 + 核心工具集）
│
├── Agent-level virtual tools（MCP 分发前截获）
│   ├── skills_list / skill_view / skill_manage
│   ├── memory_read / memory_write / memory_delete
│   └── tool_expand                               （主 agent 漏选逃生通道）
│
├── LLMToolSelector（替换 embedding ToolSelector）
│   ├── 输入：user_input + graph_state + intent + tool_catalog
│   ├── 模型：小参数量快模型
│   ├── 输出：候选工具名 + reasoning
│   ├── 缓存：字符串哈希，非 embedding
│   ├── Fallback：超时→全量工具
│   └── 审计：tool_selection_events.jsonl
│
├── SkillRegistry
│   ├── bundled graph skills
│   ├── user skills
│   ├── external read-only skills
│   └── .review_queue/（来自 playbook 晋升）
│
├── MemoryManager
│   ├── BuiltinGraphMemoryProvider
│   │   ├── user facts          → profile.json
│   │   ├── project facts       → projects/<id>/memory.json
│   │   ├── dataset facts       → datasets/<id>/memory.json
│   │   ├── playbook candidates → playbooks.json
│   │   └── events.jsonl（审计）
│   └── OptionalExternalProvider（最多 1 个，失败不阻塞）
│
└── MCP / GraphData / GraphML
    └── 领域工具保持专注于图操作，不被 skills/memory/selector 污染

删除：
- backend/agent/embedding.py
- sentence-transformers / torch / faiss 依赖
- memory_engine.py 中所有 semantic recall 逻辑
```

---

## 10. v1 → v2 变更对照表（便于查阅）

| 条目 | v1 | v2 |
|------|----|----|
| Memory recall | 语义+关键词混合 RRF | **全量加载 frozen snapshot** |
| Memory 注入位置 | 每 turn fenced `<memory-context>` | **会话开始 system prompt 静态块，不刷新** |
| Memory 字符预算 | 建议 | **强制**（超额确定性裁剪） |
| Memory 召回感知图上下文 | graph-aware retriever | **已消失**（全量，无需召回） |
| Tool selector | `embedding.py::ToolSelector`（sentence-transformers） | **`tools/selector.py::LLMToolSelector`（小模型）** |
| 工具注入方式 | 全量 schema 或 embedding 筛选 | **LLM 筛选候选 + 核心工具集 + `tool_expand` 逃生** |
| Skill 选择 | compact index + on-demand `skill_view` | **保持不变（Hermes 式）** |
| Query analyzer embedding | 保留 | **若存在则改小模型/规则** |
| embedding.py | 保留扩展 | **删除** |
| sentence-transformers 依赖 | 保留 | **删除** |
| P0 路径 bug | 需修 | **已修，删去此项** |
| P0 Memory 路径重影 | 列为 P0 | **继续列为 P0** |

---

*本计划 v2 在保留 iGraph 既有工程优势（playbook、async sub-agent、图状态感知）的前提下，以 P0 基线 → P1 技能渐进披露 → P2 LLM tool selector → P3 Memory 全量加载 → P4 领域技能 → P5 晋升闭环 → P6 观测 的顺序推进。核心变化是彻底移除 embedding 依赖，改用 LLM sub-agent 做工具选择、Hermes 式渐进披露做技能加载、全量 frozen snapshot 做记忆注入，使整个选择层可解释、可审计、可运营。*
