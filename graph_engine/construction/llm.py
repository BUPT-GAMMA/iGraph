"""A construction-only, bounded OpenAI-compatible tool calling client.

Uses the same environment conventions as text ingestion, without importing
Agent Runtime, sharing conversation state, or accepting credentials/URLs from
dataset options. No provider response bodies or credentials enter error logs.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


class ConstructionLLMError(RuntimeError):
    pass


class ToolCallingLLM:
    def __init__(self, *, model=None, timeout=120):
        self.model = model or os.getenv("GRAPH_CONSTRUCTION_MODEL") or os.getenv("LLM_MODEL") or "qwen3.6-plus"
        providers = (
            ("qwen", ("QWEN", "DASHSCOPE")),
            ("deepseek", ("DEEPSEEK",)),
            ("claude", ("CLAUDE", "ANTHROPIC")),
            ("doubao", ("ARK", "DOUBAO")),
            ("gpt", ("OPENAI",)),
        )
        prefixes = next((p for name, p in providers if self.model.startswith(name)), ("OPENAI",))
        prefixes = ("GRAPH_CONSTRUCTION", *prefixes, "LLM")
        self._key = next((os.environ[p + "_API_KEY"] for p in prefixes if os.getenv(p + "_API_KEY")), "")
        base = next((os.environ[p + "_BASE_URL"] for p in prefixes if os.getenv(p + "_BASE_URL")), "")
        if not self._key or not base:
            raise ConstructionLLMError("construction_llm_unavailable: configure GRAPH_CONSTRUCTION_API_KEY/BASE_URL or the model provider environment")
        if not base.startswith(("https://", "http://")):
            raise ConstructionLLMError("construction_llm_invalid_base_url")
        self._url = base.rstrip("/") + "/chat/completions"
        self.timeout = timeout

    def complete(self, messages, tools, *, timeout=None):
        body = json.dumps({
            "model": self.model, "messages": messages, "tools": tools,
            "tool_choice": "required", "temperature": 0, "max_tokens": 14000,
            "stream": False,
        }, ensure_ascii=False, allow_nan=False).encode("utf-8")
        request = urllib.request.Request(self._url, data=body, headers={
            "Authorization": "Bearer " + self._key, "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(request, timeout=min(self.timeout, timeout or self.timeout)) as response:
                payload = response.read(4 * 1024 * 1024 + 1)
                if len(payload) > 4 * 1024 * 1024:
                    raise ConstructionLLMError("construction_llm_response_too_large")
            message = json.loads(payload)["choices"][0]["message"]
            if not isinstance(message, dict):
                raise ValueError("invalid response")
            # Keep only standard chat fields; never persist internal reasoning.
            return {k: message[k] for k in ("role", "content", "tool_calls") if k in message}
        except urllib.error.HTTPError as exc:
            raise ConstructionLLMError(f"construction_llm_http_{exc.code}") from None
        except (urllib.error.URLError, TimeoutError):
            raise ConstructionLLMError("construction_llm_connection_or_timeout") from None
        except (KeyError, IndexError, ValueError, TypeError):
            raise ConstructionLLMError("construction_llm_invalid_response") from None
