"""In-process Agent Runtime boundary for iGraph."""

from .contracts import RuntimeFactory, RuntimeSession
from .factory import adapt_legacy_agent, create_runtime
from .legacy import LegacyIGraphRuntime
from .services import extract_generated_skill_name, stream_skill_generation_from_env

__all__ = [
    "LegacyIGraphRuntime",
    "RuntimeFactory",
    "RuntimeSession",
    "adapt_legacy_agent",
    "create_runtime",
    "extract_generated_skill_name",
    "stream_skill_generation_from_env",
]
