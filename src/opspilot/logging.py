"""Structured logging setup (structlog, JSON by default)."""

import logging
import sys

import structlog

from opspilot.config import LogLevel


def configure_logging(level: LogLevel = "INFO", *, json: bool = True) -> None:
    """Configure structlog and the stdlib root logger to write to stderr."""
    numeric_level = logging.getLevelName(level)
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=numeric_level, force=True)
    # HTTP client libraries log every request at INFO; keep them quiet.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(max(numeric_level, logging.WARNING))
    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        # Resolve sys.stderr per logger so redirected or replaced streams are honoured.
        logger_factory=lambda *_: structlog.PrintLogger(sys.stderr),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> structlog.typing.FilteringBoundLogger:
    """Return a lazy logger tagged with ``logger_name=name``.

    The logger stays lazy, so module-level loggers pick up the configuration
    applied later by :func:`configure_logging`.
    """
    logger: structlog.typing.FilteringBoundLogger = structlog.get_logger(logger_name=name)
    return logger
