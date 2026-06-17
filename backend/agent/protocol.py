from __future__ import annotations

import json
import re
from typing import Any

from backend.agent.context.text_protocol_driver import StatefulStreamParser
from backend.agent.provider_gateway import _tool_message_to_user_content


ProtocolStreamParser = StatefulStreamParser


def parse_model_output(text: str) -> dict[str, Any]:
    parser = StatefulStreamParser()
    parser.feed(text or "")
    parser.flush()
    parse_error = parser.parse_error
    if "<tool_calls>" in str(text or ""):
        parse_error = "旧版 <tool_calls> 标签已废弃，请使用 <tool_call>"
        return {
            "action": None,
            "think": parser.think_text,
            "tool_calls": [],
            "final_answer": "",
            "parse_error": parse_error,
        }
    return {
        "action": parser.action,
        "think": parser.think_text,
        "tool_calls": parser.tool_calls,
        "final_answer": parser.answer_text if parser.action == "answer" else "",
        "parse_error": parse_error,
    }


def normalize_model_output(text: str, *, reasoning_mode: str = "direct") -> dict[str, Any]:
    raw = str(text or "")
    think_match = re.search(r"<think>(.*?)</think>", raw, flags=re.DOTALL | re.IGNORECASE)
    think = think_match.group(1) if think_match else ("" if reasoning_mode == "direct" else "")
    action_match = re.search(
        r"<(answer|tool_call)>(.*?)</\1>",
        raw,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if action_match:
        normalized = f"<think>{think}</think>{action_match.group(0)}"
        return {"normalized_text": normalized, "had_action_tag": True}
    return {
        "normalized_text": f"<think>{think}</think><answer>{raw}</answer>",
        "had_action_tag": False,
    }


def build_tool_observation_batch_message(
    observations: list[dict],
    *,
    reasoning_mode: str = "direct",
) -> dict[str, Any]:
    _ = reasoning_mode
    return {
        "role": "tool",
        "name": "tool_batch",
        "observations": list(observations or []),
        "content": json.dumps({"observations": observations or []}, ensure_ascii=False),
    }


def tool_message_to_user_content(msg: dict) -> str:
    return _tool_message_to_user_content(msg)
