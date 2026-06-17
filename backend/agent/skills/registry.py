from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from backend.agent.skills.guard import scan_skill_content
from backend.agent.skills.models import SkillCondition, SkillSpec
import backend.data_paths as _dp
from backend.agent.skills.paths import review_queue_dir, user_skills_dir

_LOG = logging.getLogger(__name__)


def _strip_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    content = text.strip()
    if not content.startswith("---"):
        return {}, content
    lines = content.splitlines()
    end = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end = idx
            break
    if end is None:
        return {}, content
    return _parse_frontmatter(lines[1:end]), "\n".join(lines[end + 1 :]).strip()


def _parse_frontmatter(lines: list[str]) -> dict[str, Any]:
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]
    for raw in lines:
        if not raw.strip() or raw.strip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1] if stack else root
        parsed = _parse_scalar(value)
        parent[key] = parsed
        if value == "":
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
    return root


def _parse_scalar(value: str) -> Any:
    if value == "":
        return ""
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [item.strip().strip("'\"") for item in inner.split(",") if item.strip()]
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    return value.strip("'\"")


def _list(value: Any) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(str(v).strip() for v in value if str(v).strip())
    if isinstance(value, str) and value.strip():
        return (value.strip(),)
    return ()


def _source_for_path(path: Path) -> str:
    resolved = path.resolve()
    # Global shared user skills
    try:
        resolved.relative_to(user_skills_dir().resolve())
        return "global"
    except ValueError:
        pass
    # Global review queue
    try:
        resolved.relative_to(review_queue_dir().resolve())
        return "review_queue"
    except ValueError:
        pass
    # Per-user paths: data/users/{uid}/skills/
    try:
        resolved.relative_to((_dp.data_root() / "users").resolve())
        if ".review_queue" in resolved.parts:
            return "review_queue"
        return "user"
    except ValueError:
        pass
    return "global"


def _fallback_spec(path: Path, body: str, metadata: dict[str, Any]) -> SkillSpec:
    name = str(metadata.get("name") or path.parent.name).strip()
    description = str(metadata.get("description") or _first_heading_or_line(body) or name).strip()
    domain = str(metadata.get("domain") or name.replace("-", "_")).strip()
    igraph = {}
    raw_meta = metadata.get("metadata")
    if isinstance(raw_meta, dict):
        raw_ga = raw_meta.get("igraph")
        if not isinstance(raw_ga, dict):
            raw_ga = raw_meta.get("graphagent")
        if isinstance(raw_ga, dict):
            igraph = raw_ga
    condition = SkillCondition(
        requires_tools=_list(igraph.get("requires_tools")),
        any_tools=_list(igraph.get("any_tools")),
        fallback_for_tools=_list(igraph.get("fallback_for_tools")),
        requires_graph_state=str(igraph.get("requires_graph_state") or "none"),
        activates_on_intents=_list(igraph.get("activates_on_intents")),
        supports_protocols=_list(metadata.get("supports_protocols")) or ("text", "native"),
        tags=_list(igraph.get("tags")),
        related_skills=_list(igraph.get("related_skills")),
    )
    # config: [key1, key2]  or  config: [{key: key1}, {key: key2}]
    config_raw = igraph.get("config") or []
    config_keys: tuple[str, ...] = ()
    if isinstance(config_raw, list):
        extracted: list[str] = []
        for item in config_raw:
            if isinstance(item, str) and item.strip():
                extracted.append(item.strip())
            elif isinstance(item, dict):
                k = str(item.get("key") or "").strip()
                if k:
                    extracted.append(k)
        config_keys = tuple(extracted)
    return SkillSpec(
        name=name,
        description=description,
        domain=domain,
        path=str(path),
        body=body,
        version=str(metadata.get("version") or "1.0.0"),
        risk_level=str(metadata.get("risk_level") or "medium"),
        source=_source_for_path(path),
        condition=condition,
        config_keys=config_keys,
        metadata=metadata,
    )


def _first_heading_or_line(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
        if stripped:
            return stripped
    return ""


class SkillRegistry:
    def __init__(self, skill_dirs: list[Path] | None = None, *, include_review_queue: bool = False) -> None:
        dirs = skill_dirs
        if dirs is None:
            dirs = [user_skills_dir()]
            if include_review_queue:
                dirs.append(review_queue_dir())
        self._skill_dirs = dirs

    def discover(self) -> list[SkillSpec]:
        specs: list[SkillSpec] = []
        seen: set[str] = set()
        for root in self._skill_dirs:
            if not root.exists():
                continue
            for path in sorted(root.glob("*/SKILL.md")):
                try:
                    spec = _load_skill(str(path))
                    if spec.name in seen:
                        _LOG.warning("duplicate skill name %s at %s; skipping", spec.name, path)
                        continue
                    seen.add(spec.name)
                    specs.append(spec)
                except Exception as exc:
                    _LOG.warning("failed to load skill %s: %s", path, exc)
        return specs

    def get(self, name: str) -> SkillSpec | None:
        target = str(name or "").strip()
        if not target:
            return None
        for spec in self.discover():
            if spec.name == target or Path(spec.path).parent.name == target:
                return spec
        return None

    def high_risk_tool_names(self) -> set[str]:
        """Tools required by skills whose frontmatter says ``risk_level: high``.

        Lets skill authors flag additional tools as needing user
        confirmation / serial execution without editing Agent code. We
        only propagate risk through ``requires_tools`` (the skill's
        non-optional tools); ``any_tools`` is treated as "may use" and
        is too broad to imply a tool is itself high-risk.
        """
        names: set[str] = set()
        for spec in self.discover():
            if str(spec.risk_level or "").strip().lower() != "high":
                continue
            for tool in spec.condition.requires_tools:
                tool_name = str(tool or "").strip()
                if tool_name:
                    names.add(tool_name)
        return names


@lru_cache(maxsize=128)
def _load_skill(path: str) -> SkillSpec:
    skill_path = Path(path)
    raw = skill_path.read_text(encoding="utf-8")
    metadata, body = _strip_frontmatter(raw)
    reason = scan_skill_content(body)
    if reason:
        raise ValueError(f"skill content rejected: {reason}")
    return _fallback_spec(skill_path, body, metadata)


def clear_skill_caches() -> None:
    _load_skill.cache_clear()
