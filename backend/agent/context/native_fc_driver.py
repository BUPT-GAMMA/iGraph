"""
NativeFCDriver — 原生 Function Calling 模式。

特点：
- 工具通过 OpenAI API 的 tools 参数传入（不写入 system prompt）
- LLM 输出结构化 tool_calls 字段，无需文本解析
- 支持 reasoning_content（推理模型 o3/o4-mini 等）
- staged 模式下支持解析 delta.content 中的 <think> 标签产生 thinking 事件
- 对前端产生与 TextProtocolDriver 完全相同的事件类型
"""

from __future__ import annotations

import json
from typing import AsyncIterator, Dict, List, Optional

from backend.agent.context.protocol_driver import ProtocolDriver


class NativeFCDriver(ProtocolDriver):
    """
    原生 Function Calling 驱动器。

    注意：此模式下对话日志不包含工具描述的完整 prompt 结构，
    不适合直接用于 SFT/RL 数据构造（工具注入格式由 API 内部决定）。
    适用于纯推理部署场景。

    消息历史策略：
    - turn_end 携带 openai_tool_calls（原始 OpenAI 格式），供 igraph 写入 assistant 消息
    - build_tool_observation 生成 role=tool 消息（不经 role=user 转换）
    - create_streaming_completion 使用 native_fc=True（保留 role=tool）
    """

    def __init__(self, provider_gateway, *, logger=None) -> None:
        self._gateway = provider_gateway
        self._logger = logger

    async def stream_turn(
        self,
        messages: List[dict],
        tool_specs: List[dict],
        *,
        step: int,
    ) -> AsyncIterator[dict]:
        # 将 tool_specs 转为 OpenAI tools 格式
        tools = [
            {
                "type": "function",
                "function": {
                    "name": spec["name"],
                    "description": spec.get("description", ""),
                    "parameters": spec.get("inputSchema", {"type": "object", "properties": {}}),
                },
            }
            for spec in tool_specs
            if spec.get("name")
        ]

        thinking_started = False
        # 区分 reasoning_content（推理模型）和普通 content（可能含 <think> 标签）
        content_buf = ""         # delta.content 积累
        think_buf = ""           # 从 content 解析出的 <think> 内容
        answer_buf = ""          # 从 content 解析出的 answer 内容（<think> 之外的部分）
        in_think_tag = False     # 是否正在解析 <think> 块

        tc_buf: Dict[int, Dict] = {}  # index → {id, name, arguments}

        response = await self._gateway.create_streaming_completion(
            messages=messages,
            tools=tools if tools else None,
            step=step,
            native_fc=True,  # 保留 role=tool 消息，不转为 role=user
        )

        async for chunk in response:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if delta is None:
                continue

            # ── reasoning_content（推理模型：o3/o4-mini/etc）──
            rc = getattr(delta, "reasoning_content", None)
            if rc:
                if not thinking_started:
                    thinking_started = True
                    yield {"type": "thinking_start", "phase": "thinking"}
                yield {"type": "thinking", "phase": "thinking", "content": rc}
                continue

            # ── 普通文本 content ──
            if delta.content:
                content_buf += delta.content
                # 实时解析 <think> 标签（staged 模式下 LLM 可能在工具调用前输出思考）
                text = delta.content
                while text:
                    if in_think_tag:
                        end = text.find("</think>")
                        if end >= 0:
                            fragment = text[:end]
                            think_buf += fragment
                            if fragment:
                                if not thinking_started:
                                    thinking_started = True
                                    yield {"type": "thinking_start", "phase": "thinking"}
                                yield {"type": "thinking", "phase": "thinking", "content": fragment}
                            in_think_tag = False
                            if thinking_started:
                                yield {"type": "thinking_end", "phase": "thinking"}
                                thinking_started = False
                            text = text[end + len("</think>"):]
                        else:
                            think_buf += text
                            if not thinking_started:
                                thinking_started = True
                                yield {"type": "thinking_start", "phase": "thinking"}
                            yield {"type": "thinking", "phase": "thinking", "content": text}
                            text = ""
                    else:
                        start = text.find("<think>")
                        if start >= 0:
                            pre = text[:start]
                            if pre:
                                answer_buf += pre
                                # 有工具调用时不立即 yield token（等待 finish 判断）
                            in_think_tag = True
                            text = text[start + len("<think>"):]
                        else:
                            answer_buf += text
                            text = ""
                continue

            # ── tool_calls delta ──
            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tc_buf:
                        tc_buf[idx] = {"id": "", "name": "", "arguments": ""}
                    if tc_delta.id:
                        tc_buf[idx]["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            tc_buf[idx]["name"] += tc_delta.function.name
                        if tc_delta.function.arguments:
                            tc_buf[idx]["arguments"] += tc_delta.function.arguments
                continue

            # ── finish ──
            finish = choice.finish_reason
            if finish:
                if thinking_started:
                    yield {"type": "thinking_end", "phase": "thinking"}
                    thinking_started = False
                break

        # 流结束后处理
        if thinking_started:
            yield {"type": "thinking_end", "phase": "thinking"}

        if tc_buf:
            # 有工具调用：构造 answer 格式的 tool_calls
            parsed_calls: List[dict] = []
            openai_tool_calls: List[dict] = []   # OpenAI 格式，用于写入 assistant 消息历史
            for idx in sorted(tc_buf):
                tc = tc_buf[idx]
                try:
                    args = json.loads(tc["arguments"]) if tc["arguments"] else {}
                except json.JSONDecodeError:
                    args = {}
                call_id = tc["id"] or f"call_{idx + 1}"
                parsed_calls.append({
                    "server": "graph",
                    "name": tc["name"],
                    "arguments": args,
                    "id": call_id,
                })
                openai_tool_calls.append({
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": tc["arguments"],  # 保持原始字符串
                    },
                })
            yield {
                "type": "turn_end",
                "action": "tool_calls",
                "tool_calls": parsed_calls,
                "openai_tool_calls": openai_tool_calls,  # 供 igraph 写入 assistant 消息
                "think_text": think_buf,
                "answer_text": "",
                "raw_text": content_buf,
            }
        else:
            # 纯文本回答：answer_buf 是最终答案，现在才输出 token
            if answer_buf:
                yield {"type": "token", "content": answer_buf}
            yield {
                "type": "turn_end",
                "action": "answer",
                "tool_calls": [],
                "openai_tool_calls": [],
                "think_text": think_buf,
                "answer_text": answer_buf,
                "raw_text": content_buf,
            }

    def build_model_messages(
        self,
        working_messages: List[dict],
        tool_specs: List[dict],
        *,
        system_prompt: str,
        memory_prompt: str = "",
        skill_prompt: str = "",
        mode_prompt: str = "",
    ) -> List[dict]:
        # native 模式不在 messages 里放工具描述（走 API tools 参数）
        prefix: List[dict] = [{"role": "system", "content": system_prompt}]
        if mode_prompt:
            prefix.append({"role": "system", "content": mode_prompt})
        if memory_prompt:
            prefix.append({"role": "system", "content": memory_prompt})
        if skill_prompt:
            prefix.append({"role": "system", "content": skill_prompt})
        tail = working_messages[1:] if (working_messages and working_messages[0].get("role") == "system") else working_messages
        return [*prefix, *tail]

    def build_tool_observation(self, observations: List[dict]) -> List[dict]:
        # OpenAI 原生 tool message 格式（每个工具结果一条消息）
        return [
            {
                "role": "tool",
                "tool_call_id": ob.get("call_id", ob.get("id", "")),
                "content": str(ob.get("result", "")),
                "name": ob.get("name", ""),
            }
            for ob in observations
        ]
