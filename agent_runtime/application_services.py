"""Explicit public facade for Runtime-owned memory and skill services.

The API gateway imports this module instead of reaching into the concrete
``agent_runtime.agent`` implementation tree.  Names and call semantics remain
those of the original services.
"""

from .agent.context.token_counter import context_usage
from .agent.memory.builtin_provider import BuiltinMemoryProvider
from .agent.memory.file_provider import FileMemoryProvider
from .agent.memory.manager import MemoryManager
from .agent.memory.memory_engine import MemoryEngine
from .agent.memory.service import MemoryService
from .agent.memory.store import append_jsonl
from .agent.model_registry import DEFAULT_MODEL, SUPPORTED_MODELS, normalize_model_name
from .agent.skills.guard import scan_skill_content
from .agent.skills.manager import SkillManager
from .agent.skills.paths import (
    bundled_skills_dir,
    per_user_review_queue_dir,
    per_user_skill_events_path,
    per_user_skills_dir,
    skill_events_path,
    user_skills_dir,
)
from .agent.skills.playbook_reviewer import render_candidate_skill
from .agent.skills.registry import SkillRegistry, clear_skill_caches

__all__ = [
    "BuiltinMemoryProvider",
    "DEFAULT_MODEL",
    "FileMemoryProvider",
    "MemoryEngine",
    "MemoryManager",
    "MemoryService",
    "SUPPORTED_MODELS",
    "SkillManager",
    "SkillRegistry",
    "append_jsonl",
    "bundled_skills_dir",
    "clear_skill_caches",
    "context_usage",
    "normalize_model_name",
    "per_user_review_queue_dir",
    "per_user_skill_events_path",
    "per_user_skills_dir",
    "render_candidate_skill",
    "scan_skill_content",
    "skill_events_path",
    "user_skills_dir",
]
