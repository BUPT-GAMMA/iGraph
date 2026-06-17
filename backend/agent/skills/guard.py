from __future__ import annotations

import os
from pathlib import Path

from backend.agent.memory.safety import scan_memory_content


def scan_skill_content(text: str) -> str:
    return scan_memory_content([text])


def validate_supporting_file(skill_dir: Path, file_path: str) -> Path:
    root = skill_dir.resolve()
    target = (skill_dir / file_path).resolve()
    if os.path.commonpath([str(root), str(target)]) != str(root):
        raise ValueError("supporting file path escapes skill directory")
    if target.name == "SKILL.md":
        raise ValueError("use skill_view(name) for SKILL.md")
    if not target.is_file():
        raise FileNotFoundError(file_path)
    return target
