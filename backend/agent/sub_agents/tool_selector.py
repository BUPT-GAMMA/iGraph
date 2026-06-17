from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

_DEFAULT_TIMEOUT_S = float(os.getenv("TOOL_SELECTOR_TIMEOUT_SECONDS") or "15")

from backend.agent.memory.store import append_jsonl
from backend.agent.sub_agents.base import SubAgent
from backend.agent.tools.core_set import CORE_TOOL_NAMES


TOOL_SELECTOR_SYSTEM_PROMPT_TEMPLATE = """\
你是 iGraph 的工具选择器。根据用户请求、当前图状态和工具目录，返回最可能需要的工具。
{output_contract}
"""

TOOL_SELECTOR_SYSTEM_PROMPT = TOOL_SELECTOR_SYSTEM_PROMPT_TEMPLATE.format(
    output_contract="只输出严格 JSON。",
)

TOOL_SELECTION_USER_PROMPT_TEMPLATE = """\
返回最可能需要的 {top_k} 个工具或更少。

用户请求: {user_input}
图状态: {graph_state}
意图: {intent}
最近对话: {conversation_recent_tail}

可用工具目录:
{catalog_lines}

核心工具集已默认加载，不需要选择: {core_names}

请只输出 JSON: {{"selected":["tool_name"],"reasoning":"一句话说明"}}
"""


def _tokenize(text: str) -> set[str]:
    s = str(text or "").lower()
    # 英文 / 数字 / 下划线：提取 2+ 字符的完整词
    tokens = set(re.findall(r"[a-z0-9_]{2,}", s))
    # 中文：提取所有 2-gram（滑动窗口），使子串匹配成为可能
    chars = re.findall(r"[\u4e00-\u9fff]", s)
    for i in range(len(chars) - 1):
        tokens.add(chars[i] + chars[i + 1])
    return tokens


@dataclass(frozen=True)
class ToolBrief:
    name: str
    description: str = ""


@dataclass
class ToolSelectionRequest:
    user_input: str
    graph_state_summary: str = ""
    detected_intent: str | None = None
    conversation_recent_tail: str = ""
    tool_catalog: list[ToolBrief] = field(default_factory=list)


@dataclass
class ToolSelectionResult:
    selected: list[str]
    reasoning: str = ""
    fallback_used: bool = False
    latency_ms: int = 0


class LLMToolSelector(SubAgent):
    """LLM-backed tool selector with deterministic fallback and audit logging."""

    name = "tool_selector"
    system_prompt = TOOL_SELECTOR_SYSTEM_PROMPT
    default_max_tokens = 512
    default_temperature = 0.0

    def __init__(
        self,
        tool_specs: list[dict[str, Any]],
        *,
        invoke_llm: Callable[[str], Awaitable[str]] | None = None,
        provider_gateway: Any | None = None,
        audit_path: str = "",
        model_version: str = "",
        timeout_s: float | None = None,
    ) -> None:
        super().__init__(provider_gateway=provider_gateway, prompt_invoker=invoke_llm)
        self._tool_specs = list(tool_specs)
        self._tool_by_name = {
            str(spec.get("name")): spec
            for spec in self._tool_specs
            if str(spec.get("name") or "").strip()
        }
        self._audit_path = audit_path
        self._model_version = model_version
        self._timeout_s = timeout_s if timeout_s is not None else _DEFAULT_TIMEOUT_S
        self._cache: dict[str, ToolSelectionResult] = {}
        self._turn_id: str | None = None

    def set_turn_id(self, turn_id: str) -> None:
        """Mark the start of a new user turn; clears per-turn cache so a fresh LLM selection runs."""
        if turn_id != self._turn_id:
            self._cache.clear()
            self._turn_id = turn_id

    @property
    def catalog(self) -> list[ToolBrief]:
        return [
            ToolBrief(name=name, description=str(spec.get("description") or ""))
            for name, spec in self._tool_by_name.items()
        ]

    async def select(self, req: ToolSelectionRequest, k: int = 8) -> ToolSelectionResult:
        started = time.monotonic()
        cache_key = self._cache_key(req, k)
        if cache_key in self._cache:
            cached = self._cache[cache_key]
            return ToolSelectionResult(
                selected=list(cached.selected),
                reasoning=f"cache: {cached.reasoning}",
                fallback_used=cached.fallback_used,
                latency_ms=0,
            )

        result: ToolSelectionResult
        candidate_names = [brief.name for brief in req.tool_catalog]
        try:
            prompt = self._render_prompt(req, k)
            raw = await asyncio.wait_for(self.invoke_text(prompt), timeout=self._timeout_s)
            result = self._parse(raw, candidate_names=candidate_names, k=k)
        except Exception as exc:
            keyword_picks = self._keyword_select(req, k=k)
            result = ToolSelectionResult(
                selected=keyword_picks,
                reasoning=f"keyword_fallback: {type(exc).__name__}: {exc}",
                fallback_used=True,
            )

        result.selected = self._merge_core_set(result.selected, candidate_names)
        result.latency_ms = int((time.monotonic() - started) * 1000)
        self._cache[cache_key] = ToolSelectionResult(
            selected=list(result.selected),
            reasoning=result.reasoning,
            fallback_used=result.fallback_used,
            latency_ms=result.latency_ms,
        )
        self._audit(req, result, candidate_names)
        return result

    def select_sync(self, query: str, k: int = 5) -> list[dict[str, Any]]:
        req = ToolSelectionRequest(user_input=query, tool_catalog=self.catalog)
        names = self._merge_core_set(self._keyword_select(req, k=k), [brief.name for brief in req.tool_catalog])
        return [self._tool_by_name[name] for name in names if name in self._tool_by_name]

    def _cache_key(self, req: ToolSelectionRequest, k: int) -> str:
        payload = {
            "user_input": " ".join(str(req.user_input or "").lower().split()),
            "graph_state": req.graph_state_summary,
            "intent": req.detected_intent or "",
            "tools": [brief.name for brief in req.tool_catalog],
            "k": k,
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _render_prompt(req: ToolSelectionRequest, k: int) -> str:
        catalog_lines = "\n".join(
            f"- {brief.name}: {brief.description or '(no description)'}"
            for brief in req.tool_catalog
            if brief.name not in CORE_TOOL_NAMES
        )
        core_names = ", ".join(sorted(name for name in CORE_TOOL_NAMES if name))
        return TOOL_SELECTION_USER_PROMPT_TEMPLATE.format(
            top_k=k,
            user_input=req.user_input,
            graph_state=req.graph_state_summary or "unknown",
            intent=req.detected_intent or "unknown",
            conversation_recent_tail=req.conversation_recent_tail or "(empty)",
            catalog_lines=catalog_lines,
            core_names=core_names,
        )

    @staticmethod
    def _parse(raw: str, *, candidate_names: list[str], k: int) -> ToolSelectionResult:
        text = str(raw or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        data = json.loads(match.group(0) if match else text)
        if not isinstance(data, dict):
            raise ValueError("selector_json_not_object")
        valid = set(candidate_names)
        selected: list[str] = []
        for name in data.get("selected", []):
            if isinstance(name, str) and name in valid and name not in selected:
                selected.append(name)
            if len(selected) >= k:
                break
        return ToolSelectionResult(selected=selected, reasoning=str(data.get("reasoning") or ""))

    def _keyword_select(self, req: ToolSelectionRequest, *, k: int) -> list[str]:
        query_terms = _tokenize(req.user_input)
        if not query_terms:
            return [brief.name for brief in req.tool_catalog[:k] if brief.name not in CORE_TOOL_NAMES]
        scored: list[tuple[float, str]] = []
        for brief in req.tool_catalog:
            if brief.name in CORE_TOOL_NAMES:
                continue
            text_terms = _tokenize(f"{brief.name} {brief.description}")
            overlap = len(query_terms & text_terms)
            if overlap:
                scored.append((float(overlap), brief.name))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        if not scored:
            return [brief.name for brief in req.tool_catalog[:k] if brief.name not in CORE_TOOL_NAMES]
        return [name for _, name in scored[:k]]

    @staticmethod
    def _merge_core_set(selected: list[str], candidate_names: list[str]) -> list[str]:
        valid = set(candidate_names)
        merged: list[str] = []
        for name in sorted(CORE_TOOL_NAMES):
            if name in valid and name not in merged:
                merged.append(name)
        for name in selected:
            if name in valid and name not in merged:
                merged.append(name)
        return merged

    def _audit(
        self,
        req: ToolSelectionRequest,
        result: ToolSelectionResult,
        candidate_names: list[str],
    ) -> None:
        if not self._audit_path:
            return
        digest = hashlib.sha256(str(req.user_input or "").encode("utf-8")).hexdigest()[:16]
        append_jsonl(
            self._audit_path,
            {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "user_input_digest": digest,
                "graph_state": req.graph_state_summary,
                "intent": req.detected_intent,
                "candidate_tools": candidate_names,
                "selected_tools": result.selected,
                "reasoning": result.reasoning,
                "latency_ms": result.latency_ms,
                "model_version": self._model_version,
                "fallback": result.fallback_used,
            },
        )
