from __future__ import annotations

import json
import re
from typing import Any

from agent_runtime.agent.sub_agents.base import SubAgent


MEMORY_DECISION_SCHEMA = """\
{
  "should_write": true/false,
  "reason": "简短原因",
  "operations": [
    {
      "op": "upsert",
      "category": "preference|goal|project|constraint|profile|workflow|fact|other",
      "key": "稳定键",
      "value": "稳定值",
      "summary": "一句话总结",
      "confidence": 0.0-1.0
    },
    {
      "op": "delete",
      "memory_id": "可选",
      "key": "可选",
      "reason": "删除原因"
    }
  ],
  "should_write_playbook": true/false,
  "playbook_reason": "简短原因",
  "playbook_operations": [
    {
      "op": "upsert_playbook",
      "problem_type": "题型名称（稳定）",
      "approach": "2-5步可复用方法",
      "trigger_terms": ["关键词"],
      "tool_sequence": ["工具名"],
      "confidence": 0.0-1.0
    },
    {
      "op": "delete_playbook",
      "playbook_id": "可选",
      "problem_type": "可选",
      "reason": "删除原因"
    }
  ]
}
"""

MEMORY_RULES = """\
1) 长期记忆只写稳定偏好、长期目标、长期约束、持续项目背景、经过验证的重要事实。
2) 题型经验只写可复用方法，不写会话私有数据、一次性参数、临时文件路径。
3) 一次性任务、临时上下文、猜测、不确定信息、闲聊，不写入。
4) 如果与现有记忆或已有题型经验冲突，优先更新或删除，不要重复堆积。
5) 优先基于 current_turn_messages（当前轮完整消息链）做判断，不要只看最终回答。
6) 参考 turn.success_signal / turn.failure_signal；失败轮次不要提升题型经验置信度。
7) should_write=false 时 operations 必须为空数组；should_write_playbook=false 时 playbook_operations 必须为空数组。
8) 如果 existing_playbooks 中已存在语义相近的题型（核心关键词高度重叠，如"最短路径"与"单源最短路径"视为相近），必须直接复用其 problem_type 原文发起 upsert_playbook，不要另起新名称。
"""

MEMORY_SYSTEM_PROMPT_TEMPLATE = """\
你是{agent_name}。你的职责是判断是否更新长期记忆。
同时，你需要总结跨会话可复用的题型求解经验（problem-solving playbook）。
只保留跨会话有价值的信息，避免噪声和一次性指令。

输出必须是一个严格 JSON 对象，不要输出 markdown 或解释文字。
JSON schema:
{schema}

规则:
{rules}"""

MEMORY_SYSTEM_PROMPT = MEMORY_SYSTEM_PROMPT_TEMPLATE.format(
    agent_name="长期记忆子代理（memory sub-agent）",
    schema=MEMORY_DECISION_SCHEMA,
    rules=MEMORY_RULES,
)

MEMORY_USER_PROMPT_TEMPLATE = """\
请根据以下 JSON 输入输出决策 JSON（禁止 markdown，禁止额外解释）：
{payload_json}
"""


class MemoryUpdateSubAgent(SubAgent):
    name = "memory_update"
    system_prompt = MEMORY_SYSTEM_PROMPT
    default_max_tokens = None
    default_temperature = None

    @staticmethod
    def build_user_prompt(payload: dict[str, Any]) -> str:
        return MEMORY_USER_PROMPT_TEMPLATE.format(
            payload_json=json.dumps(payload, ensure_ascii=False),
        )

    async def decide(
        self,
        *,
        provider_gateway: Any,
        payload: dict[str, Any],
        step: int,
    ) -> tuple[str, dict[str, Any]]:
        model_msgs = self.build_messages(self.build_user_prompt(payload))
        decision_text = await provider_gateway.invoke_text(model_msgs, step=step)
        return decision_text, self.parse_decision_json(decision_text)

    @staticmethod
    def parse_decision_json(raw: str) -> dict[str, Any]:
        text = (raw or "").strip()
        if not text:
            return {
                "should_write": False,
                "reason": "empty",
                "operations": [],
                "should_write_playbook": False,
                "playbook_reason": "empty",
                "playbook_operations": [],
            }

        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if match:
            text = match.group(0)
        try:
            data = json.loads(text)
        except Exception:
            return {
                "should_write": False,
                "reason": "invalid_json",
                "operations": [],
                "should_write_playbook": False,
                "playbook_reason": "invalid_json",
                "playbook_operations": [],
            }
        if not isinstance(data, dict):
            return {
                "should_write": False,
                "reason": "not_object",
                "operations": [],
                "should_write_playbook": False,
                "playbook_reason": "not_object",
                "playbook_operations": [],
            }
        data.setdefault("operations", [])
        data.setdefault("playbook_operations", [])
        return data
