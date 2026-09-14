"""Authenticated loopback broker for attempt-scoped virtual graph-ML calls.

Ground-truth labels never cross the process boundary.  Each broker instance is
created for exactly one benchmark attempt, owns a random bearer token, binds an
ephemeral loopback port, and invokes the same handlers used by the standalone
``graph_ml_tools.py`` CLI under a request-local ``ContextVar`` binding.
"""

from __future__ import annotations

import copy
import hmac
import ipaddress
import json
import secrets
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping

from .virtual_ml import (
    VIRTUAL_ML_BROKER_ENDPOINT_ENV,
    VIRTUAL_ML_BROKER_TOKEN_ENV,
    VIRTUAL_ML_BROKER_TOKEN_HEADER,
    VIRTUAL_ML_CONTEXT_ENV,
    bind_virtual_ml_context,
    build_virtual_ml_context,
)


DEFAULT_MAX_REQUEST_BYTES = 64 * 1024
SHUTDOWN_DRAIN_SECONDS = 5.0
MAX_ARGUMENT_COUNT = 128
MAX_ARGUMENT_BYTES = 32 * 1024
BROKER_PATH = "/invoke"

# This allowlist is deliberately duplicated at the trust boundary.  Importing
# ACTION_HANDLERS and treating all of its future entries as allowed would let an
# unrelated tool-source edit silently expand broker authority.
ALLOWED_ACTIONS = frozenset(
    {
        "structural_node_embedding_classification_train",
        "structural_node_embedding_classification_infer",
        "structural_node_embedding_link_prediction_train",
        "structural_node_embedding_link_prediction_infer",
        "structural_graph_embedding_classification_train",
        "structural_graph_embedding_classification_infer",
    }
)


class VirtualMLBrokerError(RuntimeError):
    """Raised for broker lifecycle or configuration errors."""


class _RequestRejected(ValueError):
    def __init__(
        self,
        message: str,
        *,
        http_status: int = 400,
        exit_code: int = 2,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.exit_code = exit_code


class _HandlerFailed(RuntimeError):
    def __init__(self, handler_error_type: str) -> None:
        super().__init__("virtual-ML action failed")
        self.handler_error_type = handler_error_type[:128]


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _reject_non_finite(value: str) -> None:
    raise ValueError(f"non-finite JSON value is not allowed: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


class _BrokerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int], broker: "VirtualMLBroker") -> None:
        self.broker = broker
        super().__init__(address, _BrokerRequestHandler)


class _BrokerRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: Any) -> None:
        # Tokens and agent arguments must never enter process-wide HTTP logs.
        return

    def _send_json(self, status: int, payload: Mapping[str, Any]) -> None:
        try:
            body = json.dumps(
                dict(payload),
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError):
            status = 500
            body = b'{"error":{"message":"broker response is not strict JSON","type":"BrokerResponseError"},"exit_code":1,"ok":false}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _is_authorized(self) -> bool:
        supplied = self.headers.get_all(VIRTUAL_ML_BROKER_TOKEN_HEADER, [])
        if len(supplied) != 1:
            return False
        candidate = supplied[0]
        expected = self.server.broker._token
        if (
            not isinstance(candidate, str)
            or not candidate
            or not expected
            or len(candidate) > 256
            or any(
                ord(character) < 33 or ord(character) > 126
                for character in candidate
            )
        ):
            return False
        try:
            return hmac.compare_digest(
                candidate.encode("ascii"), expected.encode("ascii")
            )
        except (TypeError, UnicodeEncodeError):
            return False

    def _is_loopback_client(self) -> bool:
        try:
            return ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            return False

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        broker = self.server.broker
        if not broker._request_started():
            self._send_json(
                503,
                {
                    "ok": False,
                    "exit_code": 2,
                    "error": {
                        "type": "BrokerStopping",
                        "message": "virtual-ML broker is stopping",
                    },
                },
            )
            return
        try:
            self._handle_post(broker)
        finally:
            broker._request_finished()

    def _handle_post(self, broker: "VirtualMLBroker") -> None:
        if self.path != BROKER_PATH:
            self._send_json(
                404,
                {
                    "ok": False,
                    "exit_code": 2,
                    "error": {"type": "NotFound", "message": "unknown broker path"},
                },
            )
            return
        if not self._is_loopback_client() or not self._is_authorized():
            self._send_json(
                401,
                {
                    "ok": False,
                    "exit_code": 2,
                    "error": {"type": "Unauthorized", "message": "unauthorized"},
                },
            )
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
        if content_type != "application/json":
            broker._record_transport_failure(
                TypeError("Content-Type must be application/json"), request_bytes=0
            )
            self._send_json(
                415,
                {
                    "ok": False,
                    "exit_code": 2,
                    "error": {
                        "type": "UnsupportedMediaType",
                        "message": "Content-Type must be application/json",
                    },
                },
            )
            return
        raw_length = self.headers.get("Content-Length")
        try:
            content_length = int(raw_length) if raw_length is not None else -1
        except ValueError:
            content_length = -1
        if content_length < 0:
            broker._record_transport_failure(
                ValueError("invalid Content-Length"), request_bytes=0
            )
            self._send_json(
                411,
                {
                    "ok": False,
                    "exit_code": 2,
                    "error": {"type": "LengthRequired", "message": "invalid Content-Length"},
                },
            )
            return
        if content_length > broker.max_request_bytes:
            broker._record_transport_failure(
                ValueError("request exceeds broker size limit"),
                request_bytes=content_length,
            )
            self._send_json(
                413,
                {
                    "ok": False,
                    "exit_code": 2,
                    "error": {
                        "type": "RequestTooLarge",
                        "message": "request exceeds broker size limit",
                    },
                },
            )
            return
        body = self.rfile.read(content_length)
        if len(body) != content_length:
            broker._record_transport_failure(
                ValueError("request body is truncated"), request_bytes=len(body)
            )
            self._send_json(
                400,
                {
                    "ok": False,
                    "exit_code": 2,
                    "error": {"type": "TruncatedRequest", "message": "request body is truncated"},
                },
            )
            return

        if broker._is_stopping():
            self._send_json(
                503,
                {
                    "ok": False,
                    "exit_code": 2,
                    "error": {
                        "type": "BrokerStopping",
                        "message": "virtual-ML broker is stopping",
                    },
                },
            )
            return

        status, response = broker._dispatch(body, request_bytes=content_length)
        self._send_json(status, response)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._send_json(
            405,
            {
                "ok": False,
                "exit_code": 2,
                "error": {"type": "MethodNotAllowed", "message": "POST is required"},
            },
        )


class VirtualMLBroker:
    """One-attempt, in-memory broker for the six virtual graph-ML actions."""

    def __init__(
        self,
        task: Any,
        *,
        max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES,
    ) -> None:
        if (
            isinstance(max_request_bytes, bool)
            or not isinstance(max_request_bytes, int)
            or max_request_bytes <= 0
        ):
            raise ValueError("max_request_bytes must be a positive integer")
        self.max_request_bytes = int(max_request_bytes)
        self._context: dict[str, Any] = build_virtual_ml_context(task)
        self._token = secrets.token_urlsafe(32)
        self._server: _BrokerHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._started_once = False
        self._lifecycle = threading.Condition()
        self._stopping = False
        self._active_requests = 0
        self._trace_lock = threading.Lock()
        self._trace: list[dict[str, Any]] = []

    @property
    def environment(self) -> dict[str, str]:
        """Return only the opaque values that may be given to the agent."""

        server = self._server
        if server is None or self._thread is None or not self._thread.is_alive():
            raise VirtualMLBrokerError("virtual-ML broker is not running")
        host, port = server.server_address[:2]
        return {
            VIRTUAL_ML_BROKER_ENDPOINT_ENV: f"http://{host}:{port}{BROKER_PATH}",
            VIRTUAL_ML_BROKER_TOKEN_ENV: self._token,
        }

    def build_environment(
        self, base_environment: Mapping[str, str] | None = None
    ) -> dict[str, str]:
        """Copy an environment, remove legacy GT context, and add broker access."""

        if base_environment is None:
            import os

            environment = dict(os.environ)
        else:
            environment = dict(base_environment)
        environment.pop(VIRTUAL_ML_CONTEXT_ENV, None)
        environment.update(self.environment)
        return environment

    @property
    def trace(self) -> tuple[dict[str, Any], ...]:
        """Return a detached authoritative call trace in arrival order."""

        with self._trace_lock:
            return tuple(copy.deepcopy(self._trace))

    def start(self) -> "VirtualMLBroker":
        if self._server is not None:
            raise VirtualMLBrokerError("virtual-ML broker is already running")
        if self._started_once:
            raise VirtualMLBrokerError("virtual-ML broker instances cannot be restarted")
        self._started_once = True
        try:
            server = _BrokerHTTPServer(("127.0.0.1", 0), self)
            thread = threading.Thread(
                target=server.serve_forever,
                name=f"virtual-ml-broker-{server.server_address[1]}",
                kwargs={"poll_interval": 0.05},
                daemon=True,
            )
            self._server = server
            self._thread = thread
            thread.start()
        except Exception:
            self._server = None
            self._thread = None
            self._token = ""
            self._context.clear()
            raise
        return self

    def stop(self) -> None:
        server = self._server
        thread = self._thread
        self._server = None
        self._thread = None
        with self._lifecycle:
            self._stopping = True
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        # Revoke new access immediately, then briefly drain already accepted
        # handlers before clearing their only GT source.  If a hostile request
        # remains blocked, its finalizer performs the delayed clear instead of
        # racing it with an in-flight ContextVar bind.
        with self._lifecycle:
            self._token = ""
            deadline = time.monotonic() + SHUTDOWN_DRAIN_SECONDS
            while self._active_requests:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._lifecycle.wait(timeout=remaining)
            if self._active_requests == 0:
                self._context.clear()

    def __enter__(self) -> "VirtualMLBroker":
        return self.start()

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.stop()

    def _request_started(self) -> bool:
        with self._lifecycle:
            if self._stopping:
                return False
            self._active_requests += 1
            return True

    def _request_finished(self) -> None:
        with self._lifecycle:
            self._active_requests -= 1
            if self._active_requests < 0:
                self._active_requests = 0
            if self._stopping and self._active_requests == 0:
                self._context.clear()
            self._lifecycle.notify_all()

    def _is_stopping(self) -> bool:
        with self._lifecycle:
            return self._stopping

    def _begin_trace(self, request_bytes: int) -> tuple[int, float]:
        started = time.monotonic()
        with self._trace_lock:
            index = len(self._trace)
            self._trace.append(
                {
                    "index": index,
                    "sequence": index + 1,
                    "raw_name": None,
                    "canonical_name": None,
                    "status": "running",
                    "request_bytes": request_bytes,
                    "started_at": _utc_timestamp(),
                }
            )
        return index, started

    def _set_trace_action(self, index: int, raw_name: Any, canonical_name: Any) -> None:
        with self._trace_lock:
            if isinstance(raw_name, str) and len(raw_name) > 512:
                self._trace[index]["raw_name"] = raw_name[:512]
                self._trace[index]["raw_name_truncated"] = True
            else:
                self._trace[index]["raw_name"] = raw_name
            self._trace[index]["canonical_name"] = canonical_name

    def _finish_trace(
        self,
        index: int,
        started: float,
        *,
        success: bool,
        error: BaseException | None = None,
    ) -> None:
        update: dict[str, Any] = {
            "status": "success" if success else "failed",
            "success": success,
            "finished_at": _utc_timestamp(),
            "duration_seconds": max(0.0, time.monotonic() - started),
        }
        if error is not None:
            # Bound the diagnostic so malformed calls cannot inflate reports.
            update["error"] = {
                "type": type(error).__name__,
                "message": str(error)[:1024],
            }
        with self._trace_lock:
            self._trace[index].update(update)

    def _record_transport_failure(
        self, error: BaseException, *, request_bytes: int
    ) -> None:
        index, started = self._begin_trace(request_bytes)
        self._finish_trace(index, started, success=False, error=error)

    def _decode_request(self, body: bytes) -> list[str]:
        try:
            payload = json.loads(
                body.decode("utf-8"),
                parse_constant=_reject_non_finite,
                object_pairs_hook=_unique_object,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise _RequestRejected("request body must be strict UTF-8 JSON") from exc
        if not isinstance(payload, dict) or set(payload) != {"argv"}:
            raise _RequestRejected("request must contain exactly one 'argv' field")
        arguments = payload["argv"]
        if not isinstance(arguments, list) or not arguments:
            raise _RequestRejected("argv must be a non-empty array")
        if len(arguments) > MAX_ARGUMENT_COUNT:
            raise _RequestRejected("argv contains too many arguments")
        if any(not isinstance(item, str) for item in arguments):
            raise _RequestRejected("every argv item must be a string")
        if any(len(item.encode("utf-8")) > MAX_ARGUMENT_BYTES for item in arguments):
            raise _RequestRejected("an argv item exceeds the size limit")

        action = arguments[0]
        if action not in ALLOWED_ACTIONS:
            raise _RequestRejected(f"unknown action: {action}")
        # File-valued arguments must have been resolved in the agent's cwd.
        for value in arguments[2::2]:
            if value.endswith((".npy", ".json")) and not Path(value).is_absolute():
                raise _RequestRejected(".npy/.json arguments must use absolute paths")
        return arguments

    def _dispatch(
        self, body: bytes, *, request_bytes: int
    ) -> tuple[int, dict[str, Any]]:
        trace_index, started = self._begin_trace(request_bytes)
        try:
            arguments = self._decode_request(body)
            action = arguments[0]
            self._set_trace_action(trace_index, action, action)

            # Lazy import avoids a module cycle: the CLI imports virtual_ml to
            # look up the request-bound label.
            from graph_analysis_skills import graph_ml_tools

            if action not in graph_ml_tools.ACTION_HANDLERS:
                raise VirtualMLBrokerError("allowed action has no registered handler")
            try:
                parsed_action, kwargs = graph_ml_tools._parse_cli_arguments(arguments)
            except Exception as exc:
                raise _RequestRejected(str(exc)) from exc
            try:
                with bind_virtual_ml_context(self._context):
                    result = graph_ml_tools.ACTION_HANDLERS[parsed_action](**kwargs)
            except Exception as exc:
                # A handler exception may interpolate an injected label.  Keep
                # its concrete type for diagnostics but never return/store its
                # message outside the request-local broker context.
                raise _HandlerFailed(type(exc).__name__) from None
            # Verify before marking success so a non-JSON handler result is a
            # failed authoritative tool call, not a transport-only failure.
            json.dumps(result, allow_nan=False)
        except _RequestRejected as exc:
            action = None
            try:
                decoded = json.loads(body.decode("utf-8"))
                candidate = decoded.get("argv", [None])[0]
                action = candidate if isinstance(candidate, str) else None
            except Exception:
                pass
            canonical = action if action in ALLOWED_ACTIONS else None
            self._set_trace_action(trace_index, action, canonical)
            self._finish_trace(trace_index, started, success=False, error=exc)
            return exc.http_status, {
                "ok": False,
                "exit_code": exc.exit_code,
                "error": {"type": type(exc).__name__, "message": str(exc)[:1024]},
            }
        except _HandlerFailed as exc:
            self._finish_trace(trace_index, started, success=False, error=exc)
            return 422, {
                "ok": False,
                "exit_code": 1,
                "error": {
                    "type": exc.handler_error_type,
                    "message": "virtual-ML action failed",
                },
            }
        except (ValueError, TypeError, OSError) as exc:
            # Argument decoding and handler validation failures match the
            # standalone CLI's non-zero tool result.
            self._finish_trace(trace_index, started, success=False, error=exc)
            return 422, {
                "ok": False,
                "exit_code": 1,
                "error": {"type": type(exc).__name__, "message": str(exc)[:1024]},
            }
        except Exception as exc:
            self._finish_trace(trace_index, started, success=False, error=exc)
            return 500, {
                "ok": False,
                "exit_code": 1,
                "error": {
                    "type": "VirtualMLBrokerError",
                    "message": "virtual-ML broker could not execute the action",
                },
            }

        self._finish_trace(trace_index, started, success=True)
        return 200, {"ok": True, "exit_code": 0, "result": result}


__all__ = [
    "ALLOWED_ACTIONS",
    "BROKER_PATH",
    "DEFAULT_MAX_REQUEST_BYTES",
    "SHUTDOWN_DRAIN_SECONDS",
    "VirtualMLBroker",
    "VirtualMLBrokerError",
]
