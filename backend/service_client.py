"""HTTP clients for Agent Runtime and Graph Engine."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx
import anyio
from fastapi import HTTPException, Request


def forwarded_headers(request: Request, user_id: str) -> dict[str, str]:
    headers = {
        "X-User-ID": user_id,
        "X-Request-ID": request.headers.get("X-Request-ID", ""),
    }
    authorization = request.headers.get("Authorization", "").strip()
    if authorization:
        headers["Authorization"] = authorization
    last_event_id = request.headers.get("Last-Event-ID", "").strip()
    if last_event_id:
        headers["Last-Event-ID"] = last_event_id
    return {key: value for key, value in headers.items() if value}


class ServiceClient:
    def __init__(self, base_url: str, timeout: float, stream_timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.stream_timeout = stream_timeout

    async def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        data: Any = None,
        files: Any = None,
    ) -> Any:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout, trust_env=False) as client:
            response = await client.request(
                method,
                path,
                headers=headers,
                params=params,
                json=json_body,
                data=data,
                files=files,
            )
        if response.status_code >= 400:
            try:
                detail = response.json()
            except ValueError:
                detail = response.text
            raise HTTPException(status_code=response.status_code, detail=detail.get("detail", detail) if isinstance(detail, dict) else detail,
                                headers={"Retry-After": response.headers["Retry-After"]} if "Retry-After" in response.headers else None)
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

    @asynccontextmanager
    async def stream(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        data: Any = None,
    ) -> AsyncIterator[httpx.Response]:
        client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(self.stream_timeout, connect=self.timeout),
            trust_env=False,
        )
        try:
            async with client.stream(method, path, headers=headers, data=data) as response:
                if response.status_code >= 400:
                    body = await response.aread()
                    try:
                        detail = response.json()
                    except ValueError:
                        detail = body.decode("utf-8", "replace")
                    raise HTTPException(status_code=response.status_code, detail=detail.get("detail", detail) if isinstance(detail, dict) else detail,
                                        headers={"Retry-After": response.headers["Retry-After"]} if "Retry-After" in response.headers else None)
                yield response
        finally:
            with anyio.CancelScope(shield=True):
                await client.aclose()
