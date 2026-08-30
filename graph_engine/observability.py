"""Graph Engine-local structured service logging."""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any


class ServiceLogger:
    def __init__(self, session_id: str, service: str, logs_dir: str | None = None) -> None:
        self.session_id = session_id
        self.service = service
        self._logger = logging.getLogger(f"graph_engine.{service}.{session_id}")
        self._event_file = Path(logs_dir) / session_id / f"{service}.jsonl" if logs_dir else None

    def _write(self, event: str, **payload: Any) -> None:
        record = {"ts": time.time(), "event": event, "service": self.service, "session_id": self.session_id, **payload}
        self._logger.info("%s", json.dumps(record, ensure_ascii=False, default=str))
        if self._event_file:
            try:
                self._event_file.parent.mkdir(parents=True, exist_ok=True)
                with self._event_file.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            except OSError:
                self._logger.debug("Unable to write graph event log", exc_info=True)

    def event(self, event: str, **payload: Any) -> None:
        self._write(event, **payload)

    def request(self, operation: str, arguments: Any) -> None:
        self._write("REQUEST", operation=operation, arguments=arguments)

    def response(self, operation: str, result: Any, duration_ms: int) -> None:
        self._write("RESPONSE", operation=operation, result=result, duration_ms=duration_ms)


_LOGGERS: dict[tuple[str, str], ServiceLogger] = {}
_LOCK = threading.Lock()


def get_service_logger(session_id: str, service: str, logs_dir: str | None = None) -> ServiceLogger:
    key = (session_id, service)
    with _LOCK:
        if key not in _LOGGERS:
            _LOGGERS[key] = ServiceLogger(session_id, service, logs_dir)
        return _LOGGERS[key]


def close_service_loggers(session_id: str) -> None:
    with _LOCK:
        for key in [item for item in _LOGGERS if item[0] == session_id]:
            _LOGGERS.pop(key, None)

