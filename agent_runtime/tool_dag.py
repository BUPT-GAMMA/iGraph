"""Dependency scheduling around Agno's native single-call execution contract.

The model emits ordinary native tool calls. Resource hazards in the shared
catalog turn their ordered batch into a DAG; no second tool invocation API is
introduced, so confirmation, hooks, media, persistence and cancellation remain
on Agno's execution path. This adapter targets the pinned Agno 2.9.0 API.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
from contextlib import aclosing
from contextvars import ContextVar
from collections.abc import AsyncIterator

from agno.models.message import Message
from agno.models.response import ModelResponse, ModelResponseEvent
from agno.run.agent import ToolExecution

from agent_runtime.construction_state import _result_payload
from graph_engine.tool_execution import tool_execution_policy, tools_conflict

log = logging.getLogger(__name__)
_generator_owners = ContextVar("igraph_tool_generator_owners", default=None)


def build_dependencies(function_calls):
    """Forward-only RAW/WAR/WAW edges; unknown tools are conservative barriers."""
    policies = [tool_execution_policy(fc.function.name, fc.arguments or {}) for fc in function_calls]
    return [
        {j for j in range(i) if tools_conflict(policies[j], policy)}
        for i, policy in enumerate(policies)
    ]


def tool_parallelism():
    try:
        return max(1, min(32, int(os.getenv("AGENT_TOOL_MAX_CONCURRENCY", "4"))))
    except ValueError:
        return 4


def _outcome(fc, messages, paused):
    if paused:
        return "paused"
    if fc.error or any(getattr(message, "tool_call_error", False) for message in messages):
        return "failed"
    payload = _result_payload(fc.result)
    if isinstance(payload, dict):
        if payload.get("success") is False or payload.get("status") in {"failed", "cancelled", "canceled"}:
            return "failed"
        if payload.get("async") and payload.get("status") not in {"done", "completed", "success"}:
            # A submitted job is not a completed dependency, even for a custom
            # tool that has not installed the Graph MCP completion wrapper.
            return "pending"
    return "done"


class DAGModelMixin:
    async def arun_function_call(self, function_call):
        outcome = await super().arun_function_call(function_call)
        payload = _result_payload(function_call.result)
        if isinstance(payload, dict) and payload.get("success") is False:
            # A successful transport can still carry a failed graph operation.
            # Keep the structured error in native events and persisted history.
            function_call.error = json.dumps(payload, ensure_ascii=False)
            outcome = (False, *outcome[1:])
        owners = _generator_owners.get()
        iterator = function_call.result
        if owners is not None and isinstance(iterator, AsyncIterator):
            async def owned_iterator():
                task = asyncio.current_task()
                owners.add(task)
                try:
                    async for event in iterator:
                        yield event
                finally:
                    try:
                        close = getattr(iterator, "aclose", None)
                        if close is not None:
                            await close()
                    finally:
                        owners.discard(task)
            function_call.result = owned_iterator()
            outcome[3].result = function_call.result
        return outcome

    async def arun_function_calls(
        self, function_calls, function_call_results, additional_input=None,
        current_function_call_count=0, function_call_limit=None, skip_pause_check=False,
    ):
        calls = list(function_calls)
        dependencies = build_dependencies(calls)
        states = ["waiting"] * len(calls)
        results = [[] for _ in calls]
        queue = asyncio.Queue()
        active = {}
        native = super().arun_function_calls
        limit = tool_parallelism()
        log.debug("tool_dag nodes=%s dependencies=%s concurrency=%s",
                  [fc.function.name for fc in calls], [sorted(d) for d in dependencies], limit)

        async def execute(index):
            fc = calls[index]
            # Agno temporarily swaps run_context.messages in async hooks.
            # Give each invocation its own context object while retaining the
            # shared state dict, whose writers are ordered by the DAG.
            fc.function = copy.copy(fc.function)
            context = getattr(fc.function, "_run_context", None)
            if context is not None:
                fc.function._run_context = copy.copy(context)
                if context.messages is not None:
                    fc.function._run_context.messages = list(context.messages)
            paused = False
            generator_owners = set()
            owner_token = _generator_owners.set(generator_owners)
            try:
                iterator = native([fc], results[index], skip_pause_check=skip_pause_check)
                async with aclosing(iterator):
                    async for event in iterator:
                        paused |= getattr(event, "event", None) == ModelResponseEvent.tool_call_paused.value
                        await queue.put(("event", index, event))
                await queue.put(("done", index, _outcome(fc, results[index], paused)))
            except BaseException as exc:
                await queue.put(("error", index, exc))
            finally:
                # Agno 2.9 starts generator consumers internally. They must
                # not survive the cancellation/closure of their parent node.
                consumers = list(generator_owners)
                for task in consumers:
                    task.cancel()
                if consumers:
                    await asyncio.gather(*consumers, return_exceptions=True)
                _generator_owners.reset(owner_token)

        try:
            for index, fc in enumerate(calls):
                if function_call_limit is not None and current_function_call_count + index + 1 > function_call_limit:
                    results[index].append(self.create_tool_call_limit_error_result(fc))
                    states[index] = "failed"

            while any(state in {"waiting", "running"} for state in states):
                for index, fc in enumerate(calls):
                    if states[index] != "waiting":
                        continue
                    parents = dependencies[index]
                    if any(states[parent] in {"waiting", "running"} for parent in parents):
                        continue
                    failed = [calls[parent].call_id for parent in sorted(parents) if states[parent] != "done"]
                    stopped = any(
                        getattr(message, "stop_after_tool_call", False)
                        for prior in results[:index] for message in prior
                    )
                    if failed or stopped:
                        payload = json.dumps({
                            "success": False, "code": "TOOL_DEPENDENCY_BLOCKED",
                            "error": "前置工具未成功完成或正在等待确认；修复、完成或确认后重新提交此步骤。",
                            "dependencies": failed,
                        }, ensure_ascii=False)
                        results[index].append(Message(role="tool", content=payload, tool_call_id=fc.call_id,
                            tool_name=fc.function.name, tool_args=fc.arguments, tool_call_error=True))
                        states[index] = "blocked"
                        yield ModelResponse(event=ModelResponseEvent.tool_call_completed.value,
                            tool_executions=[ToolExecution(tool_call_id=fc.call_id, tool_name=fc.function.name,
                                tool_args=fc.arguments, tool_call_error=True, result=payload)])
                    elif len(active) < limit:
                        states[index] = "running"
                        active[index] = asyncio.create_task(execute(index))

                if not active:
                    continue
                kind, index, value = await queue.get()
                if kind == "event":
                    yield value
                elif kind == "error":
                    raise value
                else:
                    await active.pop(index)
                    states[index] = value
        finally:
            for task in active.values():
                task.cancel()
            if active:
                await asyncio.gather(*active.values(), return_exceptions=True)
            # Stream completions promptly, but keep model context deterministic.
            for messages in results:
                function_call_results.extend(messages)
            if additional_input:
                function_call_results.extend(additional_input)
