"""Structured logging setup (structlog, JSON by default)."""

import logging
import sys

import structlog

from opspilot.config import LogLevel


def configure_logging(level: LogLevel = "INFO", *, json: bool = True) -> None:
    """Configure structlog and the stdlib root logger to write to stderr."""
    numeric_level = logging.getLevelName(level)
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=numeric_level, force=True)
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
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> structlog.typing.FilteringBoundLogger:
    """Return a bound logger tagged with ``logger=name``."""
    logger: structlog.typing.FilteringBoundLogger = structlog.get_logger(name)
    return logger.bind(logger=name)
