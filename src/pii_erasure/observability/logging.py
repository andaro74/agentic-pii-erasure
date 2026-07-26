"""structlog configuration with the PII scrubber wired in as a processor.

Every event dict passes through :func:`_redact_processor` BEFORE rendering, so
invariant 5 holds even for a caller who forgot the scrubber exists. This is the
belt; calling :func:`pii_erasure.observability.redact.scrub` at the call site
for known-hot values is the suspenders.
"""

from __future__ import annotations

import logging
import os

import structlog
from structlog.typing import EventDict, WrappedLogger

from pii_erasure.observability.redact import scrub_mapping


def _redact_processor(
    _logger: WrappedLogger, _method_name: str, event_dict: EventDict
) -> EventDict:
    return scrub_mapping(event_dict)


def _level_number(name: str) -> int:
    # logging.getLevelNamesMapping() is 3.12+; the constants themselves are not.
    value = getattr(logging, name, None)
    return value if isinstance(value, int) else logging.INFO


def configure_logging(level: str | None = None) -> None:
    """Configure structlog once, idempotently. Level from PII_ERASURE_LOG_LEVEL."""
    resolved = (level or os.environ.get("PII_ERASURE_LOG_LEVEL", "INFO")).upper()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact_processor,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(sort_keys=True),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(_level_number(resolved)),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
