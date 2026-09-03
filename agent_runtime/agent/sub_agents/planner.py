from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, AsyncIterator, Awaitable, Callable

from agent_runtime.agent.sub_agents.base import PromptStreamer, SubAgent
from agent_runtime.agent.sub_agents.tool_selector import LLMToolSelector


PLANNER_SYSTEM_PROMPT_TEMPLATE = """\
你是图分析规划师。根据用户问题和当前图信息生成结构化分析计划。
{output_contract}
"""

PLANNER_SYSTEM_PROMPT = PLANNER_SYSTEM_PROMPT_TEMPLATE.format(
    output_contract="需要输出机器可解析的结构化内容。",
)

PLAN_PROMPT_TEMPLATE = """\
请只输出 JSON。
当前图信息: {graph_info}
可用工具:
{tool_summaries}
{memory_section}{conversation_section}输出格式: {{"steps":[{{"step_id":1,"goal":"...","candidate_tools":["..."],"depends_on":[]}}]}}
用户问题: {query}
"""

STREAM_PLAN_PROMPT_TEMPLATE = """\
请边思考边输出步骤，但最终只能输出若干个 `<step>...</step>` 块，不要输出其他说明文字。
当前图信息: {graph_info}
可用工具:
{tool_summaries}
{memory_section}{conversation_section}每个 `<step>` 内必须是严格合法 JSON，格式为：{{"step_id":1,"goal":"...","candidate_tools":["..."],"depends_on":[]}}
要求：
- 一旦确定一个步骤，就立刻输出对应的 `<step>...</step>`。
- `candidate_tools` 必须从可用工具中选择。
- `depends_on` 只能依赖更早的步骤。
- 不要输出 markdown，不要输出代码块，不要输出总 JSON。
用户问题: {query}
"""


@dataclass
class PlanStep:
    step_id: int
    goal: str
    candidate_tools: list[str]
    depends_on: list[int]
    status: str = "pending"
    result_summary: str = ""
    thinking: str = ""
    tools: list[dict[str, Any]] = field(default_factory=list)
    progress_items: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class AnalysisPlan:
    plan_id: str
    original_query: str
    steps: list[PlanStep]
    mode: str = "planned"
    created_at: str = field(default_factory=lambda: datetime.now().astimezone().isoformat(timespec="seconds"))
    completed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "original_query": self.original_query,
            "mode": self.mode,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "steps": [asdict(step) for step in self.steps],
        }


class Planner(SubAgent):
    name = "planner"
    system_prompt = PLANNER_SYSTEM_PROMPT
    default_max_tokens = 512
    default_temperature = 0.0

    def __init__(
        self,
        tool_selector: LLMToolSelector,
        tool_catalog: list[dict[str, Any]],
        *,
        provider_gateway: Any | None = None,
        llm: Callable[[str], Awaitable[str]] | None = None,
        llm_stream: PromptStreamer | None = None,
    ) -> None:
        super().__init__(provider_gateway=provider_gateway, prompt_invoker=llm, prompt_streamer=llm_stream)
        self._tool_selector = tool_selector
        self._tool_catalog = list(tool_catalog)
        self._valid_tool_names = {str(t.get("name")) for t in self._tool_catalog if t.get("name")}

    async def build_plan(
        self,
        query: str,
        graph_info: dict[str, Any],
        llm: Callable[[str], Awaitable[str]] | None = None,
        memory_prompt: str = "",
        conversation_messages: list[dict[str, Any]] | None = None,
    ) -> AnalysisPlan:
        prompt = self._build_plan_prompt(
            query,
            graph_info,
            memory_prompt=memory_prompt,
            conversation_messages=conversation_messages,
        )
        try:
            if llm is not None:
                response = await llm(self.compose_prompt(prompt))
            elif self._provider_gateway is not None or self._prompt_invoker is not None:
                response = await self.invoke_text(prompt)
            else:
                return self._fallback_plan(query)
            return self._parse_plan(response, query)
        except Exception:
            return self._fallback_plan(query)

    async def stream_plan(
        self,
        query: str,
        graph_info: dict[str, Any],
        *,
        llm_stream: Callable[[str], AsyncIterator[str]] | None = None,
        llm: Callable[[str], Awaitable[str]] | None = None,
        memory_prompt: str = "",
        conversation_messages: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        plan_id = uuid.uuid4().hex[:12]
        active_streamer = llm_stream

        if active_streamer is None and self._provider_gateway is None and self._prompt_streamer is None:
            plan = await self.build_plan(
                query,
                graph_info,
                llm=llm,
                memory_prompt=memory_prompt,
                conversation_messages=conversation_messages,
            )
            plan.plan_id = plan_id
            for step in plan.steps:
                yield {"type": "plan_step", "plan_id": plan_id, "step": asdict(step)}
            yield {"type": "plan_ready", "plan": plan}
            return

        prompt = self._build_stream_plan_prompt(
            query,
            graph_info,
            memory_prompt=memory_prompt,
            conversation_messages=conversation_messages,
        )
        parsed_steps: list[PlanStep] = []
        seen_step_ids: set[int] = set()
        buffer = ""

        try:
            if active_streamer is not None:
                stream = active_streamer(self.compose_prompt(prompt))
            else:
                stream = self.stream_text(prompt)
            async for chunk in stream:
                if not chunk:
                    continue
                buffer += chunk
                extracted_steps, buffer = self._extract_step_payloads(buffer)
                for payload in extracted_steps:
                    step = self._parse_step_payload(payload, default_index=len(parsed_steps) + 1)
                    if step is None or step.step_id in seen_step_ids:
                        continue
                    seen_step_ids.add(step.step_id)
                    parsed_steps.append(step)
                    yield {"type": "plan_step", "plan_id": plan_id, "step": asdict(step)}
        except Exception:
            pass

        if not parsed_steps:
            plan = await self.build_plan(
                query,
                graph_info,
                llm=llm,
                memory_prompt=memory_prompt,
                conversation_messages=conversation_messages,
            )
            plan.plan_id = plan_id
            for step in plan.steps:
                yield {"type": "plan_step", "plan_id": plan_id, "step": asdict(step)}
            yield {"type": "plan_ready", "plan": plan}
            return

        parsed_steps.sort(key=lambda step: step.step_id)
        plan = AnalysisPlan(plan_id=plan_id, original_query=query, steps=parsed_steps)
        yield {"type": "plan_ready", "plan": plan}

    def _build_plan_prompt(
        self,
        query: str,
        graph_info: dict[str, Any],
        *,
        memory_prompt: str = "",
        conversation_messages: list[dict[str, Any]] | None = None,
    ) -> str:
        tool_summaries = [
            f"- {t.get('name', '')}: {t.get('description', '')}"
            for t in self._tool_catalog
            if t.get("name")
        ]
        memory_section = (
            f"历史经验与用户偏好（仅供参考，优先服从用户当前指令）:\n{memory_prompt}\n"
            if memory_prompt.strip()
            else ""
        )
        conversation_section = self._conversation_section(conversation_messages)
        return PLAN_PROMPT_TEMPLATE.format(
            graph_info=json.dumps(graph_info, ensure_ascii=False),
            tool_summaries=chr(10).join(tool_summaries),
            memory_section=memory_section,
            conversation_section=conversation_section,
            query=query,
        )

    def _build_stream_plan_prompt(
        self,
        query: str,
        graph_info: dict[str, Any],
        *,
        memory_prompt: str = "",
        conversation_messages: list[dict[str, Any]] | None = None,
    ) -> str:
        tool_summaries = [
            f"- {t.get('name', '')}: {t.get('description', '')}"
            for t in self._tool_catalog
            if t.get("name")
        ]
        memory_section = (
            f"历史经验与用户偏好（仅供参考，可据此优化步骤和工具选择）:\n{memory_prompt}\n"
            if memory_prompt.strip()
            else ""
        )
        conversation_section = self._conversation_section(conversation_messages)
        return STREAM_PLAN_PROMPT_TEMPLATE.format(
            graph_info=json.dumps(graph_info, ensure_ascii=False),
            tool_summaries=chr(10).join(tool_summaries),
            memory_section=memory_section,
            conversation_section=conversation_section,
            query=query,
        )

    @staticmethod
    def _conversation_section(messages: list[dict[str, Any]] | None) -> str:
        if not isinstance(messages, list):
            return ""
        lines: list[str] = []
        for msg in messages[-30:]:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "").strip()
            if not role or role == "system":
                continue
            content = msg.get("content")
            if content is None and role == "assistant" and msg.get("tool_calls"):
                content = json.dumps({"tool_calls": msg.get("tool_calls")}, ensure_ascii=False)
            text = str(content or "").strip()
            if not text:
                continue
            if len(text) > 1600:
                text = text[:1600] + "..."
            lines.append(f"{role}: {text}")
        if not lines:
            return ""
        return "当前会话上下文（已过滤 system prompt，与主 Agent 共享的历史消息）:\n" + "\n".join(lines) + "\n"

    def _parse_plan(self, response: str, query: str) -> AnalysisPlan:
        payload = self._extract_json(response)
        data = json.loads(payload)
        steps: list[PlanStep] = []
        for idx, raw in enumerate(data.get("steps", []), start=1):
            if not isinstance(raw, dict):
                continue
            tools = [
                tool for tool in raw.get("candidate_tools", [])
                if isinstance(tool, str) and tool in self._valid_tool_names
            ]
            steps.append(
                PlanStep(
                    step_id=int(raw.get("step_id", idx)),
                    goal=str(raw.get("goal") or f"步骤 {idx}"),
                    candidate_tools=tools,
                    depends_on=[int(v) for v in raw.get("depends_on", []) if isinstance(v, int)],
                )
            )
        if not steps:
            return self._fallback_plan(query)
        return AnalysisPlan(plan_id=uuid.uuid4().hex[:12], original_query=query, steps=steps)

    def _fallback_plan(self, query: str) -> AnalysisPlan:
        steps = [
            PlanStep(
                step_id=1,
                goal="确认当前图状态与基础结构",
                candidate_tools=["get_graph_info"],
                depends_on=[],
            )
        ]

        matched = self._tool_selector.select_sync(query, k=5)
        tool_names = [
            str(t.get("name"))
            for t in matched
            if t.get("name") and str(t.get("name")) in self._valid_tool_names
               and str(t.get("name")) != "get_graph_info"
        ]

        if tool_names:
            steps.append(
                PlanStep(
                    step_id=2,
                    goal="执行与问题最相关的图分析",
                    candidate_tools=tool_names,
                    depends_on=[1],
                )
            )

        return AnalysisPlan(plan_id=uuid.uuid4().hex[:12], original_query=query, steps=steps)

    @staticmethod
    def _extract_json(text: str) -> str:
        if "```" in text:
            match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
            if match:
                return match.group(1)
        match = re.search(r"\{.*\}", text, re.DOTALL)
        return match.group(0) if match else "{}"

    @staticmethod
    def _extract_step_payloads(buffer: str) -> tuple[list[str], str]:
        payloads: list[str] = []
        pattern = re.compile(r"<step>(.*?)</step>", re.DOTALL | re.IGNORECASE)
        cursor = 0
        last_end = 0
        while True:
            match = pattern.search(buffer, cursor)
            if match is None:
                break
            payloads.append(match.group(1).strip())
            last_end = match.end()
            cursor = match.end()
        remainder = buffer[last_end:] if last_end else buffer
        if not payloads and len(remainder) > 8000:
            remainder = remainder[-4000:]
        return payloads, remainder

    def _parse_step_payload(self, payload: str, default_index: int) -> PlanStep | None:
        try:
            raw = json.loads(payload)
        except Exception:
            return None
        if not isinstance(raw, dict):
            return None
        tools = [
            tool for tool in raw.get("candidate_tools", [])
            if isinstance(tool, str) and tool in self._valid_tool_names
        ]
        return PlanStep(
            step_id=int(raw.get("step_id", default_index)),
            goal=str(raw.get("goal") or f"步骤 {default_index}"),
            candidate_tools=tools,
            depends_on=[int(v) for v in raw.get("depends_on", []) if isinstance(v, int)],
        )
