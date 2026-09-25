"""Error taxonomy routing.

The taxonomy is only worth having if each type reliably produces its intended
recovery. These tests assert the mapping, because a mis-scoped error is how a
single challenged account ends a five-hundred-target run.
"""

from __future__ import annotations

import pytest

from insta_report.errors import (
    AccountChallenged,
    ChannelFailError,
    CheckpointCorrupt,
    ErrorScope,
    FatalError,
    InstaReportError,
    NoEligibleAccount,
    PreflightFailed,
    ProxyUnavailable,
    ReportBudgetExhausted,
    RunAborted,
    SelectorDrift,
    SessionExpired,
    TransientError,
    UnresolvableTarget,
    recovery_for,
)


ALL_ERRORS = [
    NoEligibleAccount("x"),
    ProxyUnavailable("x"),
    SessionExpired("x"),
    AccountChallenged("x"),
    ReportBudgetExhausted("x"),
    UnresolvableTarget("x"),
    RunAborted("x"),
    CheckpointCorrupt("x"),
    SelectorDrift("x"),
    PreflightFailed("x"),
]


def test_every_concrete_error_is_part_of_the_taxonomy():
    for error in ALL_ERRORS:
        assert isinstance(error, InstaReportError)
        assert recovery_for(error)


@pytest.mark.parametrize(
    "error,behaviour",
    [
        (NoEligibleAccount("x"), "backoff+retry-same-channel"),
        (ProxyUnavailable("x"), "backoff+retry-same-channel"),
        (SelectorDrift("x"), "next-channel-in-ladder"),
        (PreflightFailed("x"), "next-channel-in-ladder"),
        (SessionExpired("x"), "stop-on-error.scope"),
    ],
)
def test_recovery_is_what_the_handler_does(error, behaviour):
    assert recovery_for(error) == behaviour


def test_a_challenged_account_does_not_end_the_run():
    """The distinction the scope field exists for.

    A challenge is fatal to one lease. If it were RUN-scoped, a single challenge
    mid-run would discard the remaining targets and every other account's work.
    """
    challenge = AccountChallenged("checkpoint required")
    assert isinstance(challenge, FatalError)
    assert challenge.scope is ErrorScope.LEASE
    assert challenge.scope is not ErrorScope.RUN


def test_a_corrupt_checkpoint_does_end_the_run():
    """Continuing past this risks double-sending, so it is RUN-scoped."""
    assert CheckpointCorrupt("truncated").scope is ErrorScope.RUN


def test_operator_interrupt_is_run_scoped():
    """The checkpoint is already durable, so the run resumes cleanly."""
    assert RunAborted("ctrl-c").scope is ErrorScope.RUN


def test_unresolvable_target_does_not_consume_budget_or_fall_through():
    """A deleted account fails identically on every channel.

    Falling through would spend every channel's budget discovering the same
    thing, and charging the account for a target that does not exist would make
    a list of stale handles look like account death.
    """
    error = UnresolvableTarget("404 on profile")
    assert error.scope is ErrorScope.REPORT
    assert error.counts_against_budget is False


def test_only_transient_errors_charge_the_budget():
    """A selector that will never work is not the account's fault."""
    for error in ALL_ERRORS:
        if isinstance(error, TransientError):
            assert error.counts_against_budget
        else:
            assert not error.counts_against_budget, type(error).__name__


def test_channel_failures_never_charge_the_budget():
    for error in (SelectorDrift("x"), PreflightFailed("x")):
        assert not error.counts_against_budget


def test_scope_can_be_overridden_per_instance():
    """A lease-scoped error the operator wants escalated."""
    error = AccountChallenged("repeated", scope=ErrorScope.RUN)
    assert error.scope is ErrorScope.RUN


def test_selector_drift_carries_the_missing_anchors():
    """The operator needs the diff to fix this with a config edit, not a code change."""
    error = SelectorDrift("anchor missing", missing=("submit", "confirm"))
    assert error.missing == ("submit", "confirm")


def test_recovery_for_rejects_a_foreign_exception():
    with pytest.raises(TypeError):
        recovery_for(ValueError("not in the taxonomy"))
