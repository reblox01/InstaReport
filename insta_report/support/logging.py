"""Logging setup.

One function exists to build the pipeline, and the order of the two handlers
it installs is the whole point:

1. :class:`RedactingFilter` -- scrubs ``record.msg`` and ``record.args`` while
   they are still structured, so nothing downstream sees a secret.
2. :class:`ScrubbingFormatter` -- scrubs the rendered text *and* tracebacks.

Both are needed. A filter alone misses tracebacks, which the logging framework
renders itself in the handler and never passes through ``record.msg``. A
formatter alone misses nothing visible, but it scrubs strings that later code
might have wanted to inspect -- and more importantly it cannot stop a secret
being handed to a third-party handler. Installing the filter first means the
record is already clean by the time any handler sees it, so a handler added
later, by anyone, inherits redaction without knowing it exists.

That is the property worth having: a new call site, or a new handler, cannot
leak by omission.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, TextIO

from .redaction import get_registry, install_redaction

__all__ = ["setup_logging", "reset_logging", "get_logger", "registry_size"]

#: Libraries whose INFO output is never the interesting part of a run. Silenced
#: rather than reconfigured, so anything they log at ERROR still gets through.
NOISY_LOGGERS = ("httpx", "httpcore", "asyncio", "urllib3", "hpack", "h2")


def setup_logging(
    *,
    verbose: bool = False,
    stream: TextIO | None = None,
    level: int | None = None,
) -> logging.Logger:
    """Build the redacting logging pipeline and return the root logger.

    Delegates the filter-and-formatter wiring to :func:`install_redaction`
    rather than restating it here. The ordering guarantee is already documented
    at that function, and a second copy of it would be a second thing to keep
    correct.

    Handlers are cleared first so the call is idempotent in *output*:
    ``install_redaction`` replaces its filter and formatter but only drops a
    handler whose stream differs, so calling this twice with the default stream
    would otherwise print every line twice -- which is a genuinely confusing
    thing to debug when the subject is redaction itself.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    resolved = level if level is not None else (logging.DEBUG if verbose else logging.INFO)
    install_redaction(root, stream=stream, level=resolved)

    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    return root


def reset_logging() -> None:
    """Strip every handler. Used by tests to isolate log capture."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Convenience so callers need not import ``logging`` alongside this."""
    return logging.getLogger(name)


def registry_size() -> int:
    """How many secrets are currently armed. Diagnostics only."""
    return len(get_registry())


def log_record_fields(record: logging.LogRecord) -> dict[str, Any]:
    """Extract a record's interesting fields for a structured sink.

    Returns the record's own attributes rather than the rendered message, so a
    consumer sees the values as data. A filter that has already run means
    these are scrubbed.
    """
    return {
        "name": record.name,
        "level": record.levelname,
        "message": record.getMessage(),
        "pathname": record.pathname,
        "lineno": record.lineno,
    }
