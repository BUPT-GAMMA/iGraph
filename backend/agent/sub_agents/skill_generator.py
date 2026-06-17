from __future__ import annotations

import asyncio
from typing import Any, AsyncGenerator

from backend.agent.sub_agents.base import SubAgent


SKILL_MD_FORMAT = """\
---
name: {lowercase_underscored_name}
description: {concise one-line description in Chinese or English}
version: 1.0.0
domain: {graph_analysis|graph_ml|data_ingestion|reporting|rag|user|generated}
risk_level: {low|medium|high}
metadata:
  igraph:
    any_tools: [tool1, tool2]
    requires_graph_state: {none|loaded|weighted|directed}
    activates_on_intents: [intent_keyword1, intent_keyword2]
---

# {Human-Readable Title}

## When to Use
{2-4 sentences describing exact scenarios and trigger conditions.}

## Procedure
1. {Specific actionable step, name the exact tool or action}
2. {Next step}
...

## Tool Sequence Patterns
`tool_a` -> `tool_b` -> `tool_c`

## Verification
{How to confirm the result is correct and complete.}

## Notes
{Important caveats, edge cases, or prerequisites. Omit if none.}
"""

SKILL_GENERATOR_SYSTEM_PROMPT_TEMPLATE = """\
You are a technical writer for iGraph, a graph-analysis AI system.

Given a problem-solving playbook captured from user sessions, generate a complete, \
production-quality SKILL.md that the agent can use as a step-by-step procedure guide.

Output ONLY the raw SKILL.md content starting with ---. No preamble, no trailing text.

SKILL.md format:
{skill_md_format}

Rules:
- name field: lowercase letters/digits/underscores only, start with a letter, <= {max_name_chars} chars.
- Choose a concise, descriptive name that reflects the problem type (e.g. community_detection, path_query_analysis).
- Infer domain, risk_level, required tools, and graph state from the approach and tool sequence.
- risk_level is "high" only if the skill involves training/inference ML models.
- Prefer Chinese for description and section content if the playbook is in Chinese.
"""

SKILL_GENERATOR_SYSTEM_PROMPT = SKILL_GENERATOR_SYSTEM_PROMPT_TEMPLATE.format(
    skill_md_format=SKILL_MD_FORMAT,
    max_name_chars=64,
)

SKILL_GENERATOR_USER_PROMPT_TEMPLATE = """\
Generate a SKILL.md for this playbook:

Problem type: {problem_type}
Approach / solution: {approach}
Tool sequence: {tool_sequence}
Trigger terms: {trigger_terms}
Evidence sessions: {evidence_sessions}
Confidence: {confidence:.2f}

Output the complete SKILL.md now:
"""

_FAKE_STREAM_CHUNK = 6
_FAKE_STREAM_DELAY = 0.008


def extract_name(content: str) -> str:
    """Extract the name field from generated SKILL.md frontmatter."""
    text = content.strip()
    if not text.startswith("---"):
        return ""
    lines = text.splitlines()
    for line in lines[1:]:
        stripped = line.strip()
        if stripped == "---":
            break
        if stripped.startswith("name:"):
            return stripped[5:].strip().strip("'\"")
    return ""


def _build_user_prompt(playbook: dict[str, Any]) -> str:
    problem_type = str(playbook.get("problem_type") or "Unknown").strip()
    approach = str(playbook.get("approach") or "").strip()
    tools = [str(t) for t in (playbook.get("tool_sequence") or []) if str(t).strip()]
    triggers = [str(t) for t in (playbook.get("trigger_terms") or []) if str(t).strip()]
    evidence = len(playbook.get("evidence_sessions") or [])
    confidence = float(playbook.get("confidence") or 0.8)
    return SKILL_GENERATOR_USER_PROMPT_TEMPLATE.format(
        problem_type=problem_type,
        approach=approach or "(not recorded)",
        tool_sequence=" -> ".join(tools) if tools else "(not recorded)",
        trigger_terms=", ".join(triggers) if triggers else "(none)",
        evidence_sessions=evidence,
        confidence=confidence,
    )


def _strip_preamble(text: str) -> str:
    """Remove any text before the first '---' frontmatter marker."""
    idx = text.find("---")
    if idx > 0:
        return text[idx:]
    return text


class SkillGenerator(SubAgent):
    """Stream SKILL.md tokens from an LLM given a playbook dict."""

    name = "skill_generator"
    system_prompt = SKILL_GENERATOR_SYSTEM_PROMPT
    default_max_tokens = 2048
    default_temperature = 0.3

    def __init__(self, *, model: str, api_key: str, base_url: str) -> None:
        super().__init__()
        self._model = model
        self._api_key = api_key
        self._base_url = base_url

    def _make_client(self):
        from openai import AsyncOpenAI
        return AsyncOpenAI(
            api_key=self._api_key,
            base_url=self._base_url,
            timeout=120,
        )

    async def _try_stream(self, messages: list[dict]) -> AsyncGenerator[str, None]:
        """Attempt a real streaming completion. Raises on any error."""
        client = self._make_client()
        response = await client.chat.completions.create(
            model=self._model,
            messages=messages,
            temperature=0.3,
            max_tokens=2048,
            stream=True,
        )
        preamble_stripped = False
        buf = ""
        async for chunk in response:
            delta = ""
            try:
                if chunk.choices and chunk.choices[0].delta:
                    delta = chunk.choices[0].delta.content or ""
            except Exception:
                pass
            if not delta:
                continue
            if not preamble_stripped:
                buf += delta
                idx = buf.find("---")
                if idx >= 0:
                    preamble_stripped = True
                    yield buf[idx:]
                    buf = ""
            else:
                yield delta

    async def _invoke_text(self, messages: list[dict]) -> str:
        """Non-streaming completion. More reliable across providers."""
        client = self._make_client()
        response = await client.chat.completions.create(
            model=self._model,
            messages=messages,
            temperature=0.3,
            max_tokens=2048,
            stream=False,
        )
        return response.choices[0].message.content or ""

    async def stream(self, playbook: dict[str, Any]) -> AsyncGenerator[str, None]:
        messages = self.build_messages(_build_user_prompt(playbook))

        try:
            async for token in self._try_stream(messages):
                yield token
            return
        except Exception:
            pass

        try:
            full = await self._invoke_text(messages)
        except Exception as exc:
            raise RuntimeError(f"LLM 调用失败: {exc}") from exc

        full = _strip_preamble(full)
        for i in range(0, len(full), _FAKE_STREAM_CHUNK):
            yield full[i : i + _FAKE_STREAM_CHUNK]
            await asyncio.sleep(_FAKE_STREAM_DELAY)

    @classmethod
    def from_env(cls, model: str | None = None) -> "SkillGenerator":
        from backend.agent.model_registry import DEFAULT_MODEL, normalize_model_name
        from backend.agent.igraph import IGraph

        resolved = normalize_model_name(model or DEFAULT_MODEL)
        _, api_key, base_url = IGraph._resolve_model_endpoint(resolved)
        return cls(model=resolved, api_key=api_key, base_url=base_url)
