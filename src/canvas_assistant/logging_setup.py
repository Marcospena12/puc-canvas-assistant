from __future__ import annotations

import contextlib
import logging
import sys


def _force_utf8_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            with contextlib.suppress(AttributeError, ValueError):
                stream.reconfigure(encoding="utf-8", errors="replace")


def setup_logging(level: str = "INFO") -> None:
    _force_utf8_streams()
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
    for noisy in ("httpx", "httpcore", "apscheduler.executors.default"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
