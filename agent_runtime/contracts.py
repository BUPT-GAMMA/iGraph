"""Stable in-process contracts between Backend and Agent Runtime."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class RuntimeSession(Protocol):
    """The complete Agent surface used by Backend and the CLI.

    Events deliberately stay as the original dictionaries.  This boundary is
    an in-process seam for replacing the implementation, not a second wire
    protocol.
    """

    @property
    def session_id(self) -> str: ...

    @property
    def user_id(self) -> str: ...

    @property
    def provider(self) -> str: ...

    @property
    def model(self) -> str: ...

    @property
    def reasoning_mode(self) -> str: ...

    @property
    def planning_mode(self) -> str: ...

    @property
    def last_active(self) -> float: ...

    def set_model(self, model: str) -> None: ...

    def set_reasoning_mode(self, mode: str) -> None: ...

    def set_planning_mode(self, mode: str) -> None: ...

    def chat(self, message: str) -> str: ...

    def chat_stream(self, message: str) -> AsyncIterator[dict[str, Any]]: ...

    def resume_feedback_stream(
        self,
        *,
        request_id: str,
        decision: str,
        patched_args: dict[str, Any] | None = None,
        comment: str = "",
    ) -> AsyncIterator[dict[str, Any]]: ...

    async def rollback_last_user_turn(self) -> None: ...

    def pending_feedback(self) -> dict[str, Any] | None: ...

    def context_usage(self) -> dict[str, Any]: ...

    def has_graph(self) -> bool: ...

    def apply_graph_load_result(self, result: Mapping[str, Any]) -> None: ...

    def clear_graph_state(self) -> None: ...

    def inject_synthetic_turn(self, user_text: str, assistant_text: str) -> None: ...

    def log_error(self, error: str) -> None: ...

    def stream_skill_generation(
        self,
        playbook_item: dict[str, Any],
        model: str | None = None,
    ) -> AsyncIterator[str]: ...

    def cleanup(self) -> None: ...


RuntimeFactory = Callable[..., RuntimeSession]
