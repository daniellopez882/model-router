"""JSON logs with a request id on every line.

The request id is a context variable set by the API middleware, so anything
logged while a request is being served -- including from the router and the
provider adapters, which know nothing about HTTP -- carries it. Prompts and
completions are never logged: they are the tenant's data.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from typing import Any

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        extra = getattr(record, "extra", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    # uvicorn's access log duplicates what the middleware logs, with less context.
    logging.getLogger("uvicorn.access").disabled = True


def log(logger: logging.Logger, level: int, msg: str, **fields: Any) -> None:
    logger.log(level, msg, extra={"extra": fields})
