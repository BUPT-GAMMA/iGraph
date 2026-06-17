# iGraph Skills / Memory Remaining Implementation Plan

> Date: 2026-04-23
> Basis: `plans/hermes_skills_memory_plan_opus.md` plus current repository state.
> Constraint: **Do not change the current ToolSelector or fallback strategy.** The current `LLMToolSelector` behavior, per-turn cache, core tool set, `tool_select`, `tool_expand`, and keyword fallback semantics are treated as intentional design and out of scope for this plan.

## 1. Scope

This plan covers the remaining unimplemented or partially implemented capabilities in the Skills / Memory / Playbook governance system:

- `skill_manage` and writable user/review skills.
- User skill discovery alongside bundled skills.
- iGraph domain skill library expansion.
- MemoryScope-backed storage and migration away from hardcoded `default_user`.
- Memory governance APIs and agent-level memory write/edit tools.
- Playbook lifecycle and playbook-to-skill promotion.
- Observability, audits, and regression tests for the above.

This plan explicitly excludes:

- Changing `backend/agent/tools/selector.py` selection behavior.
- Changing selector fallback from keyword fallback.
- Making selector fallback load all tools.
- Changing how `tool_select` treats `fallback_used`.
- Changing the core tool set unless needed to expose newly introduced virtual tools.

## 2. Current Baseline

Already implemented or mostly implemented:

- Skill frontmatter parsing for basic fields: `name`, `description`, `domain`, `version`, `risk_level`, `metadata.igraph.requires_tools`, `any_tools`, `requires_graph_state`, `activates_on_intents`, and `config`.
- Compact skill index via `build_skills_system_prompt()`.
- `skills_list` / `skill_view` virtual tools.
- Skill body injection scan and supporting-file path guard.
- Skill config variable substitution from `~/.igraph/config.yaml`.
- Frozen memory snapshot with `user_profile`, `long_term_memory`, and `playbooks`.
- Memory safety scanner and atomic JSON / locked JSONL writes.
- `MemoryManager`, `MemoryProvider`, and `BuiltinMemoryProvider` skeleton.
- `memory_read` / `memory_delete` virtual tools.
- LLM tool selector, current fallback behavior, `tool_select`, and `tool_expand`.

Main remaining gaps:

- Skills are read-only bundled files; no writable user skill system.
- No `skill_manage`.
- Only three coarse bundled skills exist.
- `MemoryScope` exists as a dataclass but storage remains hardcoded to `backend/agent/memory/default_user`.
- No `/api/memory*` endpoints.
- No `memory_write` / edit virtual tool.
- Playbooks have only `active` / `archived` behavior; no candidate / validated / promoted lifecycle.
- No reviewer that produces candidate skills.
- No metric layer for skills, memory, promotion, and governance events.

## 3. Design Principles

1. Preserve the current tool-selection design.
   Skills and memory governance should integrate with the existing active-tool system through virtual tools. They should not depend on selector fallback semantics.

2. Keep bundled skills immutable at runtime.
   Agent-created or user-created skills must write only to user-owned directories. Bundled skill changes should be represented as patch suggestions, not direct edits.

3. Treat playbooks as candidates, not automatic truths.
   A playbook can become a skill only after evidence and review. Promotion should reduce prompt duplication, not amplify it.

4. Keep memory writes governable.
   Manual writes and edits must pass the same safety scan and audit path as background memory writes.

5. Migrate incrementally.
   Existing `backend/agent/memory/default_user` data must continue to load during transition, with idempotent migration and rollback-friendly backups.

6. Preserve session-level snapshot stability.
   Writes during a session update disk/live state only; frozen prompt contents change in the next session.

## 4. Target Directory Layout

### 4.1 Skills

```text
backend/agent/skills/
  data_ingestion/SKILL.md
  graph_analysis/SKILL.md
  graph_machine_learning/SKILL.md
  ... bundled domain skills ...

~/.igraph/skills/
  user/
    <skill_name>/SKILL.md
    <skill_name>/supporting-file.md
  .review_queue/
    <candidate_skill_name>/SKILL.md
    <candidate_skill_name>/metadata.json
```

Bundled skills are discovered first. User skills override only when names are unique; duplicate names are rejected by default unless explicitly namespaced.

### 4.2 Memory

```text
~/.igraph/memory/
  default_workspace/
    default_user/
      profile.json
      playbooks.json
      events.jsonl
      projects/
        default_project/memory.json
      datasets/
        <dataset_id>/memory.json
      legacy_backup/
```

During migration, the existing repository-local storage remains readable:

```text
backend/agent/memory/default_user/
  long_term_memory.json
  user_profile.json
  problem_solving_playbooks.json
  memory_events.jsonl
```

After migration, repository-local storage should be treated as legacy fallback only.

## 5. Phase A: Skill Governance Foundation

### A1. Extend Skill Models

Files:

- `backend/agent/skills/models.py`
- `backend/agent/skills/registry.py`

Add fields while keeping backward compatibility:

```python
@dataclass(frozen=True)
class SkillCondition:
    requires_tools: tuple[str, ...] = ()
    any_tools: tuple[str, ...] = ()
    fallback_for_tools: tuple[str, ...] = ()
    requires_graph_state: str = "none"
    activates_on_intents: tuple[str, ...] = ()
    supports_protocols: tuple[str, ...] = ("text", "native")
    tags: tuple[str, ...] = ()
    related_skills: tuple[str, ...] = ()

@dataclass(frozen=True)
class SkillSpec:
    name: str
    description: str
    domain: str
    path: str
    body: str
    version: str = "1.0.0"
    risk_level: str = "medium"
    source: str = "bundled"  # bundled | user | review_queue
    condition: SkillCondition = field(default_factory=SkillCondition)
    config_keys: tuple[str, ...] = ()
```

Rules:

- Unknown frontmatter fields are ignored but retained in an optional metadata dict if useful.
- Malformed skills are logged and skipped, not fatal.
- `supports_protocols` filters by current protocol only if present.
- `fallback_for_tools` means: show the skill only if at least one fallback target tool is unavailable and the skill otherwise matches.

### A2. Add Skill Directories

Files:

- `backend/agent/skills/registry.py`
- optional `backend/agent/skills/paths.py`

Discovery order:

1. Bundled: `backend/agent/skills/*/SKILL.md`
2. User: `~/.igraph/skills/user/*/SKILL.md`
3. Review queue: `~/.igraph/skills/.review_queue/*/SKILL.md` only when listing with `include_review_queue=true`; not active by default.

Default `SkillRegistry()` should include bundled + user skills. Review queue should be opt-in.

Conflict policy:

- Same `name` in bundled and user dirs is rejected and logged.
- A future explicit override mechanism may be added, but not in this phase.

Tests:

- bundled skills still discover.
- user skills discover without changing Python code.
- review queue skills do not appear in compact prompt by default.
- duplicate skill names are skipped deterministically.

### A3. Compact Index Cache

Files:

- `backend/agent/skills/skill_loader.py`
- optional `backend/agent/skills/cache.py`

Implement:

- Process-level LRU cache keyed by:
  - skill directories manifest
  - visible tool names hash
  - graph state hash
  - protocol
  - intent
- Disk manifest at `~/.igraph/.skills_prompt_snapshot.json` with each `SKILL.md` path, mtime, size, and source.

Important:

- Cache must not hide newly created user skills after `skill_manage`; `skill_manage` should invalidate process cache after successful writes.
- Disk cache is an optimization only. If corrupt, delete and rebuild.

Tests:

- repeated compact index calls hit cache.
- changing a skill file invalidates cache.
- `skill_manage(create)` invalidates cache.

## 6. Phase B: `skill_manage`

### B1. Add Skill Manager Module

New file:

- `backend/agent/skills/manager.py`

Core API:

```python
class SkillManager:
    def create(self, *, name: str, body: str, metadata: dict, destination: str) -> dict: ...
    def patch(self, *, name: str, patch: str, destination: str) -> dict: ...
    def write_file(self, *, name: str, file_path: str, content: str, destination: str) -> dict: ...
    def remove_file(self, *, name: str, file_path: str, destination: str) -> dict: ...
    def approve(self, *, candidate_name: str, target_name: str | None = None) -> dict: ...
    def reject(self, *, candidate_name: str, reason: str) -> dict: ...
```

Destinations:

- `user`: `~/.igraph/skills/user/<name>/`
- `review_queue`: `~/.igraph/skills/.review_queue/<name>/`

Write rules:

- Never write to `backend/agent/skills` from agent runtime.
- `name` must match `^[a-z][a-z0-9_\\-]{2,80}$`.
- `file_path` must be relative, stay within the skill directory, and must not write outside allowed roots.
- `SKILL.md` must be the main entrypoint.
- File size limit: default 64 KB per file, 256 KB total per skill directory.
- Body and supporting files must pass `scan_skill_content()`.
- Frontmatter must include at least `name`, `description`, `domain`, and `risk_level`.
- `risk_level=high` skills can be created in review queue but not auto-approved.

Patch format:

- Prefer a simple unified diff patch for `patch`.
- If patch support is too large for the first implementation, implement `create`, `write_file`, `remove_file`, `approve`, `reject` first, and reserve `patch` as returning an explicit unsupported error.

Audit:

```text
~/.igraph/skills/skill_events.jsonl
```

Events:

- `skill_created`
- `skill_file_written`
- `skill_file_removed`
- `skill_approved`
- `skill_rejected`
- `skill_write_rejected`

### B2. Expose `skill_manage`

File:

- `backend/agent/skills/tools.py`

Add virtual tool schema:

```json
{
  "name": "skill_manage",
  "description": "创建、维护、审批用户或候选 iGraph skill。不能修改 bundled skills。",
  "inputSchema": {
    "type": "object",
    "properties": {
      "action": {"enum": ["create", "write_file", "remove_file", "approve", "reject"]},
      "destination": {"enum": ["user", "review_queue"]},
      "name": {"type": "string"},
      "body": {"type": "string"},
      "metadata": {"type": "object"},
      "file_path": {"type": "string"},
      "content": {"type": "string"},
      "reason": {"type": "string"}
    },
    "required": ["action", "name"]
  }
}
```

iGraph integration:

- Add `skill_manage` to `VIRTUAL_SKILL_TOOL_SPECS`.
- Add `skill_manage` to `CORE_TOOL_NAMES` only if we want it always available to the main agent.
- Recommended: add it to core tools because it is agent-level governance and should not depend on selector. This does not change selector behavior.

Tests:

- `skill_manage(create)` writes only to user/review dirs.
- bundled skill modification is rejected.
- path traversal is rejected.
- injection text is rejected.
- newly created user skill appears in `skills_list`.
- newly created user skill appears in compact index after cache invalidation.

## 7. Phase C: Domain Skill Library Expansion

### C1. Split Coarse Skills Into Domain Skills

Create bundled skills:

1. `data_ingestion_table`
2. `data_ingestion_text`
3. `graph_state_check`
4. `graph_analysis_centrality`
5. `graph_analysis_community`
6. `graph_path_connectivity`
7. `graph_ml_training`
8. `graph_ml_inference`
9. `dataset_management`
10. `graph_rag`
11. `experiment_reporting`

Existing coarse skills can remain during a transition, but should be either:

- marked as umbrella skills with broad descriptions, or
- archived by removing from active discovery after all split skills have tests.

Each `SKILL.md` should use:

```markdown
---
name: graph_ml_training
description: GraphML 训练前配置确认、参数解释、用户确认和训练后结果复核流程。
version: 1.0.0
domain: graph_ml
risk_level: high
metadata:
  igraph:
    tags: [graph-ml, training]
    requires_tools: [get_train_config]
    any_tools: [train, visualize_model_architecture]
    requires_graph_state: loaded
    activates_on_intents: [graph_ml_train]
---

# GraphML Training
## When to Use
## Required Checks
## Procedure
## Tool Sequence Patterns
## Pitfalls
## Verification
```

### C2. High-Risk Skill Enforcement

Current state: high-risk behavior is text-only in the skill body.

Target:

- Add a lightweight guard in `iGraph._run_tool()` or a dedicated policy module.
- If a high-risk tool is called, check whether the active context has shown a relevant high-risk skill or whether the user has explicitly confirmed.
- For first implementation, enforce only GraphML `train` and `infer`.

Suggested minimal policy:

```python
HIGH_RISK_TOOLS = {"train", "infer"}
CONFIRMATION_REQUIRED_TOOLS = {"train", "infer"}
```

Before calling those MCP tools:

- If the previous assistant message did not present complete parameters, return a policy error.
- If the latest user message does not contain explicit confirmation after parameter presentation, return a policy error.

This can start as a conservative string/flag check using run state, then be improved.

Tests:

- `train` without confirmation is blocked.
- `train` after parameter presentation and explicit confirmation is allowed.
- ordinary graph analysis tools are unaffected.
- GraphML skill appears only when relevant tools are visible and graph state conditions match.

## 8. Phase D: MemoryScope Real Storage

### D1. Introduce Scope Resolver

New files:

- `backend/agent/memory/paths.py`
- optional `backend/agent/memory/schemas.py`

Core API:

```python
class MemoryPathResolver:
    def root(self) -> Path: ...
    def scope_dir(self, scope: MemoryScope) -> Path: ...
    def profile_path(self, scope: MemoryScope) -> Path: ...
    def project_memory_path(self, scope: MemoryScope) -> Path: ...
    def dataset_memory_path(self, scope: MemoryScope) -> Path | None: ...
    def playbooks_path(self, scope: MemoryScope) -> Path: ...
    def events_path(self, scope: MemoryScope) -> Path: ...
```

Default scope mapping:

- `workspace_id or "default_workspace"`
- `user_id or "default_user"`
- `project_id or "default_project"`

`MemoryEngine` should receive a resolver or root path and compute paths per scope instead of assigning fixed paths in `__init__`.

### D2. Keep Compatibility With Current Files

Migration source:

- `backend/agent/memory/default_user/long_term_memory.json`
- `backend/agent/memory/default_user/user_profile.json`
- `backend/agent/memory/default_user/problem_solving_playbooks.json`
- `backend/agent/memory/default_user/memory_events.jsonl`
- legacy nested `backend/agent/memory/memory/default_user/`

Migration target:

- `~/.igraph/memory/default_workspace/default_user/projects/default_project/memory.json`
- `~/.igraph/memory/default_workspace/default_user/profile.json`
- `~/.igraph/memory/default_workspace/default_user/playbooks.json`
- `~/.igraph/memory/default_workspace/default_user/events.jsonl`

Migration behavior:

- Idempotent `.migrated_from_repo_default_user` flag in target scope dir.
- Merge by `memory_id` if present, else by stable key.
- Copy old events to `legacy_backup` and append a `memory_migrated` event.
- Do not delete repository-local files in migration code; deletion/cleanup can be a separate repo maintenance commit.

### D3. Scope-Aware MemoryEngine

Refactor `MemoryEngine` methods to accept an optional scope:

```python
def load_and_freeze(self, scope: MemoryScope | None = None) -> str: ...
def query_memories(self, scope_name: str, query: str = "", limit: int = 10, memory_scope: MemoryScope | None = None) -> list[dict]: ...
def delete_memory(self, scope_name: str, memory_id: str, reason: str = "", session_id: str = "", memory_scope: MemoryScope | None = None) -> dict | None: ...
def upsert_memory_manual(..., memory_scope: MemoryScope | None = None) -> dict: ...
```

Internal `_load_*` / `_save_*` helpers should become path-parameterized or scope-aware.

Snapshot inclusion rules:

- Always include user profile.
- Include current project memory.
- Include dataset memory only if `dataset_id` or `graph_fingerprint` is present.
- Include playbooks with `status in {"validated"}` by default.
- Exclude `candidate`, `archived`, and `promoted_to_skill` playbooks from snapshot.
- During transition, `active` playbooks can be treated as `validated` only if a compatibility flag is enabled; recommended default is:
  - first migration maps `active` to `validated` only when `evidence_sessions` length >= 2 or `hit_count >= 2`.
  - otherwise map `active` to `candidate`.

Tests:

- two user scopes do not see each other's memory.
- project memory does not leak across project IDs.
- dataset memory loads only for matching dataset scope.
- old repo-local default memory migrates once.
- frozen snapshot remains stable within one session.

## 9. Phase E: Memory Governance API and Tools

### E1. Memory Service Layer

New file:

- `backend/agent/memory/service.py`

Responsibilities:

- list memory by scope and kind.
- manual create/update/delete.
- read events.
- build current snapshot usage summary.
- call `scan_memory_content()` before writes.
- append audit events for manual operations.

Data kinds:

- `profile`
- `long_term`
- `project`
- `dataset`
- `playbook`

### E2. Virtual Tools

File:

- `backend/agent/memory/tools.py`

Add:

- `memory_write`
- `memory_update`

Recommended schemas:

```json
{
  "name": "memory_write",
  "description": "手动写入长期记忆或用户档案。写入会经过安全扫描，并在下次会话快照生效。",
  "inputSchema": {
    "type": "object",
    "properties": {
      "scope": {"enum": ["long_term", "user_profile", "project", "dataset"]},
      "category": {"type": "string"},
      "key": {"type": "string"},
      "value": {"type": "string"},
      "reason": {"type": "string"},
      "confidence": {"type": "number"}
    },
    "required": ["scope", "key", "value"]
  }
}
```

```json
{
  "name": "memory_update",
  "description": "编辑已有长期记忆、用户档案或题型经验。写入会经过安全扫描。",
  "inputSchema": {
    "type": "object",
    "properties": {
      "scope": {"enum": ["long_term", "user_profile", "project", "dataset", "playbook"]},
      "memory_id": {"type": "string"},
      "key": {"type": "string"},
      "value": {"type": "string"},
      "reason": {"type": "string"}
    },
    "required": ["scope"]
  }
}
```

Add to `CORE_TOOL_NAMES`:

- `memory_write`
- optional `memory_update`

Rationale: memory governance tools are agent-level and should not depend on selector. This does not change ToolSelector behavior.

### E3. HTTP API

File:

- `backend/api_gateway/app.py`

Routes:

```text
GET    /api/memory
POST   /api/memory
PATCH  /api/memory/{memory_id}
DELETE /api/memory/{memory_id}
GET    /api/memory/events
GET    /api/memory/usage
```

Request parameters:

- `user_id`
- `workspace_id`
- `project_id`
- `dataset_id`
- `kind`
- `query`
- `limit`

Response principles:

- Do not expose hidden system prompt.
- Return the rendered frozen memory snapshot for `/usage`, plus item IDs included in that snapshot.
- Include source session ID, timestamps, confidence, and status.
- For delete, use soft delete.

Tests:

- API list returns seeded profile / memory / playbook entries.
- POST rejects injection payloads.
- PATCH updates target item and records event.
- DELETE archives item and next snapshot excludes it.
- events endpoint returns manual and background events.

## 10. Phase F: Playbook Lifecycle and Promotion

### F1. Playbook Schema

Extend playbook entries:

```python
@dataclass
class PlaybookEntry:
    playbook_id: str
    problem_type: str
    approach: str
    trigger_terms: list[str]
    tool_sequence: list[str]
    confidence: float
    evidence_sessions: list[str]
    success_count: int = 0
    failure_count: int = 0
    hit_count: int = 0
    status: str = "candidate"  # candidate | validated | promoted_to_skill | archived
    candidate_skill_name: str | None = None
    promotion_reason: str | None = None
    last_validated_at: str = ""
    source_session_id: str = ""
    created_at: str = ""
    updated_at: str = ""
    last_seen_at: str = ""
```

Compatibility:

- Existing `active` maps to:
  - `validated` if it has enough evidence.
  - `candidate` otherwise.
- Existing `archived` remains `archived`.

### F2. Update Memory Sub-Agent Prompt

File:

- `backend/agent/memory/memory_engine.py`

Change playbook prompt semantics:

- `upsert_playbook` creates or updates a `candidate` unless evidence threshold is met.
- Include `success_signal` and `failure_signal` when available from turn data.
- Existing playbooks passed to the sub-agent include status and evidence fields.

Rules:

- New playbooks start as `candidate`.
- Candidate is not injected into frozen snapshot.
- Candidate can become `validated` when:
  - `len(evidence_sessions) >= 3`
  - `success_count >= 3`
  - `failure_count == 0`
  - tool sequence stability check passes.
- `promoted_to_skill` is excluded from snapshot.

### F3. Playbook Reviewer

New file:

- `backend/agent/skills/playbook_reviewer.py`

Responsibilities:

- scan validated playbooks not yet promoted.
- decide candidate skill name.
- render `SKILL.md` into review queue.
- render `metadata.json` with evidence.
- update playbook with `candidate_skill_name` and `promotion_reason`.

Trigger points:

- after background memory update, if playbook changed.
- optional manual API/virtual tool action:
  - `memory_update(scope="playbook", action="review")`
  - or `skill_manage(action="create_from_playbook")`

Candidate generation template:

```markdown
---
name: <candidate_skill_name>
description: <one sentence from problem_type and approach>
version: 0.1.0
domain: <inferred_domain>
risk_level: <low|medium|high>
metadata:
  igraph:
    tags: [generated, playbook]
    any_tools: [...]
    requires_graph_state: none
---

# <Title>

## When to Use
<trigger terms and problem type>

## Procedure
<approach rewritten as steps>

## Tool Sequence Patterns
<tool sequence>

## Verification
<what evidence should be checked before considering the task done>

## Provenance
- Generated from playbook: <playbook_id>
- Evidence sessions: <count>
```

High-risk policy:

- Any tool in `{"train", "infer", "delete_*", "remove_*", "external_api"}` makes candidate `risk_level: high`.
- High-risk candidates stay in review queue until explicit approval.

### F4. Promotion Approval

Use `skill_manage(approve)`:

- Move `~/.igraph/skills/.review_queue/<name>/` to `~/.igraph/skills/user/<name>/`.
- Mark playbook `status = "promoted_to_skill"`.
- Set `promotion_reason`.
- Append skill and memory events.
- Invalidate skill caches.

Use `skill_manage(reject)`:

- Keep or remove candidate files based on `remove_files` option.
- Mark playbook as `validated` or `archived` depending on reason.
- Append event.

Tests:

- playbook with insufficient evidence remains candidate and does not enter snapshot.
- playbook with sufficient success becomes validated.
- reviewer generates candidate skill files.
- approving candidate makes it discoverable in next `SkillRegistry.discover()`.
- promoted playbook is excluded from memory snapshot.
- high-risk candidate cannot auto-approve.

## 11. Phase G: Observability

### G1. Event Logs

Add or standardize:

```text
~/.igraph/skills/skill_events.jsonl
~/.igraph/memory/<workspace>/<user>/events.jsonl
logs/tool_selection_events.jsonl  # existing
```

Skill event payload:

```json
{
  "ts": "...",
  "type": "skill_created",
  "skill_name": "...",
  "source": "user|review_queue",
  "actor": "agent|user|reviewer",
  "session_id": "...",
  "result": "ok|rejected",
  "reason": "..."
}
```

Memory event payload should include:

- scope fields.
- memory kind.
- item ID.
- operation.
- result.
- reason.
- snapshot impact when applicable.

### G2. Metrics API

Add lightweight in-process counters first; avoid new dependencies.

Counters:

- `skill_index_build_count`
- `skill_index_cache_hit_count`
- `skill_view_call_count`
- `skill_manage_create_count`
- `skill_manage_reject_count`
- `memory_write_count`
- `memory_reject_count`
- `memory_delete_count`
- `snapshot_char_count`
- `snapshot_truncation_count`
- `playbook_candidate_count`
- `playbook_validated_count`
- `playbook_promoted_count`

Expose:

```text
GET /api/metrics/knowledge
```

If metrics endpoint feels too broad, defer HTTP exposure and keep JSONL events plus tests.

### G3. Snapshot Usage

Implement `GET /api/memory/usage`:

- current session ID.
- memory scope.
- frozen snapshot char count.
- included item IDs.
- excluded item counts by reason:
  - archived
  - candidate
  - promoted_to_skill
  - char_budget
  - wrong_scope

Tests:

- char-budget truncation increments excluded count.
- candidate/promoted playbooks are reported as excluded.

## 12. Phase H: Test Matrix

### Unit Tests

Skills:

- frontmatter parser with all fields.
- user skill discovery.
- review queue opt-in discovery.
- duplicate skill name conflict.
- compact index cache invalidation.
- `skill_manage` create/write/remove/approve/reject.
- path traversal and injection rejection.

Memory:

- scope path resolver.
- default legacy migration.
- profile/project/dataset isolation.
- manual memory write/update/delete.
- safety scanner applied to manual writes.
- snapshot usage item accounting.

Playbooks:

- active-to-candidate/validated migration.
- candidate excluded from snapshot.
- validated included in snapshot.
- promoted excluded from snapshot.
- reviewer candidate generation.
- approval moves files and updates playbook status.

API:

- `/api/memory` CRUD.
- `/api/memory/events`.
- `/api/memory/usage`.
- optional `/api/metrics/knowledge`.

### Integration Tests

- text protocol can call `skill_manage`.
- native protocol can call `skill_manage`.
- text protocol can call `memory_write` / `memory_update`.
- native protocol can call `memory_write` / `memory_update`.
- newly approved skill appears in next session compact index.
- memory deleted through API disappears in next session snapshot.

### E2E Tests

1. Repeated successful graph path task:
   - produces candidate playbook.
   - validates after evidence threshold.
   - reviewer generates review queue skill.
   - approval promotes skill.
   - promoted playbook no longer occupies snapshot budget.

2. User skill creation:
   - user asks agent to remember a workflow as a skill.
   - `skill_manage(create)` writes user skill.
   - next turn/session sees it in `skills_list`.

3. Memory governance:
   - user writes a preference via chat.
   - agent stores it.
   - API lists it.
   - user deletes it.
   - next session snapshot excludes it.

## 13. Migration Plan

### Step 1: Add User Skill Discovery

Low risk. Does not affect existing bundled skills if no user dir exists.

Acceptance:

- existing skill tests pass.
- new user skill test passes.

### Step 2: Add `skill_manage`

Medium risk because it writes files. Keep writes restricted to `~/.igraph/skills`.

Acceptance:

- injection/path tests pass.
- no bundled skill can be modified.

### Step 3: Add Memory Service and API Over Current Storage

Implement CRUD over current `default_user` files before full scope migration.

Acceptance:

- `/api/memory` works for current storage.
- virtual `memory_write` works.

### Step 4: Add Scope Resolver and Migration

Move implementation to scope-aware paths while preserving legacy fallback.

Acceptance:

- default user data migrates once.
- two-scope isolation tests pass.

### Step 5: Playbook Lifecycle

Introduce candidate/validated/promoted states while retaining compatibility with existing `active`.

Acceptance:

- current active playbooks are handled deterministically.
- snapshot rules match lifecycle.

### Step 6: Reviewer and Promotion

Generate candidate skills from validated playbooks.

Acceptance:

- candidate files appear in review queue.
- approval promotes skill and excludes playbook from snapshot.

### Step 7: Domain Skill Expansion

Add split domain skills with golden prompt tests.

Acceptance:

- at least 8 domain skills land first.
- all 11 land before closing the milestone.

### Step 8: Metrics and E2E Tests

Add event counters and scenario tests after behavior stabilizes.

Acceptance:

- knowledge metrics endpoint or event-based report exists.
- promotion and memory governance E2E tests pass.

## 14. Implementation Order by Commit

Recommended commits:

1. `skills: discover user and review-queue skills`
2. `skills: add skill manager and skill_manage virtual tool`
3. `memory: add governance service and manual write/update tools`
4. `api: expose memory governance endpoints`
5. `memory: introduce scope-aware storage resolver`
6. `memory: migrate default_user storage to scoped memory root`
7. `memory: add playbook lifecycle states`
8. `skills: generate review-queue skills from validated playbooks`
9. `skills: approve and reject promoted playbook skills`
10. `skills: split bundled graph domain skills`
11. `observability: add knowledge events and usage metrics`
12. `tests: add knowledge-system e2e coverage`

## 15. Risk Register

| Risk | Impact | Mitigation |
|---|---|---|
| Agent writes unsafe skill files | Prompt injection / file escape | restrict roots, path guard, content scan, size limits, event logs |
| Skill library becomes noisy | worse compact index and behavior | review queue, evidence thresholds, high-risk approval |
| Scoped migration loses memory | data loss | copy-first migration, backups, idempotent flags, merge tests |
| Existing `active` playbooks disappear unexpectedly | loss of useful experience | compatibility mapping and usage report |
| API exposes sensitive memory | privacy issue | local-only API, scoped responses, no hidden system prompt, audit events |
| Cache hides newly created skills | stale behavior | cache invalidation on skill writes and manifest checks |
| High-risk tool guard blocks valid work | UX regression | start with GraphML only, explicit confirmation path, clear error message |

## 16. Acceptance Criteria

The remaining implementation is complete when:

- `skill_manage` can create, write, approve, and reject skills under allowed user-owned directories.
- `SkillRegistry` discovers bundled and user skills; review queue is opt-in.
- At least 11 domain skills exist with golden tests.
- Memory data is stored and loaded by `MemoryScope`, with old default data migrated safely.
- `/api/memory*` endpoints support list, create, patch, delete, events, and usage.
- `memory_write` and `memory_update` virtual tools exist and pass safety scans.
- Playbooks use candidate / validated / promoted / archived lifecycle.
- Validated playbooks can generate candidate skills in review queue.
- Approved candidate skills are discoverable in the next session.
- Promoted playbooks no longer occupy memory snapshot budget.
- Knowledge events and snapshot usage are observable.
- Tests cover unit, integration, and at least one promotion E2E path.

## 17. Explicit Non-Goals

- Do not change `LLMToolSelector` fallback behavior.
- Do not change `tool_select` failure behavior when `fallback_used=True`.
- Do not reintroduce embedding-based selection.
- Do not expose skills through MCP server.
- Do not let the agent modify bundled skills at runtime.
- Do not store one-off task progress in long-term memory.
