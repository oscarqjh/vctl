"""Pretty + JSON logging on stderr (stdout is reserved for command output)."""

from __future__ import annotations

import datetime as _dt
import json
import logging
import sys
from typing import Any, Literal

_RESERVED = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "message",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        rec: dict[str, Any] = {
            "ts": _dt.datetime.fromtimestamp(record.created, tz=_dt.timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for k, v in record.__dict__.items():
            if k not in _RESERVED and not k.startswith("_"):
                rec[k] = v
        if record.exc_info:
            rec["exc"] = self.formatException(record.exc_info)
        return json.dumps(rec, default=str)


class PrettyFormatter(logging.Formatter):
    _COLORS = {"DEBUG": "\x1b[36m", "INFO": "\x1b[32m", "WARNING": "\x1b[33m", "ERROR": "\x1b[31m"}
    _RESET = "\x1b[0m"

    def format(self, record: logging.LogRecord) -> str:
        ts = _dt.datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        level = record.levelname.ljust(7)
        if sys.stderr.isatty():
            color = self._COLORS.get(record.levelname, "")
            level = f"{color}{level}{self._RESET}"
        return f"{ts} {level} {record.name} {record.getMessage()}"


def configure(level: str = "info", fmt: Literal["pretty", "json"] = "pretty") -> None:
    """Reconfigure root logger; idempotent.

    Also silences chatty third-party loggers (httpx, httpcore) at INFO —
    they emit one line per request which floods commands like `lb health`.
    Pass `--log-level debug` to see them.
    """
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt == "json" else PrettyFormatter())
    root.addHandler(handler)
    root.setLevel(level.upper())

    # Silence chatty third-party loggers unless user explicitly asked for debug.
    user_level = logging.getLevelName(level.upper())
    if not isinstance(user_level, int) or user_level > logging.DEBUG:
        for noisy in ("httpx", "httpcore", "urllib3"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
