from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from backend.agent.memory.provider import MemoryProvider

if TYPE_CHECKING:
    from backend.agent.memory.builtin_provider import BuiltinMemoryProvider
    from backend.agent.memory.memory_engine import MemoryEngine
    from backend.agent.memory.scope import MemoryScope

_LOG = logging.getLogger(__name__)


class MemoryManager:
    """Orchestrates one built-in and at most one external :class:`MemoryProvider`.

    The main agent loop depends exclusively on ``MemoryManager``; direct
    references to ``MemoryEngine`` only appear in the built-in provider and
    virtual-tool handlers.

    Design guarantees:
    - The built-in provider is always present.
    - External provider failures are isolated (try-except per call).
    - At most one external provider can be registered.
    """

    def __init__(
        self,
        builtin: "BuiltinMemoryProvider",
        extra: MemoryProvider | None = None,
    ) -> None:
        self._builtin = builtin
        self._extra = extra

    # ── Session lifecycle ────────────────────────────────────────────

    def load_and_freeze(self, scope: "MemoryScope | None" = None) -> str:
        """Initialize all providers for *scope* and return the frozen system block."""
        from backend.agent.memory.scope import DEFAULT_SCOPE
        effective_scope = scope or DEFAULT_SCOPE
        block = self._builtin.load_and_freeze(effective_scope)
        if self._extra is not None and self._extra.is_available():
            try:
                extra_block = self._extra.load_and_freeze(effective_scope)
                if extra_block:
                    block = block + "\n" + extra_block if block else extra_block
            except Exception:
                _LOG.debug("extra provider load_and_freeze failed", exc_info=True)
        return block

    def get_system_block(self) -> str:
        """Return the frozen snapshot block (stable within a session)."""
        block = self._builtin.system_prompt_block()
        if self._extra is not None and self._extra.is_available():
            try:
                extra_block = self._extra.system_prompt_block()
                if extra_block:
                    block = block + "\n" + extra_block if block else extra_block
            except Exception:
                _LOG.debug("extra provider system_prompt_block failed", exc_info=True)
        return block

    def refresh_snapshot(self) -> tuple[bool, str]:
        """Rebuild the built-in provider's snapshot from disk.

        Returns ``(changed, new_block)``: ``changed=False`` lets callers
        skip prompt rebuilds and keep prefix-cache hits when no new
        memory landed since the last freeze.
        """
        engine = self._builtin.engine
        if engine is None:
            return False, ""
        return engine.refresh_snapshot()

    # ── Tool dispatch ────────────────────────────────────────────────

    def get_tool_schemas(self) -> list[dict]:
        """Aggregate tool schemas from all available providers."""
        schemas = list(self._builtin.get_tool_schemas())
        if self._extra is not None and self._extra.is_available():
            try:
                schemas.extend(self._extra.get_tool_schemas())
            except Exception:
                _LOG.debug("extra provider get_tool_schemas failed", exc_info=True)
        return schemas

    def handle_tool_call(self, name: str, args: dict) -> str | None:
        """Route a virtual-tool call to the appropriate provider."""
        result = self._builtin.handle_tool_call(name, args)
        if result and not result.startswith("错误：未知工具"):
            return result
        if self._extra is not None and self._extra.is_available():
            try:
                return self._extra.handle_tool_call(name, args)
            except Exception:
                _LOG.debug("extra provider handle_tool_call failed", exc_info=True)
        return None

    # ── Turn synchronisation ─────────────────────────────────────────

    async def sync_turn(self, **kwargs: Any) -> None:
        """Asynchronously persist turn data through all providers.

        Exceptions from the builtin provider are intentionally *not* caught
        here — they bubble up to ``_schedule_memory_update``'s handler in
        ``iGraph`` so that ``MEMORY_AGENT_ERROR`` events are logged.
        """
        await self._builtin.engine.maybe_update_from_turn(**kwargs)
        if self._extra is not None and self._extra.is_available():
            try:
                self._extra.sync_turn(kwargs)
            except Exception:
                _LOG.debug("extra provider sync_turn failed", exc_info=True)

    def on_session_end(self, messages: list[dict]) -> None:
        self._builtin.on_session_end(messages)
        if self._extra is not None:
            try:
                self._extra.on_session_end(messages)
            except Exception:
                _LOG.debug("extra provider on_session_end failed", exc_info=True)

    def shutdown(self) -> None:
        self._builtin.shutdown()
        if self._extra is not None:
            try:
                self._extra.shutdown()
            except Exception:
                _LOG.debug("extra provider shutdown failed", exc_info=True)

    # ── Convenience accessors ────────────────────────────────────────

    @property
    def engine(self) -> "MemoryEngine | None":
        """Direct access to the underlying MemoryEngine (for virtual-tool handlers)."""
        return self._builtin.engine

    @property
    def builtin(self) -> "BuiltinMemoryProvider":
        return self._builtin

    @property
    def extra(self) -> MemoryProvider | None:
        return self._extra
