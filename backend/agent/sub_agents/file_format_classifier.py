from __future__ import annotations

import json
import logging
import re
from typing import Any, Awaitable, Callable

from backend.agent.sub_agents.base import SubAgent

_log = logging.getLogger(__name__)


def _is_retryable(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    if any(k in name for k in ("connection", "ratelimit", "overloaded", "serviceunavailable")):
        return True
    status = getattr(exc, "status_code", None)
    if status is not None:
        return int(status) >= 500 or int(status) == 429
    return True

_SYSTEM_PROMPT = """\
你是一个图数据文件格式检测专家。给你文件名和文件内容样本，判断它属于哪种格式。

格式定义：
- edge_list：每行 2-3 个字段，格式为 "节点A 节点B" 或 "节点A 节点B 数值权重"，\
分隔符为空格/制表符/逗号，可有 # 注释行或空行
- triple：每行 3-4 个字段，格式为 "主体 关系 客体" 或 "主体 关系 客体 置信度"，\
关系字段是文字标签（非数值），分隔符为空格/制表符/逗号
- freetext：自然语言段落、混合文本、或字段数量不均匀无法直接解析为边的内容

判断规则：
- 绝大多数数据行有且仅有 2-3 个字段 → edge_list 或 triple
  - 若有第三字段且是浮点/整数 → edge_list
  - 若有第三字段且是文字标签 → triple
- 有自然语言长句、段落换行、或字段数不均 → freetext
- 含少量结构行但夹杂自然语言段落 → freetext

只输出 JSON，不要输出其他内容：
{"format":"edge_list"|"triple"|"freetext","separator":"space"|"tab"|"comma"|null,"reason":"一句话判断依据","extraction_goal":"（仅 freetext 时填写）面向 LLM 的抽取引导，说明应抽取哪类实体和关系，其余格式回复「无」"}
"""


def _sample_for_precheck(text: str, *, max_lines: int = 100, max_chars: int = 3000) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            lines.append(stripped)
        if len(lines) >= max_lines:
            break
    return "\n".join(lines)[:max_chars]


class FileFormatPreCheckSubAgent(SubAgent):
    name = "file_format_pre_check"
    system_prompt = _SYSTEM_PROMPT
    default_max_tokens = 128
    default_temperature = 0.0

    def __init__(
        self,
        llm_factory: Callable[[str], Awaitable[str]] | None = None,
        *,
        provider_gateway: Any | None = None,
    ) -> None:
        super().__init__(provider_gateway=provider_gateway, prompt_invoker=llm_factory)

    def available(self) -> bool:
        return self._provider_gateway is not None or self._prompt_invoker is not None

    @staticmethod
    def _parse_response(raw: str) -> dict[str, Any]:
        if "```" in raw:
            m = re.search(r"```(?:json)?\s*(.*?)\s*```", raw, re.DOTALL)
            raw = m.group(1).strip() if m else raw
        m = re.search(r"\{[^{}]*\}", raw, re.DOTALL)
        raw = m.group(0) if m else raw
        parsed = json.loads(raw)
        fmt = str(parsed.get("format") or "freetext")
        if fmt not in {"edge_list", "triple", "freetext"}:
            fmt = "freetext"
        sep_raw = str(parsed.get("separator") or "").strip()
        return {
            "format": fmt,
            "separator": sep_raw if sep_raw and sep_raw != "null" else None,
            "reason": str(parsed.get("reason") or ""),
            "extraction_goal": str(parsed.get("extraction_goal") or "") if fmt == "freetext" else "",
            "source": "llm",
        }

    async def classify(self, text: str, *, filename: str = "", max_retries: int = 3) -> dict[str, Any]:
        sample = _sample_for_precheck(text)
        fname = f"文件名: {filename}\n\n" if filename else ""
        prompt = f"{fname}文件内容样本:\n<<<\n{sample}\n>>>"
        last_exc: Exception | None = None
        for attempt in range(1, max_retries + 1):
            try:
                response = await self.invoke_text(prompt)
                return self._parse_response(response.strip())
            except json.JSONDecodeError as exc:
                # temperature=0 下输出确定性，JSON 格式错误重试无意义，直接 fallback
                last_exc = exc
                break
            except Exception as exc:
                if _is_retryable(exc) and attempt < max_retries:
                    _log.warning("FileFormatPreCheck transient error for %r (attempt %d/%d): %s: %s", filename, attempt, max_retries, type(exc).__name__, exc)
                    continue
                _log.warning("FileFormatPreCheck failed for %r: %s: %s", filename, type(exc).__name__, exc)
                return {"format": "freetext", "separator": None, "reason": f"pre_check_failed: {type(exc).__name__}: {exc}", "source": "fallback"}
        return {"format": "freetext", "separator": None, "reason": f"pre_check_failed: JSONDecodeError: {last_exc}", "source": "fallback"}
