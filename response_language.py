"""Resolve and describe the response language for one user turn.

The resolver is deliberately deterministic and local: its result is captured before
attachments, tool observations, history, or UI locale can influence the agent run.
"""

from __future__ import annotations

import re


AUTO_LANGUAGE = "Auto"

_LANGUAGE_ALIASES: dict[str, tuple[str, ...]] = {
    "English": ("english", "英语", "英文"),
    "Chinese": ("chinese", "mandarin", "中文", "汉语", "普通话"),
    "Spanish": ("spanish", "español", "西班牙语", "西语"),
    "French": ("french", "français", "法语"),
    "German": ("german", "deutsch", "德语"),
    "Japanese": ("japanese", "日本語", "日语"),
    "Korean": ("korean", "한국어", "韩语", "朝鲜语"),
    "Portuguese": ("portuguese", "português", "葡萄牙语"),
    "Italian": ("italian", "italiano", "意大利语"),
    "Russian": ("russian", "русский", "俄语"),
    "Arabic": ("arabic", "العربية", "阿拉伯语"),
}

_ALIAS_TO_LANGUAGE = {
    alias.casefold(): language
    for language, aliases in _LANGUAGE_ALIASES.items()
    for alias in aliases
}
_ALIASES_PATTERN = "|".join(
    sorted((re.escape(alias) for alias in _ALIAS_TO_LANGUAGE), key=len, reverse=True)
)
_EXPLICIT_PATTERNS = (
    re.compile(
        rf"\b(?:answer|respond|reply|write|explain|speak)\b[^\n.!?]{{0,40}}?"
        rf"\b(?:in|using)\s+(?P<language>{_ALIASES_PATTERN})\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\buse\s+(?P<language>{_ALIASES_PATTERN})\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?:请|麻烦)?(?:用|使用|以)(?P<language>{_ALIASES_PATTERN})(?:来|进行)?"
        rf"(?:回答|回复|说明|解释|输出|写|作答)",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?:回答|回复|说明|解释|输出|写|作答)[^。！？\n]{{0,16}}?"
        rf"(?:请)?(?:用|使用|以)(?P<language>{_ALIASES_PATTERN})",
        re.IGNORECASE,
    ),
)

_LATIN_HINTS: dict[str, set[str]] = {
    "English": {
        "a", "an", "and", "analyse", "analyze", "are", "can", "could",
        "explain", "graph", "help", "how", "is", "it", "network", "of",
        "please", "show", "summarize", "summary", "tell", "the", "this", "to",
        "what", "would",
    },
    "Spanish": {
        "analiza", "analizar", "ayuda", "cómo", "el", "esta", "este", "grafo",
        "la", "los", "por", "puedes", "qué", "red", "un", "una", "favor",
    },
    "French": {
        "analyse", "analyser", "avec", "ce", "cette", "comment", "graphe", "la",
        "le", "les", "peux", "pouvez", "réseau", "s'il", "une", "vous", "svp",
    },
    "German": {
        "analysiere", "analysieren", "bitte", "das", "den", "der", "dieses",
        "ein", "eine", "graph", "kannst", "netzwerk", "und", "wie",
    },
    "Portuguese": {
        "analise", "analisar", "como", "este", "esta", "grafo", "o", "por",
        "pode", "por favor", "que", "rede", "uma", "você",
    },
    "Italian": {
        "analizza", "analizzare", "come", "grafo", "il", "la", "per", "puoi",
        "questa", "questo", "rete", "un", "una", "favore",
    },
}

_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def _explicit_language(text: str) -> str | None:
    matches: list[tuple[int, str]] = []
    for pattern in _EXPLICIT_PATTERNS:
        for match in pattern.finditer(text):
            language = _ALIAS_TO_LANGUAGE.get(match.group("language").casefold())
            if language:
                matches.append((match.start(), language))
    return max(matches, default=(-1, ""))[1] or None


def _count_range(text: str, start: int, end: int) -> int:
    return sum(start <= ord(char) <= end for char in text)


def _infer_natural_language(natural_text: str) -> str:
    if _count_range(natural_text, 0x3040, 0x30FF):
        return "Japanese"
    if _count_range(natural_text, 0xAC00, 0xD7AF):
        return "Korean"
    if _count_range(natural_text, 0x0600, 0x06FF):
        return "Arabic"

    han_count = sum(
        _count_range(natural_text, start, end)
        for start, end in ((0x3400, 0x4DBF), (0x4E00, 0x9FFF))
    )
    latin_count = sum(char.isascii() and char.isalpha() for char in natural_text)
    if han_count >= 2 and (latin_count == 0 or han_count * 4 >= latin_count):
        return "Chinese"

    tokens = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ]+(?:'[A-Za-zÀ-ÖØ-öø-ÿ]+)?", natural_text.casefold())
    if tokens:
        scores = {
            language: sum(token in hints for token in tokens)
            for language, hints in _LATIN_HINTS.items()
        }
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        best_language, best_score = ranked[0]
        second_score = ranked[1][1]
        if best_score >= 2 and best_score > second_score:
            return best_language

    return AUTO_LANGUAGE


def resolve_response_language(user_input: str) -> str:
    """Return a stable language hint for the current user-authored turn."""

    text = str(user_input or "").strip()
    if not text:
        return AUTO_LANGUAGE

    explicit = _explicit_language(text)
    if explicit:
        return explicit

    natural_text = _URL_RE.sub(" ", _FENCED_CODE_RE.sub(" ", text))
    # The leading request is a stronger signal than pasted source material. This
    # keeps an English instruction followed by a Chinese document in English.
    lead = next((line.strip() for line in natural_text.splitlines() if line.strip()), "")[:240]
    lead_language = _infer_natural_language(lead)
    if lead_language != AUTO_LANGUAGE:
        return lead_language
    return _infer_natural_language(natural_text)


def build_turn_language_instruction(response_language: str) -> str:
    """Build the system-level language lock injected throughout one agent run."""

    allowed = set(_LANGUAGE_ALIASES) | {AUTO_LANGUAGE}
    target = response_language if response_language in allowed else AUTO_LANGUAGE
    target_rule = (
        f"Use {target} for this turn."
        if target != AUTO_LANGUAGE
        else "Infer the dominant natural language of the original user-authored request once, then use it for this turn."
    )
    return "\n".join(
        (
            "## Per-turn response language (locked)",
            target_rule,
            "An explicit response-language request in the original user message overrides the automatic hint.",
            "Keep the selected language unchanged for the entire run, including every reasoning and tool-call cycle.",
            "Use it for all user-visible prose: progress notes, visible reasoning, clarification or error messages, report titles and narrative, and the final answer.",
            "Do not change language because of UI locale, system-prompt language, conversation history, memory, attachments, or tool results.",
            "Code, identifiers, quoted source text, citations, proper names, and tool schemas or arguments may remain in their original or required language.",
        )
    )
