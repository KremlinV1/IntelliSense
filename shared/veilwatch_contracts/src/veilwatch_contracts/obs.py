"""Observability helpers: structured JSON logging + correlation id context.

Kept in the contracts package so every service emits logs in the same shape —
the audit sink and SIEM pipeline assume this format."""
from __future__ import annotations

import json
import logging
import os
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

_correlation: ContextVar[dict[str, str]] = ContextVar("_correlation", default={})


def set_correlation(**ids: str) -> None:
    """Attach correlation ids (frame_id, incident_id, camera_id, ...) to the
    current async task / thread. Every subsequent log line will include them."""
    current = dict(_correlation.get())
    current.update({k: v for k, v in ids.items() if v is not None})
    _correlation.set(current)


def clear_correlation() -> None:
    _correlation.set({})


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        corr = _correlation.get()
        if corr:
            payload["corr"] = corr
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Attach any extras the caller passed via `logger.info("x", extra={...})`.
        for k, v in record.__dict__.items():
            if k in ("args", "asctime", "created", "exc_info", "exc_text",
                     "filename", "funcName", "levelname", "levelno", "lineno",
                     "module", "msecs", "message", "msg", "name", "pathname",
                     "process", "processName", "relativeCreated", "stack_info",
                     "thread", "threadName", "taskName"):
                continue
            payload[k] = v
        return json.dumps(payload, default=str)


def configure_logging(service: str) -> logging.Logger:
    """Idempotent. Call once at service startup."""
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    fmt = os.getenv("LOG_FORMAT", "json").lower()

    root = logging.getLogger()
    root.setLevel(level)
    # Drop any handlers a framework (e.g. uvicorn) installed, we want one source of truth.
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    if fmt == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s :: %(message)s"
        ))
    root.addHandler(handler)

    logger = logging.getLogger(service)
    logger.info("logging configured", extra={"service": service, "level": level})
    return logger
