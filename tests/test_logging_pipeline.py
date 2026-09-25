"""The redaction pipeline, tested through the pipeline rather than the parts.

``test_redaction.py`` proves the registry and the filter work in isolation.
This file proves the property that actually matters operationally: once
``setup_logging`` has run, a secret cannot reach any handler -- including one
added later by code that knows nothing about redaction, and including text the
logging framework renders itself and never passes through ``record.msg``.

``test_late_handler_inherits_redaction`` is the test that found a real design
bug. A ``logging.Filter`` attached to a logger runs only for records logged to
that logger; child loggers propagate to ancestor *handlers* without passing
through ancestor *filters*. Since every record in this project is emitted from
a named child logger, a root-level filter was close to a no-op and the
formatter was silently doing all the work. Redaction now installs at
``logging.setLogRecordFactory``, which is the only choke point every record
passes through.
"""

from __future__ import annotations

import io
import logging
import sys

import pytest

from insta_report.support.logging import (
    NOISY_LOGGERS,
    get_logger,
    registry_size,
    reset_logging,
    setup_logging,
)
from insta_report.support.redaction import (
    REDACTED,
    RedactingFilter,
    ScrubbingFormatter,
    get_registry,
    install_record_factory,
    register_secret,
    uninstall_record_factory,
)

SECRET = "sessionid%3AAbCdEf-1234567890abcdef"


@pytest.fixture(autouse=True)
def clean_pipeline():
    """Each test owns the root logger and the global registry.

    The registry is process-global by design -- a secret registered at config
    load has to reach the logger, wherever it was registered from -- so tests
    that register must not leak into tests that assert emptiness.
    """
    saved_handlers = list(logging.getLogger().handlers)
    saved_level = logging.getLogger().level
    get_registry().clear()
    yield
    get_registry().clear()
    reset_logging()
    # The record factory is process-global; leaving it installed would scrub
    # records in every later test file, in ways that depend on execution order.
    uninstall_record_factory()
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in saved_handlers:
        root.addHandler(handler)
    root.setLevel(saved_level)


def emitted(stream: io.StringIO) -> str:
    return stream.getvalue()


# --- the headline property --------------------------------------------------


def test_a_secret_never_reaches_the_stream():
    register_secret(SECRET)
    stream = io.StringIO()
    setup_logging(stream=stream)

    logging.getLogger("probe").warning("cookie is %s here", SECRET)

    out = emitted(stream)
    assert SECRET not in out
    assert REDACTED in out


def test_a_handler_added_after_setup_still_gets_redaction():
    """The property the record factory exists to provide.

    A handler added after setup carries neither our filter nor our formatter,
    so a record must already be clean before any handler sees it. Code that
    adds a handler knows nothing about redaction and still cannot leak.
    """
    register_secret(SECRET)
    setup_logging(stream=io.StringIO())

    late = io.StringIO()
    handler = logging.StreamHandler(late)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger().addHandler(handler)

    # Deliberately a child logger, not the root: a root logger filter would
    # not run for this record at all.
    logging.getLogger("some.third.party").warning("value=%s", SECRET)
    assert "value=" in late.getvalue()
    assert SECRET not in late.getvalue()


def test_a_child_logger_record_is_scrubbed_without_any_filter():
    """Records from named modules must be covered, not just root's own.

    This is the case the old filter-only design silently missed.
    """
    register_secret(SECRET)
    stream = io.StringIO()
    root = logging.getLogger()
    # A bare root handler: no redaction filter, no scrubbing formatter.
    root.handlers.clear()
    root.addHandler(logging.StreamHandler(stream))
    root.setLevel(logging.INFO)
    install_record_factory()

    logging.getLogger("insta_report.probe").warning("token %s", SECRET)
    assert SECRET not in emitted(stream)


def test_a_secret_in_a_traceback_is_scrubbed():
    """Why a formatter is needed at all.

    The logging framework renders tracebacks itself inside the handler. They
    never appear in ``record.msg``, so a filter cannot see them -- but a raised
    exception very often carries the value that caused it, which is exactly the
    value a sessionid or cookie would be.
    """
    register_secret(SECRET)
    stream = io.StringIO()
    setup_logging(stream=stream)

    try:
        raise RuntimeError(f"request failed with cookie {SECRET}")
    except RuntimeError:
        logging.getLogger("probe").exception("submit failed")

    out = emitted(stream)
    assert "Traceback" in out, "the traceback should still be visible"
    assert SECRET not in out, "the secret leaked through the traceback"
    assert REDACTED in out


def test_a_secret_in_an_exception_attribute_is_scrubbed():
    register_secret(SECRET)
    stream = io.StringIO()
    setup_logging(stream=stream)

    class LeakyError(Exception):
        def __str__(self) -> str:
            return f"auth header Cookie: sessionid={SECRET}"

    try:
        raise LeakyError()
    except LeakyError:
        logging.getLogger("probe").exception("boom")

    assert SECRET not in emitted(stream)


# --- idempotency ------------------------------------------------------------


def test_setup_twice_does_not_double_the_output():
    """Double output while debugging redaction is a nightmare to read."""
    register_secret(SECRET)
    first, second = io.StringIO(), io.StringIO()

    setup_logging(stream=first)
    setup_logging(stream=second)
    logging.getLogger("probe").warning("hello %s", SECRET)

    assert emitted(first) == ""
    assert emitted(second).count("hello") == 1
    assert SECRET not in emitted(second)


def test_setup_arms_the_filter_and_formatter_on_the_root_logger():
    setup_logging()
    root = logging.getLogger()
    assert any(isinstance(f, RedactingFilter) for f in root.filters)
    assert any(
        isinstance(h.formatter, ScrubbingFormatter) for h in root.handlers
    )


# --- levels -----------------------------------------------------------------


def test_verbose_lowers_the_level_to_debug():
    setup_logging(verbose=True)
    assert logging.getLogger().level == logging.DEBUG


def test_default_level_is_info():
    setup_logging()
    assert logging.getLogger().level == logging.INFO


def test_explicit_level_overrides_verbose():
    setup_logging(verbose=True, level=logging.ERROR)
    assert logging.getLogger().level == logging.ERROR


def test_noisy_libraries_are_silenced_but_still_report_errors():
    """Silenced, not neutered -- a library error must still surface."""
    setup_logging()
    for name in NOISY_LOGGERS:
        assert logging.getLogger(name).level == logging.WARNING


# --- structure preserved ----------------------------------------------------


def test_arguments_are_still_interpolated_after_scrubbing():
    """Redaction must not break %-formatting, or the log becomes unreadable."""
    register_secret(SECRET)
    stream = io.StringIO()
    setup_logging(stream=stream)

    logging.getLogger("probe").info("user=%s count=%d", SECRET, 7)

    out = emitted(stream)
    assert "count=7" in out
    assert SECRET not in out


def test_structured_dict_args_survive():
    register_secret(SECRET)
    stream = io.StringIO()
    setup_logging(stream=stream)

    logging.getLogger("probe").info("%(who)s", {"who": f"id={SECRET}"})

    out = emitted(stream)
    assert "id=" in out
    assert SECRET not in out


# --- helpers ----------------------------------------------------------------


def test_registry_size_reports_armed_secrets():
    assert registry_size() == 0
    register_secret(SECRET)
    assert registry_size() == 1


def test_get_logger_matches_logging_getlogger():
    assert get_logger("x").name == logging.getLogger("x").name


def test_reset_logging_strips_handlers():
    setup_logging(stream=io.StringIO())
    reset_logging()
    assert logging.getLogger().handlers == []


def test_a_secret_registered_after_setup_is_still_caught():
    """Registration is live, not snapshotted at setup time.

    Config loads before setup in some entry points and after it in others, so
    the registry cannot be captured once and frozen.
    """
    stream = io.StringIO()
    setup_logging(stream=stream)
    register_secret(SECRET)
    logging.getLogger("probe").warning("late %s", SECRET)
    assert SECRET not in emitted(stream)


def test_stdout_is_not_polluted_by_default(monkeypatch):
    """Diagnostics go to stderr so `probe --json | jq` stays parseable."""
    setup_logging()
    handlers = logging.getLogger().handlers
    assert handlers
    assert getattr(handlers[0], "stream", None) is sys.stderr
