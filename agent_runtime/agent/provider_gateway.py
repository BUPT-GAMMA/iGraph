"""
Provider 原生 SDK 网关：
- 统一消息归一化（dict role/content）
- 统一 request/response 原始日志打点
- 支持流式与非流式文本生成
"""

from __future__ import annotations

import json
import os
import re
from typing import List

def _tool_message_to_user_content(msg: dict) -> str:
    """将 role=tool 消息转为 role=user 文本（非 native FC 模式下使用）。"""
    def _normalize_result_text(value) -> str:
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        if value is None:
            return "(空)"
        text = str(value).strip()
        if not text:
            return "(空)"
        if text[:1] in {"{", "["}:
            try:
                return json.dumps(json.loads(text), ensure_ascii=False)
            except Exception:
                return text
        return text

    observations = msg.get("observations")
    if isinstance(observations, list):
        lines: list[str] = []
        for item in observations:
            if not isinstance(item, dict):
                continue
            tool_name = str(item.get("name") or "unknown_tool")
            result_text = _normalize_result_text(item.get("result"))
            lines.append(f"{tool_name} 工具调用结果为：{result_text}")
        if not lines:
            lines.append("工具调用结果为空。")
        return "以下是本轮工具调用结果，请基于结果继续回答：\n" + "\n".join(lines)

    tool_name = str(msg.get("name") or "unknown_tool")
    result_text = _normalize_result_text(msg.get("content", ""))
    return f"以下是工具调用结果：\n{tool_name} 工具调用结果为：{result_text}"


class ProviderGateway:
    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str,
        temperature: float,
        logger,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.temperature = temperature
        self.timeout_seconds = self._resolve_timeout_seconds()
        self.logger = logger
        self._client = self._create_client()

    def set_model_endpoint(self, model: str, api_key: str, base_url: str) -> None:
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self._client = self._create_client()

    @staticmethod
    def _resolve_timeout_seconds() -> float:
        raw = (
            os.getenv("LLM_TIMEOUT_SECONDS")
            or os.getenv("OPENAI_TIMEOUT_SECONDS")
            or "180"
        )
        try:
            value = float(raw)
            if value <= 0:
                raise ValueError
            return value
        except Exception:
            return 180.0

    def _create_client(self):
        try:
            from openai import AsyncOpenAI
        except Exception as e:
            raise ValueError(f"未安装 openai SDK: {e}") from e
        return AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout_seconds,
        )

    @staticmethod
    def _message_text(content) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
        return str(content)

    @staticmethod
    def _sanitize_assistant_protocol(content: str) -> str:
        """
        清洗 assistant 协议文本中的空 call_id，避免上游 provider 返回：
        Invalid '...call_id': empty string
        """
        if not content or "<tool_call>" not in content:
            return content

        pattern = re.compile(r"<tool_call>(.*?)</tool_call>", flags=re.DOTALL | re.IGNORECASE)

        def repl(match: re.Match[str]) -> str:
            payload_text = (match.group(1) or "").strip()
            if not payload_text:
                return match.group(0)
            try:
                payload = json.loads(payload_text)
            except Exception:
                return match.group(0)

            if not isinstance(payload, dict):
                return match.group(0)
            calls = payload.get("tool_calls")
            if not isinstance(calls, list):
                return match.group(0)

            changed = False
            for call in calls:
                if not isinstance(call, dict) or "call_id" not in call:
                    continue
                cid = call.get("call_id")
                if not isinstance(cid, str):
                    call.pop("call_id", None)
                    changed = True
                    continue
                stripped = cid.strip()
                if not stripped:
                    call.pop("call_id", None)
                    changed = True
                elif stripped != cid:
                    call["call_id"] = stripped
                    changed = True

            if not changed:
                return match.group(0)
            return f"<tool_call>{json.dumps(payload, ensure_ascii=False)}</tool_call>"

        return pattern.sub(repl, content)

    def _to_provider_messages(self, model_msgs: List[dict]) -> List[dict]:
        messages: List[dict] = []
        for msg in model_msgs:
            role = str(msg.get("role", "user"))
            content = self._message_text(msg.get("content", ""))

            if role == "assistant":
                content = self._sanitize_assistant_protocol(content)
            if role == "tool":
                messages.append({"role": "user", "content": _tool_message_to_user_content(msg)})
                continue
            if role not in {"system", "user", "assistant"}:
                role = "user"
            messages.append({"role": role, "content": content})

        return messages

    def _build_payload(self, model_msgs: List[dict], *, stream: bool) -> dict:
        provider_messages = self._to_provider_messages(model_msgs)
        return {
            "model": self.model,
            "messages": provider_messages,
            "temperature": self.temperature,
            "stream": stream,
        }

    @staticmethod
    def _to_native_fc_messages(model_msgs: List[dict]) -> List[dict]:
        """
        Native FC 消息归一化：保留 role=tool（不转为 role=user），
        支持 assistant 消息携带 tool_calls 字段。
        """
        messages: List[dict] = []
        for msg in model_msgs:
            role = str(msg.get("role", "user"))
            if role == "tool":
                messages.append({
                    "role": "tool",
                    "tool_call_id": str(msg.get("tool_call_id", "")),
                    "content": str(msg.get("content", "")),
                })
            elif role == "assistant":
                entry: dict = {
                    "role": "assistant",
                    "content": ProviderGateway._message_text(msg.get("content", "")) or None,
                }
                if msg.get("tool_calls"):
                    entry["tool_calls"] = msg["tool_calls"]
                messages.append(entry)
            elif role in {"system", "user"}:
                messages.append({
                    "role": role,
                    "content": ProviderGateway._message_text(msg.get("content", "")),
                })
            else:
                messages.append({
                    "role": "user",
                    "content": ProviderGateway._message_text(msg.get("content", "")),
                })
        return messages

    async def create_streaming_completion(
        self,
        messages: List[dict],
        *,
        step: int,
        tools: List[dict] | None = None,
        native_fc: bool = False,
    ):
        """
        返回原始 AsyncStream 对象（供 NativeFCDriver 直接迭代 chunk）。
        native_fc=True 时使用 _to_native_fc_messages（保留 role=tool，支持 assistant.tool_calls）。
        """
        if native_fc:
            provider_messages = self._to_native_fc_messages(messages)
        else:
            provider_messages = self._to_provider_messages(messages)
        payload = {
            "model": self.model,
            "messages": provider_messages,
            "temperature": self.temperature,
        }
        if tools:
            payload["tools"] = tools
        self.logger.log_trace_event(
            "PROVIDER_REQUEST_RAW",
            {"step": step, "provider": "openai_compatible", "payload": payload},
        )
        return await self._client.chat.completions.create(
            **payload,
            stream=True,
            timeout=self.timeout_seconds,
        )

    async def stream_text(
        self,
        model_msgs: List[dict],
        *,
        step: int,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ):
        payload = self._build_payload(model_msgs, stream=True)
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        self.logger.log_trace_event(
            "PROVIDER_REQUEST_RAW",
            {"step": step, "provider": "openai_compatible", "payload": payload},
        )

        request = dict(payload)
        request.pop("stream", None)
        response = await self._client.chat.completions.create(
            **request,
            stream=True,
            timeout=self.timeout_seconds,
        )
        raw_chunks = []
        try:
            async for chunk in response:
                try:
                    raw_chunks.append(chunk.model_dump())
                except Exception:
                    raw_chunks.append(str(chunk))
                delta = ""
                try:
                    if chunk.choices and chunk.choices[0].delta:
                        delta = chunk.choices[0].delta.content or ""
                except Exception:
                    delta = ""
                if delta:
                    yield delta
        finally:
            aclose = getattr(response, "aclose", None)
            if callable(aclose):
                try:
                    await aclose()
                except Exception:
                    pass
            self.logger.log_trace_event(
                "PROVIDER_RESPONSE_RAW",
                {"step": step, "provider": "openai_compatible", "chunks": raw_chunks},
            )

    async def invoke_text(
        self,
        model_msgs: List[dict],
        *,
        step: int,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        payload = self._build_payload(model_msgs, stream=False)
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        self.logger.log_trace_event(
            "PROVIDER_REQUEST_RAW",
            {"step": step, "provider": "openai_compatible", "payload": payload},
        )

        request = dict(payload)
        request.pop("stream", None)
        response = await self._client.chat.completions.create(
            **request,
            timeout=self.timeout_seconds,
        )
        try:
            raw_response = response.model_dump()
        except Exception:
            raw_response = str(response)
        self.logger.log_trace_event(
            "PROVIDER_RESPONSE_RAW",
            {"step": step, "provider": "openai_compatible", "response": raw_response},
        )
        try:
            return response.choices[0].message.content or ""
        except Exception:
            return ""
