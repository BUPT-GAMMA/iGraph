"""A construction-only, bounded OpenAI-compatible tool calling client.

Uses the same environment conventions as text ingestion, without importing
Agent Runtime, sharing conversation state, or accepting credentials/URLs from
dataset options. No provider response bodies or credentials enter error logs.
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request


class ConstructionLLMError(RuntimeError):
    pass


_DNS_OVERRIDE_LOCK = threading.Lock()


class ToolCallingLLM:
    def __init__(self, *, model=None, timeout=120, tool_choice=None):
        self.model = model or os.getenv("GRAPH_CONSTRUCTION_MODEL") or os.getenv("LLM_MODEL") or "qwen3.6-plus"
        # Some compatible providers reject required with nested operation
        # schemas. Auto preserves the complete schema; the agent still requires
        # actual tool calls within bounded workload/evolution attempts.
        self.tool_choice = tool_choice if tool_choice is not None else os.getenv("GRAPH_CONSTRUCTION_TOOL_CHOICE", "auto")
        if not isinstance(self.tool_choice, str) or self.tool_choice not in ("auto", "required"):
            raise ConstructionLLMError("construction_llm_invalid_tool_choice: expected auto or required")
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
        self._provider_host = urllib.parse.urlsplit(self._url).hostname
        resolved_ip = os.getenv("GRAPH_CONSTRUCTION_RESOLVED_IP", "").strip()
        if resolved_ip:
            try:
                ipaddress.ip_address(resolved_ip)
            except ValueError:
                raise ConstructionLLMError("construction_llm_invalid_resolved_ip") from None
        self._resolved_ip = resolved_ip or None
        self.timeout = timeout
        self.successful_calls = 0
        self.response_models = []
        self.usage_available_calls = 0
        self.token_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    def audit_metadata(self):
        """Return bounded provider metadata; never include prompts or response text."""
        return {
            "successful_calls": self.successful_calls,
            "provider_host": self._provider_host,
            "dns_override_used": self._resolved_ip is not None,
            "response_models": self.response_models.copy(),
            "usage_available_calls": self.usage_available_calls,
            "token_usage": self.token_usage.copy(),
        }

    def _urlopen(self, request, timeout):
        if self._resolved_ip is None:
            return urllib.request.urlopen(request, timeout=timeout)
        # Keep the URL host for Host/SNI/certificate validation while replacing
        # only its DNS result. This is an explicit process-local recovery path
        # for servers whose system resolver is unavailable.
        with _DNS_OVERRIDE_LOCK:
            original = socket.getaddrinfo

            def redirected(host, port, *args, **kwargs):
                target = self._resolved_ip if host == self._provider_host else host
                return original(target, port, *args, **kwargs)

            socket.getaddrinfo = redirected
            try:
                return urllib.request.urlopen(request, timeout=timeout)
            finally:
                socket.getaddrinfo = original

    def _request(self, body, *, timeout=None):
        body = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        request = urllib.request.Request(self._url, data=body, headers={
            "Authorization": "Bearer " + self._key, "Content-Type": "application/json",
        })
        try:
            with self._urlopen(request, timeout=min(self.timeout, timeout or self.timeout)) as response:
                payload = response.read(4 * 1024 * 1024 + 1)
                if len(payload) > 4 * 1024 * 1024:
                    raise ConstructionLLMError("construction_llm_response_too_large")
            document = json.loads(payload)
            message = document["choices"][0]["message"]
            if not isinstance(message, dict):
                raise ValueError("invalid response")
            response_model = document.get("model")
            if isinstance(response_model, str) and 0 < len(response_model) <= 200:
                if response_model not in self.response_models:
                    self.response_models.append(response_model)
            usage = document.get("usage")
            if isinstance(usage, dict):
                captured = {}
                for key in self.token_usage:
                    value = usage.get(key)
                    if type(value) is int and value >= 0:
                        captured[key] = value
                if captured:
                    self.usage_available_calls += 1
                    for key, value in captured.items():
                        self.token_usage[key] += value
            self.successful_calls += 1
            # Keep only standard chat fields; never persist internal reasoning.
            return {k: message[k] for k in ("role", "content", "tool_calls") if k in message}
        except urllib.error.HTTPError as exc:
            raise ConstructionLLMError(f"construction_llm_http_{exc.code}") from None
        except (urllib.error.URLError, TimeoutError):
            raise ConstructionLLMError("construction_llm_connection_or_timeout") from None
        except (KeyError, IndexError, ValueError, TypeError):
            raise ConstructionLLMError("construction_llm_invalid_response") from None

    def complete(self, messages, tools, *, timeout=None):
        return self._request({
            "model": self.model, "messages": messages, "tools": tools,
            "tool_choice": self.tool_choice, "temperature": 0, "max_tokens": 14000,
            "stream": False,
        }, timeout=timeout)

    def complete_text(self, messages, *, timeout=None, max_tokens=8192):
        """Request bounded plain text without exposing construction tools.

        Semantic TXT extraction has a strict text parser and must not inherit a
        provider-level ``required`` tool choice from the tabular evolution loop.
        """
        if type(max_tokens) is not int or not 1 <= max_tokens <= 14000:
            raise ConstructionLLMError("construction_llm_invalid_max_tokens")
        return self._request({
            "model": self.model, "messages": messages, "temperature": 0,
            "max_tokens": max_tokens, "stream": False,
        }, timeout=timeout)
