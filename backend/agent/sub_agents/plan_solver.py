from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable, List, Optional

from backend.agent.sub_agents.planner import AnalysisPlan


def _is_tool_error(result: str) -> bool:
    """Return True when a tool call result string represents a failure."""
    if result.startswith("错误：") or "执行错误" in result:
        return True
    try:
        data = json.loads(result)
        if isinstance(data, dict) and data.get("ok") is False:
            return True
    except Exception:
        pass
    return False

if TYPE_CHECKING:
    from backend.agent.context.context_manager import ContextManager


class PlanSolver:
    """Execute planned analysis steps with isolated per-step context."""

    name = "plan_solver"
    max_turns_per_step = 4

    def __init__(
        self,
        *,
        context_manager: "ContextManager",
        logger: Any,
        protocol_getter: Callable[[], str],
        active_tool_specs: Callable[[], List[dict]],
        visible_tool_specs: Callable[[], List[dict]],
        memory_prompt: Callable[[], str],
        graph_context_summary: Callable[[], str],
        graph_info_payload: Callable[[], dict],
        run_tool: Callable[
            [dict, Optional[int], Optional[Callable[[dict[str, Any]], None]]],
            Awaitable[str],
        ],
        clone_data: Callable[[Any], Any],
        pending_feedback: Callable[[], Optional[dict[str, Any]]],
        set_paused_plan_state: Callable[[dict[str, Any]], None],
        register_tools: Optional[Callable[[List[str]], List[str]]] = None,
        should_run_batch_serially: Optional[Callable[[List[dict]], bool]] = None,
        run_tool_calls_parallel: Optional[
            Callable[[List[dict], Optional[int]], AsyncIterator[dict]]
        ] = None,
    ) -> None:
        self.context_manager = context_manager
        self.logger = logger
        self._protocol_getter = protocol_getter
        self._active_tool_specs = active_tool_specs
        self._visible_tool_specs = visible_tool_specs
        self._memory_prompt = memory_prompt
        self._graph_context_summary = graph_context_summary
        self._graph_info_payload = graph_info_payload
        self._run_tool = run_tool
        self._clone_data = clone_data
        self._pending_feedback = pending_feedback
        self._set_paused_plan_state = set_paused_plan_state
        self._register_tools = register_tools
        self._should_run_batch_serially = should_run_batch_serially
        self._run_tool_calls_parallel = run_tool_calls_parallel

    def plan_overview_text(self, plan: AnalysisPlan) -> str:
        lines = [f"原始问题: {plan.original_query}", "规划步骤:"]
        for step in plan.steps:
            deps = ", ".join(str(x) for x in step.depends_on) or "无"
            tools = ", ".join(step.candidate_tools) or "全部"
            lines.append(
                f"- 步骤 {step.step_id}: {step.goal}；依赖: {deps}；候选工具: {tools}"
            )
        return "\n".join(lines)

    def build_step_result_message(self, plan_step: Any) -> dict:
        content = "\n".join(
            [
                f"[PLAN_STEP_RESULT step_id={plan_step.step_id} status={plan_step.status}]",
                f"目标: {plan_step.goal}",
                f"图状态: {self._graph_context_summary()}",
                f"结论: {str(plan_step.result_summary or '').strip() or '无明确结论'}",
                "注意: 如结论包含假设、置信度不足或需核查事项，以上结论文本为准。",
            ]
        )
        return {"role": "user", "content": content}

    def build_step_prompt(self, plan: AnalysisPlan, plan_step: Any) -> str:
        return (
            f"[计划步骤 {plan_step.step_id}/{len(plan.steps)}]\n"
            f"目标: {plan_step.goal}\n"
            f"候选工具: {', '.join(plan_step.candidate_tools) or '全部'}\n"
            "请仅完成当前步骤，并在必要时调用工具。\n"
            "最终回答请精炼，但要包含结论；如存在不确定性，请显式写出注意、假设或需核查事项。"
        )

    def build_step_messages(
        self,
        plan: AnalysisPlan,
        plan_step: Any,
        prior_result_messages: List[dict],
    ) -> List[dict]:
        # system_prompt is always sourced from context_manager.messages[0] inside
        # build_model_messages — no need to duplicate it here.
        overview_msg = {
            "role": "user",
            "content": (
                "[PLAN_SOLVER_CONTEXT]\n"
                "你正在隔离执行一个规划步骤。只能使用本消息中的原始问题、规划列表、"
                "前序步骤最终结果、当前图状态和当前步骤目标；不要假设可见其他步骤的中间过程。\n\n"
                f"{self.plan_overview_text(plan)}\n\n"
                f"当前图状态: {self._graph_context_summary()}"
            ),
        }
        return [
            overview_msg,
            *self._prior_result_messages(prior_result_messages),
            {"role": "user", "content": self.build_step_prompt(plan, plan_step)},
        ]

    @staticmethod
    def _prior_result_messages(msgs: List[dict]) -> List[dict]:
        return [
            dict(msg)
            for msg in msgs
            if isinstance(msg, dict)
            and str(msg.get("role") or "") == "user"
            and str(msg.get("content") or "").lstrip().startswith("[PLAN_STEP_RESULT")
        ]

    def _log_intermediate(self, message: dict) -> None:
        self.logger.log_context_message(
            message,
            visible=False,
            kind="plan_step_intermediate",
            include_in_context=False,
        )

    @staticmethod
    async def _progress_events_until_done(
        task: asyncio.Task,
        queue: asyncio.Queue,
        *,
        step_id: int,
        tool: str,
        call_id: str,
    ):
        while not task.done():
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
            if isinstance(payload, dict):
                yield {
                    "type": "tool_progress",
                    "step_id": step_id,
                    "tool": tool,
                    "call_id": call_id,
                    "progress": payload,
                }
        while not queue.empty():
            payload = queue.get_nowait()
            if isinstance(payload, dict):
                yield {
                    "type": "tool_progress",
                    "step_id": step_id,
                    "tool": tool,
                    "call_id": call_id,
                    "progress": payload,
                }

    @staticmethod
    def _upsert_step_progress(
        plan_step: Any,
        *,
        tool: str,
        call_id: str,
        payload: dict[str, Any],
        done: bool = False,
    ) -> None:
        items = getattr(plan_step, "progress_items", None)
        if not isinstance(items, list):
            items = []
            setattr(plan_step, "progress_items", items)
        key = f"{tool}:{call_id or 'latest'}"
        existing = next((item for item in items if item.get("key") == key), None)
        if existing is None:
            existing = {
                "key": key,
                "tool": tool,
                "call_id": call_id,
                "step_id": getattr(plan_step, "step_id", None),
                "done": bool(done),
                "progress": {},
            }
            items.append(existing)
        existing["done"] = bool(done or existing.get("done"))
        existing["progress"] = dict(payload)

    async def run(
        self,
        plan: AnalysisPlan,
        msgs: List[dict],
        *,
        persist_plan: Callable[[AnalysisPlan], None],
        start_step_id: int | None = None,
        resume_step_messages: Optional[List[dict]] = None,
    ):
        prior_result_messages = self._prior_result_messages(msgs)

        for plan_step in plan.steps:
            if start_step_id is not None and plan_step.step_id < start_step_id:
                continue
            if any(
                dep.status != "done"
                for dep in plan.steps
                if dep.step_id in plan_step.depends_on
            ):
                plan_step.status = "skipped"
                yield {
                    "type": "solve_step_done",
                    "step_id": plan_step.step_id,
                    "status": "skipped",
                    "summary": "依赖步骤未完成",
                }
                continue

            plan_step.status = "running"
            persist_plan(plan)
            if self._register_tools is not None and plan_step.candidate_tools:
                try:
                    self._register_tools(list(plan_step.candidate_tools))
                except Exception:
                    pass
            yield {
                "type": "solve_step_start",
                "step_id": plan_step.step_id,
                "goal": plan_step.goal,
            }

            is_resuming_step = (
                resume_step_messages is not None
                and start_step_id is not None
                and plan_step.step_id == start_step_id
            )
            step_messages = (
                [dict(msg) for msg in resume_step_messages]
                if is_resuming_step
                else self.build_step_messages(plan, plan_step, prior_result_messages)
            )
            step_prompt = self.build_step_prompt(plan, plan_step)
            if not is_resuming_step:
                self._log_intermediate({"role": "user", "content": step_prompt})

            step_answer = ""
            step_answered = False
            step_tool_calls = 0      # total tool invocations this step
            step_tool_errors = 0     # invocations that returned an error

            for turn in range(1, self.max_turns_per_step + 1):
                streamed_thinking_chunks: list[str] = []
                active_tool_specs = self._active_tool_specs()
                model_msgs = self.context_manager.build_model_messages(
                    active_tool_specs,
                    working_messages=step_messages,
                    memory_prompt=self._memory_prompt(),
                    query=step_prompt,
                    visible_tool_specs=self._visible_tool_specs(),
                    graph_state=self._graph_info_payload(),
                )

                turn_result = None
                async for event in self.context_manager.stream_turn(
                    model_msgs,
                    active_tool_specs,
                    step=900 + plan_step.step_id * 10 + turn,
                ):
                    etype = event.get("type", "")
                    if etype == "turn_end":
                        turn_result = event
                        break
                    if etype == "thinking_start":
                        yield {
                            "type": "solve_thinking",
                            "step_id": plan_step.step_id,
                            "phase": "start",
                        }
                    elif etype == "thinking":
                        chunk = str(event.get("content") or "")
                        if chunk:
                            streamed_thinking_chunks.append(chunk)
                            plan_step.thinking = f"{plan_step.thinking}{chunk}"
                        yield {
                            "type": "solve_thinking",
                            "step_id": plan_step.step_id,
                            "phase": "content",
                            "content": chunk,
                        }
                    elif etype == "thinking_end":
                        yield {
                            "type": "solve_thinking",
                            "step_id": plan_step.step_id,
                            "phase": "end",
                        }
                    elif etype == "token":
                        yield {
                            "type": "solve_token",
                            "step_id": plan_step.step_id,
                            "content": event.get("content", ""),
                        }

                if turn_result is None:
                    break

                raw_text = turn_result.get("raw_text", "")
                action = turn_result.get("action")
                tool_calls = turn_result.get("tool_calls", [])
                step_answer = turn_result.get("answer_text", "").strip()
                think_text = turn_result.get("think_text", "")
                if think_text and not streamed_thinking_chunks:
                    plan_step.thinking = f"{plan_step.thinking}{think_text}"

                if self._protocol_getter() == "native" and action == "tool_calls":
                    assistant_msg = {
                        "role": "assistant",
                        "content": think_text or None,
                        "tool_calls": turn_result.get("openai_tool_calls") or [],
                    }
                else:
                    assistant_content = raw_text or (
                        f"<think>{think_text}</think><answer>{step_answer}</answer>"
                        if action == "answer"
                        else f"<think>{think_text}</think>"
                    )
                    assistant_msg = {"role": "assistant", "content": assistant_content}
                step_messages.append(assistant_msg)
                self._log_intermediate(assistant_msg)

                if action == "tool_calls" and tool_calls:
                    batch_obs: List[dict] = []
                    can_parallel = (
                        self._run_tool_calls_parallel is not None
                        and self._should_run_batch_serially is not None
                        and not self._should_run_batch_serially(tool_calls)
                    )
                    if can_parallel:
                        if isinstance(getattr(plan_step, "tools", None), list):
                            for tc in tool_calls:
                                plan_step.tools.append(
                                    {
                                        "tool": tc["name"],
                                        "args": tc.get("arguments", {}) or {},
                                        "call_id": str(tc.get("id") or ""),
                                        "server": tc.get("server", "graph"),
                                        "done": False,
                                    }
                                )
                        async for batch_event in self._run_tool_calls_parallel(
                            tool_calls, plan_step.step_id
                        ):
                            etype = batch_event.get("type", "")
                            if etype == "_batch_done":
                                batch_obs.extend(batch_event.get("observations") or [])
                                continue
                            if etype == "tool_call":
                                yield {
                                    "type": "solve_tool_call",
                                    "step_id": plan_step.step_id,
                                    "tool": batch_event.get("tool"),
                                    "args": batch_event.get("args", {}),
                                    "call_id": batch_event.get("call_id"),
                                }
                            elif etype == "tool_progress":
                                self._upsert_step_progress(
                                    plan_step,
                                    tool=str(batch_event.get("tool") or ""),
                                    call_id=str(batch_event.get("call_id") or ""),
                                    payload=batch_event.get("progress") or {},
                                )
                                yield {
                                    "type": "tool_progress",
                                    "step_id": plan_step.step_id,
                                    "tool": batch_event.get("tool"),
                                    "call_id": batch_event.get("call_id"),
                                    "progress": batch_event.get("progress") or {},
                                }
                            elif etype == "tool_result":
                                tool_name = str(batch_event.get("tool") or "")
                                call_id_str = str(batch_event.get("call_id") or "")
                                if isinstance(getattr(plan_step, "tools", None), list):
                                    for vt in plan_step.tools:
                                        if (
                                            vt.get("tool") == tool_name
                                            and str(vt.get("call_id") or "") == call_id_str
                                        ):
                                            vt["done"] = True
                                if isinstance(getattr(plan_step, "progress_items", None), list):
                                    for item in plan_step.progress_items:
                                        if (
                                            item.get("tool") == tool_name
                                            and str(item.get("call_id") or "") == call_id_str
                                        ):
                                            item["done"] = True
                                yield {
                                    "type": "solve_tool_result",
                                    "step_id": plan_step.step_id,
                                    "tool": tool_name,
                                    "call_id": batch_event.get("call_id"),
                                }
                        for obs in batch_obs:
                            step_tool_calls += 1
                            if _is_tool_error(str(obs.get("result") or "")):
                                step_tool_errors += 1
                        for obs_msg in self.context_manager.build_tool_observation(batch_obs):
                            step_messages.append(obs_msg)
                            self._log_intermediate(obs_msg)
                        continue
                    for tc in tool_calls:
                        args = tc.get("arguments", {})
                        call_id = tc.get("id")
                        visible_tool = {
                            "tool": tc["name"],
                            "args": args if isinstance(args, dict) else {},
                            "call_id": str(call_id or ""),
                            "server": tc.get("server", "graph"),
                            "done": False,
                        }
                        if isinstance(getattr(plan_step, "tools", None), list):
                            plan_step.tools.append(visible_tool)
                        self.logger.log_tool_call(tc["name"], args)
                        yield {
                            "type": "solve_tool_call",
                            "step_id": plan_step.step_id,
                            "tool": tc["name"],
                            "args": args,
                            "call_id": call_id,
                        }
                        progress_queue: asyncio.Queue = asyncio.Queue()
                        result_task = asyncio.create_task(
                            self._run_tool(
                                tc,
                                plan_step.step_id,
                                lambda payload, q=progress_queue: q.put_nowait(payload),
                            )
                        )
                        async for progress_event in self._progress_events_until_done(
                            result_task,
                            progress_queue,
                            step_id=plan_step.step_id,
                            tool=tc["name"],
                            call_id=str(call_id or ""),
                        ):
                            self._upsert_step_progress(
                                plan_step,
                                tool=tc["name"],
                                call_id=str(call_id or ""),
                                payload=progress_event.get("progress") or {},
                            )
                            yield progress_event
                        result_str = await result_task
                        pending = self._pending_feedback()
                        if (
                            pending is not None
                            and str(pending.get("call_id") or "") == str(call_id or "")
                        ):
                            if batch_obs:
                                for obs_msg in self.context_manager.build_tool_observation(batch_obs):
                                    step_messages.append(obs_msg)
                                    self._log_intermediate(obs_msg)
                            plan_step.status = "waiting_user"
                            plan_step.result_summary = str(pending.get("message") or "")
                            self._set_paused_plan_state(
                                {
                                    "origin": "plan",
                                    "plan": plan,
                                    "step_id": plan_step.step_id,
                                    "pending_tool_call": self._clone_data(tc),
                                    "step_messages": self._clone_data(step_messages),
                                }
                            )
                            persist_plan(plan)
                            yield {"type": "await_user_feedback", "request": pending}
                            yield {
                                "type": "solve_step_done",
                                "step_id": plan_step.step_id,
                                "status": plan_step.status,
                                "summary": plan_step.result_summary,
                            }
                            return
                        self.logger.log_tool_result(tc["name"], result_str)
                        visible_tool["done"] = True
                        if isinstance(getattr(plan_step, "progress_items", None), list):
                            for item in plan_step.progress_items:
                                if (
                                    item.get("tool") == tc["name"]
                                    and str(item.get("call_id") or "") == str(call_id or "")
                                ):
                                    item["done"] = True
                        step_tool_calls += 1
                        if _is_tool_error(result_str):
                            step_tool_errors += 1
                        batch_obs.append(
                            {
                                "call_id": call_id,
                                "server": tc.get("server", "graph"),
                                "name": tc["name"],
                                "arguments": args,
                                "result": result_str,
                            }
                        )
                        yield {
                            "type": "solve_tool_result",
                            "step_id": plan_step.step_id,
                            "tool": tc["name"],
                            "call_id": call_id,
                        }
                    for obs_msg in self.context_manager.build_tool_observation(batch_obs):
                        step_messages.append(obs_msg)
                        self._log_intermediate(obs_msg)
                    continue

                # 空响应 / 只有 <think> 没有 <answer>：消耗一次 turn 重试
                if not raw_text.strip() and not tool_calls:
                    retry_notice = "上一轮模型返回了空响应，请重新生成本步骤的分析结论。"
                    step_messages.append({"role": "user", "content": retry_notice})
                    self._log_intermediate({"role": "user", "content": retry_notice})
                    continue
                if action == "answer" and not step_answer and not tool_calls:
                    retry_notice = (
                        "上一轮输出只有 <think> 推理内容但缺少 <answer> 正文。"
                        "请将本步骤的分析结论放入 <answer>...</answer> 标签中输出。"
                    )
                    step_messages.append({"role": "user", "content": retry_notice})
                    self._log_intermediate({"role": "user", "content": retry_notice})
                    continue

                if not step_answer:
                    step_answer = "步骤已执行但未生成明确结论。"
                step_answered = True
                break

            all_tools_failed = step_tool_calls > 0 and step_tool_errors == step_tool_calls
            plan_step.status = (
                "done" if (step_answered and not all_tools_failed) else "failed"
            )
            plan_step.result_summary = step_answer
            result_msg = self.build_step_result_message(plan_step)
            msgs.append(result_msg)
            prior_result_messages.append(result_msg)
            self.logger.log_context_message(
                result_msg,
                visible=False,
                kind="plan_step_result",
                include_in_context=True,
            )
            persist_plan(plan)
            yield {
                "type": "solve_step_done",
                "step_id": plan_step.step_id,
                "status": plan_step.status,
                "summary": plan_step.result_summary,
            }
