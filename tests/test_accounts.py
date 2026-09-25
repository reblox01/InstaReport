"""Account pool invariants.

Three of these are properties rather than behaviours, and they are the reason
the module exists:

* **No concentration.** When nothing is eligible, the pool refuses. It does not
  return the least-bad account, because handing extra work to the one identity
  that still works is the burst pattern the whole design avoids (D4).
* **Lease affinity.** An account's IP does not change while its lease is live,
  and rebinding installs the replacement before retiring the old one so the
  account is never momentarily unbound.
* **Dispatch always costs budget.** Whether a report was acked is a question
  about Instagram's response; that it was *sent* is a fact about this tool.
"""

from __future__ import annotations

from datetime import date

import pytest

from insta_report.accounts import (
    DEFAULT_QUARANTINE_BASE_SECONDS,
    Account,
    AccountPool,
    Lease,
)
from insta_report.errors import (
    AccountChallenged,
    NoEligibleAccount,
    ReportBudgetExhausted,
)
from insta_report.outcomes import Outcome, TerminalState, utc_now


class FakeClock:
    """A monotonic clock the test drives. No sleeping, no wall time."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.advance(seconds)


def make_account(ref: str = "alpha", budget: int = 20, **kwargs) -> Account:
    kwargs.setdefault("username", f"{ref}_user")
    kwargs.setdefault("sessionid", "sessionid%3Anever-log-this-value")
    return Account(ref=ref, daily_budget=budget, **kwargs)


def make_pool(*accounts: Account, clock: FakeClock | None = None, **kwargs) -> AccountPool:
    """Build a pool over *accounts*, or a single default one if none are given.

    ``accounts or [make_account()]`` treats an explicitly empty call as "no
    argument given" and quietly invents an account, so a genuinely empty pool
    gets its own constructor below rather than a sentinel parameter here.
    """
    clock = clock or FakeClock()
    chosen = list(accounts) if accounts else [make_account()]
    return AccountPool(
        chosen,
        monotonic=clock,
        wall_clock=kwargs.pop("wall_clock", lambda: date(2026, 9, 25)),
        **kwargs,
    )


def empty_pool(clock: FakeClock | None = None) -> AccountPool:
    """A pool with no accounts at all -- a config error, but one we must report
    clearly rather than paper over by inventing a default identity."""
    return AccountPool(
        [],
        monotonic=clock or FakeClock(),
        wall_clock=lambda: date(2026, 9, 25),
    )


def now_of(pool: AccountPool) -> float:
    """The pool's own monotonic reading, so tests cannot drift out of step."""
    return pool._monotonic()  # noqa: SLF001 - test reads the injected clock


def outcome(
    terminal: TerminalState,
    target: str = "t1",
    *,
    dispatched: bool = True,
    detail: str = "",
) -> Outcome:
    """Build an Outcome for pool tests.

    ``dispatched`` is explicit because it decides account health: a report that
    was sent says something about the account, one that never left does not.
    Defaulting it to True silently made every outcome in the file look like a
    dispatch, which is exactly the mistake
    ``test_unattempted_outcomes_do_not_penalise_the_account`` was written to
    catch.
    """
    return Outcome(
        terminal=terminal,
        target_ref=target,
        channel="browser",
        account_ref="alpha",
        lease_id="l1",
        dispatched_at=utc_now() if dispatched else None,
        finished_at=utc_now(),
        detail=detail,
    )


# --- budget -----------------------------------------------------------------


def test_fresh_account_has_its_full_budget():
    account = make_account(budget=20)
    assert account.remaining == 20
    assert not account.budget_spent


def test_dispatch_consumes_budget():
    account = make_account(budget=3)
    for _ in range(3):
        account.count_dispatch()
    assert account.remaining == 0
    assert account.budget_spent


def test_budget_cannot_go_negative():
    account = make_account(budget=2)
    for _ in range(9):
        account.count_dispatch()
    assert account.remaining == 0


def test_budget_resets_on_a_new_day():
    """The one place wall-clock time is consulted."""
    account = make_account(budget=5)
    account.count_dispatch()
    account.count_dispatch()
    assert account.remaining == 3

    assert account.roll_day_if_needed(date(2026, 9, 26)) is True
    assert account.remaining == 5


def test_roll_day_on_the_same_day_is_a_no_op():
    account = make_account(budget=5)
    account.count_dispatch()
    assert account.roll_day_if_needed(date(2026, 9, 25)) is False
    assert account.remaining == 4


def test_rolled_account_becomes_eligible_again_uses_pool_clock():
    """A next-day run must not inherit yesterday's exhaustion."""
    pool = make_pool(make_account(budget=1))
    lease = pool.lease()
    pool.note_dispatch(lease)
    with pytest.raises(NoEligibleAccount):
        pool.lease()
    pool._wall_clock = lambda: date(2026, 9, 26)  # noqa: SLF001 - drives the clock
    assert pool.lease() is not None


# --- eligibility ------------------------------------------------------------


def test_disabled_account_is_never_eligible():
    account = make_account()
    account.enabled = False
    assert not account.eligible(0.0)
    assert account.why_ineligible(0.0) == "disabled in config"


def test_spent_account_is_never_eligible():
    account = make_account(budget=1)
    account.count_dispatch()
    assert not account.eligible(0.0)


def test_quarantined_account_is_not_eligible():
    account = make_account()
    account.record_failure("challenge", now=100.0, base=60.0)
    assert not account.eligible(120.0)
    assert account.eligible(161.0)


def test_why_ineligible_is_null_when_eligible():
    assert make_account().why_ineligible(0.0) is None


# --- no concentration ------------------------------------------------------


def test_pool_refuses_rather_than_concentrating():
    """D4. The refusal is the guarantee.

    Returning the least-bad account here would send every report one identity
    still works, which is precisely the burst that gets an account flagged.
    """
    clock = FakeClock()
    pool = make_pool(make_account("a"), make_account("b"), clock=clock)
    for account in pool:
        account.record_failure("bad", now=clock.now, base=100.0)

    with pytest.raises(NoEligibleAccount) as excinfo:
        pool.lease()

    message = str(excinfo.value)
    assert "no eligible account" in message
    assert "would concentrate load" in message
    assert "a:" in message and "b:" in message


def test_refusal_names_each_accounts_reason():
    pool = make_pool(make_account("spent", budget=1), make_account("cool", enabled=False))
    lease = pool.lease()
    pool.note_dispatch(lease)

    with pytest.raises(NoEligibleAccount) as excinfo:
        pool.lease()
    message = str(excinfo.value)
    assert "daily budget spent" in message
    assert "disabled in config" in message


def test_empty_pool_refuses_clearly():
    with pytest.raises(NoEligibleAccount, match="no accounts configured"):
        empty_pool().lease()


def test_a_pool_built_with_no_accounts_contains_none():
    assert len(empty_pool()) == 0


def test_one_good_account_still_gets_used_while_another_is_sidelined():
    """Sidelining removes a bad account; it does not silence the pool."""
    clock = FakeClock()
    pool = make_pool(make_account("bad"), make_account("good"), clock=clock)
    pool.get("bad").record_failure("bad", now=clock.now, base=10_000.0)

    lease = pool.lease()
    assert lease.account_ref == "good"


# --- LRU --------------------------------------------------------------------


def test_least_recently_used_is_chosen_not_first_in_config():
    """Two accounts must alternate, not drain the first one.

    Leasing alone does not alternate -- only a dispatch advances recency,
    because holding a lease is not using the account. The clock advances
    between dispatches because that is what real dispatch latency does;
    ``test_exact_ties_resolve_towards_the_least_used`` covers the frozen case.
    """
    clock = FakeClock()
    pool = make_pool(make_account("first"), make_account("second"), clock=clock)
    chosen = []
    for _ in range(4):
        lease = pool.lease()
        pool.note_dispatch(lease)
        clock.advance(0.01)
        chosen.append(lease.account_ref)
    assert chosen == ["first", "second", "first", "second"]


def test_exact_ties_resolve_towards_the_least_used():
    """Under a frozen clock two accounts can be genuinely tied.

    Recency alone would leave that to sort by name, handing the tied window to
    whichever account happens to sort first. The secondary key on total_reports
    resolves it towards the account that has done less, which is the same
    anti-concentration rule the primary key follows.
    """
    clock = FakeClock()  # never advances
    pool = make_pool(make_account("aaa"), make_account("zzz"), clock=clock)

    pool.get("zzz").total_reports = 0
    lease = pool.lease()  # tie at (-inf, 0) -> alphabetical 'aaa'
    assert lease.account_ref == "aaa"

    # Now 'aaa' is used once and 'zzz' zero times, at the same instant.
    pool.note_dispatch(lease)
    assert pool.lease().account_ref == "zzz"


def test_lru_prefers_the_older_account_after_a_dispatch():
    clock = FakeClock()
    pool = make_pool(make_account("a"), make_account("b"), clock=clock)
    first = pool.lease()
    pool.note_dispatch(first)
    clock.advance(5)

    # 'a' was just used, so 'b' should get the next one.
    assert pool.lease().account_ref == "b"


def test_a_never_used_account_is_preferred_over_a_recently_used_one():
    clock = FakeClock()
    pool = make_pool(make_account("used"), make_account("fresh"), clock=clock)
    first = pool.lease()
    assert first.account_ref == "fresh"  # alphabetical tiebreak at -inf
    pool.note_dispatch(first)
    clock.advance(1)

    # 'fresh' now has a timestamp; 'used' is still at -inf and wins.
    assert pool.lease().account_ref == "used"


# --- lease affinity ---------------------------------------------------------


def test_account_keeps_its_ip_for_the_life_of_the_lease():
    """The correctness invariant. A report is a *sequence* of requests."""
    pool = make_pool(make_account("a"), make_account("b"))
    lease = pool.lease(proxy_for=lambda a: "residential:1234")
    pool.note_dispatch(lease)
    pool.note_dispatch(lease)
    assert pool.active_lease("a") == lease
    assert lease.proxy == "residential:1234"
    assert lease.proxy == pool.active_lease("a").proxy


def test_rebind_installs_the_replacement_before_retiring_the_old():
    pool = make_pool(make_account("a"))
    original = pool.lease(proxy_for=lambda a: "ip-1")

    replacement = pool.rebind("a", "ip-2")
    assert pool.active_lease("a") == replacement
    assert replacement.proxy == "ip-2"
    assert replacement.lease_id != original.lease_id
    assert replacement != original


def test_a_superseded_lease_cannot_unbind_its_account():
    """A stale lease must not tear down a binding that replaced it.

    Without this, a slow in-flight task holding an old Lease object could
    release the account's new IP mid-run.
    """
    pool = make_pool(make_account("a"))
    original = pool.lease(proxy_for=lambda a: "ip-1")
    replacement = pool.rebind("a", "ip-2")

    pool.release_lease(original)  # stale
    assert pool.active_lease("a") == replacement
    assert pool.active_lease("a").proxy == "ip-2"


def test_releasing_the_live_lease_does_unbind():
    pool = make_pool(make_account("a"))
    lease = pool.lease(proxy_for=lambda a: "ip-1")
    pool.release_lease(lease)
    assert pool.active_lease("a") is None


def test_rebind_accepts_a_callable():
    pool = make_pool(make_account("a"))
    pool.lease()
    replacement = pool.rebind("a", lambda a: f"ip-for-{a.ref}")
    assert replacement.proxy == "ip-for-a"


def test_lease_is_immutable():
    """A mutable lease would make the affinity invariant unenforceable."""
    lease = Lease(lease_id="l", account_ref="a", proxy="ip")
    with pytest.raises(Exception):
        lease.proxy = "other"  # type: ignore[misc]


def test_lease_exhaustion_is_reported():
    lease = Lease(lease_id="l", account_ref="a", proxy="ip", max_reports=2)
    assert not lease.exhausted(1)
    assert lease.exhausted(2)
    assert lease.exhausted(3)


# --- quarantine backoff -----------------------------------------------------


def test_quarantine_backoff_doubles():
    account = make_account()
    base = DEFAULT_QUARANTINE_BASE_SECONDS
    waits = [account.record_failure("x", now=0.0, base=base) for _ in range(5)]
    assert waits == [base, base * 2, base * 4, base * 8, base * 16]


def test_quarantine_backoff_is_capped():
    account = make_account()
    account.record_failure("x", now=0.0, base=100.0, cap=1000.0)
    for _ in range(20):
        wait = account.record_failure("x", now=0.0, base=100.0, cap=1000.0)
    assert wait == 1000.0


def test_only_a_confirmed_ack_clears_the_failure_streak():
    """An UNKNOWN means we do not know. Treating that as success is how a
    challenged account stays in rotation."""
    clock = FakeClock()
    pool = make_pool(make_account("a"), clock=clock)
    lease = pool.lease()
    account = pool.get("a")
    account.record_failure("x", now=clock.now, base=100.0)

    for terminal in (TerminalState.UNKNOWN, TerminalState.SUBMITTED_UNCONFIRMED):
        pool.note_outcome(lease, outcome(terminal))
        assert account.consecutive_failures > 0

    pool.note_outcome(lease, outcome(TerminalState.SUBMITTED_ACKED))
    assert account.consecutive_failures == 0
    assert account.total_acked == 1


def test_unattempted_outcomes_do_not_penalise_the_account():
    """A selector that will never work is not the account's fault.

    QUARANTINED is deliberately absent: sidelining an account is that
    terminal state's entire purpose, so it is covered by its own test rather
    than being lumped in here as if it were a passive observation.
    """
    pool = make_pool(make_account("a"))
    lease = pool.lease()
    account = pool.get("a")

    for terminal in (TerminalState.CHANNEL_FAILED, TerminalState.NOT_REPORTABLE):
        pool.note_outcome(lease, outcome(terminal, dispatched=False))

    assert account.consecutive_failures == 0
    assert account.quarantined_until is None


def test_quarantine_escalates_the_streak_even_when_nothing_was_dispatched():
    """QUARANTINED is a decision, not a verdict on the response.

    The dispatch check below it does not apply, so an operator-initiated
    sidelining still backs the account off.
    """
    pool = make_pool(make_account("a"))
    lease = pool.lease()
    account = pool.get("a")

    not_dispatched = Outcome(
        terminal=TerminalState.QUARANTINED,
        target_ref="t1",
        channel="browser",
        account_ref="a",
        lease_id=lease.lease_id,
    )
    assert not not_dispatched.was_dispatched
    pool.note_outcome(lease, not_dispatched)

    assert account.consecutive_failures == 1
    assert account.quarantined_until is not None


def test_unknown_outcome_does_not_clear_a_quarantine():
    clock = FakeClock()
    pool = make_pool(make_account("a"), clock=clock)
    lease = pool.lease()
    account = pool.get("a")
    account.record_failure("challenge", now=clock.now, base=600.0)
    until = account.quarantined_until

    pool.note_outcome(lease, outcome(TerminalState.UNKNOWN))
    assert account.quarantined_until == until


def test_quarantined_outcome_sidelines_the_account():
    pool = make_pool(make_account("a"))
    lease = pool.lease()
    pool.note_outcome(lease, outcome(TerminalState.QUARANTINED))
    assert not pool.get("a").eligible(now_of(pool))
    assert pool.active_lease("a") is None


def test_challenge_quarantines_and_drops_the_lease():
    """A challenge is frequently a bad IP, not a bad account: sidelined, not
    deleted."""
    pool = make_pool(make_account("a"))
    lease = pool.lease(proxy_for=lambda a: "ip-1")
    wait = pool.note_challenge(lease, "checkpoint required")
    assert wait > 0
    assert pool.active_lease("a") is None
    assert pool.get("a") in pool.accounts  # still there


def test_session_expiry_sidelines():
    pool = make_pool(make_account("a"))
    lease = pool.lease()
    pool.note_session_expired(lease)
    assert pool.active_lease("a") is None
    assert pool.get("a").sidelined_for == "session expired"


# --- budget exhaustion is an error -----------------------------------------


def test_budget_exhaustion_raises_instead_of_silently_dropping():
    """F6. The original code continued and counted a report that never happened."""
    pool = make_pool(make_account("a", budget=1))
    lease = pool.lease()
    pool.note_dispatch(lease)
    with pytest.raises(ReportBudgetExhausted, match=r"1/1"):
        pool.note_budget_exhausted(lease)


# --- self-report ------------------------------------------------------------


def test_self_report_is_detected_case_and_at_insensitively():
    pool = make_pool(make_account("a", username="reporter.one"))
    assert pool.would_self_report("reporter.one")
    assert pool.would_self_report("@Reporter.One")
    assert pool.would_self_report("  reporter.one  ")


def test_other_accounts_are_not_self_reports():
    pool = make_pool(make_account("a", username="reporter.one"))
    assert not pool.would_self_report("reporter.two")
    assert not pool.would_self_report("reporter.onex")


def test_self_report_catches_a_confusable_but_distinct_handle():
    """Not a confusable *character* -- those are the harm vector F8.

    This only proves exact matching; character-level confusable detection is
    the target resolver's job and is tested there.
    """
    pool = make_pool(make_account("a", username="reporter"))
    assert not pool.would_self_report("rep0rter")


# --- misc -------------------------------------------------------------------


def test_duplicate_ref_is_rejected():
    """Silently running two identities under one name would misattribute every
    outcome in the summary."""
    with pytest.raises(ValueError, match="duplicate account ref"):
        make_pool(make_account("a"), make_account("a"))


def test_status_never_includes_the_sessionid():
    pool = make_pool(make_account("a"))
    rendered = repr(pool.status())
    assert "never-log-this-value" not in rendered
    assert "a" in rendered


def test_repr_of_an_account_hides_the_sessionid():
    assert "never-log-this-value" not in repr(make_account("a"))


def test_summary_shows_a_state_for_every_account():
    pool = make_pool(make_account("a", budget=1), make_account("b", enabled=False))
    lease = pool.lease()
    pool.note_dispatch(lease)
    text = pool.summary()
    assert "daily budget spent" in text
    assert "disabled in config" in text


def test_summary_marks_an_eligible_account_as_eligible():
    pool = make_pool(make_account("a"), make_account("b"))
    assert "eligible" in pool.summary()


def test_status_reports_every_account():
    pool = make_pool(make_account("a"), make_account("b"))
    assert {row["ref"] for row in pool.status()} == {"a", "b"}


def test_rolled_account_becomes_eligible_again_uses_pool_clock():
    pool = make_pool(make_account("a", budget=1))
    lease = pool.lease()
    pool.note_dispatch(lease)
    with pytest.raises(NoEligibleAccount):
        pool.lease()
    pool._wall_clock = lambda: date(2026, 9, 26)  # noqa: SLF001 - drives the clock
    assert pool.lease() is not None
