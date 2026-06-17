# Codex Implementation Review
> Branch: `upgrade_skills_memory`
> Reviewed against: `remaining_skills_memory_implementation_plan.md`
> Date: 2026-04-23

---

## 1. 总体评价

Codex 完成了规划中大部分功能，核心骨架是正确的。技能管理（skill_manage）、记忆治理 API、Playbook 生命周期和 PlaybookReviewer 均已落地。11 个领域技能文件全部创建，`memory_write`/`memory_update` 虚拟工具已加入核心工具集，测试覆盖基础路径。

主要遗漏：scoped memory 默认不激活（需 env var）、`skill_manage(approve)` 不更新 playbook 状态、Playbook prune 逻辑有 bug、validation 阈值不完整、`/api/memory/usage` 过于简单、E2E 测试缺失。

---

## 2. 规划条目完成情况

### Phase A：Skill 治理基础

| 条目 | 状态 | 说明 |
|---|---|---|
| A1 扩展 SkillCondition / SkillSpec | ✅ 完成 | models.py 与计划完全一致，额外加了 `metadata` 字段 |
| A2 多目录发现（bundled/user/review_queue） | ✅ 完成 | paths.py + registry.py，重名检测、review_queue opt-in 均实现 |
| A3 Compact Index 缓存 | ⚠️ 部分 | `_load_skill` 以 lru_cache 缓存单个文件读取，`clear_skill_caches()` 在 skill_manage 写入后调用；但计划要求的**多维 cache key**（tool names hash、graph state hash、protocol、intent）和**磁盘 manifest** (`~/.igraph/.skills_prompt_snapshot.json`) 均未实现；`build_skills_system_prompt` 每次调用都触发 filesystem glob |

### Phase B：skill_manage

| 条目 | 状态 | 说明 |
|---|---|---|
| B1 SkillManager 模块 | ✅ 完成 | manager.py 实现 create/write_file/remove_file/approve/reject，路径守卫、内容扫描、大小限制、审计事件均在位 |
| B2 暴露 skill_manage 虚拟工具 | ✅ 完成 | tools.py 有完整 schema，加入 CORE_TOOL_NAMES |
| `patch` action | ✅ 完成（隐式） | 未知 action 返回 `{"ok":false,"error":"unsupported_action"}`，计划允许此做法 |

### Phase C：领域技能库扩展

| 条目 | 状态 | 说明 |
|---|---|---|
| C1 11 个分裂领域技能 | ✅ 完成 | data_ingestion_table/text、graph_state_check、graph_analysis_centrality/community、graph_path_connectivity、graph_ml_training/inference、dataset_management、graph_rag、experiment_reporting 全部存在 |
| C2 高风险工具拦截 | ✅ 完成 | `_high_risk_tool_confirmed()` 实现，train/infer 在无确认时返回错误 |

### Phase D：MemoryScope 真实存储

| 条目 | 状态 | 说明 |
|---|---|---|
| D1 MemoryPathResolver | ✅ 完成 | paths.py 实现所有规划的方法；`dataset_memory_path` 计划返回 `Path|None`，实现改为始终返回路径（用 "default_dataset" 占位），偏差轻微 |
| D2 兼容旧文件 / 迁移 | ⚠️ 部分 | 实现了两个迁移：旧嵌套路径 bug 修复、更早的 memory_snapshot.json 格式迁移；但规划的**主迁移**（`backend/agent/memory/default_user/` → `~/.igraph/memory/`）**未实现**，仍通过 `IGRAPH_USE_SCOPED_MEMORY=1` 环境变量手动切换，默认仍用本地路径 |
| D3 MemoryEngine scope 参数 | ⚠️ 部分 | 在 scoped 模式下使用 resolver，但方法签名未加 `scope: MemoryScope` 参数；多租户隔离只能通过不同进程/不同 engine 实例实现 |

### Phase E：记忆治理 API 和工具

| 条目 | 状态 | 说明 |
|---|---|---|
| E1 MemoryService 层 | ✅ 完成 | service.py 实现 list/write/update/delete/events/usage |
| E2 memory_write / memory_update 虚拟工具 | ✅ 完成 | tools.py 有 schema，均加入 CORE_TOOL_NAMES；`memory_delete` 不在核心集，需 tool_select（计划未明确要求） |
| E3 HTTP API | ✅ 基本完成 | GET/POST/PATCH/DELETE /api/memory，/api/memory/events，/api/memory/usage 均在 app.py；scope 查询参数（user_id/workspace_id/project_id）未实现；DELETE 用 body 传 scope/reason（见 Bug 5） |

### Phase F：Playbook 生命周期与晋升

| 条目 | 状态 | 说明 |
|---|---|---|
| F1 PlaybookEntry 扩展字段 | ⚠️ 部分 | `lifecycle_status`、`evidence_sessions`、`success_count`、`failure_count`、`hit_count`、`last_validated_at` 等字段已加入插入逻辑；但保留了 `status = "active"` 双字段模式，导致语义混乱（见 Bug 1） |
| F2 子代理 Prompt 更新 | ⚠️ 部分 | prompt 中未包含 `success_signal`/`failure_signal`；传给子代理的 `existing_playbooks` 不含 `lifecycle_status`/`evidence_sessions` 等新字段 |
| F3 PlaybookReviewer | ✅ 完成 | playbook_reviewer.py 生成候选技能到 review queue，带 metadata.json |
| F4 approve / reject 晋升 | ⚠️ 部分 | `skill_manage(approve)` 移动文件，但**未将 playbook 的 status 更新为 `promoted_to_skill`**（见 Bug 3），导致已晋升 playbook 仍占用快照 budget |

### Phase G：可观测性

| 条目 | 状态 | 说明 |
|---|---|---|
| G1 事件日志 | ✅ 完成 | skill_events.jsonl 和 memory events.jsonl 均实现 |
| G2 Metrics API | ⚠️ 简化 | `GET /api/metrics/knowledge` 实现，但通过逐行扫描事件文件统计类型频次，而非维护内存计数器；每次请求读取最多 10000 行，大文件下性能差；未拆分计划要求的细粒度计数器（如 snapshot_truncation_count、playbook_candidate_count） |
| G3 Snapshot Usage | ⚠️ 部分 | `/api/memory/usage` 返回快照字符数和快照文本，但缺失：包含的条目 ID 列表、按原因分类的被排除条数（archived/candidate/promoted/char_budget/wrong_scope） |

### Phase H：测试矩阵

| 条目 | 状态 | 说明 |
|---|---|---|
| 单元测试 | ⚠️ 部分 | test_skill_manage.py（create + 注入拒绝）、test_memory_governance.py（手动写/更新 + candidate 不入快照）；缺：scope 隔离、路径穿越、review_queue 发现、重名冲突、缓存失效等 |
| 集成测试 | ❌ 缺失 | 无 text/native 协议调用 skill_manage/memory_write 的集成测试 |
| E2E 测试 | ❌ 缺失 | 无 "多次成功→candidate→validated→reviewer→approve→下次会话发现" 完整路径测试 |

---

## 3. Bugs

### Bug 1（高）：`_prune_playbook_snapshot` 把 validated playbook 当 archived 处理

**位置**：`backend/agent/memory/memory_engine.py:1400`

```python
active = [it for it in items if isinstance(it, dict) and it.get("status") == "active"]
archived = [it for it in items if isinstance(it, dict) and it.get("status") != "active"]
```

`_apply_playbook_upsert` 在 3 个不同证据 session 后将 `status` 设为 `"validated"`。但 prune 逻辑把所有非 `"active"` 的条目放入 `archived` 桶（仅保留最后 100 条）。这意味着 validated playbook 在 prune 时可能被截断，从快照中消失。

**修复**：将 `"validated"` 和 `"candidate"` 也归入 active 桶：
```python
NON_ARCHIVED_STATUSES = {"active", "candidate", "validated"}
active = [it for it in items if isinstance(it, dict) and it.get("status") in NON_ARCHIVED_STATUSES]
archived = [it for it in items if isinstance(it, dict) and it.get("status") not in NON_ARCHIVED_STATUSES]
```

---

### Bug 2（高）：`skill_manage(approve)` 未将 playbook 更新为 `promoted_to_skill`

**位置**：`backend/agent/skills/manager.py:105-118`

计划要求：
> Mark playbook `status = "promoted_to_skill"`. Promoted playbooks are excluded from snapshot.

`SkillManager.approve()` 只移动文件，不更新 playbooks JSON。已晋升的 playbook 仍保持 `lifecycle_status = "validated"`，继续占用快照 budget，永远不会被排除。

**修复**：approve 方法需接受并更新对应的 playbook_snapshot，或暴露一个 `on_approved` 回调机制，由 MemoryEngine 在 approve 成功后更新对应条目。

---

### Bug 3（中）：`remove_file` 在目标文件不存在时抛出异常

**位置**：`backend/agent/skills/manager.py:102`

```python
target.unlink()
```

若目标文件已被删除，`Path.unlink()` 抛出 `FileNotFoundError`。该异常未被 `manage()` 的外层 try/except 捕获（捕获在 `tools.py:109`，会返回 error 字典，但 stacktrace 会写入日志）。

**修复**：
```python
target.unlink(missing_ok=True)
```

---

### Bug 4（中）：playbook validation 阈值不完整

**位置**：`backend/agent/memory/memory_engine.py:986`

```python
if len(set(target["evidence_sessions"])) >= 3 and target["failure_count"] == 0:
    target["status"] = "validated"
```

计划要求：`len(evidence_sessions) >= 3 AND success_count >= 3 AND failure_count == 0`。

`success_count >= 3` 未检查。一个 playbook 可以在 3 个不同 session 各被提到一次（success_count 累加）但如果工具调用失败，success_count 可能不足 3。结果是过于宽松的 validated 状态。

**修复**：加入 `success_count` 检查：
```python
if (
    len(set(target["evidence_sessions"])) >= 3
    and int(target.get("success_count", 0)) >= 3
    and target["failure_count"] == 0
):
```

---

### Bug 5（中）：`DELETE /api/memory/{memory_id}` body 参数不符合 REST 规范

**位置**：`backend/api_gateway/app.py:944-956`

```python
@app.delete("/api/memory/{memory_id}")
def delete_memory(memory_id: str, req: MemoryDeleteReq | None = None):
```

HTTP DELETE 一般不携带 body；部分 HTTP 客户端和代理会丢弃 DELETE 请求体。`scope` 和 `reason` 应改为查询参数：

```python
@app.delete("/api/memory/{memory_id}")
def delete_memory(memory_id: str, scope: str = "long_term", reason: str = ""):
```

---

### Bug 6（低）：`update_memory_manual` 对 `user_profile` 范围只匹配 key，不匹配 memory_id

**位置**：`backend/agent/memory/memory_engine.py:498-511`

当调用方只传 `memory_id` 而不传 `key` 时，user_profile 范围的 update 直接返回 `None`（找不到），而其他范围可通过 memory_id 匹配。

**修复**：user_profile 范围也应支持 `target_id` 匹配（profile 条目通常无独立 ID，但应至少统一行为或在文档中说明）。

---

### Bug 7（低）：`PlaybookReviewer.review_snapshot()` 用 `status` 而非 `lifecycle_status` 判断 validated

**位置**：`backend/agent/skills/playbook_reviewer.py:65`

```python
if str(item.get("status") or "") != "validated":
    continue
```

这与 `_apply_playbook_upsert` 的行为一致（同时写 `status` 和 `lifecycle_status`），但违背了 `lifecycle_status` 作为规范字段的设计初衷。若后续 Bug 1 修复将 `status` 改回 `"active"` 并只用 `lifecycle_status` 区分生命周期，这里也需同步更新。

---

## 4. 可改进的设计问题

### 4.1 Scoped Memory 未默认激活

`IGRAPH_USE_SCOPED_MEMORY=1` 环境变量控制是否使用 `~/.igraph/memory/`。默认仍使用 repo 本地路径，与计划 "After migration, repo-local storage should be treated as legacy fallback only" 的目标相反。建议在 `MemoryEngine.__init__` 中将 scoped 模式设为默认，repo 本地路径作为显式 opt-out fallback。

### 4.2 `build_skills_system_prompt` 每次触发 filesystem glob

`SkillRegistry.discover()` 每次调用都执行 `sorted(root.glob("*/SKILL.md"))`，无进程级缓存。在高并发或工具密集场景下，每轮 LLM 调用都会多次 glob 文件系统。建议在 `SkillRegistry` 上加 TTL 缓存（例如 30 秒内复用结果）。

### 4.3 `bundled_skills_dir()` 与 Python 模块共用同一目录

`paths.py:14` 返回 `Path(__file__).resolve().parent`，即 `backend/agent/skills/`，而该目录同时存放 `guard.py`、`manager.py` 等 Python 模块。glob `*/SKILL.md` 不会误匹配 `.py` 文件，但从设计层面看，技能文件和 Python 代码混在同一目录是代码异味。长期来看建议分离（如 `backend/agent/skills/bundled/`）。

### 4.4 `memory_delete` 不在 CORE_TOOL_NAMES 中

`memory_delete` 需要先调用 `tool_select` 才能使用，增加了记忆清理的操作成本。考虑是否加入核心工具集。

### 4.5 `sub-agent payload` 中 playbook 字段不完整

`_build_sub_agent_payload` 传给记忆子代理的 `existing_playbooks` 只包含 `playbook_id`、`problem_type`、`approach`、`trigger_terms`、`tool_sequence`、`confidence`。新增的 `lifecycle_status`、`evidence_sessions`、`success_count`、`failure_count` 均未传递，子代理无法基于证据做自适应更新决策。

### 4.6 `/api/memory/usage` 信息量不足

计划要求返回：当前 session ID、memory scope、快照字符数、包含的条目 ID 列表、按原因分类的排除条数。当前实现只返回字符数和快照全文（后者可能泄露记忆内容给前端，应考虑是否合适）。

### 4.7 `knowledge_metrics` 每次读全量事件文件

`GET /api/metrics/knowledge` 每次请求都读取 memory 和 skill events 文件各最多 10000 行，进行 O(n) 线性扫描。随着文件增大，该接口会越来越慢。建议改为进程内计数器（启动时从文件重建，后续增量更新），或分离为异步批处理报告。

### 4.8 高风险工具确认逻辑脆弱

`_high_risk_tool_confirmed()` 用字符串匹配最近 8 条消息来判断用户是否已确认。这可能被包含 "confirm" 一词的无关消息误触发，也可能因系统提示注入而绕过。更可靠的做法是在工具调用链路上记录明确的确认 flag。

---

## 5. 遗漏条目汇总

以下条目在规划中明确要求但实现中未完成：

| 条目 | 规划位置 | 说明 |
|---|---|---|
| 主迁移：default_user → ~/.igraph/memory/ | D2 | 仅 env var 切换，无自动迁移 |
| `active` playbook 兼容性映射（evidence≥2 → validated，否则 candidate） | D3/F1 | 未实现，直接保留旧 active |
| 子代理 prompt 包含 success_signal/failure_signal | F2 | 未实现 |
| `skill_manage(approve)` 更新 playbook status=promoted_to_skill | F4 | 未实现 |
| `/api/memory` 请求参数 user_id/workspace_id/project_id/dataset_id | E3 | 未实现 |
| `/api/memory/usage` 完整响应（item IDs、excluded counts） | G3 | 仅返回字符数 |
| scope 路径隔离测试、两用户不互相可见 | H | 未实现 |
| reviewer candidate 生成 + approve 推广 E2E 测试 | H | 未实现 |
| 磁盘 cache manifest | A3 | 未实现 |

---

## 6. 结论

Codex 的实现覆盖了规划中约 **70%** 的内容，核心功能（skill_manage、11 个领域技能、memory_write/update 工具、PlaybookReviewer、memory API 骨架）均可用。

**必须修复的 bugs**（会导致功能不正确）：
- Bug 1：validated playbook 被 prune 逻辑误归档
- Bug 2：approve 后 playbook 未标记 promoted_to_skill，快照无法排除

**应修复的 bugs**（边界场景或接口不规范）：
- Bug 3：remove_file FileNotFoundError
- Bug 4：validation 阈值缺 success_count 检查
- Bug 5：DELETE body 参数

**合理的待实现项**（可后续迭代）：
- 主迁移（scoped memory 默认化）
- 测试覆盖补全（scope 隔离、E2E 晋升路径）
- usage API 完善
- sub-agent payload 补充新字段
