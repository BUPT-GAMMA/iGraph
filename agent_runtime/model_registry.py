"""Agent Runtime-owned LLM model naming and provider registry."""

from __future__ import annotations

from typing import Optional


SUPPORTED_MODELS = {
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "gpt-5-4",
    "gpt-5-2",
    "qwen3.6-plus",
    "qwen3.7-max",
    "deepseek-v4-pro",
    "deepseek-v4-flash",
    "deepseek-v4-lite",
    "doubao-seed-2-0-pro-260215",
}

DEFAULT_MODEL = "claude-sonnet-4-6"


def normalize_model_name(model: Optional[str], *, default: Optional[str] = None) -> str:
    raw = str(model or default or "").strip()
    if raw not in SUPPORTED_MODELS:
        raise ValueError(f"不支持的模型: {raw}，可选: {sorted(SUPPORTED_MODELS)}")
    return raw


def provider_for_model(model: str) -> str:
    normalized = normalize_model_name(model)
    if normalized.startswith("gpt-"):
        return "openai"
    if normalized.startswith("claude-"):
        return "anthropic"
    if normalized.startswith("qwen"):
        return "qwen"
    if normalized.startswith("deepseek"):
        return "deepseek"
    if normalized.startswith("doubao"):
        return "doubao"
    raise ValueError(f"不支持的模型: {model}")


def default_model_for_provider(provider_hint: Optional[str] = None) -> str:
    if provider_hint == "anthropic":
        return "claude-sonnet-4-6"
    if provider_hint == "qwen":
        return "qwen3.6-plus"
    if provider_hint == "deepseek":
        return "deepseek-v4-pro"
    if provider_hint == "doubao":
        return "doubao-seed-2-0-pro-260215"
    return DEFAULT_MODEL
