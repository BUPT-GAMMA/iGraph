"""LLM endpoint support used only for Graph Engine text ingestion.

This is deliberately independent of Agno.  Graph algorithms and ingestion do
not import Agent Runtime.
"""

from __future__ import annotations

import os
from typing import Any

from openai import AsyncOpenAI
from model_protocols import protocol_for, sdk_endpoint


SUPPORTED_MODELS = {
    "claude-opus-4-6", "claude-sonnet-4-6", "gpt-5-4", "gpt-5-2",
    "qwen3.6-plus", "qwen3.7-max", "deepseek-v4-pro", "deepseek-v4-flash",
    "deepseek-v4-lite", "doubao-seed-2-0-pro-260215",
}
DEFAULT_MODEL = "qwen3.6-plus"


def normalize_model_name(model: str) -> str:
    value = str(model or DEFAULT_MODEL).strip()
    if value not in SUPPORTED_MODELS:
        raise ValueError(f"Unsupported graph-ingestion model: {value}")
    return value


def provider_for_model(model: str) -> str:
    value = normalize_model_name(model)
    if value.startswith("claude-"):
        return "anthropic"
    if value.startswith("gpt-"):
        return "openai"
    if value.startswith("qwen"):
        return "qwen"
    if value.startswith("deepseek"):
        return "deepseek"
    return "doubao"


class ProviderGateway:
    def __init__(self, *, model: str, api_key: str, base_url: str, temperature: float, logger: Any,
                 resolved_config: dict | None = None) -> None:
        self.model = str(resolved_config['model_id']) if resolved_config else normalize_model_name(model)
        self.output_limit = int(resolved_config['max_output_tokens']) if resolved_config else None
        self.context_window = int(resolved_config['context_window']) if resolved_config else None
        self.temperature = temperature
        self.logger = logger
        self.protocol = protocol_for(resolved_config or {})
        root, version = sdk_endpoint(base_url, self.protocol)
        timeout = float(os.getenv('LLM_TIMEOUT_SECONDS', '180'))
        if self.protocol == 'anthropic':
            from anthropic import AsyncAnthropic
            self.client = AsyncAnthropic(api_key=api_key or 'local-no-auth', base_url=root, timeout=timeout)
        elif self.protocol == 'gemini':
            from google import genai
            self.client = genai.Client(api_key=api_key or 'local-no-auth', vertexai=False,
                http_options={'base_url': root, 'api_version': version, 'timeout': int(timeout * 1000)})
        else:
            self.client = AsyncOpenAI(api_key=api_key or 'local-no-auth', base_url=root, timeout=timeout)

    async def invoke_text(
        self,
        messages: list[dict[str, str]],
        *,
        step: int,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        if self.output_limit:
            max_tokens = min(max_tokens or self.output_limit, self.output_limit)
        if self.context_window:
            import json
            estimated = len(json.dumps(messages, ensure_ascii=False).encode('utf-8')) // 2 + 512
            if estimated + (max_tokens or 0) > self.context_window:
                raise ValueError('MODEL_CONTEXT_TOO_LARGE')
        sampling = self.temperature if temperature is None else temperature
        if self.protocol == 'openai-responses':
            response = await self.client.responses.create(model=self.model, input=messages,
                max_output_tokens=max_tokens, store=False)
            return response.output_text or ''
        if self.protocol == 'anthropic':
            system = '\n\n'.join(m['content'] for m in messages if m['role'] in {'system', 'developer'})
            response = await self.client.messages.create(model=self.model,
                messages=[m for m in messages if m['role'] not in {'system', 'developer'}],
                **({'system': system} if system else {}), max_tokens=max_tokens or 4096)
            return ''.join(block.text for block in response.content if block.type == 'text')
        if self.protocol == 'gemini':
            system = '\n\n'.join(m['content'] for m in messages if m['role'] in {'system', 'developer'})
            response = await self.client.aio.models.generate_content(model=self.model.removeprefix('models/'),
                contents=[{'role': 'model' if m['role'] == 'assistant' else 'user', 'parts': [{'text': m['content']}]} for m in messages if m['role'] not in {'system', 'developer'}],
                config={'system_instruction': system or None, 'max_output_tokens': max_tokens, 'temperature': sampling})
            return response.text or ''
        response = await self.client.chat.completions.create(model=self.model, messages=messages,
            temperature=sampling, max_tokens=max_tokens, stream=False)
        content = response.choices[0].message.content if response.choices else ''
        return str(content or '')
