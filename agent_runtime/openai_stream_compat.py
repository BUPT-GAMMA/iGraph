"""Normalize relay tool indices before Agno assembles streamed arguments."""
from __future__ import annotations

import copy


class RelayToolCallsMixin:
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
