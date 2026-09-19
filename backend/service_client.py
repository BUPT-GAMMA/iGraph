"""HTTP clients for Agent Runtime and Graph Engine."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx
import uuid
import anyio
from fastapi import HTTPException, Request
from execution_protocol import controls_enabled, execution_headers, service_headers


def forwarded_headers(request: Request, user_id: str) -> dict[str, str]:
    headers = {
        "X-User-ID": user_id,
        "X-Request-ID": request.headers.get("X-Request-ID", ""),
        **(service_headers(user_id) if controls_enabled() else {}),
        **execution_headers.get(),
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

    def _streaming_timeout(self) -> httpx.Timeout:
        """Keep response streams open while retaining bounded setup/write waits.

        Agent runs can legitimately spend more than 30 minutes inside one
        durable graph tool without emitting an upstream byte. A finite HTTP
        read timeout would tear down an otherwise healthy background run.
        The Backend sends downstream SSE heartbeats, so idle client/proxy
        connections are still observable and can be closed by their owner.
        """

        return httpx.Timeout(
            connect=self.timeout,
            read=None,
            write=self.stream_timeout,
            pool=self.timeout,
        )

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
        timeout: float | httpx.Timeout | None = None,
    ) -> Any:
        # A preparation contains multiple independent graph operations. Allocate
        # per HTTP operation, not per Run; keep caller-supplied retry keys intact.
        if headers.get("X-Run-ID") and not headers.get("X-Operation-ID"):
            headers = {**headers, "X-Operation-ID": str(uuid.uuid4())}
        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout if timeout is not None else self.timeout,
            trust_env=False,
        ) as client:
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

    async def open_stream(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        params: dict[str, Any] | None = None,
    ) -> tuple[httpx.AsyncClient, httpx.Response]:
        """Open a validated upstream stream; the caller owns both handles."""
        client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(self.stream_timeout, connect=self.timeout),
            trust_env=False,
        )
        try:
            request = client.build_request(method, path, headers=headers, params=params)
            response = await client.send(request, stream=True)
            if response.status_code >= 400:
                body = await response.aread()
                await response.aclose()
                await client.aclose()
                raise HTTPException(
                    status_code=response.status_code,
                    detail=body.decode("utf-8", "replace"),
                )
            return client, response
        except BaseException:
            if not client.is_closed:
                await client.aclose()
            raise

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
            timeout=self._streaming_timeout(),
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
