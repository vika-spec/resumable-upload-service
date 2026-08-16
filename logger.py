"""
Structured logging setup using structlog.

Every log line is emitted as JSON in production and as pretty-printed
colored text in development (detected by the LOG_FORMAT env var).

Usage anywhere in the codebase:
    from logger import get_logger
    log = get_logger(__name__)
    log.info("upload_created", upload_id="up_abc", user="alice", chunks=100)
"""

## this is logger.py
import logging
import os
import sys

import structlog

LOG_LEVEL  = os.getenv("LOG_LEVEL",  "INFO").upper()
LOG_FORMAT = os.getenv("LOG_FORMAT", "pretty")   # "json" | "pretty"


def _configure() -> None:
    # Shared processors run for both structlog and stdlib loggers
    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    if LOG_FORMAT == "json":
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(LOG_LEVEL)

    # Silence noisy third-party loggers
    for noisy in ("uvicorn.access", "passlib"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


_configure()


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
