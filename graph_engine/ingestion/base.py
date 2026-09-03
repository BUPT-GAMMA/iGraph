from __future__ import annotations

import time
from typing import Any, AsyncIterator, Awaitable, Callable


PromptInvoker = Callable[[str], Awaitable[str]]
PromptStreamer = Callable[[str], AsyncIterator[str]]


class SubAgent:
    """Shared base for Graph Engine LLM ingestion workers.

    Subclasses differ primarily by system prompt, optional tool schemas, and
    the user prompt they render for a task.
    """

    name = "sub_agent"
    system_prompt = ""
    tool_specs: list[dict[str, Any]] = []
    default_max_tokens: int | None = 512
    default_temperature: float | None = 0.0

    def __init__(
        self,
        *,
        provider_gateway: Any | None = None,
        prompt_invoker: PromptInvoker | None = None,
        prompt_streamer: PromptStreamer | None = None,
    ) -> None:
        self._provider_gateway = provider_gateway
        self._prompt_invoker = prompt_invoker
        self._prompt_streamer = prompt_streamer

    def build_messages(self, user_prompt: str) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.append({"role": "user", "content": user_prompt})
        return messages

    def compose_prompt(self, user_prompt: str) -> str:
        if not self.system_prompt:
            return user_prompt
        return f"{self.system_prompt}\n\n{user_prompt}"

    def _step(self) -> int:
        return 920000 + int(time.time() * 1000) % 10000

    async def invoke_text(
        self,
        user_prompt: str,
        *,
        step: int | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        if self._provider_gateway is not None:
            kwargs: dict[str, Any] = {"step": step if step is not None else self._step()}
            resolved_max_tokens = self.default_max_tokens if max_tokens is None else max_tokens
            resolved_temperature = self.default_temperature if temperature is None else temperature
            if resolved_max_tokens is not None:
                kwargs["max_tokens"] = resolved_max_tokens
            if resolved_temperature is not None:
                kwargs["temperature"] = resolved_temperature
            return await self._provider_gateway.invoke_text(self.build_messages(user_prompt), **kwargs)
        if self._prompt_invoker is None:
            raise RuntimeError(f"{self.name}_llm_unavailable")
        return await self._prompt_invoker(self.compose_prompt(user_prompt))

    async def stream_text(
        self,
        user_prompt: str,
        *,
        step: int | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[str]:
        if self._provider_gateway is not None:
            kwargs: dict[str, Any] = {"step": step if step is not None else self._step()}
            resolved_max_tokens = self.default_max_tokens if max_tokens is None else max_tokens
            resolved_temperature = self.default_temperature if temperature is None else temperature
            if resolved_max_tokens is not None:
                kwargs["max_tokens"] = resolved_max_tokens
            if resolved_temperature is not None:
                kwargs["temperature"] = resolved_temperature
            async for delta in self._provider_gateway.stream_text(
                self.build_messages(user_prompt), **kwargs
            ):
                yield delta
            return
        if self._prompt_streamer is None:
            raise RuntimeError(f"{self.name}_stream_unavailable")
        async for delta in self._prompt_streamer(self.compose_prompt(user_prompt)):
            yield delta
