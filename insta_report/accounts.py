"""Account model and pool.

Two rules shape everything here, and both exist because of how report weight
actually works.

**A report's weight comes from a trusted account filing correctly-categorized
reports over time, not from volume.** Instagram discounts coordinated batches.
That is why this module is built to *remove* failing accounts from rotation
rather than to shift their load onto the ones still working: concentrating load
on a good account to compensate for a bad one produces exactly the burst
pattern the design exists to avoid. So a quarantined account is waited out, never
replaced in place, and if every account is unavailable the pool refuses to lease
rather than returning the least-bad one.

**Account-to-IP affinity is a correctness invariant, not a preference.** A
report is a sequence of requests, and a sequence that starts on one IP and ends
on another is the exact shape of a hijacked session. The rotation unit is
therefore the *lease* -- an (account, IP) pair held for several reports -- not
the report. A lease is only ever bound to one account, and an account's IP does
not change while its lease is live.

Clock discipline: every *interval* here uses :func:`time.monotonic`. An NTP
correction or a DST change must not double a quarantine or un-quarantine one
early. The single exception is the calendar-day budget reset, which is the one
thing that genuinely needs wall-clock time -- monotonic cannot know what day it
is -- and it is isolated in :meth:`Account.roll_day_if_needed` so the exception
is visible rather than spread around.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Iterable, Iterator, Sequence

from .errors import AccountChallenged, NoEligibleAccount, ReportBudgetExhausted
from .outcomes import Outcome, TerminalState

__all__ = [
    "Account",
    "AccountPool",
    "Lease",
    "DEFAULT_QUARANTINE_BASE_SECONDS",
    "MAX_QUARANTINE_SECONDS",
]

log = logging.getLogger(__name__)

#: First failure waits this long. Doubles per consecutive failure, capped.
DEFAULT_QUARANTINE_BASE_SECONDS = 300.0
#: Ten hours. Long enough that a genuinely flagged account is not retried inside
#: the same attended session, which is the only window we care about.
MAX_QUARANTINE_SECONDS = 36_000.0

#: Reports a lease may carry before it must be re-bound. Holding a residential
#: IP for many hours is what a provider's sticky TTL is for; holding it for the
#: whole run from one account is what a timeout is for.
DEFAULT_LEASE_REPORTS = 10


def _default_wall_clock() -> date:
    return date.today()


@dataclass
class Account:
    """One reporting identity, plus the runtime state the pool needs.

    ``sessionid`` is a live credential. It is never logged, never put in an
    exception message, and never serialised into a checkpoint -- the account is
    referenced by ``ref`` everywhere a record is written.
    """

    ref: str
    username: str
    sessionid: str = field(repr=False, default="")
    daily_budget: int = 20
    enabled: bool = True

    # -- runtime state -------------------------------------------------
    used_today: int = 0
    day: date = field(default_factory=_default_wall_clock)
    last_report_monotonic: float | None = None
    quarantined_until: float | None = None
    consecutive_failures: int = 0
    total_reports: int = 0
    total_acked: int = 0
    #: Human-readable reason the account is currently out of rotation.
    sidelined_for: str | None = None

    # -- budget --------------------------------------------------------

    def roll_day_if_needed(self, today: date) -> bool:
        """Reset the daily budget when the calendar day turns over.

        The one place wall-clock time is consulted. Isolated here so that
        everything else in this module can stay strictly monotonic.
        """
        if self.day == today:
            return False
        self.day = today
        self.used_today = 0
        return True

    @property
    def remaining(self) -> int:
        return max(0, self.daily_budget - self.used_today)

    @property
    def budget_spent(self) -> bool:
        return self.used_today >= self.daily_budget

    # -- eligibility ---------------------------------------------------

    def quarantined_at(self, now: float) -> bool:
        return self.quarantined_until is not None and self.quarantined_until > now

    def quarantine_remaining(self, now: float) -> float:
        if self.quarantined_until is None:
            return 0.0
        return max(0.0, self.quarantined_until - now)

    def eligible(self, now: float) -> bool:
        """Whether this account may be handed out right now.

        Checked in cheapest-first order. Every condition here is a reason the
        account would fail, and a failure costs more than an idle account.
        """
        return (
            self.enabled
            and not self.budget_spent
            and not self.quarantined_at(now)
        )

    def why_ineligible(self, now: float) -> str | None:
        """Operator-facing reason. Reported rather than silently skipped."""
        if not self.enabled:
            return "disabled in config"
        if self.budget_spent:
            return f"daily budget spent ({self.used_today}/{self.daily_budget})"
        if self.quarantined_at(now):
            return f"quarantined for another {self.quarantine_remaining(now):.0f}s"
        return None

    # -- accounting ----------------------------------------------------

    def count_dispatch(self) -> None:
        """Charge one report. Called for every dispatch, acked or not.

        A report that went out has cost a report whatever the response said.
        Not charging an UNKNOWN would let a run send the same target repeatedly
        while the site silently discards them.
        """
        self.used_today += 1
        self.total_reports += 1

    def count_ack(self) -> None:
        self.total_acked += 1

    def record_success(self) -> None:
        """A confirmed ack clears the failure streak."""
        self.consecutive_failures = 0
        self.quarantined_until = None
        self.sidelined_for = None

    def record_failure(
        self,
        reason: str,
        now: float,
        *,
        base: float = DEFAULT_QUARANTINE_BASE_SECONDS,
        cap: float = MAX_QUARANTINE_SECONDS,
    ) -> float:
        """Quarantine with exponential backoff. Returns the wait in seconds.

        The streak is *not* cleared by the pool for you: only a real ack clears
        it. A run full of failures must not look healthy again because the pool
        got bored.
        """
        self.consecutive_failures += 1
        wait = min(cap, base * (2 ** (self.consecutive_failures - 1)))
        self.quarantined_until = now + wait
        self.sidelined_for = reason
        log.warning(
            "account %s quarantined for %.0fs after failure %d: %s",
            self.ref,
            wait,
            self.consecutive_failures,
            reason,
        )
        return wait

    def status(self, now: float) -> dict[str, object]:
        """Diagnostics. Never includes the sessionid."""
        return {
            "ref": self.ref,
            "username": self.username,
            "enabled": self.enabled,
            "used_today": self.used_today,
            "daily_budget": self.daily_budget,
            "remaining": self.remaining,
            "total_reports": self.total_reports,
            "total_acked": self.total_acked,
            "consecutive_failures": self.consecutive_failures,
            "quarantined_for": round(self.quarantine_remaining(now), 1),
            "sidelined_for": self.sidelined_for,
        }


@dataclass(frozen=True)
class Lease:
    """An (account, IP) pairing, valid for several reports.

    Frozen on purpose. A lease that could be mutated after a report was
    dispatched would make the affinity invariant unenforceable -- the pool
    would have no way to tell that an in-flight report was sent under a
    different binding than the one it recorded.
    """

    lease_id: str
    account_ref: str
    proxy: str | None
    max_reports: int = DEFAULT_LEASE_REPORTS

    def exhausted(self, reports_used: int) -> bool:
        return reports_used >= self.max_reports


class AccountPool:
    """Hands out leases, enforces affinity, and keeps bad accounts out.

    Construct with an injectable monotonic clock so tests can drive quarantine
    and budget windows without sleeping.
    """

    def __init__(
        self,
        accounts: Iterable[Account],
        *,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], date] = _default_wall_clock,
        quarantine_base: float = DEFAULT_QUARANTINE_BASE_SECONDS,
        quarantine_cap: float = MAX_QUARANTINE_SECONDS,
        lease_reports: int = DEFAULT_LEASE_REPORTS,
    ) -> None:
        self._accounts: dict[str, Account] = {}
        for account in accounts:
            if account.ref in self._accounts:
                # Config parsing rejects duplicate refs, so reaching this means
                # a caller built the pool by hand with a mistake. Silence here
                # would run two identities under one name and misattribute
                # every outcome.
                raise ValueError(f"duplicate account ref {account.ref!r}")
            self._accounts[account.ref] = account
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._quarantine_base = quarantine_base
        self._quarantine_cap = quarantine_cap
        self._lease_reports = lease_reports
        #: account_ref -> the live lease. One lease per account, always.
        self._leases: dict[str, Lease] = {}
        #: lease_id -> reports already dispatched under it.
        self._lease_uses: dict[str, int] = {}
        self._retired: list[Lease] = []

    # -- inspection -----------------------------------------------------

    @property
    def accounts(self) -> tuple[Account, ...]:
        return tuple(self._accounts.values())

    def get(self, ref: str) -> Account:
        return self._accounts[ref]

    def status(self) -> list[dict[str, object]]:
        now = self._monotonic()
        self._roll_day()
        return [a.status(now) for a in self._accounts.values()]

    def _roll_day(self) -> None:
        today = self._wall_clock()
        for account in self._accounts.values():
            if account.roll_day_if_needed(today):
                log.info("account %s: new day, daily budget reset to %d", account.ref, account.daily_budget)

    def eligible(self, now: float | None = None) -> list[Account]:
        now = self._monotonic() if now is None else now
        return [a for a in self._accounts.values() if a.eligible(now)]

    def would_self_report(self, username: str) -> bool:
        """True if *username* is one of our own reporting accounts.

        Checked before every dispatch. A report filed against your own account
        is not a wasted report, it is the fastest way to get the account
        challenged or locked, and it would do it to the identity doing the
        filing.
        """
        target = username.lstrip("@").strip().casefold()
        return any(a.username.casefold() == target for a in self._accounts.values())

    # -- leasing --------------------------------------------------------

    def lease(self, proxy_for: Callable[[Account], str | None] | None = None) -> Lease:
        """Acquire the least-recently-used eligible account and bind it.

        LRU rather than least-used-count: two accounts with equal counts should
        alternate, and picking by count alone would keep choosing whichever one
        happens to be first in the config, concentrating everything on it.

        Raises :class:`NoEligibleAccount` rather than returning the least-bad
        account. That refusal is the D4 guarantee in one line -- when every
        account is out, the run waits or stops, and never piles onto the one
        that still works.
        """
        self._roll_day()
        now = self._monotonic()

        candidates = self.eligible(now)
        if not candidates:
            raise NoEligibleAccount(self._ineligibility_report(now))

        def lru_key(account: Account) -> tuple[float, int, str]:
            # Accounts never used sort first (monotonic starts near boot, but
            # -inf is explicit and cannot be beaten by a real timestamp).
            last = (
                account.last_report_monotonic
                if account.last_report_monotonic is not None
                else float("-inf")
            )
            # total_reports breaks exact ties towards the least-used account.
            # Ordering by recency alone leaves ties to sort by name, which would
            # hand a frozen clock's worth of work to whichever account happens
            # to sort first -- the same concentration the primary key avoids.
            return (last, account.total_reports, account.ref)

        chosen = min(candidates, key=lru_key)
        lease = Lease(
            lease_id=uuid.uuid4().hex[:12],
            account_ref=chosen.ref,
            proxy=proxy_for(chosen) if proxy_for else None,
            max_reports=self._lease_reports,
        )
        self._leases[chosen.ref] = lease
        self._lease_uses[lease.lease_id] = 0
        log.debug("leased account %s on %s", chosen.ref, lease.proxy or "direct")
        return lease

    def _ineligibility_report(self, now: float) -> str:
        """Why nothing is available. This goes straight to the operator."""
        if not self._accounts:
            return "no accounts configured"
        lines = [f"no eligible account at t+{now:.0f}s:"]
        for account in self._accounts.values():
            reason = account.why_ineligible(now)
            lines.append(f"  {account.ref}: {reason or 'eligible (unexpected)'}")
        lines.append(
            "Waiting is correct here. Substituting the least-bad account would "
            "concentrate load on it, which is the pattern this design avoids."
        )
        return "\n".join(lines)

    def active_lease(self, account_ref: str) -> Lease | None:
        return self._leases.get(account_ref)

    def rebind(self, account_ref: str, proxy_for: Callable[[Account], str | None] | str | None) -> Lease:
        """End an account's current lease and bind a new one.

        Ordering is the point. The replacement is installed *before* the old
        lease id is retired, so the account is never momentarily unbound. An
        implementation that retires first would leave a window where a
        concurrent `lease()` could hand this account to a caller expecting a
        different IP -- or worse, bind nothing and let a report go out direct.
        """
        previous = self._leases.get(account_ref)
        new_proxy = (
            proxy_for(self._accounts[account_ref])
            if callable(proxy_for)
            else proxy_for
        )
        replacement = Lease(
            lease_id=uuid.uuid4().hex[:12],
            account_ref=account_ref,
            proxy=new_proxy,
            max_reports=self._lease_reports,
        )
        # 1. install the replacement -- from here the account is bound
        self._leases[account_ref] = replacement
        self._lease_uses[replacement.lease_id] = 0
        # 2. only now retire the old one
        if previous is not None:
            self._lease_uses.pop(previous.lease_id, None)
            self._retired.append(previous)
        return replacement

    def release(self, account_ref: str) -> None:
        """Drop an account's lease. Used on lease-scoped fatals only."""
        previous = self._leases.pop(account_ref, None)
        if previous is not None:
            self._lease_uses.pop(previous.lease_id, None)
            self._retired.append(previous)

    def release_lease(self, lease: Lease) -> None:
        """Release a specific lease, but only if it is still the live one.

        A lease that has already been superseded must not be able to unbind its
        account: the replacement is deliberately in force.
        """
        if self._leases.get(lease.account_ref) == lease:
            self._leases.pop(lease.account_ref, None)
            self._lease_uses.pop(lease.lease_id, None)
            self._retired.append(lease)

    # -- accounting -----------------------------------------------------

    def note_dispatch(self, lease: Lease) -> None:
        """Record that a report went out under *lease*.

        Charges the account and the lease. Called after the checkpoint intent is
        durable, because from that point the report is committed regardless of
        what happens next.
        """
        account = self._accounts[lease.account_ref]
        account.count_dispatch()
        now = self._monotonic()
        account.last_report_monotonic = now
        self._lease_uses[lease.lease_id] = self._lease_uses.get(lease.lease_id, 0) + 1

    def note_outcome(self, lease: Lease, outcome: Outcome) -> None:
        """Apply an outcome to the account's health.

        Only a *confirmed* ack clears a failure streak. An UNKNOWN or an
        UNCONFIRMED means we do not know whether the report landed, and treating
        that as success is how a challenged account stays in rotation.
        """
        account = self._accounts[lease.account_ref]
        if outcome.terminal is TerminalState.SUBMITTED_ACKED:
            account.count_ack()
            account.record_success()
            return

        if outcome.terminal is TerminalState.QUARANTINED:
            account.record_failure(
                outcome.detail or "quarantined",
                self._monotonic(),
                base=self._quarantine_base,
                cap=self._quarantine_cap,
            )
            self.release(lease.account_ref)
            return

        # Unattempted outcomes say nothing about the account's health. A
        # selector that will never work is not the account's fault, and
        # quarantining here would empty the pool for no reason.
        if not outcome.was_dispatched:
            return

        # A report went out and was not confirmed. The streak grows, but the
        # account is not sidelined: UNCONFIRMED and UNKNOWN are weak signals
        # and quarantining on them would empty the pool over responses we
        # cannot read. The streak is the thing that escalates if this persists.
        account.consecutive_failures += 1
        log.debug(
            "account %s: %s (failure streak %d, not quarantined)",
            account.ref,
            outcome.terminal.value,
            account.consecutive_failures,
        )

    def note_challenge(self, lease: Lease, detail: str) -> float:
        """A challenge is the strongest signal there is. Quarantine and rebind.

        Returns the quarantine wait. The account is not dropped -- a challenge
        is frequently a bad IP rather than a bad account -- but it does not keep
        its current binding either.
        """
        account = self._accounts[lease.account_ref]
        wait = account.record_failure(
            detail,
            self._monotonic(),
            base=self._quarantine_base,
            cap=self._quarantine_cap,
        )
        self.release(lease.account_ref)
        return wait

    def note_budget_exhausted(self, lease: Lease) -> None:
        """Called when an account reports its allowance is gone. Always fatal.

        In the original code an exhausted budget meant an option that was
        simply not actionable: the run continued, the report was silently
        dropped, and the count at the end included a report that never
        happened. Here it is an error, because a silent drop is exactly the
        false-success bug in a different costume.
        """
        account = self._accounts[lease.account_ref]
        raise ReportBudgetExhausted(
            f"account {account.ref} has used its daily budget "
            f"({account.used_today}/{account.daily_budget})"
        )

    def note_session_expired(self, lease: Lease) -> None:
        account = self._accounts[lease.account_ref]
        account.record_failure(
            "session expired",
            self._monotonic(),
            base=self._quarantine_base,
            cap=self._quarantine_cap,
        )
        self.release(lease.account_ref)

    # -- reporting ------------------------------------------------------

    def summary(self) -> str:
        now = self._monotonic()
        self._roll_day()
        lines = [f"{'ref':<12} {'used':>9} {'streak':>7} {'state':<28}"]
        lines.append("-" * 60)
        for account in self._accounts.values():
            state = account.why_ineligible(now) or "eligible"
            lines.append(
                f"{account.ref:<12} "
                f"{account.used_today:>4}/{account.daily_budget:<4} "
                f"{account.consecutive_failures:>7} "
                f"{state:<28}"
            )
        return "\n".join(lines)

    def __len__(self) -> int:
        return len(self._accounts)

    def __iter__(self) -> Iterator[Account]:
        return iter(self._accounts.values())
