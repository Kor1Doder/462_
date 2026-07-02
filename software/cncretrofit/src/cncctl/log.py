"""Structured logging helpers.

structlog with JSON output; every logger is bound with a ``module`` field and
``event`` is supplied per call (the two required fields). Configuration is
opt-in via :func:`configure_logging` — importing the library must not hijack the
host application's logging setup, so nothing is configured at import time.
"""

from __future__ import annotations

import logging

import structlog
from structlog.typing import FilteringBoundLogger, Processor


def get_logger(module: str) -> FilteringBoundLogger:
    """Return a logger bound with ``module=<module>``.

    ``module`` is one of the required fields; pass ``event`` as the first
    positional argument of each log call, e.g. ``log.info("serial_open", port=p)``.
    """
    logger: FilteringBoundLogger = structlog.get_logger(module=module)
    return logger


def configure_logging(*, level: int = logging.INFO, json: bool = True) -> None:
    """Configure structlog for the process (call once from an app/CLI/test).

    Args:
        level: minimum level to emit.
        json: emit JSON when True, or a colorized
            console renderer (local dev) when False.
    """
    renderer: Processor = (
        structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    )
    processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        renderer,
    ]
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )


__all__ = ["configure_logging", "get_logger"]
