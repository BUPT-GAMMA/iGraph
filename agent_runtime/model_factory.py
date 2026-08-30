"""Agno model construction for the OpenAI-compatible model gateway."""

from __future__ import annotations

import os

from agno.models.openai.like import OpenAILike

from agent_runtime.config import AgentRuntimeConfig
from agent_runtime.model_registry import SUPPORTED_MODELS, normalize_model_name, provider_for_model


def _first_env(*names: str, fallback: str = "") -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return fallback.strip()


def _endpoint(model_id: str, config: AgentRuntimeConfig) -> tuple[str, str]:
    provider = provider_for_model(model_id)
    if provider == "openai":
        key = _first_env("OPENAI_API_KEY", "LLM_API_KEY", fallback=config.llm_api_key)
        base = _first_env("OPENAI_BASE_URL", "LLM_BASE_URL", fallback=config.llm_base_url)
    elif provider == "anthropic":
        key = _first_env("CLAUDE_API_KEY", "ANTHROPIC_API_KEY", "LLM_API_KEY", fallback=config.llm_api_key)
        base = _first_env("CLAUDE_BASE_URL", "ANTHROPIC_BASE_URL", "LLM_BASE_URL", fallback=config.llm_base_url)
    elif provider == "deepseek":
        key = _first_env("DEEPSEEK_API_KEY", "LLM_API_KEY", fallback=config.llm_api_key)
        base = _first_env("DEEPSEEK_BASE_URL", "LLM_BASE_URL", fallback=config.llm_base_url)
    elif provider == "doubao":
        key = _first_env("ARK_API_KEY", "DOUBAO_API_KEY", "LLM_API_KEY", fallback=config.llm_api_key)
        base = _first_env("ARK_BASE_URL", "DOUBAO_BASE_URL", "LLM_BASE_URL", fallback=config.llm_base_url)
    else:
        key = _first_env("QWEN_API_KEY", "DASHSCOPE_API_KEY", "LLM_API_KEY", fallback=config.llm_api_key)
        base = _first_env("QWEN_BASE_URL", "DASHSCOPE_BASE_URL", "LLM_BASE_URL", fallback=config.llm_base_url)
    if not key or not base:
        raise RuntimeError(f"模型 {model_id} 缺少 API Key 或 OpenAI-compatible Base URL")
    return key, base


def create_model(model_id: str, config: AgentRuntimeConfig) -> OpenAILike:
    normalized = normalize_model_name(model_id, default=config.default_model)
    api_key, base_url = _endpoint(normalized, config)
    return OpenAILike(
        id=normalized,
        api_key=api_key,
        base_url=base_url,
    )


def allowed_model(model_id: str, claims: dict, config: AgentRuntimeConfig) -> str:
    """Validate the requested model against verified Backend claims when present."""

    normalized = normalize_model_name(model_id, default=config.default_model)
    claimed = claims.get("allowed_models") if isinstance(claims, dict) else None
    if claimed is not None:
        allowed = {str(item) for item in claimed if str(item) in SUPPORTED_MODELS}
        if normalized not in allowed:
            raise PermissionError(f"Model {normalized} is not allowed for this user")
    return normalized
