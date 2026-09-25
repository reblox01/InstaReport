"""Secret redaction.

Redaction happens in exactly two places, both upstream of anything that
formats text:

1. :class:`RedactingFilter` mutates ``record.msg`` and ``record.args`` while
   they are still structured, so *every* handler sees scrubbed values.
2. :class:`ScrubbingFormatter` scrubs the fully-rendered string, which is the
   only thing that can catch an exception traceback -- tracebacks are produced
   by the handler, not stored on the record, so a filter alone cannot see them.

The design constraint from review item 8 is that a *new* call site must not be
able to leak by omission. That is why redaction is a property of the logging
pipeline rather than a discipline expected of callers. Calling
``log.info(f"token={token}")`` is safe without the caller doing anything.

Deliberately fail-safe: it is better to redact a word that turned out not to be
a secret than to print a session cookie. See :data:`MIN_SECRET_LENGTH` for the
one guard that stops that from becoming unusable.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

__all__ = [
    "MIN_SECRET_LENGTH",
    "REDACTED",
    "SecretRegistry",
    "RedactingFilter",
    "ScrubbingFormatter",
    "get_registry",
    "register_secret",
    "scrub",
    "install_redaction",
]

#: Secrets shorter than this are refused. A 3-character "secret" would match
#: common substrings everywhere and make logs unreadable, which trains operators
#: to ignore the place secrets should be scrubbed. Real session cookies and
#: provider keys are far longer than this, so nothing legitimate is lost.
MIN_SECRET_LENGTH = 6

REDACTED = "[REDACTED]"


class SecretRegistry:
    """Holds known secret values and scrubs them out of arbitrary text."""

    def __init__(self) -> None:
        self._secrets: set[str] = set()

    def register(self, value: Any) -> bool:
        """Register *value* as secret. Returns whether it was accepted.

        Rejects ``None``, non-strings that render too short, and anything below
        :data:`MIN_SECRET_LENGTH`. Longest secrets are scrubbed first so a
        secret that is a prefix of another cannot be partially masked.
        """
        if value is None:
            return False
        if isinstance(value, (bytes, bytearray)):
            # A bytes secret's str() form is "b'...'", which can never match a
            # decoded log line. Registering it would be a silent no-op that
            # reads as protection, so it is refused outright.
            return False
        text = value if isinstance(value, str) else str(value)
        if len(text) < MIN_SECRET_LENGTH:
            return False
        self._secrets.add(text)
        return True

    def register_all(self, values: Iterable[Any]) -> int:
        return sum(1 for value in values if self.register(value))

    def clear(self) -> None:
        self._secrets.clear()

    def __len__(self) -> int:
        return len(self._secrets)

    @property
    def _ordered(self) -> list[str]:
        # Longest first: prevents a short secret that prefixes a long one from
        # leaving a partially-visible tail behind.
        return sorted(self._secrets, key=len, reverse=True)

    def scrub(self, text: str) -> str:
        if not text or not self._secrets:
            return text
        for secret in self._ordered:
            if secret in text:
                text = text.replace(secret, REDACTED)
        return text

    def scrub_any(self, value: Any) -> Any:
        """Scrub strings inside arbitrarily nested log arguments."""
        if isinstance(value, str):
            return self.scrub(value)
        if isinstance(value, dict):
            return {k: self.scrub_any(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            rebuilt = [self.scrub_any(v) for v in value]
            return type(value)(rebuilt) if isinstance(value, tuple) else rebuilt
        return value


_registry = SecretRegistry()


def get_registry() -> SecretRegistry:
    return _registry


def register_secret(value: Any) -> bool:
    return _registry.register(value)


def scrub(text: str) -> str:
    return _registry.scrub(text)


class RedactingFilter(logging.Filter):
    """Scrubs ``record.msg`` and ``record.args`` before any handler sees them."""

    def __init__(self, registry: SecretRegistry | None = None) -> None:
        super().__init__()
        self._registry = registry or get_registry()

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._registry.scrub(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = self._registry.scrub_any(record.args)
            else:
                record.args = tuple(self._registry.scrub_any(a) for a in record.args)
        # True: redaction must never silently drop a log record.
        return True


class ScrubbingFormatter(logging.Formatter):
    """Scrubs the rendered output, catching exception tracebacks a filter cannot."""

    def __init__(self, *args: Any, registry: SecretRegistry | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._registry = registry or get_registry()

    def format(self, record: logging.LogRecord) -> str:
        return self._registry.scrub(super().format(record))


def install_redaction(
    logger: logging.Logger | None = None,
    *,
    registry: SecretRegistry | None = None,
    fmt: str = "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt: str = "%H:%M:%S",
    stream: Any = None,
    level: int = logging.INFO,
) -> logging.Logger:
    """Configure *logger* with redaction wired in at both choke points.

    Idempotent for the filter and formatter: repeated calls replace the existing
    instances rather than stacking them, so a reload does not scrub twice or
    leave a stale formatter holding a dead registry reference.
    """
    target = logger or logging.getLogger()
    reg = registry or get_registry()

    for existing in list(target.filters):
        if isinstance(existing, RedactingFilter):
            target.removeFilter(existing)
    target.addFilter(RedactingFilter(reg))

    scrubber = ScrubbingFormatter(fmt=fmt, datefmt=datefmt, registry=reg)
    for handler in target.handlers:
        handler.setFormatter(scrubber)
        if stream is not None and getattr(handler, "stream", None) is not stream:
            handler.close()
            target.removeHandler(handler)
    if not target.handlers:
        handler = logging.StreamHandler(stream) if stream is not None else logging.StreamHandler()
        handler.setFormatter(scrubber)
        target.addHandler(handler)

    target.setLevel(level)
    return target
