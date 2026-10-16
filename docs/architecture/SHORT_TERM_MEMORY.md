# Short-Term Context Memory

## Goal

Short-term memory keeps a long-running session usable when the active model
context approaches the configured ceiling. It tracks the current session
context token count, exposes usage to the UI, and compresses older context with
the `ContextCompressor` sub-agent when the threshold is reached.

---

## Token Accounting

- **Default limit**: `192000` tokens.
- **Override**: `SESSION_CONTEXT_TOKEN_LIMIT`.
- **Counted messages**: current `ContextManager.messages`.
- **Excluded messages**: `role=system` (static role prompts and dynamic system
  prompt blocks are never counted toward the limit).
- **Encoder**: `tiktoken` (via `context/token_counter.py`) when installed;
  otherwise a conservative local estimator (`estimate_text_tokens`) that
  counts CJK characters as 1 token each and Latin/code text as 4 chars/token.

`context_usage()` returns:

```json
{
  "current_tokens": 1234,
  "token_limit": 192000,
  "percent": 0.64,
  "status": "ok",
  "excludes": ["system"],
  "compression_count": 0
}
```

`status` 三档：`"ok"` / `"warning"` (≥80%) / `"critical"` (≥100%)。
`compression_count` 累计当前 session 内发生的压缩次数。

---

## Compression Trigger

压缩由百分比阈值控制（而非固定 token 数）：

- **触发阈值**: `CONTEXT_COMPRESSION_TRIGGER_PCT`（默认 **90**），即 `token_limit × 90%`。
- 在每个 user turn 开始时（append 新消息后）评估。若 `current_tokens ≥ threshold`，立即进入压缩流程。
- 压缩后若 `after_tokens ≥ token_limit`（压缩仍未降至 100% 以下），记录告警但不再重试。

---

## Compression Flow

1. 新的 user turn 追加到 `ContextManager.messages`。
2. iGraph emit `context_usage` SSE 事件（当前 token 状态）。
3. 若 `current_tokens ≥ threshold`，emit `context_compressing` 事件（预告压缩开始，含 `usage` 和 `threshold` 字段）。
4. `_split_context_for_compression()` 将消息拆分为三段：
   - **system_msg**：始终保留（第一条 `role=system` 消息）
   - **compressible**：从 `messages[1]` 到保留窗口起始（不含）的所有非 system 消息
   - **preserved**：保留窗口（最近 N 个已完成 user turn + 当前 user turn 及其后续上下文）
5. `ContextCompressor.compress()` 以 compressible 消息为输入，调用辅助 LLM，生成中文 Markdown 摘要。
   - 若 LLM 调用失败，fallback 到 `_fallback_context_summary()`（纯文本拼接摘要）。
6. 摘要以 `role=user` + `[CONTEXT_SUMMARY - ...]` 前缀注入（满足 API 首条非 system 消息为 user 的约束）。
7. `ContextManager.messages` 替换为 `[system_msg, summary_msg, *preserved]`。
8. iGraph emit `context_compressed`（含 `before`/`after`/`stats`）和刷新的 `context_usage` 事件。

---

## Preserve Window

- **默认**：保留最近 **3** 个 completed user turn（不含当前正在回答的 turn）。
- **覆盖**：`CONTEXT_COMPRESSION_PRESERVE_RECENT_USER_TURNS`。
- 窗口以 user turn 为单位（而非消息条数）：保留一个 user turn 时，其之后直到下一个 user turn 之前的全部 assistant/tool 消息一并保留。

压缩后活跃模型上下文结构：

```text
[system prompt]
user: [CONTEXT_SUMMARY - 历史上下文摘要，非用户实际输入]
      以下是本会话较早上下文的压缩摘要，请据此继续作答：
      <摘要内容>
[recent completed user turn N-2（含工具调用/回答）]
[recent completed user turn N-1]
[recent completed user turn N]
[当前 user turn（正在回答）]
```

---

## ContextCompressor 子代理

`ContextCompressor`（`sub_agents/context_compressor.py`）继承 `SubAgent`，专职上下文压缩。

### 输入构造

```
请压缩以下 iGraph 会话上下文。...
当前 token 使用情况: {...}
当前图状态摘要: {graph_summary}

需要压缩的上下文如下：

### 消息 1 role=user
...

### 消息 2 role=assistant
...
```

- **输入字符上限**：`_MAX_INPUT_CHARS = 120_000`。序列化后超出时截断最早内容，保留最近部分（防止压缩器自身超出模型窗口）。
- **tool_calls 截断**：每条消息的 `tool_calls` JSON blob 超出 `_MAX_TOOL_CALLS_CHARS = 500` 字符时截断为 `…[截断]`，避免大型工具结果撑爆压缩器输入。

### 输出要求

- 纯中文 Markdown 摘要。
- 优先保留：用户目标、关键约束、图状态、重要工具调用与结果、已验证结论、未完成事项、用户纠正。
- 可省略：寒暄、重复推理、失败重试细节、已被后续覆盖的中间假设。

### 参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `CONTEXT_COMPRESSOR_MAX_TOKENS` | `8192` | 压缩器回复 token 上限 |

---

## SSE 事件

| 事件 type | 字段 | 含义 |
|-----------|------|------|
| `context_usage` | `usage` | token 使用状态（每轮开始时和压缩后均 emit） |
| `context_compressing` | `usage`, `threshold` | 即将开始压缩（压缩前预告） |
| `context_compressed` | `before`, `after`, `stats` | 压缩完成，`stats` 含 `before_tokens`/`after_tokens`/`compression_count`/`preserved_recent_user_turns` |

`done` 事件也携带 `context_usage` 字段（本轮结束时的最终状态）。

---

## 持久化

可见聊天历史不删除。`SessionStore` 的 `log_context_compaction` / `log_context_compaction_async` 重写上下文参与标记：

- 旧消息记录保留在 `message_list.jsonl`，供 UI/历史导出；
- 旧消息标记 `include_in_context=false`，不再注入模型；
- 压缩摘要消息插入到保留窗口之前；
- 会话恢复时加载摘要 + 保留的近期消息。

中间步骤消息（plan_step_intermediate 类型）记录时就已标记 `include_in_context=False`，不会参与 token 计算或压缩输入。

---

## 前端

页面标题栏显示紧凑的上下文计量表：

- tooltip：`current / limit tokens（不含 system prompts）`；
- 颜色三档：`<80%` 正常 / `>=80%` 警告 / `>=100%` 危险；
- 数据来源：`create`/`activate` 响应、`context_usage` SSE 事件、`done.context_usage`；
- 收到 `context_compressing` 时显示"正在压缩"状态；
- 收到 `context_compressed` 时显示压缩完成通知（可见历史不变）。
