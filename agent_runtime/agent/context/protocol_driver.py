"""
ProtocolDriver — 协议驱动器抽象基类。

两种实现都产生相同的事件流：
  thinking_start / thinking / thinking_end  → 前端推理展示
  tool_call                                  → 工具调用信息
  token                                      → 最终回答文本
  turn_end                                   → 本轮结束（含解析结果，内部消费）

通过环境变量 LLM_TOOL_PROTOCOL 切换：
  "text"   → TextProtocolDriver（默认，透明上下文，支持 SFT/RL 数据采集）
  "native" → NativeFCDriver（原生 function calling，结构化工具调用）
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import AsyncIterator, List, Optional


class ProtocolDriver(ABC):
    """协议驱动器接口。"""

    @abstractmethod
    async def stream_turn(
        self,
        messages: List[dict],
        tool_specs: List[dict],
        *,
        step: int,
    ) -> AsyncIterator[dict]:
        """
        执行一轮 LLM 调用，yield 统一事件字典。

        产生的事件类型：
          {"type": "thinking_start", "phase": str}
          {"type": "thinking",       "phase": str, "content": str}
          {"type": "thinking_end",   "phase": str}
          {"type": "token",          "content": str}
          {"type": "turn_end",       "action": "tool_calls"|"answer",
                                     "tool_calls": list[dict],
                                     "answer_text": str,
                                     "think_text": str,
                                     "raw_text": str}
        """
        ...
        # 使 type checker 识别为 AsyncIterator
        yield {}  # type: ignore[misc]

    @abstractmethod
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
        """
        构造本轮模型输入消息列表。

        text 模式：工具 JSON 注入 system prompt
        native 模式：messages 不含工具描述（工具走 API tools 参数）
        """
        ...

    @abstractmethod
    def build_tool_observation(self, observations: List[dict]) -> List[dict]:
        """
        将工具执行结果格式化为下一轮模型输入消息。

        返回 list[dict] 以支持 native FC 的多条 tool message 格式。
        text 模式返回单元素列表。
        """
        ...


def create_driver(provider_gateway, logger=None) -> "ProtocolDriver":
    """根据环境变量创建协议驱动器实例。"""
    protocol = os.getenv("LLM_TOOL_PROTOCOL", "text").strip().lower()
    if protocol == "native":
        from agent_runtime.agent.context.native_fc_driver import NativeFCDriver
        return NativeFCDriver(provider_gateway, logger=logger)
    # 默认文本协议
    from agent_runtime.agent.context.text_protocol_driver import TextProtocolDriver
    return TextProtocolDriver(provider_gateway, logger=logger)
