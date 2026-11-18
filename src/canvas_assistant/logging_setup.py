from __future__ import annotations

import contextlib
import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_SECRET_PATTERNS = (
    re.compile(
        r"(?i)(canvas_token|telegram_bot_token|anthropic_api_key|api_key|bot_token|access_token|secret_key|token)\s*([=:]\s*|\s+)(['\"]?)([a-zA-Z0-9_\-.:~]{8,})\3"
    ),
    re.compile(r"(?i)(bearer\s+)([a-zA-Z0-9_\-.:~]{8,})"),
    re.compile(r"\b(\d{8,11}:[a-zA-Z0-9_\-]{30,45})\b"),
)


def redact_secrets(text: str) -> str:
    redacted = text
    for pattern in _SECRET_PATTERNS:
        if pattern.groups == 4:
            redacted = pattern.sub(r"\1\2\3***REDACTED***\3", redacted)
        elif pattern.groups == 2:
            redacted = pattern.sub(r"\1***REDACTED***", redacted)
        elif pattern.groups == 1:
            redacted = pattern.sub(r"***REDACTED***", redacted)
    return redacted


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        formatted = super().format(record)
        return redact_secrets(formatted)


def _force_utf8_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            with contextlib.suppress(AttributeError, ValueError):
                stream.reconfigure(encoding="utf-8", errors="replace")


def setup_logging(level: str = "INFO", *, log_file: Path | str | None = None) -> None:
    _force_utf8_streams()
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()

    formatter = RedactingFormatter(
        fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    if log_file:
        file_path = Path(log_file)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            file_path,
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    for noisy in ("httpx", "httpcore", "apscheduler.executors.default"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
