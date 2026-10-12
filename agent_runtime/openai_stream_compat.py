"""Normalize relay tool indices before Agno assembles streamed arguments."""
from __future__ import annotations

import copy


class RelayToolCallsMixin:
    def _format_message(self, message, compress_tool_results=False):
        payload = super()._format_message(message, compress_tool_results=compress_tool_results)
        # Agno serializes content=None as an empty string even on tool calls.
        # Relays to Claude reject that as an empty text block. Tool-only
        # assistant messages have no text, which Chat Completions represents
        # with a neutral marker: some relays turn null back into an empty text
        # block too. Keep tool calls and signatures intact.
        content = payload.get("content")
        if isinstance(content, list):
            content = [part for part in content if not (isinstance(part, dict)
                and part.get("type") == "text" and not str(part.get("text") or "").strip())]
            payload["content"] = content or None
        if content is None or (isinstance(content, str) and not content.strip()) or content == []:
            if payload.get("role") == "assistant" and payload.get("tool_calls"):
                payload["content"] = "[Tool invocation; see tool_calls.]"
            elif payload.get("role") == "tool":
                payload["content"] = "[Tool returned no textual content.]"
        return payload

    def parse_tool_calls(self, tool_calls_data):
        by_id, by_index, extras = {}, {}, {}
        normalized = []
        count = 0
        for delta in tool_calls_data:
            source_index = delta.index or 0
            if delta.id and delta.id in by_id:
                target = by_id[delta.id]
            elif delta.id or source_index not in by_index:
                target = count
                count += 1
            else:
                target = by_index[source_index]
            if delta.id:
                by_id[delta.id] = target
            by_index[source_index] = target
            normalized.append(delta.model_copy(update={'index': target}))
            # Gemini relays attach signed state to tool calls. Keep it for the
            # next request on this same connection, including streaming calls.
            extra = getattr(delta, 'extra_content', None)
            if extra:
                extras[target] = copy.deepcopy(extra)
        calls = super().parse_tool_calls(normalized)
        for index, extra in extras.items():
            calls[index]['extra_content'] = extra
        return calls
