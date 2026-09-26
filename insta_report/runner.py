"""Run orchestration: the ladder, the boundary, and the abort latch.

Everything above this module is a pure function over observed facts. This is
where observations become decisions, so it is also where the invariants have to
be *held* rather than merely stated.

The ladder
----------

D2 settled this as a **ladder**, not a fan-out: one report per target, tried on
the browser channel, then the mobile API, then the web API. Fan-out would file
three reports for one target on a working day, which is both a duplicate and a
signal.

```
   target
     │
     ├─► our own account? ──yes──► REFUSE. No dispatch, no intent.
     │        │                     The run latches: see _refuse.
     │        no
     ▼
   ┌────────────────────────────────────────────────────────────────┐
   │  for each live channel, in order:                               │
   │                                                                 │
   │    ┌─ TransientError, nothing dispatched ──► backoff, retry   │
   │    │      this same channel, up to the retry limit            │
   │    │                                                          │
   │    ├─ TransientError, intent already on disk ──► UNKNOWN,     │
   │    │      stop. A retry could double-file.                    │
   │    │                                                          │
   │    ├─ CHANNEL_FAILED (never dispatched) ──► next channel      │
   │    │                                                          │
   │    └─ anything else ──► terminal, stop                        │
   └────────────────────────────────────────────────────────────────┘
     │
     ▼
   one Outcome, appended to the checkpoint, and nothing further
```

The dispatch boundary
---------------------

The boundary is :meth:`CheckpointStore.record_intent` -- a write, not a flag.
This module's only job at the boundary is to make it impossible to dispatch
without it:

* the intent is fsynced *before* the channel is allowed to click
* the account is charged only *after* the fsync returns, so a failed write
  leaves the account unbilled and the target unattempted
* a write that raises propagates out of the channel, and the click never
  happens
* a second call on the same target is refused, so one target cannot become two
  reports however badly a channel is written

The invariant that makes ``--resume`` safe is checked rather than assumed. At
the end of every run :attr:`RunReport.unsettled` is either empty or the report
says so loudly: a target with an intent and no outcome is exactly the case where
a report may or may not have landed, and it must never be quietly retried.

Account-to-IP affinity
----------------------

A session established on one exit and continued on another is a correlation
Instagram can see. The binding therefore belongs to a *lease*, not to a report:
a slot that rotates its exit also rotates its account. The ordering guarantee
inside that rotation lives in :meth:`insta_report.accounts.AccountPool.rebind`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import (
    Any,
    Callable,
    ClassVar,
    Iterator,
    Mapping,
    Protocol,
    Sequence,
    runtime_checkable,
)

from .accounts import AccountPool, Lease
from .checkpoint import CheckpointStore, Intent
from .errors import (
    AccountChallenged,
    ChannelFailError,
    ErrorScope,
    InstaReportError,
    NoEligibleAccount,
    ProxyUnavailable,
    RunAborted,
    SessionExpired,
    TransientError,
)
from .outcomes import Outcome, TerminalState, utc_now
from .pacing import Pacer
from .proxies import ProxyLease, ProxyPool
from .targets import Target, TargetList

__all__ = [
    "NoChannelsAvailable",
    "ReportChannel",
    "ChannelSpec",
    "ChannelHealth",
    "Refusal",
    "RunOptions",
    "RunReport",
    "Runner",
]

log = logging.getLogger(__name__)

#: Terminal states that *assert a report went out*. These are the only ones
#: that may not appear without an intent on disk to back them up.
#:
#: The complement matters just as much. ``CHANNEL_FAILED``, ``NOT_REPORTABLE``
#: and ``QUARANTINED`` are all legitimate pre-dispatch verdicts: the profile 404s,
#: the ladder is down, a login wall is in the way. None of them means a report
#: was sent, so none of them needs an intent, and none of them is written to
#: the ledger. A 404 is a fact about right now, not a verdict on the target.
_DISPATCHED_TERMINALS = frozenset(
    {
        TerminalState.SUBMITTED_ACKED,
        TerminalState.SUBMITTED_UNCONFIRMED,
        TerminalState.UNKNOWN,
    }
)


class NoChannelsAvailable(RunAborted):
    """Every channel is disabled, so there is nothing left to try.

    Run-scoped, and raised rather than absorbed. The alternative -- looping over
    a dead ladder and reporting "nothing to do" -- is how a run ends with a
    clean exit code and zero reports filed, which is the failure this whole
    project exists to remove.
    """


class _BoundaryFailure(RunAborted):
    """The durable write at the dispatch boundary failed.

    Carries its own type because it is the one failure that must *not* be
    graded. Nothing was sent -- the click is downstream of the write -- so
    grading it as a channel failure would fall through the ladder and report
    the target nowhere, silently. Retrying is not available either, because a
    write that failed may still have landed. So the run stops, the checkpoint is
    left exactly as it was, and the operator decides.

    Run-scoped by construction: if one fsync fails, the disk or the filesystem
    is the problem and every later write is in doubt too.
    """


@runtime_checkable
class ReportChannel(Protocol):
    """What the runner needs from a channel.

    Deliberately narrow. The runner owns scheduling, accounts, exits, and the
    boundary; a channel owns one report attempt and returns what it observed. A
    channel that wanted to retry, fall through, or rotate an exit would be
    re-implementing the part of this that is hardest to test.
    """

    #: A ClassVar, not an instance attribute. A channel's identity is a property
    #: of the channel it *is*, not of the report it is currently filing -- so the
    #: protocol declares it the same way the implementation does. Declaring it as
    #: an instance variable here let a channel that declared it a ClassVar fail
    #: the protocol for no reason, which is the worst kind of type error: one
    #: that says your working code is wrong.
    name: ClassVar[str]

    async def report(
        self,
        target: Target,
        *,
        on_dispatch: Callable[[], None],
        account_ref: str | None = ...,
        lease: ProxyLease | None = ...,
        attempt: int = ...,
    ) -> Outcome: ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True)
class ChannelSpec:
    """A channel plus the schedule the runner gives it.

    ``capacity`` is the runner's business, not the channel's: a channel that
    drives one page at a time should not also have to know that the runner is
    the thing deciding how many of its pages may run at once.
    """

    name: str
    channel: ReportChannel
    capacity: int = 1


@dataclass(frozen=True)
class RunPlan:
    """What a dry run says it will do.

    A named shape rather than a dict, and ``frozen`` so a consumer cannot
    quietly "adjust" the plan into a different plan than the one the runner
    computed. The two things a dry run exists to tell the operator are the
    counts -- how many targets, how many pending, how many already settled -- and
    a plan whose fields are unchecked ``object`` lookups is a plan that can print
    a number that was never verified against the field it claims to come from.

    Every field is derived by the same code the real run uses. That is the
    property that makes a dry run worth reading: a plan computed by looser logic
    than the run is a plan that promises forty and delivers five.
    """

    run_id: str
    targets: int
    pending: int
    already_settled: int
    channels: tuple[str, ...]
    capacity: int
    #: ``None`` means "no per-run ceiling", which is different from any number
    #: and is the reason this field is optional rather than defaulted to 0.
    #: Coercing it to 0 here would make an uncapped plan print a cap of zero.
    max_reports: int | None
    horizon_seconds: float
    max_concurrent: int
    workers: int
    #: One status mapping per account, from
    #: :meth:`~insta_report.accounts.Account.status`. Kept as mappings rather
    #: than flattened to strings so the plan and the live status view cannot
    #: disagree about what an account's state is.
    accounts: tuple[Mapping[str, object], ...]
    self_reporting: tuple[str, ...]


@dataclass
class ChannelHealth:
    """Per-channel accounting for one run.

    The number that matters is ``consecutive_channel_failures``, and it counts
    *only* failures where nothing was dispatched. A channel that failed three
    targets in a row without sending anything has a structural problem -- a
    layout change, a preflight that stopped answering -- and continuing to spend
    accounts on it is how a session gets burned for nothing. A channel that
    dispatched and got an unreadable response has told us nothing about its own
    health, and counting that would retire every channel on a bad afternoon.
    """

    name: str
    capacity: int
    attempts: int = 0
    dispatches: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)
    consecutive_channel_failures: int = 0
    worst_consecutive: int = 0
    disabled: bool = False
    disabled_reason: str = ""

    def note(self, outcome: Outcome) -> None:
        """Record one completed channel attempt."""
        self.attempts += 1
        if outcome.was_dispatched:
            self.dispatches += 1
        key = outcome.terminal.value
        self.outcomes[key] = self.outcomes.get(key, 0) + 1

        if self._is_structural(outcome):
            self.consecutive_channel_failures += 1
            self.worst_consecutive = max(
                self.worst_consecutive, self.consecutive_channel_failures
            )
        else:
            # Any other outcome -- including a target that turned out not to
            # exist -- proves the channel is answering. Resetting on those is
            # what stops three scattered failures a day apart from retiring a
            # channel that is working.
            self.consecutive_channel_failures = 0

    @staticmethod
    def _is_structural(outcome: Outcome) -> bool:
        return (
            outcome.terminal is TerminalState.CHANNEL_FAILED
            and not outcome.was_dispatched
        )

    def disable(self, reason: str) -> None:
        if not self.disabled:
            self.disabled = True
            self.disabled_reason = reason
            log.error("channel %s disabled: %s", self.name, reason)

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "capacity": self.capacity,
            "attempts": self.attempts,
            "dispatches": self.dispatches,
            "outcomes": dict(self.outcomes),
            "worst_consecutive_failures": self.worst_consecutive,
            "disabled": self.disabled,
            "disabled_reason": self.disabled_reason,
        }


@dataclass(frozen=True)
class Refusal:
    """A target the runner declined to file, and why.

    Refusals are reported rather than recorded. Nothing was dispatched, so
    there is no intent for an outcome to pair with, and inventing one would put
    a "dispatched" record in the ledger for a report that never existed.
    """

    target_key: str
    display: str
    reason: str
    #: Whether this kind of problem also stops the run. See :meth:`Runner._refuse`.
    fatal_to_run: bool = False


@dataclass(frozen=True)
class RunOptions:
    """Everything the operator can change about *this* run.

    The defaults are the conservative ones. A run that starts without being
    told anything stops at the per-run cap rather than at the end of the target
    list, because the blast radius of an unattended run is the thing that is
    actually dangerous -- not the number of lines in a file.
    """

    #: Hard cap on dispatches for this run. ``None`` means uncapped, which the
    #: CLI never passes and which exists for tests and for an operator who has
    #: reasoned about it.
    max_reports: int | None = 100
    #: Wall time in seconds after which no new work starts. An attended run
    #: (D12): this is a stop condition, not a schedule to fill.
    horizon_seconds: float = 6 * 3600.0
    #: Retries of the *same* channel after a transient, pre-dispatch failure.
    transient_retries: int = 2
    #: Base backoff between those retries, doubled per attempt.
    backoff_seconds: float = 5.0
    #: Times an expired exit is replaced mid-target, before the channel is
    #: blamed for it. Zero means a stale binding is a CHANNEL_FAILED straight
    #: away, which is the right answer for a pool with one address and the
    #: wrong one for a pool with several.
    exit_rotations: int = 2
    #: Consecutive non-dispatched channel failures that retire a channel.
    channel_failure_threshold: int = 3
    #: Concurrent workers. Bounded in practice by eligible accounts and by the
    #: channels' own capacity.
    max_concurrent: int = 1
    #: Report what would happen. Touches no network and writes no checkpoint.
    dry_run: bool = False

    def __post_init__(self) -> None:
        if self.max_reports is not None and self.max_reports < 1:
            raise ValueError(f"max_reports must be >= 1, got {self.max_reports}")
        if self.horizon_seconds <= 0:
            raise ValueError(
                f"horizon_seconds must be positive, got {self.horizon_seconds}"
            )
        if self.transient_retries < 0:
            raise ValueError(
                f"transient_retries must be >= 0, got {self.transient_retries}"
            )
        if self.exit_rotations < 0:
            raise ValueError(
                f"exit_rotations must be >= 0, got {self.exit_rotations}"
            )
        if self.channel_failure_threshold < 1:
            raise ValueError(
                "channel_failure_threshold must be >= 1, got "
                f"{self.channel_failure_threshold}"
            )
        if self.max_concurrent < 1:
            raise ValueError(
                f"max_concurrent must be >= 1, got {self.max_concurrent}"
            )


@dataclass(frozen=True)
class RunReport:
    """What a run did, in the only vocabulary that means anything.

    The headline is ``counts``, and it describes *requests*, not accounts.
    Nothing in this tool can tell an operator that Instagram acted on a report,
    and a summary implying otherwise would be the original bug wearing a
    summary.
    """

    run_id: str
    started_at: datetime
    finished_at: datetime
    dispatches: int
    targets_considered: int
    counts: dict[str, int]
    refusals: tuple[Refusal, ...]
    channel_names: tuple[str, ...]
    health: tuple[ChannelHealth, ...]
    review: tuple[Outcome, ...]
    #: Targets with an intent and no outcome. Empty for a clean run; anything
    #: here means a report may or may not have landed.
    unsettled: tuple[str, ...]
    aborted: bool
    abort_reason: str = ""
    dry_run: bool = False
    errors: tuple[str, ...] = ()

    @property
    def acked(self) -> int:
        return self.counts.get(TerminalState.SUBMITTED_ACKED.value, 0)

    @property
    def needs_attention(self) -> bool:
        """Whether a human has to look at this run's output before the next."""
        return bool(self.review or self.refusals or self.unsettled)

    def channel_report(self) -> list[dict[str, object]]:
        return [health.as_dict() for health in self.health]

    def render(self) -> str:
        """Operator-facing summary. Says what was requested, never what happened."""
        lines = [
            f"run {self.run_id}"
            + ("  (dry run -- nothing was sent)" if self.dry_run else ""),
            f"  started    {self.started_at.isoformat(timespec='seconds')}",
            f"  finished   {self.finished_at.isoformat(timespec='seconds')}",
            f"  considered {self.targets_considered} target(s), "
            f"dispatched {self.dispatches}",
            "  outcomes:",
        ]
        if any(self.counts.get(state.value, 0) for state in TerminalState):
            for state in TerminalState:
                count = self.counts.get(state.value, 0)
                if count:
                    lines.append(f"    {state.value:<24} {count}")
        else:
            lines.append("    (none)")
        lines.append("  channels:")
        for entry in self.channel_report():
            verdict = "disabled" if entry["disabled"] else "live"
            lines.append(
                f"    {str(entry['name']):<12} {verdict:<9} "
                f"attempts={entry['attempts']} dispatches={entry['dispatches']}"
            )
            if entry["disabled_reason"]:
                lines.append(f"        {entry['disabled_reason']}")
        if self.refusals:
            lines.append("  refused before dispatch:")
            for refusal in self.refusals:
                lines.append(f"    {refusal.display:<30} {refusal.reason}")
        if self.unsettled:
            # Should be impossible. Shown loudly rather than hidden, because
            # every target here may or may not have been reported.
            lines.append("  UNSETTLED (dispatched, outcome not recorded):")
            for key in self.unsettled:
                lines.append(f"    {key}")
        if self.review:
            lines.append("  needs a human:")
            for outcome in self.review:
                suffix = f" -- {outcome.detail}" if outcome.detail else ""
                lines.append(
                    f"    {outcome.target_ref:<30} {outcome.terminal.value}{suffix}"
                )
        for message in self.errors:
            lines.append(f"  error: {message}")
        if self.aborted:
            lines.append(f"  STOPPED EARLY: {self.abort_reason}")
        return "\n".join(lines)


class _Slot:
    """One (account, exit) binding, reused across reports.

    The unit of rotation is the lease, not the report. A slot keeps its account
    and its exit until one of three things happens: the lease has used up its
    report allowance, the account is no longer eligible, or the exit's sticky
    window has closed. Re-picking either per report would move the IP out from
    under a live session, which is F10 and the entire reason a lease exists.
    """

    def __init__(
        self,
        index: int,
        *,
        pool: AccountPool,
        proxies: ProxyPool | None,
        held: set[str],
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.index = index
        self._pool = pool
        self._proxies = proxies
        self._held = held
        self._monotonic = monotonic
        self.lease: Lease | None = None
        self.proxy_lease: ProxyLease | None = None
        self.reports_used = 0
        self._pending_proxy: ProxyLease | None = None

    # -- inspection ------------------------------------------------------

    @property
    def account_ref(self) -> str | None:
        return self.lease.account_ref if self.lease else None

    @property
    def exit_origin(self) -> str:
        return self.proxy_lease.endpoint.origin if self.proxy_lease else "direct"

    def budget_remaining(self) -> int:
        if self.lease is None:
            return 0
        account = self._pool.get(self.lease.account_ref)
        return max(0, account.daily_budget - account.used_today)

    def budget_total(self) -> int:
        if self.lease is None:
            return 0
        return self._pool.get(self.lease.account_ref).daily_budget

    def needs_binding(self) -> bool:
        if self.lease is None:
            return True
        if self.lease.exhausted(self.reports_used):
            return True
        if not self._pool.get(self.lease.account_ref).eligible(self._monotonic()):
            return True
        if self.proxy_lease is not None and self.proxy_lease.expired(self._monotonic()):
            # The provider has already moved this address. Carrying on with it
            # is how a session starts as one identity and finishes as another,
            # so the binding is stale even though the lease object still looks
            # perfectly valid.
            return True
        return False

    def describe(self) -> str:
        if self.lease is None:
            return f"slot {self.index}: unbound"
        return (
            f"slot {self.index}: {self.lease.account_ref} on {self.exit_origin} "
            f"({self.reports_used}/{self.lease.max_reports} reports)"
        )

    # -- rotation --------------------------------------------------------

    def bind(self) -> None:
        """Acquire or re-acquire the (account, exit) pair.

        Synchronous on purpose. It reads and writes the shared ``held`` set and
        calls into two pools, and asyncio can only switch at an ``await`` -- so
        making this a coroutine would open a window in which two workers both
        read an empty ``held``, both call ``lease()``, and both receive the same
        identity. The absence of ``async`` is the concurrency guarantee.
        """
        if not self.needs_binding():
            return
        previous_account = self.lease.account_ref if self.lease else None
        keep = {previous_account} if previous_account else set()
        previous = self.lease
        previous_proxy = self.proxy_lease

        # Order: acquire the replacement *before* releasing the old. A failure
        # anywhere below leaves the current binding in force, which is the only
        # safe way to fail -- an unbound slot is an account with no verified
        # exit, and a half-rotated one is worse than either.
        lease = self._pool.lease(exclude=set(self._held) - keep)
        self._attach_exit(lease)

        if previous_account:
            self._held.discard(previous_account)
        self._held.add(lease.account_ref)
        if previous is not None:
            self._pool.release_lease(previous)
        if previous_proxy is not None and self._proxies is not None:
            self._proxies.release(previous_proxy)

        self.lease = lease
        self.proxy_lease = self._pending_proxy
        self._pending_proxy = None
        self.reports_used = 0
        log.info("bound %s", self.describe())

    def _attach_exit(self, lease: Lease) -> None:
        """Bind a verified exit to *lease*.

        Direct connection is not offered as a fallback. ``build_pool`` already
        fails closed on an empty pool, and quietly going direct here would undo
        that: the operator would have paid for isolation and not received it,
        with nothing in the output to say so.
        """
        if self._proxies is None:
            return
        proxy_lease = self._proxies.acquire()
        if proxy_lease.expired(self._monotonic()):
            # Should be impossible -- acquire() probes before binding. Treated
            # as a hard refusal rather than ignored, because the alternative is
            # a report sent through an address that has already moved.
            raise ProxyUnavailable(
                f"acquired exit {proxy_lease.endpoint.origin} is already past its "
                "sticky window; refusing to report through it"
            )
        self._pool.rebind(lease.account_ref, proxy_lease.endpoint.url)
        self._pending_proxy = proxy_lease

    def retire(self) -> None:
        """Drop the binding because the account, not the schedule, is the problem."""
        self.release()

    def release(self) -> None:
        if self.lease is not None:
            self._held.discard(self.lease.account_ref)
            self._pool.release_lease(self.lease)
        if self.proxy_lease is not None and self._proxies is not None:
            self._proxies.release(self.proxy_lease)
        self.lease = None
        self.proxy_lease = None
        self._pending_proxy = None
        self.reports_used = 0


class _AsyncSleeper:
    """``asyncio.sleep`` that the abort latch can cut short.

    A pacing gap can reach :data:`insta_report.pacing.MAX_GAP_SECONDS`, and the
    latch has to be able to stop a worker sitting in one -- otherwise
    ``--max-reports`` and Ctrl-C both take effect only after the current hour is
    over, which is the difference between a control and a suggestion. The sleep
    is cancelled rather than left pending so the loop does not emit "task was
    destroyed but it is pending" on the way out.
    """

    def __init__(self, abort: asyncio.Event) -> None:
        self._abort = abort

    async def __call__(self, seconds: float) -> bool:
        """Sleep *seconds*; return ``False`` if the abort fired first."""
        if seconds <= 0:
            return True
        sleep = asyncio.ensure_future(asyncio.sleep(seconds))
        abort = asyncio.ensure_future(self._abort.wait())
        try:
            await asyncio.wait({sleep, abort}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (sleep, abort):
                if not task.done():
                    task.cancel()
            # Awaiting the cancelled tasks lets them actually finish, so no
            # exception surfaces later from a handle nobody kept.
            await asyncio.gather(sleep, abort, return_exceptions=True)
        return sleep.done() and not sleep.cancelled()


class Runner:
    """Runs one pass over a target list.

    Everything here is injected, including the wait. A pacing gap bottoms out at
    :data:`insta_report.pacing.MIN_GAP_SECONDS`, so a test that used the real
    sleeper would spend eight seconds proving that a gap is applied -- eight
    seconds per report, per channel, per retry. The seam is
    ``sleeper(seconds) -> bool``, where ``False`` means the abort fired.
    """

    def __init__(
        self,
        *,
        store: CheckpointStore,
        pool: AccountPool,
        channels: Sequence[ChannelSpec],
        targets: TargetList,
        options: RunOptions | None = None,
        pacer_factory: Callable[[], Pacer] = Pacer,
        proxies: ProxyPool | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        on_progress: Callable[[Outcome], None] | None = None,
        sleeper: Callable[[float], Any] | None = None,
    ) -> None:
        self._store = store
        self._pool = pool
        self._targets = targets
        self._options = options or RunOptions()
        self._pacer_factory = pacer_factory
        self._proxies = proxies
        self._monotonic = monotonic
        self._on_progress = on_progress
        self._sleeper = sleeper

        self._channels = tuple(channels)
        self._health = {
            spec.name: ChannelHealth(name=spec.name, capacity=spec.capacity)
            for spec in self._channels
        }
        self._slots: list[_Slot] = []
        self._held: set[str] = set()
        self._abort = asyncio.Event()
        self._wait: Callable[[float], Any] = sleeper or _AsyncSleeper(self._abort)
        self._abort_reason = ""
        self._dispatches = 0
        self._refusals: list[Refusal] = []
        self._errors: list[str] = []
        self._fatal: InstaReportError | None = None
        self._considered = 0
        self._deadline = float("inf")
        #: Every terminal state this run reached, *including* the ones that were
        #: never written to the ledger.
        #:
        #: Kept separately from the checkpoint on purpose. The ledger answers
        #: "may a report have landed?", so it only holds dispatched targets. The
        #: summary answers "what happened to these targets?", and that question
        #: includes the ones that 404'd, hit a login wall, or found every rung of
        #: the ladder down. Reading the summary out of the ledger would print
        #: zeroes next to a quarantine and read as a run that did nothing --
        #: which is the exact failure this project was rebuilt to remove.
        self._tally: dict[str, int] = {}

    # -- public surface --------------------------------------------------

    @property
    def aborted(self) -> bool:
        return self._abort.is_set()

    @property
    def abort_reason(self) -> str:
        return self._abort_reason

    @property
    def dispatches(self) -> int:
        return self._dispatches

    def request_abort(self, reason: str) -> None:
        """Latch the abort. Idempotent; the first reason is the one kept.

        Ctrl-C is not a blast-radius control. It stops new work being
        *started*; it never abandons a report that has already been dispatched,
        because abandoning one leaves an intent with no outcome and that target
        can then never be settled.
        """
        if not self._abort.is_set():
            self._abort_reason = reason
            log.warning("aborting: %s", reason)
        self._abort.set()

    def live_channels(self) -> tuple[ChannelSpec, ...]:
        return tuple(
            spec for spec in self._channels if not self._health[spec.name].disabled
        )

    def channel_health(self) -> dict[str, ChannelHealth]:
        return dict(self._health)

    def plan(self) -> "RunPlan":
        """What a dry run reports.

        Pure with respect to the outside world: no network, no writes, and no
        clock reads that could make the plan disagree with the run it describes.

        A dataclass rather than a dict. It has two consumers -- the dry-run
        report and the ``--dry-run`` printer -- and as a ``dict[str, object]``
        every field access was an unchecked cast, so a renamed key or a changed
        type surfaced as a wrong number on the operator's screen rather than as
        an error. The plan is a contract; it is now written down as one.
        """
        # Deliberately the same computation the real run uses. A plan built from
        # a looser notion of "pending" than the run itself is how an operator
        # gets told 40 targets are left and then watches 5 reports happen.
        queue = self._work_queue()
        self_reporting = self._targets.self_reporting(
            [account.username for account in self._pool.accounts]
        )
        return RunPlan(
            run_id=self._store.run_id,
            targets=len(self._targets),
            pending=len(queue),
            already_settled=len(self._targets) - len(queue),
            channels=tuple(spec.name for spec in self._channels),
            capacity=sum(spec.capacity for spec in self._channels),
            max_reports=self._options.max_reports,
            horizon_seconds=self._options.horizon_seconds,
            max_concurrent=self._options.max_concurrent,
            workers=self._worker_count(),
            accounts=tuple(
                account.status(self._monotonic()) for account in self._pool.accounts
            ),
            self_reporting=tuple(target.key for target in self_reporting),
        )

    async def run(self) -> RunReport:
        if not self._channels:
            raise NoChannelsAvailable(
                "no channels are configured; nothing could be attempted",
                scope=ErrorScope.RUN,
            )
        started_at = utc_now()
        if self._options.dry_run:
            return self._dry_report(started_at)

        self._deadline = self._monotonic() + self._options.horizon_seconds
        try:
            with self._store.exclusive():
                # Opened *inside* the lock. Opening first would let a second
                # process create and append to the same ledger before the lock
                # was held, and the two would interleave records into one file
                # with no way to tell which run wrote which.
                self._store.open()
                try:
                    self._reconcile()
                    await self._drive()
                finally:
                    self._store.close()
        finally:
            await self._close_channels()
        return self._report(started_at)

    # -- reconciliation --------------------------------------------------

    def _reconcile(self) -> None:
        """Settle anything dispatched without an outcome, before any new work.

        This is ``--resume``'s whole job. Those targets may or may not have been
        reported; the tool cannot find out, so the checkpoint records UNKNOWN,
        marks them settled, and never touches them again. Doing it first means a
        resumed run cannot even *see* them as candidates.
        """
        reconciled = self._store.reconcile_pending()
        if not reconciled:
            return
        for outcome in reconciled:
            # Counted in this run's summary, not just the ledger. These targets
            # are part of what the operator asked this run to handle, and a
            # summary that silently dropped them would understate the problem.
            self._tally_one(outcome)
        log.warning(
            "%d target(s) were dispatched with no recorded outcome; marking them "
            "UNKNOWN. They will not be retried -- a human decides.",
            len(reconciled),
        )

    # -- the run ---------------------------------------------------------

    async def _drive(self) -> None:
        queue = self._work_queue()
        if not queue:
            log.info("no targets pending; nothing to do")
            return

        workers = self._worker_count()
        if workers <= 0:
            self.request_abort("no eligible account is available")
            return

        self._slots = [
            _Slot(
                index,
                pool=self._pool,
                proxies=self._proxies,
                held=self._held,
                monotonic=self._monotonic,
            )
            for index in range(workers)
        ]
        log.info(
            "starting %d worker(s) over %d target(s); channel capacity %d",
            workers,
            len(queue),
            sum(spec.capacity for spec in self._channels),
        )
        # ONE iterator shared by every worker, not the list handed to each of
        # them. This is the entire distribution mechanism, and it needs no lock
        # for a reason worth stating: pulling from a list iterator does not
        # await, and asyncio only switches tasks at await points, so two workers
        # cannot be handed the same target. Handing each worker the list
        # instead -- which looks equivalent and is not -- gives every worker
        # every target, and the first pair to collide is a DoubleDispatch.
        work = iter(queue)
        tasks = [
            asyncio.create_task(self._worker(slot, work), name=f"worker-{slot.index}")
            for slot in self._slots
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for slot in self._slots:
                slot.release()

    def _work_queue(self) -> list[Target]:
        """Targets worth attempting, with the settled ones already removed.

        Order is the file's order. A run that reshuffled its list would make the
        ledger hard to read against the file the operator is looking at, and no
        throughput argument beats that.
        """
        out: list[Target] = []
        skipped: list[str] = []
        for target in self._targets.pending():
            reason = self._store.state.skip_reason(target.key)
            if reason is None:
                out.append(target)
            else:
                skipped.append(f"{target.key} ({reason.value})")
        if skipped:
            log.info("skipping %d already-settled target(s)", len(skipped))
        return out

    def _worker_count(self) -> int:
        """How many concurrent workers to actually start.

        Bounded by three things. Eligible accounts, because every worker needs
        its own identity -- two workers sharing one account is two sessions on
        one exit with one budget between them, which is the correlation the
        pool exists to prevent. The requested ``max_concurrent``. And the
        channels' own capacity, so the runner does not start four workers
        against a browser that can serve one page.
        """
        capacity = sum(
            spec.capacity
            for spec in self._channels
            if not self._health[spec.name].disabled
        )
        if capacity <= 0:
            return 0
        eligible = len(self._pool.eligible(self._monotonic()))
        return max(0, min(self._options.max_concurrent, eligible, capacity))

    async def _worker(self, slot: _Slot, work: Iterator[Target]) -> None:
        """Pull targets off the shared iterator until it runs dry or the run stops.

        A worker that breaks early does not hand its backlog to anyone. The
        targets it never pulled stay unattempted, and an unattempted target is
        safe: nothing was dispatched for it, so the next run may have it.
        """
        pacer = self._pacer_factory()
        for target in work:
            if self._abort.is_set():
                break
            if self._at_cap():
                self.request_abort(
                    f"per-run cap of {self._options.max_reports} dispatch(es) reached"
                )
                break
            try:
                slot.bind()
            except (NoEligibleAccount, ProxyUnavailable) as exc:
                # A wait, not a failure, when there is something to wait for;
                # a stop, when there is not. Either way the run says which.
                self.request_abort(f"could not bind a lease: {exc}")
                break

            if not await self._pace(pacer, slot):
                break

            self._considered += 1
            outcome = await self._attempt(slot, target, pacer)
            if outcome is None:
                # A refusal, not a failure. Nothing dispatched, so the account
                # and the ledger are untouched.
                continue

            self._settle(target, outcome)
            self._note_account(slot, outcome)
            if self._on_progress is not None:
                self._on_progress(outcome)

            if outcome.needs_human_review:
                # Do not keep going quietly. The operator is the only person
                # who can resolve an UNKNOWN, and a run that files a thousand
                # more reports while the first ten are unresolved is a run that
                # has stopped listening.
                self.request_abort(
                    f"{outcome.target_ref} is {outcome.terminal.value} and needs a "
                    "human; stopping the run here rather than compounding it"
                )
                break

    def _at_cap(self) -> bool:
        cap = self._options.max_reports
        return cap is not None and self._dispatches >= cap

    async def _pace(self, pacer: Pacer, slot: _Slot) -> bool:
        """Wait out the gap before dispatching. ``False`` means stop this worker."""
        if slot.budget_remaining() <= 0:
            # Unreachable in practice: ``bind`` only hands back an eligible
            # account and an eligible account has budget left. Handled anyway,
            # because the alternative to handling it is a divide-by-nothing
            # pacing calculation that silently becomes "report as fast as
            # possible" -- the exact failure pacing exists to prevent.
            log.warning(
                "account %s is out of budget after binding; rotating",
                slot.account_ref,
            )
            slot.release()
            try:
                slot.bind()
            except (NoEligibleAccount, ProxyUnavailable) as exc:
                self.request_abort(f"no account has budget left: {exc}")
                return False
            if slot.budget_remaining() <= 0:
                self.request_abort("no account has budget left")
                return False

        horizon_left = self._horizon_left()
        if horizon_left <= 0:
            self.request_abort(
                f"run horizon of {self._options.horizon_seconds:.0f}s reached"
            )
            return False

        await pacer.async_wait(
            budget_remaining=slot.budget_remaining(),
            budget_total=slot.budget_total(),
            horizon_remaining=horizon_left,
            sleeper=self._wait,
        )
        return not self._abort.is_set()

    def _horizon_left(self) -> float:
        return self._deadline - self._monotonic()

    # -- one target ------------------------------------------------------

    async def _attempt(
        self, slot: _Slot, target: Target, pacer: Pacer
    ) -> Outcome | None:
        """Run the ladder for one target. ``None`` means never attempted."""
        if self._pool.would_self_report(target.handle):
            self._refuse(
                target,
                "this is one of our own reporting accounts. Filing it would lock "
                "the identity doing the filing.",
                fatal_to_run=True,
            )
            return None

        problems = target.validate()
        if problems:
            # Not fatal to the run, and the difference is deliberate. A handle
            # that fails validation provably is not the operator's own account,
            # so it is a bad line rather than a bad list. The operator fixes the
            # line; the rest of the list is still trustworthy.
            self._refuse(target, "; ".join(problems), fatal_to_run=False)
            return None

        if not self.live_channels():
            self.request_abort("every channel is disabled; nothing is left to try")
            return None

        last: Outcome | None = None
        for spec in self.live_channels():
            if self._abort.is_set():
                break
            outcome = await self._try_channel(slot, target, spec, pacer)
            last = outcome
            if outcome.stops_ladder:
                return outcome
        return last

    def _refuse(self, target: Target, reason: str, *, fatal_to_run: bool) -> None:
        refusal = Refusal(
            target_key=target.key,
            display=target.escaped(),
            reason=reason,
            fatal_to_run=fatal_to_run,
        )
        self._refusals.append(refusal)
        log.error("REFUSING %s: %s", target.escaped(), reason)
        if fatal_to_run:
            # A list containing one of your own handles cannot be vouched for,
            # and the cost of working out which other entries are also wrong is
            # a locked reporting account. One bad line costs the operator a
            # re-edit and a --resume; a wrong report costs them the identity.
            self.request_abort(
                f"{target.escaped()} is a self-report; the target list cannot be "
                "trusted with the rest of the run"
            )

    # -- one channel -----------------------------------------------------

    async def _try_channel(
        self, slot: _Slot, target: Target, spec: ChannelSpec, pacer: Pacer
    ) -> Outcome:
        """One channel, with its rotations and its retries. Always terminal.

        Two loops, not one, because they are two different problems. The outer
        one is about the *binding*: the provider moved the exit out from under a
        live session, and the remedy is a different address rather than another
        try at the same one. The inner one is about the *channel*: it is
        refusing to serve, and the remedy may be a moment's patience.
        """
        health = self._health[spec.name]
        stale: ProxyUnavailable | None = None
        for rotation in range(self._options.exit_rotations + 1):
            try:
                outcome = await self._attempt_channel(slot, target, spec, pacer)
            except ProxyUnavailable as exc:
                stale = exc
                if rotation < self._options.exit_rotations and self._rotate_exit(
                    slot, target, spec, exc
                ):
                    continue
                # Out of rotations, or nothing left to rotate to. Graded below.
                break
            self._finish_channel(health, outcome)
            return outcome

        # Only reachable when a stale binding could not be replaced. The exit is
        # the reason this target failed, not the channel, so it is charged to the
        # channel only as a non-dispatch -- the streak is what escalates, and the
        # ladder still has somewhere to go.
        assert stale is not None
        outcome = self._outcome_from_error(slot, target, spec, stale, attempt=0)
        self._finish_channel(health, outcome)
        return outcome

    async def _attempt_channel(
        self,
        slot: _Slot,
        target: Target,
        spec: ChannelSpec,
        pacer: Pacer,
    ) -> Outcome:
        """Attempts of one channel against one binding. Raises ``ProxyUnavailable``
        if the binding turns out to be unusable, for the rotation loop to handle."""
        attempt = 0
        while True:
            attempt += 1
            try:
                return await self._call(slot, target, spec, attempt, pacer)
            except (_BoundaryFailure, ProxyUnavailable):
                # Not graded here. A boundary failure is the disk's problem and
                # ends the run; a stale binding is the scheduler's and belongs
                # one level up, where a replacement can be obtained.
                raise
            except InstaReportError as exc:
                outcome = self._outcome_from_error(slot, target, spec, exc, attempt)
                self._apply_scope(exc)
                if not self._retryable(exc, target, attempt):
                    break
                delay = self._options.backoff_seconds * (2 ** (attempt - 1))
                log.warning(
                    "%s: %s hit a transient failure (%s); retrying in %.0fs",
                    target.key,
                    spec.name,
                    exc,
                    delay,
                )
                if not await self._nap(delay):
                    break
                continue
            except Exception as exc:  # noqa: BLE001
                # A bug in the channel, not a condition the taxonomy describes.
                # Graded once and never retried: repeating a bug produces three
                # identical tracebacks instead of one, and the ladder still has
                # somewhere to go because nothing was dispatched.
                log.exception("%s: %s raised an unclassified error", target.key, spec.name)
                outcome = self._outcome_from_error(slot, target, spec, exc, attempt)
                break
            break
        return outcome

    def _finish_channel(self, health: ChannelHealth, outcome: Outcome) -> None:
        """Charge the outcome to the channel, and retire a channel that is done."""
        health.note(outcome)
        self._retire_if_broken(health)

    def _rotate_exit(
        self, slot: _Slot, target: Target, spec: ChannelSpec, exc: ProxyUnavailable
    ) -> bool:
        """Replace a binding the provider has moved, and report whether we got one.

        This is a rotation, not a retry, and the difference is why it is allowed
        at all. Nothing was dispatched -- the freshness assertion runs before the
        channel is handed anything -- so no report can have landed and no attempt
        is consumed. Retrying the same channel against the same expired address
        would fail identically every time, which is how a target ends up
        CHANNEL_FAILED on a rung that was never given a fair chance.
        """
        log.warning(
            "%s: %s -- the binding is stale, not the channel. Rotating the exit.",
            target.key,
            exc,
        )
        try:
            slot.bind()
        except (NoEligibleAccount, ProxyUnavailable) as bind_error:
            log.error(
                "%s: no replacement exit available (%s); the target is not servable "
                "on this channel right now",
                target.key,
                bind_error,
            )
            return False
        log.info("%s: rotated to %s", target.key, slot.describe())
        return True

    def _retryable(self, exc: InstaReportError, target: Target, attempt: int) -> bool:
        """Whether the *same* channel may be tried again.

        Two conditions, and the second is the one that protects the target. A
        transient is only retryable while nothing has been dispatched: once the
        intent is on disk the report may already have landed, and a second
        attempt is how one target becomes two reports.

        ``attempt`` is 1-based, so ``transient_retries=2`` allows attempts 1 and
        2 and gives up on the third.
        """
        if not isinstance(exc, TransientError):
            return False
        if self._store.state.has_intent(target.key):
            return False
        return attempt <= self._options.transient_retries

    async def _call(
        self,
        slot: _Slot,
        target: Target,
        spec: ChannelSpec,
        attempt: int,
        pacer: Pacer,
    ) -> Outcome:
        """The dispatch boundary, expressed as a callable the channel must call."""
        proxy_lease = slot.proxy_lease

        # Asserted before the channel is handed anything, not after. A stale
        # exit discovered mid-wizard means a report assembled on one IP and sent
        # from another -- and by then the intent is already written, so the only
        # available verdict would be UNKNOWN.
        if proxy_lease is not None and self._proxies is not None:
            self._proxies.assert_lease_fresh(proxy_lease)

        dispatched = False

        def on_dispatch() -> None:
            nonlocal dispatched
            if dispatched:
                raise RunAborted(
                    f"{target.key}: {spec.name} asked to dispatch twice. A second "
                    "click is how one target becomes two reports.",
                    scope=ErrorScope.RUN,
                )
            intent = Intent(
                run_id=self._store.run_id,
                target_ref=target.key,
                account_ref=slot.account_ref or "",
                lease_id=proxy_lease.lease_id if proxy_lease else "direct",
                channel=spec.name,
                attempt=attempt,
                resolved_user_id=target.user_id,
            )
            # The write. Until it returns, nothing has been sent and nothing is
            # charged; if it raises, the click downstream never happens.
            #
            # Wrapped rather than allowed through raw, because the two failure
            # modes look identical from inside the channel and mean opposite
            # things. A boundary write that fails is the disk's problem, not the
            # channel's, and _try_channel must be able to tell them apart.
            try:
                self._store.record_intent(intent)
            except Exception as write_error:  # noqa: BLE001
                raise _BoundaryFailure(
                    f"{target.key}: could not record the dispatch intent, so "
                    f"nothing was sent. {type(write_error).__name__}: "
                    f"{write_error}. The run stopped rather than report a target "
                    "it could not record."
                ) from write_error
            dispatched = True
            # Charged only now. Billing before the fsync would spend part of an
            # account's allowance on a report that provably never went out --
            # the silent-drop bug in a different costume.
            if slot.lease is not None:
                self._pool.note_dispatch(slot.lease)
            slot.reports_used += 1
            self._dispatches += 1
            # health.dispatches is *not* bumped here. ChannelHealth counts it
            # from the returned outcome, which is the only place that knows
            # whether the dispatch actually completed. Counting it at the
            # boundary as well would double every successful report and make
            # the per-channel number disagree with the run's.
            pacer.note_dispatch()

        return await spec.channel.report(
            target,
            on_dispatch=on_dispatch,
            account_ref=slot.account_ref,
            lease=proxy_lease,
            attempt=attempt,
        )

    def _outcome_from_error(
        self,
        slot: _Slot,
        target: Target,
        spec: ChannelSpec,
        exc: BaseException,
        attempt: int,
    ) -> Outcome:
        """Grade a raised error, so a raise never becomes the absence of a result.

        The split is the dispatch boundary. Before it, a raised error means the
        channel could not serve and the ladder may move on. After it, the report
        may have landed, so the only honest state is UNKNOWN and the only
        correct action is to stop.

        Accepts any exception, not just the taxonomy, because a channel bug must
        still leave a record. What it cannot do is pretend to know which sort it
        is: an unclassified error is graded by the same boundary rule, and
        carries no error scope, because nothing declared one.
        """
        name = type(exc).__name__
        dispatched = self._store.state.has_intent(target.key)
        if dispatched:
            terminal = TerminalState.UNKNOWN
            detail = f"{name} after dispatch: {exc}"
        elif isinstance(exc, (AccountChallenged, SessionExpired)):
            terminal = TerminalState.QUARANTINED
            detail = str(exc)
        elif isinstance(exc, ChannelFailError):
            terminal = TerminalState.CHANNEL_FAILED
            detail = f"{name}: {exc}"
        else:
            terminal = TerminalState.CHANNEL_FAILED
            detail = f"{name} before dispatch: {exc}"
        scope = getattr(exc, "scope", None)
        return Outcome(
            terminal=terminal,
            target_ref=target.key,
            channel=spec.name,
            account_ref=slot.account_ref,
            lease_id=slot.proxy_lease.lease_id if slot.proxy_lease else None,
            attempt=attempt,
            error_class=name,
            error_scope=getattr(scope, "value", None),
            dispatched_at=utc_now() if dispatched else None,
            detail=detail,
        )

    def _apply_scope(self, exc: InstaReportError) -> None:
        """Honour the error's scope. Report- and lease-scoped errors continue."""
        if exc.scope is not ErrorScope.RUN:
            return
        self._fatal = exc
        self.request_abort(f"fatal ({type(exc).__name__}): {exc}")

    def _retire_if_broken(self, health: ChannelHealth) -> None:
        threshold = self._options.channel_failure_threshold
        if not health.disabled and health.consecutive_channel_failures >= threshold:
            health.disable(
                f"{health.consecutive_channel_failures} consecutive attempts failed "
                f"without dispatching anything (threshold {threshold})"
            )

    # -- bookkeeping -----------------------------------------------------

    def _tally_one(self, outcome: Outcome) -> None:
        key = outcome.terminal.value
        self._tally[key] = self._tally.get(key, 0) + 1

    def _settle(self, target: Target, outcome: Outcome) -> None:
        """Append the outcome, and keep the one-intent-one-outcome rule."""
        settled = replace(
            outcome, target_ref=target.key, finished_at=outcome.finished_at or utc_now()
        )
        # Tallied before any of the bookkeeping below, so a state the run has to
        # reject still shows up in the summary. A run that stopped because a
        # channel lied about dispatching needs to say so, and it cannot say so
        # by raising alone.
        self._tally_one(settled)
        if not self._store.state.has_intent(target.key):
            if settled.terminal in _DISPATCHED_TERMINALS:
                # A channel claimed a report went out, and there is no record
                # that it was even attempted. Writing nothing would leave the
                # target unattempted and free to be filed again; writing
                # something would invent a dispatch. Neither is acceptable, so
                # the run stops and says which channel said what.
                raise RunAborted(
                    f"{target.key}: {settled.terminal.value} from "
                    f"{settled.channel or 'a channel'} with no intent on disk. "
                    "A channel reported a dispatched terminal state for a report "
                    "it never dispatched. The run stopped rather than risk "
                    "reporting this target twice.",
                    scope=ErrorScope.RUN,
                )
            # A state that does not assert a dispatch, with nothing dispatched.
            # Not written, deliberately: the ledger exists to answer "may a
            # report have landed?", and for this target the answer is a
            # provable no. A later run is free to try it again, which is what
            # you want -- a 404 today is a 404, not a verdict, and a channel
            # that was down at 3am should be free to work at 9.
            return
        self._store.record_outcome(settled)

    def _note_account(self, slot: _Slot, outcome: Outcome) -> None:
        """Apply the outcome to the account's health and to its lease."""
        if slot.lease is None:
            return
        if outcome.terminal is TerminalState.QUARANTINED:
            if outcome.error_class == "SessionExpired":
                self._pool.note_session_expired(slot.lease)
            else:
                self._pool.note_challenge(slot.lease, outcome.detail or outcome.terminal.value)
            # The binding is gone. A challenged account that keeps its exit
            # hands the next worker a clean IP to repeat the same mistake from.
            slot.retire()
            return
        self._pool.note_outcome(slot.lease, outcome)

    async def _nap(self, seconds: float) -> bool:
        return await self._wait(seconds)

    async def _close_channels(self) -> None:
        for spec in self._channels:
            try:
                await spec.channel.aclose()
            except Exception:  # noqa: BLE001
                # A channel that will not close is usually a browser that will
                # not exit. Not fatal: the run's accounting is already durable,
                # and refusing to print the summary over a stuck handle loses
                # the part the operator needs.
                log.warning(
                    "channel %s did not close cleanly", spec.name, exc_info=True
                )

    # -- reporting -------------------------------------------------------

    def _dry_report(self, started_at: datetime) -> RunReport:
        return RunReport(
            run_id=self._store.run_id,
            started_at=started_at,
            finished_at=utc_now(),
            dispatches=0,
            targets_considered=len(self._targets),
            counts={},
            refusals=(),
            channel_names=tuple(spec.name for spec in self._channels),
            health=tuple(self._health.values()),
            review=(),
            unsettled=(),
            aborted=False,
            dry_run=True,
        )

    def _report(self, started_at: datetime) -> RunReport:
        state = self._store.state
        unsettled = tuple(sorted(state.pending))
        if unsettled:
            # Every one of these may or may not have been reported. This is the
            # single condition that makes a run's headline number untrustworthy,
            # so it is not allowed to pass quietly.
            self._errors.append(
                f"{len(unsettled)} target(s) were dispatched with no recorded "
                "outcome; they are UNKNOWN and will not be retried"
            )
        if self._fatal is not None:
            self._errors.append(f"{type(self._fatal).__name__}: {self._fatal}")
        else:
            self._errors.extend(
                f"channel {name} was retired: {health.disabled_reason}"
                for name, health in self._health.items()
                if health.disabled
            )

        return RunReport(
            run_id=self._store.run_id,
            started_at=started_at,
            finished_at=utc_now(),
            dispatches=self._dispatches,
            targets_considered=self._considered,
            counts=dict(self._tally),
            refusals=tuple(self._refusals),
            channel_names=tuple(spec.name for spec in self._channels),
            health=tuple(self._health.values()),
            review=tuple(state.outcomes_needing_review()),
            unsettled=unsettled,
            aborted=self._abort.is_set(),
            abort_reason=self._abort_reason,
            dry_run=self._options.dry_run,
            errors=tuple(self._errors),
        )
