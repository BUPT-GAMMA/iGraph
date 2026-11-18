"""Native provider formats for the construction agent's tool messages."""
import json
from uuid import uuid4


def _messages(body):
    messages = body["messages"]
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    return system, [m for m in messages if m["role"] != "system"]


def _append(messages, role, key, parts):
    if not parts:
        return
    if messages and messages[-1]["role"] == role:
        messages[-1][key].extend(parts)
    else:
        messages.append({"role": role, key: parts})


def _anthropic_request(body, tools):
    system, history = _messages(body)
    messages = []
    for message in history:
        role = message["role"]
        if role == "tool":
            _append(messages, "user", "content", [
                {"type": "tool_result", "tool_use_id": message["tool_call_id"], "content": message["content"]}])
            continue
        parts = [{"type": "text", "text": message["content"]}] if message.get("content") else []
        for call in message.get("tool_calls", []):
            fn = call["function"]
            parts.append({"type": "tool_use", "id": call["id"], "name": fn["name"], "input": json.loads(fn["arguments"])})
        _append(messages, role, "content", parts)
    request = {"model": body["model"], "messages": messages, "max_tokens": body["max_tokens"]}
    if system:
        request["system"] = system
    if tools:
        request["tools"] = [{"name": t["name"], "description": t.get("description", ""),
                             "input_schema": t["parameters"]} for t in tools]
        request["tool_choice"] = {"type": "any" if body["tool_choice"] == "required" else "auto"}
    return request


def _gemini_request(body, tools):
    system, history = _messages(body)
    contents, seen = [], {}
    for message in history:
        role = message["role"]
        if role == "tool":
            call = seen[message["tool_call_id"]]
            response = {"name": call["name"], "response": json.loads(message["content"])}
            if call.get("id"):
                response["id"] = call["id"]
            _append(contents, "user", "parts", [{"functionResponse": response}])
            continue
        parts = [{"text": message["content"]}] if message.get("content") else []
        for call in message.get("tool_calls", []):
            fn = call["function"]
            data = {"name": fn["name"], "args": json.loads(fn["arguments"])}
            if call.get("native_id"):
                data["id"] = call["native_id"]
            seen[call["id"]] = data
            part = {"functionCall": data}
            if call.get("thought_signature"):
                part["thoughtSignature"] = call["thought_signature"]
            parts.append(part)
        _append(contents, "model" if role == "assistant" else "user", "parts", parts)
    request = {"contents": contents, "generationConfig": {"maxOutputTokens": body["max_tokens"]}}
    if system:
        request["systemInstruction"] = {"parts": [{"text": system}]}
    if tools:
        request["tools"] = [{"functionDeclarations": [
            {"name": t["name"], "description": t.get("description", ""),
             "parametersJsonSchema": t["parameters"]} for t in tools]}]
        request["toolConfig"] = {"functionCallingConfig": {"mode": "ANY" if body["tool_choice"] == "required" else "AUTO"}}
    return request


def _responses_request(body, tools):
    items = []
    for message in body["messages"]:
        if message["role"] == "tool":
            items.append({"type": "function_call_output", "call_id": message["tool_call_id"], "output": message["content"]})
            continue
        if message.get("content"):
            items.append({"role": message["role"], "content": message["content"]})
        for call in message.get("tool_calls", []):
            items.extend(call.get("reasoning", []))
            items.append({"type": "function_call", "call_id": call["id"], **call["function"]})
    request = {"model": body["model"], "input": items, "max_output_tokens": body["max_tokens"], "store": False}
    if tools:
        request["tools"] = [{"type": "function", "strict": False, **t} for t in tools]
        request["tool_choice"] = body["tool_choice"]
        request["include"] = ["reasoning.encrypted_content"]
    return request


_ENCODERS = {"anthropic": _anthropic_request, "gemini": _gemini_request, "openai-responses": _responses_request}


def native_request(protocol, body):
    return _ENCODERS[protocol](body, [tool["function"] for tool in body.get("tools", [])])


def _tool_call(call_id, name, arguments, **extra):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}, **extra}


def native_response(protocol, doc):
    text, calls = [], []
    if protocol == "anthropic":
        for part in doc["content"]:
            if part["type"] == "text":
                text.append(part["text"])
            if part["type"] == "tool_use":
                calls.append(_tool_call(part["id"], part["name"], json.dumps(part["input"], ensure_ascii=False)))
        finish = "length" if doc["stop_reason"] == "max_tokens" else "stop"
        usage = doc.get("usage", {})
        counts = {"prompt_tokens": usage.get("input_tokens"), "completion_tokens": usage.get("output_tokens")}
    elif protocol == "gemini":
        candidate = doc["candidates"][0]
        for part in candidate.get("content", {}).get("parts", []):
            if part.get("text") and not part.get("thought"):
                text.append(part["text"])
            if fn := part.get("functionCall"):
                calls.append(_tool_call(fn.get("id") or "call_" + uuid4().hex, fn["name"],
                    json.dumps(fn.get("args", {}), ensure_ascii=False),
                    native_id=fn.get("id"), thought_signature=part.get("thoughtSignature")))
        finish = "length" if candidate.get("finishReason") == "MAX_TOKENS" else "stop"
        usage = doc.get("usageMetadata", {})
        counts = {"prompt_tokens": usage.get("promptTokenCount"), "completion_tokens": usage.get("candidatesTokenCount"),
                  "total_tokens": usage.get("totalTokenCount")}
    else:
        reasoning = []
        for item in doc["output"]:
            if item["type"] == "message":
                text.extend(p["text"] for p in item["content"] if p["type"] == "output_text")
            elif item["type"] == "reasoning" and item.get("encrypted_content"):
                reasoning.append(item)
            elif item["type"] == "function_call":
                calls.append(_tool_call(item["call_id"], item["name"], item["arguments"], reasoning=reasoning))
                reasoning = []
        finish = "length" if doc.get("status") == "incomplete" and (doc.get("incomplete_details") or {}).get("reason") == "max_output_tokens" else "stop"
        usage = doc.get("usage") or {}
        counts = {"prompt_tokens": usage.get("input_tokens"), "completion_tokens": usage.get("output_tokens"),
                  "total_tokens": usage.get("total_tokens")}
    message = {"role": "assistant", "content": "".join(text)}
    if calls:
        message["tool_calls"] = calls
    return {"model": doc.get("model") or doc.get("modelVersion"), "usage": counts,
            "choices": [{"message": message, "finish_reason": finish}]}
