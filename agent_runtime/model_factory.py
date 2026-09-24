"""Construct the native Agno adapter selected by the saved connection."""

from __future__ import annotations

import os

from agno.models.openai.like import OpenAILike
from agno.models.base import Model
from model_protocols import protocol_for, sdk_endpoint
from agent_runtime.cancellation_compat import CancellableOpenAILike
from agent_runtime.tool_dag import DAGModelMixin
from agent_runtime.model_history import ModelHistoryMixin
from agent_runtime.openai_stream_compat import RelayToolCallsMixin
from execution_protocol import controls_enabled

from agent_runtime.config import AgentRuntimeConfig
from agent_runtime.model_registry import SUPPORTED_MODELS, normalize_model_name, provider_for_model
import re

CUSTOM_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")


class DAGOpenAILike(ModelHistoryMixin, RelayToolCallsMixin, DAGModelMixin, OpenAILike):
    pass


class CancellableDAGOpenAILike(ModelHistoryMixin, RelayToolCallsMixin, DAGModelMixin, CancellableOpenAILike):
    pass


def _first_env(*names: str, fallback: str = "") -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return fallback.strip()


def _endpoint(model_id: str, config: AgentRuntimeConfig, *, allow_missing: bool = False) -> tuple[str, str]:
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
    if not allow_missing and (not key or not base):
        raise RuntimeError(f"模型 {model_id} 缺少 API Key 或 OpenAI-compatible Base URL")
    return key, base


def create_model(model_id: str, config: AgentRuntimeConfig, custom_model: dict | None = None) -> Model:
    custom = custom_model if isinstance(custom_model, dict) else None
    if custom:
        normalized = str(custom.get("model_id") or model_id).strip()
        base_url = str(custom.get("base_url") or "").strip().rstrip("/")
        api_key = str(custom.get("api_key") or "").strip()
        if not CUSTOM_MODEL_ID_RE.fullmatch(normalized):
            raise ValueError("自定义模型 ID 格式无效")
        if not base_url.startswith(("http://", "https://")) or (not api_key and not custom.get('allow_anonymous')):
            raise ValueError("自定义模型必须配置有效的 Base URL 和 API Key")
    else:
        normalized = normalize_model_name(model_id, default=config.default_model)
        api_key, base_url = _endpoint(normalized, config)
    protocol = protocol_for(custom or {})
    options = dict(id=str(custom.get('upstream_model_id') or normalized) if custom else normalized,
                   api_key=api_key or 'local-no-auth')
    limit = int(custom['max_output_tokens']) if custom and 'max_output_tokens' in custom else None
    if protocol == 'openai-compatible':
        constructor = CancellableDAGOpenAILike if controls_enabled() else DAGOpenAILike
        model = constructor(**options, base_url=base_url, **({'max_tokens': limit} if limit else {}))
    else:
        from agent_runtime.native_models import NativeClaude, NativeGemini, NativeResponses
        root, version = sdk_endpoint(base_url, protocol)
        if protocol == 'anthropic':
            model = NativeClaude(**options, client_params={'base_url': root}, max_tokens=limit or 4096)
        elif protocol == 'gemini':
            model = NativeGemini(**options, vertexai=False, max_output_tokens=limit,
                client_params={'vertexai': False, 'http_options': {'base_url': root, 'api_version': version}})
        else:
            model = NativeResponses(**options, base_url=root, max_output_tokens=limit, store=False)
    import hashlib
    model.igraph_protocol = protocol
    # Opaque provenance prevents forwarding another connection's signed state.
    model.igraph_history_key = hashlib.sha256((protocol + base_url + options['id'] + api_key).encode()).hexdigest()
    if custom and 'context_window' in custom:
        model.igraph_capabilities = custom
    return model


def allowed_model(model_id: str, claims: dict, config: AgentRuntimeConfig, custom_model: dict | None = None) -> str:
    """Validate the requested model against verified Backend claims when present."""

    if custom_model:
        normalized = str(custom_model.get("model_id") or model_id).strip()
        if normalized != str(model_id or "").strip() or not CUSTOM_MODEL_ID_RE.fullmatch(normalized):
            raise ValueError("自定义模型配置与模型 ID 不匹配")
        return normalized
    normalized = normalize_model_name(model_id, default=config.default_model)
    claimed = claims.get("allowed_models") if isinstance(claims, dict) else None
    if claimed is not None:
        allowed = {str(item) for item in claimed if str(item) in SUPPORTED_MODELS}
        if normalized not in allowed:
            raise PermissionError(f"Model {normalized} is not allowed for this user")
    return normalized
