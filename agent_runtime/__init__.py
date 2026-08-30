"""iGraph Agent Runtime package.

The package root is intentionally lightweight so ``python -m agent_runtime``
starts the standalone AgentOS service without importing the deprecated
in-process runtime.  Legacy symbols remain available through lazy imports.
"""

from __future__ import annotations

from importlib import import_module

__all__ = [
    "LegacyIGraphRuntime",
    "RuntimeFactory",
    "RuntimeSession",
    "adapt_legacy_agent",
    "create_runtime",
    "extract_generated_skill_name",
    "stream_skill_generation_from_env",
]


_LEGACY_EXPORTS = {
    "LegacyIGraphRuntime": ("agent_runtime.legacy", "LegacyIGraphRuntime"),
    "RuntimeFactory": ("agent_runtime.contracts", "RuntimeFactory"),
    "RuntimeSession": ("agent_runtime.contracts", "RuntimeSession"),
    "adapt_legacy_agent": ("agent_runtime.factory", "adapt_legacy_agent"),
    "create_runtime": ("agent_runtime.factory", "create_runtime"),
    "extract_generated_skill_name": ("agent_runtime.services", "extract_generated_skill_name"),
    "stream_skill_generation_from_env": ("agent_runtime.services", "stream_skill_generation_from_env"),
}


def __getattr__(name: str):
    target = _LEGACY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, symbol = target
    value = getattr(import_module(module_name), symbol)
    globals()[name] = value
    return value
