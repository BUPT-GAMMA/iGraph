"""Personal Playground credentials kept in account preferences and resolved only for the owning user."""
import re
from functools import cache

from fastapi.responses import JSONResponse
from fastapi import Request
from pydantic import BaseModel, Field, SecretStr
from anyio import to_thread

from agent_runtime.execution_store import ControlError
from agent_runtime.provider_catalog import validate_base_url
from agent_runtime.remote_graph.secrets import SecretBox

DEFAULT_URL = "https://tokensave.pro/v1"
DEFAULT_MODEL = "deepseek-v4-flash-0731-ga"
PROJECT_MODEL = "playground:personal"


class ConnectionWrite(BaseModel):
    revision: int = Field(ge=0)
    base_url: str = Field(min_length=1, max_length=2048)
    model: str = Field(min_length=1, max_length=256)
    api_key: SecretStr | None = None


def sync_project_model(catalog, db, uid, saved):
    # Match the model catalog's normal analysis limits, including the graph tool schemas.
    data = dict(source="playground", provider="openai-compatible", provider_name="Playground",
                model_id=saved["model"], display_name=saved["model"], base_url=saved["base_url"],
                ciphertext=saved["ciphertext"], connection_revision=saved["revision"], requires_api_key=True,
                tools=True, vision=False, enabled=True, context_window=131072, max_output_tokens=4096)
    row = catalog._row(db, uid, PROJECT_MODEL)
    if not row or row["data"] != data:
        catalog._write(db, uid, PROJECT_MODEL, data, row)


def install_connection(router, catalog, identity):
    @cache
    def agent_tools():
        from agent_runtime.agno_factory import describe_agent_tools
        return describe_agent_tools(catalog.config)

    def connection(uid, *, update=None, revision=None, resolve=False, clear=False):
        with catalog.store.transaction(uid, "__models__") as db:
            preferences = catalog._preference(db, uid)
            saved = preferences.get("playground") or {}
            current = saved.get("revision", 0)
            if revision is not None and current != revision:
                raise ControlError("MODEL_CONFIG_CHANGED")
            if update or resolve:
                box = SecretBox(catalog.config.remote_graph_secret_key, catalog.config.remote_graph_secret_key_file)
            if update:
                url = validate_base_url(update.base_url)
                model = update.model.strip()
                if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,255}", model):
                    raise ControlError("MODEL_INVALID_CONFIG", 422)
                key = update.api_key.get_secret_value().strip() if update.api_key else ""
                if key and (len(key) > 8192 or any(c.isspace() for c in key)):
                    raise ControlError("MODEL_INVALID_CONFIG", 422)
                if not key and (not saved.get("ciphertext") or url != saved.get("base_url")):
                    raise ControlError("PLAYGROUND_KEY_REQUIRED", 422)
                saved = {"revision": current + 1, "base_url": url, "model": model,
                         "ciphertext": box.encrypt(key) if key else saved["ciphertext"]}
            if clear:
                saved = {"revision": current + 1}
            if update or clear:
                catalog._set_preference(db, uid, {**preferences, "playground": saved})
            if saved.get("ciphertext"):
                sync_project_model(catalog, db, uid, saved)
            elif clear:
                row = catalog._row(db, uid, PROJECT_MODEL)
                if row and not row["data"].get("deleted"):
                    catalog._remove_models(db, uid, [row])
            public = {"config_id": PROJECT_MODEL, "revision": saved.get("revision", 0), "base_url": saved.get("base_url", DEFAULT_URL),
                      "model": saved.get("model", DEFAULT_MODEL), "configured": bool(saved.get("ciphertext"))}
            if resolve:
                if not public["configured"]:
                    raise ControlError("PLAYGROUND_NOT_CONFIGURED", 409)
                return {**public, "api_key": box.decrypt(saved["ciphertext"])}
            return public

    @router.get("/internal/playground/connection")
    async def read(request: Request):
        uid = identity(request)
        return await to_thread.run_sync(lambda: connection(uid))

    @router.put("/internal/playground/connection")
    async def save(req: ConnectionWrite, request: Request):
        uid = identity(request)
        return await to_thread.run_sync(lambda: connection(uid, update=req, revision=req.revision))

    @router.delete("/internal/playground/connection")
    async def remove(revision: int, request: Request):
        uid = identity(request)
        return await to_thread.run_sync(lambda: connection(uid, clear=True, revision=revision))

    @router.get("/internal/playground/connection/resolve")
    async def resolve(revision: int, request: Request, include_tools: bool = False):
        uid = identity(request)
        result = await to_thread.run_sync(lambda: connection(uid, resolve=True, revision=revision))
        if include_tools:
            result["agent_tools"] = await to_thread.run_sync(agent_tools)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
