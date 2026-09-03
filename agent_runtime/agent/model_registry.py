"""Compatibility re-export for the pre-AgentOS runtime."""

from agent_runtime.model_registry import (
    DEFAULT_MODEL,
    SUPPORTED_MODELS,
    default_model_for_provider,
    normalize_model_name,
    provider_for_model,
)

__all__ = [
    "DEFAULT_MODEL",
    "SUPPORTED_MODELS",
    "default_model_for_provider",
    "normalize_model_name",
    "provider_for_model",
]
