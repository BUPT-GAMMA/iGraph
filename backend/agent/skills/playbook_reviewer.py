from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from backend.agent.skills.manager import SkillManager
from backend.agent.skills.paths import review_queue_dir


def candidate_name_for_playbook(item: dict[str, Any]) -> str:
    base = str(item.get("problem_type") or "playbook_skill").strip().lower()
    base = re.sub(r"[^a-z0-9_\-\u4e00-\u9fff]+", "_", base)
    # Skill names are restricted to ASCII identifiers. Use a stable playbook suffix
    # when the problem type is non-ASCII or otherwise unsuitable.
    ascii_base = re.sub(r"[^a-z0-9_\-]+", "", base)
    if not ascii_base or not ascii_base[0].isalpha():
        ascii_base = "playbook"
    suffix = str(item.get("playbook_id") or "")[-8:] or "generated"
    return f"{ascii_base}_{suffix}".replace("-", "_")[:80]


def render_candidate_skill(item: dict[str, Any], *, name: str) -> tuple[str, dict[str, Any]]:
    problem_type = str(item.get("problem_type") or "Reusable Playbook").strip()
    approach = str(item.get("approach") or "").strip()
    tools = [str(v) for v in item.get("tool_sequence", []) if str(v).strip()]
    triggers = [str(v) for v in item.get("trigger_terms", []) if str(v).strip()]
    risk_level = "high" if set(tools) & {"train", "infer"} else "medium"
    body = (
        f"# {problem_type}\n\n"
        "## When to Use\n"
        f"Use this skill for requests matching: {', '.join(triggers) or problem_type}.\n\n"
        "## Procedure\n"
        f"{approach}\n\n"
        "## Tool Sequence Patterns\n"
        f"{' -> '.join(tools) if tools else 'No fixed tool sequence captured.'}\n\n"
        "## Verification\n"
        "Confirm the result against the current graph state and explain any limits.\n\n"
        "## Provenance\n"
        f"- Generated from playbook: {item.get('playbook_id')}\n"
        f"- Evidence sessions: {len(item.get('evidence_sessions', []) if isinstance(item.get('evidence_sessions'), list) else [])}\n"
    )
    metadata = {
        "description": f"从 playbook 晋升候选生成的流程：{problem_type}",
        "domain": "generated",
        "risk_level": risk_level,
        "version": "0.1.0",
    }
    return body, metadata


class PlaybookReviewer:
    def __init__(self, manager: SkillManager | None = None) -> None:
        self._manager = manager or SkillManager()

    def review_snapshot(self, playbook_snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        items = playbook_snapshot.get("items")
        if not isinstance(items, list):
            return []
        changes: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            lifecycle = str(item.get("lifecycle_status") or "").strip().lower()
            status = str(item.get("status") or "").strip().lower()
            if lifecycle != "validated" and not (not lifecycle and status == "validated"):
                continue
            if item.get("candidate_skill_name"):
                continue
            name = candidate_name_for_playbook(item)
            body, metadata = render_candidate_skill(item, name=name)
            result = self._manager.create(name=name, body=body, metadata=metadata, destination="review_queue")
            if result.get("ok") or (review_queue_dir() / name).exists():
                item["candidate_skill_name"] = name
                item["promotion_reason"] = "validated playbook generated review-queue skill"
                meta_path = Path(review_queue_dir()) / name / "metadata.json"
                meta_path.parent.mkdir(parents=True, exist_ok=True)
                meta_path.write_text(
                    json.dumps(
                        {
                            "source": "playbook_reviewer",
                            "playbook_id": item.get("playbook_id"),
                            "evidence_sessions": item.get("evidence_sessions", []),
                            "tool_sequence": item.get("tool_sequence", []),
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                changes.append({"playbook_id": item.get("playbook_id"), "candidate_skill_name": name})
        return changes
