from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.agent.memory.scope import MemoryScope


class MemoryProvider(ABC):
    """Abstract base for all memory providers.

    The built-in provider wraps :class:`MemoryEngine`; external providers
    (e.g. Honcho, mem0) implement this interface and are registered into
    :class:`MemoryManager` as the optional *extra* provider.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider identifier (e.g. ``"builtin"``, ``"mem0"``)."""

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if the provider is fully configured and reachable."""

    @abstractmethod
    def initialize(self, scope: "MemoryScope", **kwargs: object) -> None:
        """Bind the provider to the given *scope* for this session."""

    @abstractmethod
    def get_tool_schemas(self) -> list[dict]:
        """Return OpenAI-style tool schema dicts this provider exposes."""

    @abstractmethod
    def handle_tool_call(self, name: str, args: dict) -> str:
        """Execute a tool call and return the result string."""

    # ── Optional hooks ───────────────────────────────────────────────

    def system_prompt_block(self) -> str:
        """Return a frozen snapshot block for injection into system prompt."""
        return ""

    def load_and_freeze(self, scope: "MemoryScope") -> str:
        """Initialize for the scope and return the frozen snapshot block."""
        self.initialize(scope)
        return self.system_prompt_block()

    def sync_turn(self, turn_data: dict) -> None:
        """Called after each completed turn; may persist relevant facts."""

    def on_session_end(self, messages: list[dict]) -> None:
        """Called when the session terminates."""

    def shutdown(self) -> None:
        """Release any resources held by the provider."""
