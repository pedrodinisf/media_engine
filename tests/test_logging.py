"""Logging mode smoke tests."""

from __future__ import annotations

import json
import logging

from media_engine.config import EngineConfig
from media_engine.logging_setup import _JSONFormatter


def test_json_formatter_emits_valid_json() -> None:
    formatter = _JSONFormatter()
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname="x.py", lineno=1,
        msg="hello %s", args=("world",), exc_info=None,
    )
    out = formatter.format(record)
    parsed = json.loads(out)
    assert parsed["level"] == "INFO"
    assert parsed["msg"] == "hello world"
    assert parsed["logger"] == "test"


def test_json_formatter_includes_extras() -> None:
    formatter = _JSONFormatter()
    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname="x.py", lineno=1,
        msg="m", args=(), exc_info=None,
    )
    record.op_run_id = "run-abc"  # type: ignore[attr-defined]
    parsed = json.loads(formatter.format(record))
    assert parsed["op_run_id"] == "run-abc"


def test_default_config_picks_text_format() -> None:
    cfg = EngineConfig()
    assert cfg.log_format == "text"
