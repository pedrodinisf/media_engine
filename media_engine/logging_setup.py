"""Logging setup. Text mode by default; JSON mode when
``MEDIA_ENGINE_LOG_FORMAT=json`` (suitable for container log collection).

The JSON format is one record per line: timestamp, level, logger, message,
plus any ``extra`` fields. Console + a rotating file handler.
"""

from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from media_engine.config import EngineConfig

_logging_configured = False


class _JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        for k, v in record.__dict__.items():
            if k in ("name", "msg", "args", "levelname", "levelno", "pathname",
                     "filename", "module", "exc_info", "exc_text", "stack_info",
                     "lineno", "funcName", "created", "msecs", "relativeCreated",
                     "thread", "threadName", "processName", "process",
                     "getMessage", "message", "asctime", "taskName"):
                continue
            payload[k] = v
        return json.dumps(payload, default=str)


def configure_logging(config: EngineConfig | None = None) -> None:
    """Idempotent: safe to call multiple times. Honors the ``log_format`` config."""
    global _logging_configured
    if _logging_configured:
        return
    cfg = config or EngineConfig.load()
    root = logging.getLogger()
    root.setLevel(cfg.log_level)

    # Console
    console = logging.StreamHandler()
    if cfg.log_format == "json":
        console.setFormatter(_JSONFormatter())
    else:
        console.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s :: %(message)s")
        )
    root.addHandler(console)

    # Rotating file handler — best-effort; skip if permanent_store unwritable
    try:
        log_dir: Path = cfg.permanent_store / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / "engine.log", maxBytes=10 * 1024 * 1024, backupCount=5
        )
        file_handler.setFormatter(
            _JSONFormatter() if cfg.log_format == "json"
            else logging.Formatter("%(asctime)s %(levelname)-7s %(name)s :: %(message)s")
        )
        root.addHandler(file_handler)
    except (PermissionError, OSError):
        pass

    _logging_configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
