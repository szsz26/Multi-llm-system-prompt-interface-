"""Structured (JSON-lines) logging. Metadata only - never prompts, responses or keys.

Enabled with COCKPIT_LOG=debug|info|warning; written to <data dir>/cockpit.log.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from .secrets import redact

logger = logging.getLogger("cockpit")
logger.addHandler(logging.NullHandler())


class _JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname.lower(),
            "event": record.getMessage(),
        }
        payload.update(getattr(record, "fields", {}))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return redact(json.dumps(payload, default=str))


def configure(data_dir: Path) -> None:
    level = os.environ.get("COCKPIT_LOG", "").upper()
    if not level:
        return
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    handler = logging.FileHandler(data_dir / "cockpit.log", encoding="utf-8")
    handler.setFormatter(_JSONFormatter())
    logger.addHandler(handler)
    logger.setLevel(getattr(logging, level, logging.INFO))


def event(name: str, level: int = logging.INFO, exc_info: bool = False, **fields: Any) -> None:
    logger.log(level, name, extra={"fields": fields}, exc_info=exc_info)
