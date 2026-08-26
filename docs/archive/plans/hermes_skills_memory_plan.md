# iGraph Skills & Memory 迭代计划

> 参考来源：hermes-agent 完整架构分析  
> 对比目标：iGraph 当前实现  
> 日期：2026-04-21

---

## 一、现状对比总结

### 1.1 Skills 机制对比

| 维度 | hermes-agent | iGraph（当前） | 差距 |
|------|-------------|-------------------|------|
| **Skill 数量** | 27+ 分类，数百个 | 3 个（固定） | 规模差距大 |
| **Skill 定义格式** | YAML frontmatter + Markdown，字段丰富 | 纯 Markdown，无元数据 | 缺乏结构化描述 |
| **Skill 加载机制** | 文件系统扫描 + 2层缓存（内存+磁盘） | 硬编码判断工具列表 | 无扩展性 |
| **触发条件** | 工具集/工具存在性 + 平台 + 配置 + 标签 | 仅检查 4 个工具名 | 逻辑简单 |
| **渐进披露** | 先列表（tier-1），按需加载全文（tier-2/3） | 全量注入系统提示 | 无按需加载 |
| **Skill Hub** | GitHub 多源 + 信任等级 + 审计日志 | 不存在 | 完全缺失 |
| **配置变量** | 声明式 config key + 用户可配置 | 无 | 缺失 |
| **平台过滤** | macOS / Linux / Windows | 无 | 缺失 |
| **用户自定义** | `~/.hermes/skills/` 用户目录 | 无 | 缺失 |
| **Skill 缓存** | mtime/size manifest 磁盘快照 + in-process LRU | 每次重新构建 | 无缓存 |
| **Prompt 复用** | 系统提示中列出 skill 索引 | 无 skill 索引 | 缺少发现机制 |

### 1.2 Memory 机制对比

| 维度 | hermes-agent | iGraph（当前） | 差距 |
|------|-------------|-------------------|------|
| **存储类型** | MEMORY.md（Agent 笔记）+ USER.md（用户档案） | 仅 long_term_memory.json | 缺用户档案分层 |
| **插件扩展** | 8 个可插拔 Provider（honcho, mem0, holographic…） | 无插件机制 | 无扩展性 |
| **写入方式** | Agent 主动调用 memory 工具（on-demand） | LLM sub-agent 异步后台 | 两种路径各有优劣 |
| **读取时机** | 会话开始冻结快照注入系统提示；工具写入仅改内存和磁盘 | 每 turn 动态 recall 注入 | 缺 prefix-cache 稳定性设计 |
| **安全性** | 20+ 注入检测 pattern + 不可见字符拦截 | 无威胁检测 | 安全漏洞 |
| **记忆类型** | MEMORY.md + USER.md（通用）+ holographic（结构化事实） | long_term_memory + playbooks | 缺用户档案维度 |
| **Playbook** | 无专门 Playbook（workflow 写入 MEMORY.md） | problem_solving_playbooks.json | iGraph 有优势 |
| **字符限制** | MEMORY.md 2200 chars，USER.md 1375 chars | 无限制 | 无膨胀控制 |
| **原子写入** | fcntl 文件锁 + 临时文件 rename | 直接写文件 | 并发安全风险 |
| **去重机制** | 加载时去重 | 依赖 LLM 判断 | 去重不可靠 |
| **语义索引** | holographic HRR（1024维向量）| sentence-transformers，384维 | 技术相近 |
| **Provider 并发** | 最多 1 个外部 provider | N/A | N/A |

---

## 二、核心差距归纳

### Skills 核心差距
1. **无 Skill 扩展框架**：技能数量固定，无法由用户或社区添加
2. **无渐进披露**：所有 skill 内容直接注入系统提示，token 浪费
3. **无 Skill 索引**：Agent 无法在提示中看到有哪些可用技能
4. **Skill 触发逻辑耦合**：`skill_loader.py` 硬编码工具名判断，新增 skill 需改代码

### Memory 核心差距
1. **无用户档案（USER.md）**：跨会话的用户偏好、习惯没有专用存储
2. **无安全防护**：外部内容可通过 memory 写入实现提示注入
3. **缓存不稳定**：每 turn 动态构建 memory prompt，无法利用 prefix cache
4. **无限制膨胀**：long_term_memory.json 无字符/条目上限，recall 质量随条目增加下降
5. **并发不安全**：文件写入无锁保护

---

## 三、迭代计划

### Phase 1：基础巩固（2–3 周）

#### P1-1：Skills —— 结构化 Skill 定义格式

**目标**：将现有 3 个 SKILL.md 升级为带 YAML frontmatter 的结构化格式

**改动**：
- 在每个 `SKILL.md` 顶部添加 YAML frontmatter
- 扩展 `skill_loader.py` 解析 frontmatter，提取 `name`、`description`、`requires_tools`、`tags`

**新格式**（示例 `graph_analysis/SKILL.md`）：
```yaml
---
name: graph-analysis
description: 图谱分析技能：中心性、社区检测、路径算法的标准化工作流
version: 1.0.0
requires_tools:
  - get_graph_info
  - get_node_info
tags: [analysis, centrality, community, path]
---
# 图分析技能

...（原有 Markdown 内容）
```

**实现文件**：
- `backend/agent/skills/skill_loader.py` → 新增 `parse_skill_frontmatter()`
- `backend/agent/skills/*/SKILL.md` → 升级格式

---

#### P1-2：Memory —— 原子写入与字符限制

**目标**：修复 `memory_engine.py` 的并发安全问题，添加字符上限

**改动**：
```python
# memory_engine.py

MEMORY_CHAR_LIMIT = 8000     # long_term_memory 总字符上限
PLAYBOOK_CHAR_LIMIT = 4000   # playbooks 总字符上限

def _atomic_write(path: Path, data: dict):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.rename(path)   # atomic on POSIX

def _enforce_char_limit(memories: list, limit: int) -> list:
    """按 confidence 降序保留，直到总字符数在 limit 内"""
    sorted_mems = sorted(memories, key=lambda m: m["confidence"], reverse=True)
    result, total = [], 0
    for m in sorted_mems:
        size = len(json.dumps(m, ensure_ascii=False))
        if total + size > limit:
            break
        result.append(m)
        total += size
    return result
```

**实现文件**：`backend/agent/memory/memory_engine.py`

---

#### P1-3：Memory —— 注入安全防护

**目标**：阻止外部内容通过 memory 工具写入实施提示注入

**检测策略**（参考 hermes-agent `_scan_memory_content()`）：
```python
INJECTION_PATTERNS = [
    r'ignore (all )?previous instructions?',
    r'you are now',
    r'disregard (all )?previous',
    r'system prompt',
    r'<\|.*?\|>',                    # special tokens
    r'[​‌‍﻿]',   # invisible Unicode
    r'(curl|wget).*(ANTHROPIC|API_KEY|\.env)',
    r'cat\s+.*\.env',
]

def _scan_memory_content(content: str) -> Optional[str]:
    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            return f"Memory write blocked: detected pattern '{pattern}'"
    return None
```

**实现文件**：`backend/agent/memory/memory_engine.py`

---

### Phase 2：Skills 扩展框架（3–4 周）

#### P2-1：动态 Skill 发现机制

**目标**：技能不再硬编码，支持文件系统扫描，新增技能只需放入目录

**新目录结构**：
```
backend/agent/skills/
├── skill_loader.py          # 升级为扫描器
├── data_ingestion/
│   └── SKILL.md
├── graph_analysis/
│   └── SKILL.md
├── graph_machine_learning/
│   └── SKILL.md
└── [用户新增技能目录，自动发现]
```

**核心逻辑**（`skill_loader.py` 重构）：
```python
@dataclass
class SkillMeta:
    name: str
    description: str
    path: Path
    requires_tools: list[str]
    requires_toolsets: list[str]
    fallback_for_tools: list[str]
    tags: list[str]
    platform: list[str]     # [], ["macos"], ["linux"], ["windows"]

class SkillLoader:
    def __init__(self, skills_dirs: list[Path]):
        self._dirs = skills_dirs
        self._cache: dict = {}

    def discover(self) -> list[SkillMeta]:
        """扫描所有 SKILL.md，解析 frontmatter，返回 meta 列表"""

    def filter_active(self, available_tools: list[str]) -> list[SkillMeta]:
        """根据当前可用工具过滤激活的 skill"""

    def load_full(self, name: str) -> str:
        """按需加载 skill 完整内容（含 references/ 目录）"""

    def build_skills_index(self, active_skills: list[SkillMeta]) -> str:
        """生成 skills 索引（tier-1，仅 name + description）"""
```

**用户自定义 Skill 目录**：`~/.igraph/skills/`，与内置技能合并扫描。

---

#### P2-2：Skills 渐进披露（按需加载）

**目标**：系统提示只注入 skill 索引（compact），Agent 按需调用工具获取全文

**新增工具**：
```python
# tools/skill_tools.py

def skills_list() -> str:
    """返回所有可用 skill 的名称和简短描述（tier-1）"""

def skill_view(name: str) -> str:
    """返回指定 skill 的完整内容 + references 文件（tier-2/3）"""
```

**系统提示修改**：
- 移除：将 skill 全文直接注入到 role prompt 中
- 新增：在系统提示末尾插入 skill 索引（50–200 tokens）
- 使用说明：提示 Agent 调用 `skill_view` 获取详细指引

**索引格式示例**：
```
## Available Skills
- **graph-analysis**: 图谱分析技能：中心性、社区检测、路径算法
- **data-ingestion**: 图数据导入技能：字段映射、格式验证
- **graph-ml**: 图机器学习技能：节点分类、链接预测训练流程

Use `skill_view(name)` to load full instructions for any skill.
```

**缓存策略**：
- Layer 1：进程内 LRU dict（key = skill_dirs + available_tools + platform hash）
- Layer 2：磁盘快照 `~/.igraph/.skills_prompt_snapshot.json`，以各 SKILL.md 的 mtime+size 构成 manifest 做失效判断

---

#### P2-3：Skill 配置变量系统

**目标**：Skill 可声明配置参数，从 `config.yaml` 解析，支持用户覆盖

**SKILL.md frontmatter 扩展**：
```yaml
---
name: graph-ml
metadata:
  igraph:
    config:
      - key: ml.default_epochs
        description: 默认训练轮数
        default: "100"
        prompt: 默认 epoch 数量
      - key: ml.device
        description: 训练设备
        default: "auto"
---
```

**Config 解析**（`skill_loader.py`）：
```python
def resolve_skill_config(skill_name: str, config: dict) -> dict:
    """从 config.yaml skills.config.<key> 解析，返回 {key: resolved_value}"""
```

**配置文件**：`~/.igraph/config.yaml`
```yaml
skills:
  config:
    ml.default_epochs: 200
    ml.device: cuda
```

---

### Phase 3：Memory 架构升级（3–4 周）

#### P3-1：用户档案（User Profile）模块

**目标**：独立维护用户档案，与 Agent 笔记分离，实现有针对性的用户偏好学习

**新增文件**：`backend/agent/memory/default_user/user_profile.json`

**数据结构**：
```python
@dataclass
class UserProfile:
    language: str                    # zh-CN, en-US
    expertise_level: str             # beginner, intermediate, expert
    preferred_output_format: str     # markdown, plain, structured
    domain_knowledge: list[str]      # ["graph theory", "ML"]
    communication_style: str         # concise, detailed, step-by-step
    known_constraints: list[str]     # ["no internet access", "GPU only"]
    custom_facts: list[dict]         # 任意用户声明的事实
    updated_at: str
```

**写入触发**（在 memory sub-agent 提示中扩展）：
- 用户明确声明偏好（"我喜欢简洁的回答"）
- 用户纠正 Agent 行为（"不要每次都确认，直接执行"）
- 用户透露背景（"我是图数据库工程师"）

**注入时机**：会话开始时一次性注入系统提示（frozen snapshot 模式）

---

#### P3-2：Frozen Snapshot 模式（Prefix Cache 稳定）

**目标**：会话开始时冻结 memory 内容，工具写入仅修改内存和磁盘，不破坏 prefix cache

**当前问题**：
- 每 turn 重新 recall 并注入 memory prompt → 系统提示变化 → prefix cache 失效 → 每 turn 均为 cache miss

**新设计**：
```python
class MemoryEngine:
    _system_prompt_snapshot: str = ""   # 会话开始时冻结

    def load_and_freeze(self):
        """会话开始时调用：加载所有记忆 → 生成系统提示块 → 冻结"""
        memories = self._load_from_disk()
        user_profile = self._load_user_profile()
        self._system_prompt_snapshot = self._format_for_system_prompt(memories, user_profile)

    def get_system_prompt_block(self) -> str:
        """返回冻结快照，在整个会话中不变"""
        return self._system_prompt_snapshot

    def add_memory(self, content: str):
        """工具写入：更新内存状态 + 原子写磁盘，但不更新快照"""
        self._live_memories.append(content)
        self._atomic_write(...)
```

**Recall 变化**：
- 取消每 turn recall 动态注入
- 改为在会话开始时将 top-k recall 结果包含在冻结快照中
- 或：将 recall 结果通过 `<memory-context>` 标签在每个 user message 前注入（不破坏 system prompt）

```python
# 每 turn 在 user message 前注入（不是系统提示）
context_block = f"""<memory-context>
以下是从长期记忆中召回的相关信息（非用户输入）：
{self.recall(user_input)}
</memory-context>

{user_input}"""
```

---

#### P3-3：Memory Provider 插件接口

**目标**：建立可扩展的 memory provider 机制，支持接入第三方记忆服务

**抽象基类**（`backend/agent/memory/memory_provider.py`）：
```python
from abc import ABC, abstractmethod

class MemoryProvider(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def is_available(self) -> bool: ...

    @abstractmethod
    def initialize(self, session_id: str, **kwargs) -> None: ...

    @abstractmethod
    def get_tool_schemas(self) -> list[dict]: ...

    @abstractmethod
    def handle_tool_call(self, tool_name: str, args: dict) -> str: ...

    # 可选钩子
    def system_prompt_block(self) -> str: return ""
    def prefetch(self, query: str) -> str: return ""
    def sync_turn(self, user: str, assistant: str) -> None: pass
    def on_session_end(self, messages: list) -> None: pass
    def shutdown(self) -> None: pass
```

**目录结构**：
```
backend/agent/memory/providers/
├── __init__.py         # 发现与加载
├── builtin/            # 内置 provider（现有 MemoryEngine 封装）
│   └── __init__.py
└── [用户自定义 providers]
```

**Manager**（`backend/agent/memory/memory_manager.py`）：
- 内置 provider 始终存在
- 最多 1 个外部 provider（避免冲突）
- 失败不阻塞（各 provider 独立 try-catch）

**初期内置 Provider 优先级**：
1. `builtin`：现有 MemoryEngine 封装（始终激活）
2. `mem0`：mem0.ai 云端记忆（可选，需 API key）
3. `local_vector`：本地向量数据库（可选，需 faiss/chromadb）

---

#### P3-4：记忆召回质量优化

**目标**：提升 recall 精度，减少无关记忆噪声注入

**当前问题**：
- 语义相似度阈值（0.15）过低，无关记忆易混入
- 关键词匹配与语义匹配融合策略不够精细

**改进方案**：

**多策略混合召回**：
```python
def recall(self, query: str, top_k: int = 6) -> list[MemoryItem]:
    semantic_hits = self._semantic_recall(query, top_k=10, threshold=0.3)
    keyword_hits  = self._keyword_recall(query, top_k=10)
    recency_hits  = self._recency_recall(top_k=5)

    # 融合：RRF (Reciprocal Rank Fusion)
    return rrf_merge([semantic_hits, keyword_hits, recency_hits], top_k=top_k)
```

**置信度衰减**：
```python
# 超过 30 天未被 hit 的记忆置信度下降 10%
def _apply_temporal_decay(self, memories: list) -> list:
    now = datetime.utcnow()
    for m in memories:
        days_idle = (now - m.last_seen_at).days
        if days_idle > 30:
            m.confidence *= max(0.5, 1 - (days_idle - 30) * 0.01)
    return memories
```

**召回后过滤**：
- 置信度 < 0.2 的记忆不注入（仅保存，不使用）
- 状态 `archived` 的记忆不注入
- 注入 token 上限：500 tokens（超出则截断低置信记忆）

---

### Phase 4：Skill Hub 与生态建设（4–6 周）

#### P4-1：iGraph Skill Hub（轻量版）

**目标**：支持从 GitHub 仓库安装社区 Skill，建立 iGraph 技能生态

**Hub 状态目录**：`~/.igraph/skills/.hub/`
```
.hub/
├── lock.json          # 已安装 skill 来源追踪
├── audit.log          # 安装/卸载历史
├── taps.json          # 注册的 GitHub 仓库
└── index-cache/       # 远程索引缓存（TTL：3600s）
```

**CLI 命令**（通过 `main.py` 扩展）：
```bash
igraph skills list                   # 列出所有 skill（本地+已安装）
igraph skills view graph-analysis    # 查看 skill 详情
igraph skills tap https://github.com/user/my-skills  # 注册 GitHub 仓库
igraph skills install skill-name     # 安装 skill
igraph skills uninstall skill-name   # 卸载 skill
```

**信任等级**：
- `builtin`：随 iGraph 内置
- `community`：GitHub 公开仓库安装（需用户确认）
- `local`：用户手动放入 `~/.igraph/skills/`

---

#### P4-2：Skill 测试框架

**目标**：为每个 Skill 提供标准化测试，确保 Skill 在工具版本迭代后仍正确触发

**测试规范**（`skills/*/tests/`）：
```yaml
# skills/graph_analysis/tests/trigger_test.yaml
cases:
  - input: "帮我分析节点的中心性"
    expected_skill_triggered: true
    expected_tools_called: [get_graph_info, compute_centrality]
  - input: "帮我训练一个模型"
    expected_skill_triggered: false  # 该场景应触发 graph-ml skill
```

**运行方式**：
```bash
igraph skills test graph-analysis
```

---

### Phase 5：进阶记忆能力（6–8 周）

#### P5-1：结构化知识图谱记忆（参考 holographic provider）

**目标**：在 JSON 记忆之上，构建实体-关系结构化记忆层，支持推理查询

**技术选型**：SQLite + 自定义向量索引（避免引入外部服务）

**数据模型**：
```sql
-- facts 表
CREATE TABLE facts (
    id TEXT PRIMARY KEY,
    subject TEXT,       -- 实体
    predicate TEXT,     -- 关系
    object TEXT,        -- 值/目标实体
    confidence REAL,
    trust REAL,
    session_id TEXT,
    created_at TEXT,
    updated_at TEXT,
    embedding BLOB      -- 384-dim float32 向量
);

-- entities 表（实体规范化）
CREATE TABLE entities (
    canonical TEXT PRIMARY KEY,
    aliases TEXT        -- JSON array of alternative names
);
```

**工具接口**：
```python
# 新增工具
def knowledge_store(action: str, subject: str, predicate: str, object: str) -> str:
    """action: add | search | probe | reason | contradict | remove"""

def knowledge_feedback(fact_id: str, helpful: bool) -> str:
    """调整 fact trust score"""
```

**查询类型**：
- `probe`：查询实体所有相关事实
- `reason`：跨实体组合推理
- `contradict`：查找矛盾声明

---

#### P5-2：会话压缩前记忆提取

**目标**：在上下文窗口压缩前，提取有价值信息保存到长期记忆，防止丢失

**触发点**：在 `context_manager.py` 触发压缩前调用 `memory_engine.on_pre_compress()`

```python
class MemoryEngine:
    def on_pre_compress(self, messages: list[dict]) -> str:
        """
        在上下文压缩前被调用。
        提取关键信息，写入 long_term_memory。
        返回提取摘要（可注入新上下文开头）。
        """
        payload = self._format_compress_payload(messages)
        extraction = self._invoke_memory_llm(COMPRESS_EXTRACTION_PROMPT + payload)
        ops = json.loads(extraction)
        self._apply_operations(ops)
        return self._format_summary(ops)
```

---

## 四、优先级与时间表

| 阶段 | 内容 | 优先级 | 预计周期 | 收益 |
|------|------|--------|---------|------|
| P1-1 | Skill 结构化格式 | 高 | 1 周 | 为后续所有 skill 扩展奠基 |
| P1-2 | Memory 原子写入+字符限制 | 高 | 1 周 | 数据安全，防止膨胀 |
| P1-3 | Memory 注入安全防护 | 高 | 1 周 | 防止提示注入攻击 |
| P2-1 | 动态 Skill 发现 | 高 | 2 周 | 解除技能硬编码限制 |
| P2-2 | Skills 渐进披露 | 中 | 2 周 | 节省 token，提升扩展性 |
| P3-1 | User Profile 模块 | 中 | 1 周 | 个性化响应基础 |
| P3-2 | Frozen Snapshot 模式 | 中 | 2 周 | prefix cache 命中率显著提升 |
| P3-3 | Memory Provider 插件接口 | 中 | 3 周 | 长远扩展能力 |
| P3-4 | Recall 质量优化 | 中 | 2 周 | 记忆相关性提升 |
| P2-3 | Skill 配置变量 | 低 | 1 周 | 用户体验优化 |
| P4-1 | Skill Hub 轻量版 | 低 | 4 周 | 生态建设 |
| P4-2 | Skill 测试框架 | 低 | 2 周 | 工程质量 |
| P5-1 | 结构化知识图谱记忆 | 低 | 4 周 | 高级推理能力 |
| P5-2 | 压缩前记忆提取 | 低 | 2 周 | 防止上下文丢失 |

---

## 五、关键设计原则

### 5.1 Skills 设计原则
1. **声明式触发**：Skill 通过 `requires_tools` 声明自己何时激活，不在代码中硬判断
2. **渐进披露**：索引先行，全文按需，避免 token 浪费
3. **文件即配置**：添加新 Skill 只需新建目录 + SKILL.md，无需改代码
4. **版本追踪**：每个 Skill 有版本号，支持 Hub 安装的版本管理

### 5.2 Memory 设计原则
1. **冻结快照**：系统提示中的 memory 块在会话内不变，保护 prefix cache
2. **写分离**：工具写入 → 内存+磁盘；系统提示读取 → 始终使用冻结快照
3. **安全优先**：任何外部内容写入 memory 前必须通过安全扫描
4. **有界存储**：字符/条目限制是强制的，而非建议性的
5. **置信度驱动**：低置信度记忆自动衰减，不干扰 recall 质量

### 5.3 iGraph 已有优势应保留
- **Playbook 机制**：hermes-agent 无此设计，是 iGraph 的独特优势，继续强化
- **Sub-agent 异步写入**：让 memory 更新不阻塞主 react loop，继续保留
- **语义 + 关键词双路召回**：混合策略优于单一方式，在优化中继续保留
- **图状态感知**：节点/边数量、是否有权重等元数据对 skill 触发有指导价值，可扩展到 skill 条件中

---

## 六、文件变更影响清单

```
iGraph/backend/
├── agent/
│   ├── skills/
│   │   ├── skill_loader.py              [重构] 动态发现 + frontmatter 解析 + 缓存
│   │   ├── data_ingestion/SKILL.md      [升级] 添加 YAML frontmatter
│   │   ├── graph_analysis/SKILL.md      [升级] 添加 YAML frontmatter
│   │   └── graph_machine_learning/SKILL.md [升级] 添加 YAML frontmatter
│   ├── memory/
│   │   ├── memory_engine.py             [重构] 原子写入+安全扫描+字符限制+冻结快照
│   │   ├── memory_provider.py           [新增] 抽象 Provider 基类
│   │   ├── memory_manager.py            [新增] Provider 编排器
│   │   ├── user_profile.py              [新增] 用户档案模块
│   │   └── providers/
│   │       └── builtin/__init__.py      [新增] 现有引擎封装为 Provider
│   ├── context/
│   │   └── context_manager.py           [修改] 注入 frozen memory snapshot
│   └── igraph.py                   [修改] 集成 MemoryManager + SkillLoader 重构
├── tools/
│   └── skill_tools.py                   [新增] skills_list / skill_view 工具
└── main.py                              [修改] 添加 skills CLI 子命令
```

---

*本计划基于 hermes-agent 完整架构分析与 iGraph 现状对比，按照从基础到高级、从稳定性到扩展性的顺序排列，可根据实际开发节奏灵活调整各阶段优先级。*
