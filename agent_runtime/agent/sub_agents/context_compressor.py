from __future__ import annotations

import json
import os
from typing import Any, Iterable

from agent_runtime.agent.sub_agents.base import SubAgent


class ContextCompressor(SubAgent):
    name = "context_compressor"
    default_max_tokens = int(os.getenv("CONTEXT_COMPRESSOR_MAX_TOKENS", "8192"))
    default_temperature = 0.0
    system_prompt = """\
你是 iGraph 的 context_compressor sub-agent，负责把当前会话较早的上下文压缩为短期记忆摘要。

目标：
- 在明显减少 token 的同时，尽量保留该会话后续推理仍需要的核心内容。
- 摘要必须忠实于输入上下文，不得添加未出现的事实、图结构、工具结果或用户偏好。
- 优先保留：用户目标、关键约束、已加载/构造的图状态、重要工具调用及结果、已验证结论、未完成事项、用户明确纠正和当前决策。
- 可以省略：寒暄、重复推理、失败重试细节、被后续结果覆盖的中间假设。

输出要求：
- 只输出中文 Markdown 摘要。
- 使用紧凑结构，适合重新注入为后续模型上下文。
- 如果没有可保留内容，输出“暂无可压缩的历史上下文。”
""".strip()

    # Rough char budget for the serialised history fed to the compressor.
    # Keeps the compressor's own input well below typical model context limits.
    _MAX_INPUT_CHARS = 120_000

    # Max chars kept per tool_calls blob to avoid inflating compressor input
    # with large raw JSON payloads.
    _MAX_TOOL_CALLS_CHARS = 500

    @staticmethod
    def _message_lines(
        messages: Iterable[dict[str, Any]],
        *,
        max_tool_calls_chars: int = 500,
    ) -> str:
        rows: list[str] = []
        for idx, msg in enumerate(messages, 1):
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "unknown")
            content = msg.get("content")
            if isinstance(content, (dict, list)):
                try:
                    content_text = json.dumps(content, ensure_ascii=False)
                except Exception:
                    content_text = str(content)
            else:
                content_text = str(content or "")
            if msg.get("tool_calls"):
                try:
                    tool_calls_str = json.dumps(msg.get("tool_calls"), ensure_ascii=False)
                except Exception:
                    tool_calls_str = str(msg.get("tool_calls"))
                if len(tool_calls_str) > max_tool_calls_chars:
                    tool_calls_str = tool_calls_str[:max_tool_calls_chars] + "…[截断]"
                content_text = f"{content_text}\n[tool_calls]\n{tool_calls_str}".strip()
            rows.append(f"### 消息 {idx} role={role}\n{content_text}".rstrip())
        return "\n\n".join(rows)

    def build_prompt(
        self,
        *,
        messages: list[dict[str, Any]],
        current_usage: dict[str, Any],
        graph_summary: str,
    ) -> str:
        lines = self._message_lines(
            messages, max_tool_calls_chars=self._MAX_TOOL_CALLS_CHARS
        )
        # Guard: if serialised history still exceeds char budget, keep only the
        # most recent portion so the compressor call itself won't exceed the
        # model's context window.
        if len(lines) > self._MAX_INPUT_CHARS:
            lines = "…[较早内容已截断，以下为最近上下文]\n\n" + lines[-self._MAX_INPUT_CHARS :]
        return (
            "请压缩以下 iGraph 会话上下文。"
            "这些内容将被替换为一条短期记忆摘要，后续模型只能看到摘要和未压缩的新消息。\n\n"
            f"当前 token 使用情况: {json.dumps(current_usage, ensure_ascii=False)}\n"
            f"当前图状态摘要: {graph_summary or 'graph_loaded=false'}\n\n"
            "需要压缩的上下文如下：\n\n"
            f"{lines}"
        )

    async def compress(
        self,
        *,
        messages: list[dict[str, Any]],
        current_usage: dict[str, Any],
        graph_summary: str,
    ) -> str:
        prompt = self.build_prompt(
            messages=messages,
            current_usage=current_usage,
            graph_summary=graph_summary,
        )
        text = await self.invoke_text(prompt)
        return str(text or "").strip()
