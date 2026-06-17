from __future__ import annotations

import json
from typing import Iterable


def _extract_tool_calls(records: list[dict]) -> list[dict]:
    """从 trace records 中提取工具调用链路。"""
    calls: list[dict] = []
    for record in records:
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        kind = str(record.get("kind") or "")
        if kind == "tool_observation":
            calls.append({
                "tool": str(message.get("name") or record.get("tool_name") or ""),
                "result_preview": str(message.get("content") or "")[:200],
            })
    return calls


def export_sft_records(records: list[dict], fmt: str = "alpaca") -> list[dict]:
    fmt = (fmt or "alpaca").strip().lower()
    if fmt == "raw":
        return records

    turns: list[tuple[str, str, list[dict]]] = []
    current_user = ""
    current_tools: list[dict] = []
    for record in records:
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        kind = str(record.get("kind") or "")
        content = str(record.get("display_content") or message.get("content") or "")
        if role == "user" and kind != "plan_step":
            if current_user and not current_tools:
                pass
            current_user = content
            current_tools = []
        elif kind == "tool_observation":
            tool_name = str(message.get("name") or record.get("tool_name") or "")
            if tool_name:
                current_tools.append({"tool": tool_name})
        elif role == "assistant" and current_user:
            turns.append((current_user, content, list(current_tools)))
            current_user = ""
            current_tools = []

    if fmt == "sharegpt":
        rows: list[dict] = []
        for user, assistant, tools in turns:
            convs: list[dict] = [{"from": "human", "value": user}]
            for tc in tools:
                convs.append({"from": "function_call", "value": json.dumps(tc, ensure_ascii=False)})
            convs.append({"from": "gpt", "value": assistant})
            rows.append({"conversations": convs})
        return rows

    return [
        {
            "instruction": user,
            "input": ", ".join(tc["tool"] for tc in tools) if tools else "",
            "output": assistant,
        }
        for user, assistant, tools in turns
    ]
