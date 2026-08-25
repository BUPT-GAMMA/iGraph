from __future__ import annotations

import re
from typing import Iterable


INJECTION_PATTERNS: tuple[str, ...] = (
    r"ignore (all )?previous instructions?",
    r"you are now",
    r"disregard (all )?previous",
    r"system prompt",
    r"<\|.*?\|>",
    r"[\u200b-\u200f\ufeff]",
    r"(curl|wget).*(ANTHROPIC|API_KEY|\.env)",
    r"cat\s+.*\.env",
)


def scan_memory_content(values: Iterable[object]) -> str:
    text = "\n".join(str(value or "") for value in values)
    if not text.strip():
        return ""
    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL):
            return f"injection_pattern:{pattern}"
    return ""
