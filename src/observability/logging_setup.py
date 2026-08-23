from __future__ import annotations
"""Application logging — structured JSON lines to file, human-readable to console.

Deliberately built on the stdlib `logging` module. No log aggregator, no
OpenTelemetry, no metrics backend: nothing in this system's scale justifies
them, and a rotating JSONL file is greppable, diffable and demo-friendly.

Two handlers:
  - RotatingFileHandler -> data/logs/app.log   (JSON lines, DEBUG and up)
  - StreamHandler       -> stderr              (compact text, WARNING and up)

Console output stays quiet by default so the CLI's own presentation layer
(the pretty run output) is not drowned in log lines. Everything still lands
in the file.

Correlation: every record carries run_id / case_id / task_id / action_id when
those are known. Use `bind()` to get a LoggerAdapter that stamps them
automatically, and `log_event()` for machine-readable business events.
"""

import json
import logging
import logging.handlers
import os
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

# Fields that always exist on a LogRecord. Anything else in record.__dict__
# was attached by us (via `extra=`) and belongs in the structured payload.
_STANDARD_FIELDS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName", "message", "asctime",
})

_CORRELATION_KEYS = ("run_id", "case_id", "task_id", "action_id")

_configured = False


class JsonLinesFormatter(logging.Formatter):
    """One JSON object per line. Stable key order for readable diffs."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S") + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Correlation IDs first, so grep-by-eye works.
        extras = {
            k: v for k, v in record.__dict__.items()
            if k not in _STANDARD_FIELDS and not k.startswith("_")
        }
        for key in _CORRELATION_KEYS:
            if key in extras:
                payload[key] = extras.pop(key)

        if extras:
            payload["data"] = _jsonable(extras)

        payload["src"] = f"{record.module}:{record.lineno}"

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = record.stack_info

        return json.dumps(payload, default=str)


class ConsoleFormatter(logging.Formatter):
    """Compact single-line text for the terminal."""

    def format(self, record: logging.LogRecord) -> str:
        ids = " ".join(
            f"{k}={record.__dict__[k]}"
            for k in _CORRELATION_KEYS
            if k in record.__dict__
        )
        suffix = f"  [{ids}]" if ids else ""
        base = f"{record.levelname:<8} {record.name:<28} {record.getMessage()}{suffix}"
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def _jsonable(value: Any) -> Any:
    """Best-effort conversion so a stray object never kills a log write."""
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return str(value)


def setup_logging(
    log_dir: str = "data/logs",
    file_level: str = "DEBUG",
    console_level: str = "WARNING",
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 5,
) -> str:
    """Configure root logging. Idempotent — safe to call from CLI and API both.

    Args:
        log_dir: Directory for app.log (created if absent)
        file_level: Level for the rotating file handler
        console_level: Level for stderr. WARNING keeps the demo output clean.
        max_bytes: Rotate after this many bytes
        backup_count: Number of rotated files to retain (retention policy)

    Returns:
        Absolute path to the active log file.
    """
    global _configured

    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.abspath(os.path.join(log_dir, "app.log"))

    root = logging.getLogger()
    if _configured:
        return log_path

    root.setLevel(logging.DEBUG)

    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8",
    )
    file_handler.setLevel(getattr(logging, file_level.upper(), logging.DEBUG))
    file_handler.setFormatter(JsonLinesFormatter())
    root.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(getattr(logging, console_level.upper(), logging.WARNING))
    console_handler.setFormatter(ConsoleFormatter())
    root.addHandler(console_handler)

    # Third-party libraries are chatty and none of it is our signal.
    for noisy in ("httpx", "httpcore", "chromadb", "sentence_transformers",
                  "urllib3", "asyncio", "uvicorn.access", "PIL", "matplotlib"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True
    logging.getLogger(__name__).info(
        "Application logging configured",
        extra={"log_path": log_path, "file_level": file_level,
               "console_level": console_level, "max_bytes": max_bytes,
               "backup_count": backup_count},
    )
    return log_path


def get_logger(name: str) -> logging.Logger:
    """Module logger. Use `logging.getLogger(__name__)` semantics."""
    return logging.getLogger(name)


def bind(logger: logging.Logger, **correlation: Any) -> logging.LoggerAdapter:
    """Return an adapter that stamps correlation IDs onto every record.

    Example:
        log = bind(get_logger(__name__), run_id=ctx.run_id, case_id=case.id)
        log.info("Task started", extra={"task_id": task.id})
    """
    clean = {k: v for k, v in correlation.items() if v is not None}

    class _Adapter(logging.LoggerAdapter):
        def process(self, msg, kwargs):
            extra = dict(clean)
            extra.update(kwargs.get("extra") or {})
            kwargs["extra"] = extra
            return msg, kwargs

    return _Adapter(logger, clean)


def log_event(
    logger: Any,
    event: str,
    *,
    level: int = logging.INFO,
    duration_ms: Optional[float] = None,
    **fields: Any,
) -> None:
    """Emit a named business event with a machine-readable `event` key.

    Business/event logs are what the briefing and dashboards are derived from;
    keeping them under a stable `event` name means they can be counted without
    parsing prose.
    """
    extra: dict[str, Any] = {"event": event}
    if duration_ms is not None:
        extra["duration_ms"] = round(duration_ms, 2)
    extra.update(fields)
    logger.log(level, event, extra=extra)


@contextmanager
def timed(logger: Any, event: str, **fields: Any) -> Iterator[dict]:
    """Time a block and log its duration on both success and failure.

    Yields a mutable dict; anything put in it is merged into the final event.
    """
    started = time.perf_counter()
    carrier: dict[str, Any] = {}
    try:
        yield carrier
    except Exception:
        log_event(
            logger, f"{event}.failed",
            level=logging.ERROR,
            duration_ms=(time.perf_counter() - started) * 1000,
            **{**fields, **carrier},
        )
        raise
    else:
        log_event(
            logger, f"{event}.completed",
            duration_ms=(time.perf_counter() - started) * 1000,
            **{**fields, **carrier},
        )


class SecurityLog:
    """Dedicated security event stream (separate file, separate retention).

    Security events are kept apart from application logs on purpose: they have
    a different audience (whoever reviews abuse), a different retention need,
    and they must not be lost in DEBUG noise.
    """

    def __init__(self, log_dir: str = "data/logs"):
        os.makedirs(log_dir, exist_ok=True)
        self.path = os.path.abspath(os.path.join(log_dir, "security.log"))
        self._logger = logging.getLogger("caseworkers.security")
        self._logger.propagate = False
        if not self._logger.handlers:
            handler = logging.handlers.RotatingFileHandler(
                self.path, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8",
            )
            handler.setFormatter(JsonLinesFormatter())
            self._logger.addHandler(handler)
            self._logger.setLevel(logging.INFO)

    def record(self, event: str, severity: str = "warning", **fields: Any) -> None:
        level = {
            "info": logging.INFO,
            "warning": logging.WARNING,
            "high": logging.ERROR,
            "critical": logging.CRITICAL,
        }.get(severity.lower(), logging.WARNING)
        self._logger.log(level, event, extra={"event": event, "severity": severity, **fields})
