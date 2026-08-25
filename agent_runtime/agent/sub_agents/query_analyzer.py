from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from agent_runtime.agent.sub_agents.base import SubAgent


COMPLEXITY_JUDGE_SYSTEM_PROMPT_TEMPLATE = """\
你是一个图分析任务复杂度判断器。
{output_contract}
"""

COMPLEXITY_JUDGE_SYSTEM_PROMPT = COMPLEXITY_JUDGE_SYSTEM_PROMPT_TEMPLATE.format(
    output_contract="只输出严格 JSON。",
)

COMPLEXITY_JUDGE_PROMPT_TEMPLATE = """请根据当前图状态、可用工具和用户问题，判断是否需要显式规划。
只输出 JSON：
{{"needs_plan": true/false, "reason": "一句话", "estimated_tool_calls": 1}}

当前图状态：
{graph_context}

可用工具：
{tool_names}

用户问题：
{user_query}
"""

COMPLEXITY_JUDGE_PROMPT = COMPLEXITY_JUDGE_PROMPT_TEMPLATE


@dataclass
class ComplexityDecision:
    needs_plan: bool
    reason: str
    estimated_tool_calls: int


class QueryAnalyzer(SubAgent):
    name = "query_analyzer"
    system_prompt = COMPLEXITY_JUDGE_SYSTEM_PROMPT
    default_max_tokens = 256
    default_temperature = 0.0

    def __init__(
        self,
        llm_factory: Callable[[str], Awaitable[str]] | None = None,
        *,
        provider_gateway: Any | None = None,
    ) -> None:
        super().__init__(provider_gateway=provider_gateway, prompt_invoker=llm_factory)

    async def should_plan(
        self,
        message: str,
        graph_context: str,
        tool_names: list[str],
    ) -> tuple[bool, str]:
        heuristic = self._heuristic_decision(message)
        if self._provider_gateway is None and self._prompt_invoker is None:
            return heuristic.needs_plan, heuristic.reason

        prompt = COMPLEXITY_JUDGE_PROMPT.format(
            graph_context=graph_context or "unknown",
            tool_names=", ".join(tool_names),
            user_query=message,
        )
        try:
            response = await self.invoke_text(prompt)
            parsed = self._extract_decision(response)
            should_plan = bool(parsed.needs_plan and parsed.estimated_tool_calls >= 3)
            return should_plan, parsed.reason
        except Exception:
            return heuristic.needs_plan, heuristic.reason

    @staticmethod
    def _heuristic_decision(message: str) -> ComplexityDecision:
        text = str(message or "").strip().lower()
        if not text:
            return ComplexityDecision(False, "empty query", 1)
        markers = [
            "然后", "再", "并分析", "并且", "先", "之后", "同时比较",
            "compare", "then", "after", "and analyze", "step by step",
        ]
        multi_objectives = sum(1 for marker in markers if marker in text)
        estimated = 1 + multi_objectives
        needs_plan = estimated >= 3 or any(
            phrase in text
            for phrase in ["最重要社区", "风险分析", "可疑社区", "重要节点并解释", "train", "embedding"]
        )
        if needs_plan and estimated < 3:
            estimated = 3
        reason = "query suggests chained graph analysis" if needs_plan else "direct analysis is enough"
        return ComplexityDecision(needs_plan, reason, estimated)

    @classmethod
    def _extract_decision(cls, text: str) -> ComplexityDecision:
        payload = cls._extract_json(text)
        data = json.loads(payload)
        return ComplexityDecision(
            needs_plan=bool(data.get("needs_plan", False)),
            reason=str(data.get("reason") or ""),
            estimated_tool_calls=max(int(data.get("estimated_tool_calls", 1)), 1),
        )

    @staticmethod
    def _extract_json(text: str) -> str:
        if "```" in text:
            match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
            if match:
                return match.group(1)
        match = re.search(r"\{.*\}", text, re.DOTALL)
        return match.group(0) if match else "{}"
