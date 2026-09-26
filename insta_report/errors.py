"""Error taxonomy.

Three behaviours exist in a report run -- retry, fall through, stop -- and
collapsing them into one path is how the previous version of this tool printed
"reported successfully" on a request that never sent anything. The exception
type *is* the control flow, so each handler names the recovery it performs.

    TransientError     backoff, retry the SAME channel, counts against the
                       account's report budget
    ChannelFailError   the ladder moves to the NEXT channel. Does NOT count
                       against the budget: a selector that will never work is
                       not the account's fault
    FatalError         cannot continue *on the error's scope*. Scope is what
                       stops one challenged account from ending a 500-target
                       run

Scope exists because ``Fatal -> halt run`` is right for a corrupt checkpoint
and wrong for "this account got challenged". Both are fatal; they are not the
same kind of fatal.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

__all__ = [
    "ErrorScope",
    "InstaReportError",
    "TransientError",
    "ChannelFailError",
    "FatalError",
    "NoEligibleAccount",
    "ProxyUnavailable",
    "NavigationTimeout",
    "SelectorDrift",
    "PreflightFailed",
    "SessionExpired",
    "AccountChallenged",
    "ReportBudgetExhausted",
    "UnresolvableTarget",
    "RunAborted",
    "CheckpointCorrupt",
    "recovery_for",
]


class ErrorScope(str, Enum):
    """How far up a failure propagates."""

    REPORT = "report"  # this one target only
    LEASE = "lease"    # this (account, ip) pair; pool rebinds
    RUN = "run"        # halt everything; checkpoint stays resumable


class InstaReportError(Exception):
    """Base for every error the runner knows how to route."""

    default_scope: ErrorScope = ErrorScope.REPORT

    def __init__(self, message: str, *, scope: ErrorScope | None = None) -> None:
        super().__init__(message)
        self.scope = scope or self.default_scope

    @property
    def counts_against_budget(self) -> bool:
        """Whether consuming this failure burns part of the account's allowance.

        Retries do. A channel that structurally cannot serve does not -- the
        budget exists to pace a human, not to punish an account for Instagram
        shipping a layout change.
        """
        return isinstance(self, TransientError)


# --- retry the same channel -------------------------------------------------


class TransientError(InstaReportError):
    """Retryable. Back off, retry the same channel, charge the budget."""

    default_scope = ErrorScope.REPORT


class NoEligibleAccount(TransientError):
    """Every account is cooling down. Retry much later; not a failure of any one."""


class ProxyUnavailable(TransientError):
    """Proxy dead, refused, or returning an HTML interstitial instead of a response.

    Health checks that only look at the status code pass on the HTML case, which
    is why :mod:`insta_report.transport.proxy` inspects the body.
    """

    default_scope = ErrorScope.LEASE


class NavigationTimeout(TransientError):
    """Page did not settle in time. Retrying is safe only pre-dispatch."""


# --- move to the next channel ----------------------------------------------


class ChannelFailError(InstaReportError):
    """This channel structurally cannot serve. Fall through; do not charge budget."""

    default_scope = ErrorScope.REPORT


class SelectorDrift(ChannelFailError):
    """An anchor from the TOML table was not found on the live page.

    The expected cause is Instagram shipping a layout change. Carries the diff
    so the operator can fix it with a config edit rather than a code change.
    """

    def __init__(
        self,
        message: str,
        *,
        missing: tuple[str, ...] = (),
        **kwargs: Any,
    ) -> None:
        # ``**kwargs: Any`` rather than a named ``scope`` parameter, because
        # every typed failure in this module takes a scope and spelling it out
        # here would mean this subclass silently stops accepting it the day
        # someone adds a second. Annotated at all, though: unannotated
        # ``**kwargs`` makes the whole signature unchecked, so a caller can pass
        # anything at all and the constructor takes it.
        super().__init__(message, **kwargs)
        self.missing = missing


class PreflightFailed(ChannelFailError):
    """The channel's preflight did not answer, so it is not trusted at all.

    A fallback that has not been verified is a black hole: every browser failure
    would drop into it silently.
    """


# --- stop, at the right altitude -------------------------------------------


class FatalError(InstaReportError):
    """Cannot continue on the error's scope. The scope decides what stops."""

    default_scope = ErrorScope.LEASE


class SessionExpired(FatalError):
    """The session cookie is no longer valid. A human must re-authenticate."""

    default_scope = ErrorScope.LEASE


class AccountChallenged(FatalError):
    """A challenge interstitial, on a lease or a run.

    Challenges are not status codes -- they arrive as a DOM event or as a 200
    with a challenge field -- so this is a distinct typed signal rather than
    something inferred from an HTTP code. The lease is quarantined; a different
    account continues the run.
    """

    default_scope = ErrorScope.LEASE


class ReportBudgetExhausted(FatalError):
    """The account used its report allowance for the window.

    Silences itself: Instagram stops letting the account report and returns no
    error at all, so the option is simply not actionable. That makes it the most
    common and least visible way an account dies.
    """

    default_scope = ErrorScope.LEASE


class UnresolvableTarget(FatalError):
    """The target does not exist, is gone, or cannot be reported.

    REPORT scope, and distinct from channel failure on purpose: a deleted
    account must not consume budget and must not push the ladder to the next
    channel, because every channel will fail identically.
    """

    default_scope = ErrorScope.REPORT


class RunAborted(FatalError):
    """Operator interrupt, per-run cap, or an unrecoverable precondition.

    RUN scope: the checkpoint is already durable, so the run resumes cleanly.
    """

    default_scope = ErrorScope.RUN


class CheckpointCorrupt(FatalError):
    """The checkpoint cannot be trusted. RUN scope -- continuing risks double-sends."""

    default_scope = ErrorScope.RUN


#: What the runner does with each kind of error. Exposed so the mapping is
#: inspectable in one place instead of being spread across handler bodies.
_RECOVERY: dict[type[InstaReportError], str] = {
    TransientError: "backoff+retry-same-channel",
    ChannelFailError: "next-channel-in-ladder",
    FatalError: "stop-on-error.scope",
}


def recovery_for(error: InstaReportError) -> str:
    """Name the runtime behaviour an error triggers. Used by tests and logging."""
    for base, behaviour in _RECOVERY.items():
        if isinstance(error, base):
            return behaviour
    raise TypeError(f"{type(error).__name__} is not part of the taxonomy")
