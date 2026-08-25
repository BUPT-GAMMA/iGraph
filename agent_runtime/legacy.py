"""Transparent adapter for the original :class:`IGraph` implementation."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Mapping
from typing import Any


class LegacyIGraphRuntime:
    """Expose the existing IGraph object through the stable Runtime contract.

    The adapter intentionally owns no copied state and performs no event or
    exception translation.  Operations that Backend previously performed on
    private IGraph fields live here with the same assignments and ordering.
    """

    def __init__(self, agent: Any) -> None:
        self._agent = agent

    @property
    def session_id(self) -> str:
        return str(self._agent.session_id)

    @property
    def user_id(self) -> str:
        return str(getattr(self._agent, "user_id", "") or "")

    @property
    def provider(self) -> str:
        return str(self._agent.provider)

    @property
    def model(self) -> str:
        return str(self._agent.model)

    @property
    def reasoning_mode(self) -> str:
        return str(self._agent.reasoning_mode)

    @property
    def planning_mode(self) -> str:
        return str(self._agent.planning_mode)

    @property
    def last_active(self) -> float:
        return float(getattr(self._agent, "last_active", time.monotonic()))

    def set_model(self, model: str) -> None:
        self._agent.set_model(model)

    def set_reasoning_mode(self, mode: str) -> None:
        self._agent.set_reasoning_mode(mode)

    def set_planning_mode(self, mode: str) -> None:
        self._agent.set_planning_mode(mode)

    def chat(self, message: str) -> str:
        return self._agent.chat(message)

    async def chat_stream(self, message: str):
        async for event in self._agent.chat_stream(message):
            yield event

    async def resume_feedback_stream(
        self,
        *,
        request_id: str,
        decision: str,
        patched_args: dict[str, Any] | None = None,
        comment: str = "",
    ):
        async for event in self._agent.resume_feedback_stream(
            request_id=request_id,
            decision=decision,
            patched_args=patched_args,
            comment=comment,
        ):
            yield event

    async def rollback_last_user_turn(self) -> None:
        await self._agent.rollback_last_user_turn()

    def pending_feedback(self) -> dict[str, Any] | None:
        method = getattr(self._agent, "pending_feedback", None)
        return method() if callable(method) else None

    def context_usage(self) -> dict[str, Any]:
        return self._agent.context_usage()

    def has_graph(self) -> bool:
        return bool(self._agent.has_graph())

    def apply_graph_load_result(self, result: Mapping[str, Any]) -> None:
        self._agent._has_graph = True
        self._agent._graph_node_count = int(result.get("num_nodes") or 0)
        self._agent._graph_edge_count = int(
            result.get("num_edge_entries") or result.get("num_edges") or 0
        )
        self._agent._graph_unique_edge_count = int(
            result.get("num_unique_edges") or 0
        )
        semantic_type = str(result.get("semantic_type") or "").strip().lower()
        self._agent._graph_semantic_type = semantic_type or (
            "directed" if bool(result.get("directed")) else "unknown"
        )
        self._agent._graph_storage_type = str(result.get("storage_type") or "mixed")
        self._agent._graph_is_directed = (
            self._agent._graph_semantic_type == "directed"
        )
        self._agent.logger.set_graph_flag(True)

    def clear_graph_state(self) -> None:
        self._agent._has_graph = False
        self._agent._graph_node_count = 0
        self._agent._graph_edge_count = 0
        self._agent._graph_unique_edge_count = 0
        self._agent._graph_is_directed = False
        self._agent._graph_semantic_type = "unknown"
        self._agent._graph_storage_type = "mixed"
        self._agent._graph_has_weights = False
        self._agent.logger.set_graph_flag(False)

    def inject_synthetic_turn(self, user_text: str, assistant_text: str) -> None:
        user_msg = {"role": "user", "content": user_text}
        assistant_msg = {"role": "assistant", "content": assistant_text}
        self._agent.context_manager.messages.append(user_msg)
        self._agent.context_manager.messages.append(assistant_msg)
        self._agent.logger.log_context_message(
            user_msg,
            visible=True,
            display_content=user_text,
            kind="user_synthetic",
            include_in_context=True,
        )
        self._agent.logger.log_context_message(
            assistant_msg,
            visible=True,
            display_content=assistant_text,
            kind="assistant_synthetic",
            include_in_context=True,
        )

    def log_error(self, error: str) -> None:
        self._agent.logger.log_error(error)

    def stream_skill_generation(
        self,
        playbook_item: dict[str, Any],
        model: str | None = None,
    ) -> AsyncIterator[str]:
        from .agent.sub_agents.skill_generator import SkillGenerator

        gateway = self._agent._provider_gateway
        generator = SkillGenerator(
            model=model or self.model,
            api_key=gateway.api_key,
            base_url=gateway.base_url,
        )
        return generator.stream(playbook_item)

    def cleanup(self) -> None:
        self._agent.cleanup()
