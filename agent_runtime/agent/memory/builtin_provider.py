from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from agent_runtime.agent.memory.provider import MemoryProvider

if TYPE_CHECKING:
    from agent_runtime.agent.memory.memory_engine import MemoryEngine
    from agent_runtime.agent.memory.scope import MemoryScope

_LOG = logging.getLogger(__name__)


class BuiltinMemoryProvider(MemoryProvider):
    """Built-in memory provider that delegates to :class:`MemoryEngine`.

    This is always the primary provider; external providers are optional.
    """

    def __init__(self, engine: "MemoryEngine") -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "builtin"

    @property
    def engine(self) -> "MemoryEngine":
        return self._engine

    def is_available(self) -> bool:
        return True

    def initialize(self, scope: "MemoryScope", **kwargs: Any) -> None:
        pass

    def get_tool_schemas(self) -> list[dict]:
        from agent_runtime.agent.memory.tools import VIRTUAL_MEMORY_TOOL_SPECS
        return list(VIRTUAL_MEMORY_TOOL_SPECS)

    def handle_tool_call(self, name: str, args: dict) -> str:
        from agent_runtime.agent.memory.tools import handle_memory_tool_call
        result = handle_memory_tool_call(name, args, self._engine)
        return result if result is not None else f"错误：未知工具 {name}"

    def system_prompt_block(self) -> str:
        return self._engine.get_system_block()

    def load_and_freeze(self, scope: "MemoryScope") -> str:
        self.initialize(scope)
        return self._engine.load_and_freeze()

    def sync_turn(self, turn_data: dict) -> None:
        # NOTE: MemoryManager.sync_turn bypasses this and calls
        # engine.maybe_update_from_turn directly so exceptions bubble up.
        # This method exists to fulfil the MemoryProvider interface and
        # may be used by callers that hold a BuiltinMemoryProvider directly.
        coro = self._engine.maybe_update_from_turn(**turn_data)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(coro)
        except RuntimeError:
            asyncio.run(coro)

    def on_session_end(self, messages: list[dict]) -> None:
        pass

    def shutdown(self) -> None:
        pass
