from __future__ import annotations

import io
import logging

import pytest

from insta_report.support.redaction import (
    MIN_SECRET_LENGTH,
    REDACTED,
    RedactingFilter,
    ScrubbingFormatter,
    SecretRegistry,
    get_registry,
    install_redaction,
    register_secret,
    scrub,
)

SECRET = "sessionid=abcdef1234567890XYZ"
OTHER = "abcdef1234567890XYZ-and-more-characters"


# --- registry ---------------------------------------------------------------


def test_register_rejects_short_values():
    """A 3-char secret would match everywhere and make logs unreadable."""
    registry = SecretRegistry()
    assert registry.register("abc") is False
    assert len(registry) == 0


def test_register_accepts_at_the_minimum_length():
    registry = SecretRegistry()
    assert registry.register("x" * MIN_SECRET_LENGTH) is True


@pytest.mark.parametrize("value", [None, "", "  ", b"short"])
def test_register_rejects_empty_and_non_string_short_values(value):
    assert SecretRegistry().register(value) is False


def test_register_rejects_bytes_even_when_long_enough():
    """Length alone would admit it, and it could never match anything.

    ``str(b"sessionid-value")`` is ``"b'sessionid-value'"`` -- 20 chars, so it
    clears MIN_SECRET_LENGTH and registers, while matching no decoded log line
    ever. Refusing is the honest outcome; accepting would report protection
    that does not exist.
    """
    payload = b"sessionid-value"
    assert len(str(payload)) >= MIN_SECRET_LENGTH
    assert SecretRegistry().register(payload) is False


def test_register_coerces_non_strings():
    registry = SecretRegistry()
    assert registry.register(12345678901234) is True
    assert registry.scrub("value is 12345678901234 here") == f"value is {REDACTED} here"


def test_scrub_replaces_every_occurrence():
    registry = SecretRegistry()
    registry.register(SECRET)
    text = f"first {SECRET} then {SECRET} again"
    assert text.count(SECRET) == 2
    assert SECRET not in registry.scrub(text)


def test_scrub_is_idempotent():
    registry = SecretRegistry()
    registry.register(SECRET)
    once = registry.scrub(f"cookie {SECRET}")
    assert registry.scrub(once) == once


def test_scrub_prefers_the_longest_secret_first():
    """A short secret that prefixes a long one must not leave a visible tail.

    Naive set iteration would replace only the shared prefix and leave
    ``-and-more-characters`` sitting in the log.
    """
    registry = SecretRegistry()
    registry.register("abcdef1234567890XYZ")            # prefix of OTHER
    registry.register(OTHER)                          # full value
    assert OTHER not in registry.scrub(f"token {OTHER}")
    assert "and-more-characters" not in registry.scrub(f"token {OTHER}")


def test_scrub_handles_empty_text_and_empty_registry():
    assert SecretRegistry().scrub("") == ""
    registry = SecretRegistry()
    assert registry.scrub("nothing to do") == "nothing to do"


def test_scrub_any_walks_nested_structures():
    registry = SecretRegistry()
    registry.register(SECRET)
    cleaned = registry.scrub_any(
        {"a": [SECRET, {"b": f"x{SECRET}"}], "c": ("y", 5), "d": None}
    )
    assert SECRET not in repr(cleaned)
    assert cleaned["c"] == ("y", 5)
    assert cleaned["d"] is None


# --- filter -----------------------------------------------------------------


def test_filter_scrubs_msg_and_args():
    """A new call site must not be able to leak by omission.

    This is the case the whole design exists for: the caller does nothing
    special, and the secret still does not reach the handler.
    """
    registry = SecretRegistry()
    registry.register(SECRET)
    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname=__file__, lineno=1,
        msg=f"submitting with {SECRET}", args=None, exc_info=None,
    )
    assert RedactingFilter(registry).filter(record) is True
    assert SECRET not in record.getMessage()


def test_filter_scrubs_tuple_args():
    registry = SecretRegistry()
    registry.register(SECRET)
    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname=__file__, lineno=1,
        msg="cookie=%s", args=(SECRET,), exc_info=None,
    )
    RedactingFilter(registry).filter(record)
    assert SECRET not in record.getMessage()


def test_filter_scrubs_dict_args():
    """Mapping args arrive as a 1-tuple wrapping the dict -- logging's own shape.

    Constructed the way ``logger.info("%(a)s", {"a": ...})`` produces them, so
    the test exercises the real structure rather than one pytest would reject.
    """
    registry = SecretRegistry()
    registry.register(SECRET)
    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname=__file__, lineno=1,
        msg="%(cookie)s", args=({"cookie": SECRET},), exc_info=None,
    )
    RedactingFilter(registry).filter(record)
    assert SECRET not in record.getMessage()
    assert REDACTED in record.getMessage()


def test_filter_never_drops_a_record():
    registry = SecretRegistry()
    registry.register(SECRET)
    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname=__file__, lineno=1,
        msg=SECRET, args=None, exc_info=None,
    )
    assert RedactingFilter(registry).filter(record) is True


# --- formatter --------------------------------------------------------------


def test_formatter_scrubs_exception_tracebacks():
    """A filter cannot see traceback text -- the handler renders it.

    This is the gap that makes the formatter load-bearing rather than
    redundant.
    """
    registry = SecretRegistry()
    registry.register(SECRET)
    try:
        raise RuntimeError(f"auth failed for {SECRET}")
    except RuntimeError:
        import sys

        exc_info = sys.exc_info()

    record = logging.LogRecord(
        name="t", level=logging.ERROR, pathname=__file__, lineno=1,
        msg="submit failed", args=None, exc_info=exc_info,
    )
    rendered = ScrubbingFormatter(registry=registry).format(record)
    assert SECRET not in rendered
    assert REDACTED in rendered
    assert "RuntimeError" in rendered


# --- installation -----------------------------------------------------------


def test_install_redaction_wires_both_choke_points():
    stream = io.StringIO()
    logger = logging.getLogger("test.install")
    logger.handlers.clear()
    install_redaction(logger, stream=stream, level=logging.DEBUG)

    register_secret(SECRET)
    logger.info("leaking %%s here: %s", SECRET)
    try:
        raise ValueError(f"boom {SECRET}")
    except ValueError:
        logger.exception("context")

    output = stream.getvalue()
    assert SECRET not in output
    assert REDACTED in output
    assert "ValueError" in output


def test_install_redaction_is_idempotent():
    """A reload must not stack filters or leave a formatter on a dead registry."""
    logger = logging.getLogger("test.idempotent")
    logger.handlers.clear()

    first = SecretRegistry()
    first.register("registry-one-secret-value")
    install_redaction(logger, registry=first, stream=io.StringIO())

    second = SecretRegistry()
    second.register("registry-two-secret-value")
    install_redaction(logger, registry=second, stream=io.StringIO())

    filters = [f for f in logger.filters if isinstance(f, RedactingFilter)]
    assert len(filters) == 1
    assert filters[0]._registry is second


def test_module_level_helpers_use_the_shared_registry():
    assert register_secret(SECRET) is True
    assert SECRET not in scrub(f"value: {SECRET}")
    assert SECRET not in get_registry().scrub(f"value: {SECRET}")
