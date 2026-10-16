"""Logging: JSON-рядки в stdout і у файл data/logs/collector.log (з нього пишеться звіт про ліміти)."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from collector.config import settings

# Поля, які можна передати через logger.info("...", extra={...})
_EXTRA_FIELDS = (
    "target", "url", "status", "attempt", "sleep_s", "duration_ms", "items", "new", "dup",
    "cursor", "error", "ratelimit", "token", "run_id", "path",
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in _EXTRA_FIELDS:
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str | None = None, to_file: bool = True) -> None:
    fmt: logging.Formatter
    if settings.log_format == "json":
        fmt = JsonFormatter()
    else:
        fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    handlers: list[logging.Handler] = []
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    handlers.append(stream)

    if to_file:
        log_dir = settings.data_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(log_dir / "collector.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(JsonFormatter())  # у файлі завжди JSON — для аналізу
        handlers.append(fh)

    root = logging.getLogger()
    root.handlers[:] = handlers
    root.setLevel(level or settings.log_level)

    for noisy in ("urllib3", "requests", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
