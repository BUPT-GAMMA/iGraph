"""
TextProtocolDriver — 文本协议模式。

特点：
- 工具描述序列化为 system prompt 中的 JSON（上下文完全透明）
- LLM 输出 <think>...</think><tool_call>...</tool_call> 或 <think>...</think><answer>...</answer>
- StatefulStreamParser 一次流式解析，不再二次 normalize/parse
- 每条对话日志可直接用于 SFT/RL 数据构造
"""

from __future__ import annotations

import json
import re
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from agent_runtime.agent.context.protocol_driver import ProtocolDriver
from agent_runtime.agent.tools.core_set import CORE_TOOL_NAMES

_CORE_TOOL_NAMES_TEXT = ", ".join(sorted(CORE_TOOL_NAMES))

# ──────────────────────────────────────────────────────────────────────────────
# StatefulStreamParser
# ──────────────────────────────────────────────────────────────────────────────

class StatefulStreamParser:
    """
    状态化流式解析器。

    消费 <think>/<tool_call>/<answer> 标签，同时产生事件 AND 积累解析结果。

    流结束后可直接读取（无需二次解析）：
      .action        "tool_calls" | "answer" | None
      .think_text    <think> 内累积文本
      .answer_text   <answer> 内累积文本
      .tool_calls    解析后的 list[dict]
      .raw_text      完整原始文本（供日志记录）

    内建容错（替代原 normalize_model_output）：
      - 只有 <think> 无后续  → flush() 后 action="answer", answer_text=think_text
      - <think> 后直接正文    → 将正文作为隐式 answer 流式输出
      - 无任何标签           → flush() 后 action="answer", answer_text=全文
      - 标签未闭合           → flush() 时自动补全处理
      - 多个动作标签         → 只保留第一个
    """

    _OPEN_TAGS = {
        "<think>": "think",
        "<tool_call>": "tool_calls",
        "<answer>": "answer",
    }
    _CLOSE_TAGS = {
        "</think>": "think",
        "</tool_call>": "tool_calls",
        "</answer>": "answer",
    }
    _ALL_TAGS = list(_OPEN_TAGS) + list(_CLOSE_TAGS)

    def __init__(self) -> None:
        self._buf: str = ""
        self._phase: Optional[str] = None
        self._closed: bool = False

        # 解析结果状态
        self._think_chunks: List[str] = []
        self._answer_chunks: List[str] = []
        self._tool_call_chunks: List[str] = []
        self._action: Optional[str] = None  # "tool_calls" | "answer"
        self._raw_chunks: List[str] = []
        self._saw_tag: bool = False
        self._implicit_answer_started: bool = False
        self._parse_error: Optional[str] = None

    # ── 公开属性 ──────────────────────────────────────────────────

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def action(self) -> Optional[str]:
        return self._action

    @property
    def think_text(self) -> str:
        return "".join(self._think_chunks).strip()

    @property
    def answer_text(self) -> str:
        return "".join(self._answer_chunks).strip()

    @property
    def raw_text(self) -> str:
        return "".join(self._raw_chunks)

    @property
    def parse_error(self) -> Optional[str]:
        return self._parse_error

    @property
    def tool_calls(self) -> List[dict]:
        """解析 <tool_call> 块内的 JSON，返回标准化的 tool_calls 列表。"""
        payload_str = "".join(self._tool_call_chunks).strip()
        if not payload_str:
            return []
        try:
            decoded = json.loads(payload_str)
        except json.JSONDecodeError:
            return []
        if not isinstance(decoded, dict):
            return []
        calls = decoded.get("tool_calls", [])
        if not isinstance(calls, list):
            return []
        result: List[dict] = []
        for i, call in enumerate(calls):
            normalized = _normalize_tool_call(call, default_index=i + 1)
            if normalized:
                result.append(normalized)
        return result

    # ── 流式接口 ──────────────────────────────────────────────────

    def feed(self, chunk: str) -> List[dict]:
        if self._closed:
            return []
        self._raw_chunks.append(chunk)
        self._buf += chunk
        return self._process()

    def flush(self) -> List[dict]:
        """流结束时调用，处理容错情况，不产生额外事件（容错仅影响属性值）。"""
        if self._closed:
            self._buf = ""
            return []

        events: List[dict] = []

        # 如果 phase 是 think 但没有后续动作标签（模型只输出了 <think>）
        # 把 think 内容当作 answer
        if self._phase == "think":
            events.append({"type": "thinking_end", "phase": "thinking"})
            self._phase = None

        # 如果整个流没有任何动作标签（无 <tool_call> / <answer>）
        # 把纯文本当作 answer；若只有 think，则沿用 think_text 作为兜底。
        if self._action is None:
            if self._think_chunks:
                fallback = self.think_text
            elif not self._saw_tag:
                fallback = self._buf.strip() or self.raw_text
            else:
                fallback = ""
            if not self._answer_chunks and fallback:
                self._answer_chunks.append(fallback)
                events.append({"type": "token", "content": fallback})
            self._action = "answer"

        self._buf = ""
        return events

    # ── 内部解析 ──────────────────────────────────────────────────

    def _process(self) -> List[dict]:
        events: List[dict] = []
        while self._buf and not self._closed:
            matched = None
            for tag in self._ALL_TAGS:
                if self._buf.startswith(tag):
                    matched = tag
                    break

            if matched:
                self._saw_tag = True
                if not self._is_valid_tag_transition(matched):
                    self._mark_invalid(f"协议标签顺序不合法: {matched}")
                    break
                self._buf = self._buf[len(matched):]
                if matched in self._OPEN_TAGS:
                    new_phase = self._OPEN_TAGS[matched]
                    # 容错：上一个 phase 未闭合就切换（自动收尾 think）
                    if self._phase == "think" and new_phase != "think":
                        events.append({"type": "thinking_end", "phase": "thinking"})
                    # 忽略重复的动作标签（多个 tool_call / answer，只保留第一个）
                    if new_phase in ("tool_calls", "answer") and self._action is not None:
                        continue
                    self._phase = new_phase
                    if new_phase == "think":
                        events.append({"type": "thinking_start", "phase": "thinking"})
                else:
                    closing_phase = self._CLOSE_TAGS[matched]
                    if self._phase == "think" and closing_phase == "think":
                        events.append({"type": "thinking_end", "phase": "thinking"})
                        self._phase = None
                        continue
                    if closing_phase in ("tool_calls", "answer"):
                        if closing_phase == "answer":
                            self._action = "answer"
                        else:
                            self._action = "tool_calls"
                        self._phase = None
                        self._closed = True
                        self._buf = ""
                continue

            safe = self._safe_flush_len()
            if safe > 0:
                text = self._buf[:safe]
                self._buf = self._buf[safe:]
                events.extend(self._emit_text(text))
            else:
                break

        return events

    def _safe_flush_len(self) -> int:
        for i in range(len(self._buf)):
            if self._buf[i] == "<":
                sub = self._buf[i:]
                if any(tag.startswith(sub) or sub.startswith(tag) for tag in self._ALL_TAGS):
                    return i
        return len(self._buf)

    def _emit_text(self, text: str) -> List[dict]:
        if not text:
            return []
        if self._phase == "think":
            self._think_chunks.append(text)
            return [{"type": "thinking", "phase": "thinking", "content": text}]
        if self._phase == "answer":
            self._answer_chunks.append(text)
            return [{"type": "token", "content": text}]
        if self._phase == "tool_calls":
            self._tool_call_chunks.append(text)
            return []  # tool_call 内容不向前端输出
        if text.strip():
            self._phase = "answer"
            self._action = "answer"
            self._implicit_answer_started = True
            self._answer_chunks.append(text)
            return [{"type": "token", "content": text}]
        return []

    def _is_valid_tag_transition(self, tag: str) -> bool:
        if self._phase == "answer":
            return tag == "</answer>"
        if self._phase == "tool_calls":
            return tag == "</tool_call>"
        if self._implicit_answer_started:
            return False
        return True

    def _mark_invalid(self, reason: str) -> None:
        self._parse_error = reason
        self._closed = True
        self._buf = ""


def _normalize_tool_call(raw_call: dict, default_index: int) -> dict:
    if not isinstance(raw_call, dict):
        return {}
    name = raw_call.get("name")
    if not isinstance(name, str) or not name.strip():
        return {}
    arguments = raw_call.get("arguments", raw_call.get("args", {}))
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}
    call_id = raw_call.get("call_id") or raw_call.get("id")
    if isinstance(call_id, str):
        call_id = call_id.strip() or None
    if not call_id:
        call_id = f"call_{default_index}"
    server = raw_call.get("server", "graph")
    return {
        "server": str(server),
        "name": name.strip(),
        "arguments": arguments,
        "id": str(call_id),
    }


# ──────────────────────────────────────────────────────────────────────────────
# TextProtocolDriver
# ──────────────────────────────────────────────────────────────────────────────

_TOOLS_PROMPT_HEADER = """\
你当前可调用的 MCP 工具列表如下（原样 JSON）。

当你需要调用工具时，必须在 `<tool_call>...</tool_call>` 中输出严格合法的 JSON，格式如下：

{
  "tool_calls": [
    {
      "server": "graph",
      "name": "tool_name",
      "arguments": {},
      "call_id": "optional_call_id"
    }
  ]
}

约束：
- `<tool_call>` 内只能输出 JSON，不要添加任何解释文字。
- `tool_calls` 必须是数组。
- 每个元素至少包含 `name` 和 `arguments`。
- `arguments` 必须是 JSON 对象。
- 若工具所属服务明确，优先填写 `server`。
- 若需要多个工具调用，请按预期执行顺序排列。
- 核心工具（__CORE_TOOL_NAMES__）的 schema 会常驻当前可用工具列表，可直接调用。对于不在当前可用工具列表中的非核心图分析业务工具，必须先调用 `tool_select` 获取完整工具调用 schema，再基于返回 schema 调用目标工具；query 要具体描述当前任务或子目标。
- 调用 `train` 时，应基于其 input schema 尽量构造完整参数 JSON；用户明确要求训练即视为本次任务授权。只提交一次训练，其全部 epoch、轮询、评估以及后续只读的 `infer` 都不得重复请求确认。
- 如果 `tool_select` 返回缺少必要工具，不要臆造工具调用，应向用户说明当前阻塞点。

可用工具 JSON：""".strip().replace("__CORE_TOOL_NAMES__", _CORE_TOOL_NAMES_TEXT)


class TextProtocolDriver(ProtocolDriver):
    """
    文本协议驱动器。

    工具描述序列化为 system prompt，LLM 按文本标签协议回复。
    上下文完全透明，适合 SFT/RL 数据采集。
    """

    def __init__(self, provider_gateway, *, logger=None) -> None:
        self._gateway = provider_gateway
        self._logger = logger
        # 工具 JSON 序列化缓存：tuple(tool names) → prompt string
        self._tools_prompt_cache: Dict[Tuple[str, ...], str] = {}

    async def stream_turn(
        self,
        messages: List[dict],
        tool_specs: List[dict],
        *,
        step: int,
    ) -> AsyncIterator[dict]:
        parser = StatefulStreamParser()

        async for text in self._gateway.stream_text(messages, step=step):
            if not text:
                continue
            for event in parser.feed(text):
                yield event
            if parser.closed:
                break

        for event in parser.flush():
            yield event

        tool_calls = parser.tool_calls
        parse_error = parser.parse_error
        if parser.action == "tool_calls" and not tool_calls and parse_error is None:
            parse_error = "tool_call JSON 无效或为空"

        yield {
            "type": "turn_end",
            "action": parser.action or "answer",
            "tool_calls": tool_calls,
            "answer_text": parser.answer_text,
            "think_text": parser.think_text,
            "raw_text": parser.raw_text,
            "parse_error": parse_error,
        }

    def build_model_messages(
        self,
        working_messages: List[dict],
        tool_specs: List[dict],
        *,
        system_prompt: str,
        language_prompt: str = "",
        memory_prompt: str = "",
        skill_prompt: str = "",
        mode_prompt: str = "",
    ) -> List[dict]:
        prefix: List[dict] = [{"role": "system", "content": system_prompt}]
        if language_prompt:
            prefix.append({"role": "system", "content": language_prompt})
        if mode_prompt:
            prefix.append({"role": "system", "content": mode_prompt})
        if memory_prompt:
            prefix.append({"role": "system", "content": memory_prompt})
        prefix.append({"role": "system", "content": self._tools_system_prompt(tool_specs)})
        if skill_prompt:
            prefix.append({"role": "system", "content": skill_prompt})
        # working_messages[0] 是旧的 system prompt，跳过，使用新的 prefix
        tail = working_messages[1:] if (working_messages and working_messages[0].get("role") == "system") else working_messages
        return [*prefix, *tail]

    def build_tool_observation(self, observations: List[dict]) -> List[dict]:
        lines: List[str] = []
        for ob in observations:
            tool_name = str(ob.get("name") or "unknown_tool")
            result = ob.get("result", "")
            lines.append(f"{tool_name} 工具调用结果为：{result}")
        content = "以下是本轮工具调用结果，请基于结果继续回答：\n" + "\n".join(lines)
        return [{"role": "user", "content": content}]

    def _tools_system_prompt(self, tool_specs: List[dict]) -> str:
        key = tuple(t.get("name", "") for t in tool_specs)
        if key not in self._tools_prompt_cache:
            import os, re as _re
            header = _TOOLS_PROMPT_HEADER
            if os.getenv("USE_TOOL_SELECT", "1").strip().lower() in {"0", "false", "no", "off"}:
                header = _re.sub(
                    r"- 核心工具（[^）]+）的 schema 会常驻当前可用工具列表，可直接调用。[^\n]+\n"
                    r"(- 如果 `tool_select` 返回缺少必要工具，不要臆造工具调用，应向用户说明当前阻塞点\。\n)?",
                    "- 所有图分析工具 schema 已全量加载，可直接调用。\n",
                    header,
                )
            tools_json = json.dumps(tool_specs, ensure_ascii=False, separators=(",", ":"))
            self._tools_prompt_cache[key] = f"{header}\n{tools_json}"
        return self._tools_prompt_cache[key]
