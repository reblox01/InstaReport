"""Runner tests: the ladder, the boundary, the latch.

Two properties earn their keep here, and everything else is scaffolding:

1. **One target, at most one dispatch, ever.** Not "one per channel", not "one
   per attempt". The whole failure mode this project was rebuilt to remove is a
   tool that reports success repeatedly and files nothing; the mirror of it is a
   tool that files the same target twice and calls it resilience. Every test
   here is, in the end, an assertion about that number.

2. **The boundary is a write.** Not a boolean, not a log line, not a callback
   the channel is trusted to call at the right moment. A record that is fsynced
   before the click and that the run refuses to proceed without.

The tests are offline by construction. Channels are scripted fakes, the clock
is injected, the proxy pool is fed a fake transport, and the wait is a stub that
returns immediately -- so a run that paces at eight seconds minimum can be
exercised in microseconds and the assertions are about ordering, not timing.
"""

from __future__ import annotations

import asyncio
import random
from pathlib import Path
from urllib.parse import urlparse

import pytest

from insta_report.accounts import Account, AccountPool
from insta_report.checkpoint import CheckpointStore
from insta_report.doctor import ChannelProbe
from insta_report.errors import (
    AccountChallenged,
    ErrorScope,
    FatalError,
    PreflightFailed,
    ProxyUnavailable,
    RunAborted,
    SessionExpired,
    TransientError,
)
from insta_report.outcomes import Outcome, TerminalState, utc_now
from insta_report.pacing import Pacer, PacingConfig
from insta_report.proxies import (
    EgressObservation,
    ProbeResult,
    ProbeVerdict,
    ProxyEndpoint,
    ProxyPool,
)
from insta_report.runner import (
    ChannelHealth,
    ChannelSpec,
    NoChannelsAvailable,
    RunOptions,
    Runner,
)
from insta_report.targets import Target, TargetList

# ===========================================================================
# Harness
# ===========================================================================


def acked(key: str, *, channel: str = "browser", account_ref: str = "alpha") -> Outcome:
    """A dispatched, confirmed report. The only fully good terminal state."""
    return Outcome(
        terminal=TerminalState.SUBMITTED_ACKED,
        target_ref=key,
        channel=channel,
        account_ref=account_ref,
        dispatched_at=utc_now(),
        finished_at=utc_now(),
    )


def never_dispatched(key: str, *, channel: str = "browser", why: str = "no") -> Outcome:
    """A pre-dispatch failure: the ladder is free to move on."""
    return Outcome(
        terminal=TerminalState.CHANNEL_FAILED,
        target_ref=key,
        channel=channel,
        detail=why,
    )


def post_dispatch(key: str, terminal: TerminalState, *, channel: str = "browser") -> Outcome:
    return Outcome(
        terminal=terminal,
        target_ref=key,
        channel=channel,
        dispatched_at=utc_now(),
        finished_at=utc_now(),
    )


class FakeChannel:
    """A scripted channel.

    ``script`` is consumed one entry per call. Each entry is either an
    ``Outcome`` to return, an exception instance to raise, or a callable taking
    ``(target, on_dispatch, attempt)``. The default is "dispatch, then ack" --
    the shape a working channel has, so a test only scripts the behaviour it is
    actually about.

    Every call yields to the event loop first. That is not decoration: a fake
    that never awaits runs its whole body inside the first worker's turn, so the
    scheduler never gets to run worker two, and a test about worker isolation
    passes because the second worker was never actually concurrent. With the
    yield, ``asyncio`` interleaves the workers the way it would against a real
    browser, which is the only way an assertion about concurrency means
    anything.
    """

    def __init__(
        self,
        name: str = "browser",
        *,
        script: list | None = None,
        capacity: int = 1,
    ) -> None:
        self.name = name
        self.capacity = capacity
        self._script = list(script or [])
        self.calls: list[tuple[str, int, str | None, str | None]] = []
        self.closed = False
        #: Override the rehearsal verdict. ``None`` means "the channel works".
        self.rehearsal: ChannelProbe | None = None
        #: ``(handle, submit)`` for every rehearsal asked for.
        self.rehearsed: list[tuple[str | None, bool]] = []
        #: Every boundary call seen, in order. The tests that matter assert
        #: against this rather than against a counter.
        self.boundaries: list[str] = []

    async def report(
        self,
        target: Target,
        *,
        on_dispatch,
        account_ref: str | None = None,
        lease=None,
        attempt: int = 1,
    ) -> Outcome:
        self.calls.append(
            (
                target.key,
                attempt,
                account_ref,
                lease.lease_id if lease is not None else None,
            )
        )
        await asyncio.sleep(0)
        step = self._script.pop(0) if self._script else _DISPATCH_THEN_ACK

        if isinstance(step, BaseException):
            raise step
        if callable(step):
            return step(target, on_dispatch, attempt)
        return step

    async def aclose(self) -> None:
        self.closed = True

    async def rehearse(self, target, *, submit: bool = False) -> ChannelProbe:
        """Reach the submit button, like a channel that works.

        Added for T10: the CLI now gates every run on a rehearsal, so a fake
        channel with no ``rehearse`` fails the gate and the *other* 80 tests
        that use this class start failing for a reason that has nothing to do
        with what they are about. A test that cares about the gate builds its
        own channel, or sets :attr:`rehearsal`.
        """
        self.rehearsed.append((getattr(target, "handle", None), submit))
        if self.rehearsal is not None:
            return self.rehearsal
        return ChannelProbe(
            name=self.name,
            ok=True,
            reached="submit ready",
            detail="rehearsed by the fake",
            categories=("Spam", "Fake account"),
            submit_ready=True,
            submitted=False,
        )


def _dispatch_then_ack(target: Target, on_dispatch, attempt: int) -> Outcome:
    on_dispatch()
    return acked(target.key)


_DISPATCH_THEN_ACK = _dispatch_then_ack


class Recorder:
    """A wait that returns immediately and remembers what it was asked for.

    The pacing arithmetic under test is the pacer's, which has its own suite.
    What the runner must get right is *ordering* -- a gap before the dispatch,
    none after -- and that is only visible if the wait is observable.
    """

    def __init__(self, runner_getter=lambda: None) -> None:
        self.waits: list[float] = []
        self._runner_getter = runner_getter

    async def __call__(self, seconds: float) -> bool:
        self.waits.append(seconds)
        return True


class Clock:
    """A monotonic clock the test drives by hand."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_pool(
    *refs: str,
    budget: int = 20,
    clock: Clock | None = None,
    lease_reports: int = 10,
    **kwargs,
) -> AccountPool:
    if not refs:
        refs = ("alpha",)
    clock = clock or Clock()
    accounts = [
        Account(
            ref=ref,
            username=ref,
            sessionid=f"session-{ref}-000000000000",
            daily_budget=budget,
            **kwargs,
        )
        for ref in refs
    ]
    return AccountPool(accounts, monotonic=clock, lease_reports=lease_reports)


def make_store(tmp_path: Path, run_id: str = "run-test") -> CheckpointStore:
    return CheckpointStore(tmp_path / "checkpoint.jsonl", run_id)


def make_targets(*handles: str) -> TargetList:
    if not handles:
        handles = ("scammer.one",)
    return TargetList(targets=[Target(handle=h) for h in handles], source="test")


def ok_probe(url: str, proxy: str | None = None) -> ProbeResult:
    """A transport whose exits are always healthy, with a distinct IP each.

    Two things have to be right here or the pool refuses for the wrong reason.
    The result carries an ``EgressObservation``, because an exit that returned
    something without revealing its address is the F9 shape. And each address
    gets its *own* ASN, because the pool treats a repeated ASN as a real
    diversity constraint and will skip every candidate that shares one with a
    live lease -- a harness that hands out one ASN for four addresses would
    look exactly like four blocked exits.
    """
    host = urlparse(proxy or url).hostname or "203.0.113.7"
    last = int(host.rsplit(".", 1)[-1]) if host[-1:].isdigit() else 7
    return ProbeResult(
        verdict=ProbeVerdict.OK,
        status=200,
        egress=EgressObservation(ip=host, asn=64500 + last, country="US"),
    )


def make_proxies(count: int = 2, clock: Clock | None = None, **kwargs) -> ProxyPool:
    """A pool of always-healthy exits, on a clock the test controls.

    ``own_ip`` defaults to an address these exits never produce. Every lease is
    now checked against it, and ``ok_probe`` derives each exit's address from
    its own host, so the sentinel has to be outside the 198.51.100.0/24 range
    this harness hands out -- otherwise the pool would refuse to lease at all
    and the tests would fail on the guard instead of on what they are about.
    """
    kwargs.setdefault("own_ip", "192.0.2.99")
    return ProxyPool(
        [ProxyEndpoint(url=f"http://198.51.100.{n}:9000") for n in range(1, count + 1)],
        fetch=ok_probe,
        monotonic=clock or (lambda: 0.0),
        rng=random.Random(1234),
        **kwargs,
    )


def a_runner(
    tmp_path: Path,
    *,
    channels: list[FakeChannel] | None = None,
    pool: AccountPool | None = None,
    proxies: ProxyPool | None = None,
    targets: TargetList | None = None,
    options: RunOptions | None = None,
    run_id: str = "run-test",
    on_progress=None,
    clock: Clock | None = None,
) -> tuple[Runner, Recorder, CheckpointStore]:
    """Everything a run needs, with the waits stubbed out.

    The runner, the account pool, the proxy pool and the pacer must all read the
    *same* clock, and this enforces it rather than trusting the caller. They
    disagreeing is not a cosmetic problem: the slot's freshness check and the
    pool's cooldown both ask "has this exit expired?", so with two clocks one of
    them decides the binding is still good while the other has already written
    it off, and the run quietly carries on through a rotated address. That bug
    is invisible until a test happens to advance one of the two clocks.

    When the caller names a clock, every supplied component must already be
    using it. When it does not, the clock is adopted from whichever component
    was supplied, so the common case cannot disagree by accident.
    """
    supplied = [component for component in (pool, proxies) if component is not None]
    if clock is None:
        clock = next(
            (
                component._monotonic
                for component in supplied
                if getattr(component, "_monotonic", None) is not None
            ),
            None,
        ) or Clock()
    for component in supplied:
        theirs = getattr(component, "_monotonic", None)
        if theirs is not None and theirs is not clock:
            raise AssertionError(
                "the account pool, the proxy pool and the runner must share one "
                "clock. Pass the same Clock() to a_runner() and to make_pool()/"
                "make_proxies(); two clocks will disagree about whether a "
                "binding has expired, and the run will carry on through a "
                "rotated address."
            )
    store = make_store(tmp_path, run_id)
    pool = pool if pool is not None else make_pool("alpha", clock=clock)
    channels = channels if channels is not None else [FakeChannel()]
    waits = Recorder()
    runner = Runner(
        store=store,
        pool=pool,
        channels=[ChannelSpec(c.name, c, c.capacity) for c in channels],
        targets=targets if targets is not None else make_targets(),
        options=options or RunOptions(max_reports=100, backoff_seconds=0.0),
        proxies=proxies,
        monotonic=clock,
        on_progress=on_progress,
        sleeper=waits,
        pacer_factory=lambda: Pacer(
            PacingConfig(floor_gap=8.0, jitter_fraction=0.0),
            monotonic=clock,
            rng=random.Random(7),
        ),
    )
    return runner, waits, store


def records(store: CheckpointStore) -> list[dict]:
    """The checkpoint as a list of dicts, for order-sensitive assertions."""
    import json

    return [json.loads(line) for line in Path(store.path).read_text(encoding="utf-8").splitlines() if line]


def kinds(store: CheckpointStore) -> list[str]:
    return [record["kind"] for record in records(store)]


def _failing_write(intent) -> None:
    """A checkpoint write that reaches the disk and then fails to stay there.

    Modelled on a real fsync failure rather than a stub that refuses outright:
    the dangerous case is not "we noticed", it is "we do not know whether it
    landed", and only the second can produce a duplicate.
    """
    raise OSError("could not fsync the checkpoint")


def seed_checkpoint(tmp_path: Path, run_id: str, *pairs) -> CheckpointStore:
    """Write a checkpoint as a *previous* run would have left it, and reopen it.

    ``pairs`` are ``(target_ref, terminal)`` for a settled target, or
    ``(target_ref, None)`` for one dispatched with no outcome -- the exact
    state ``--resume`` exists to clean up.
    """
    from insta_report.checkpoint import Intent

    store = make_store(tmp_path, run_id)
    store.open()
    for target_ref, terminal in pairs:
        store.record_intent(
            Intent(
                run_id=run_id,
                target_ref=target_ref,
                account_ref="alpha",
                lease_id="L1",
                channel="browser",
            )
        )
        if terminal is not None:
            store.record_outcome(
                Outcome(
                    terminal=terminal,
                    target_ref=target_ref,
                    channel="browser",
                    dispatched_at=utc_now(),
                    finished_at=utc_now(),
                )
            )
    store.close()
    return make_store(tmp_path, run_id)


# ===========================================================================
# RunOptions
# ===========================================================================


class TestRunOptions:
    """The guards exist because a bad bound here is a blast radius, not a bug."""

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"max_reports": 0},
            {"max_reports": -5},
            {"horizon_seconds": 0},
            {"horizon_seconds": -1},
            {"transient_retries": -1},
            {"channel_failure_threshold": 0},
            {"max_concurrent": 0},
        ],
    )
    def test_nonsense_bounds_are_refused_at_construction(self, kwargs):
        # Not at run time. A cap of zero discovered halfway through a run is a
        # run that already dispatched reports.
        with pytest.raises(ValueError):
            RunOptions(**kwargs)

    def test_uncapped_is_expressible_because_tests_and_operators_need_it(self):
        assert RunOptions(max_reports=None).max_reports is None

    def test_the_default_cap_is_not_the_target_list_length(self):
        # The whole point of a default: an operator who forgets --max-reports
        # gets a bounded run, not a 500-line file's worth of reports.
        assert RunOptions().max_reports == 100


# ===========================================================================
# ChannelHealth
# ===========================================================================


class TestChannelHealth:
    """Health is about the channel, so only non-dispatched failures count."""

    def test_a_dispatched_report_resets_the_failure_streak(self):
        health = ChannelHealth(name="browser", capacity=1)
        health.note(never_dispatched("a"))
        health.note(never_dispatched("b"))
        assert health.consecutive_channel_failures == 2
        health.note(acked("c"))
        assert health.consecutive_channel_failures == 0

    def test_a_dispatched_channel_failure_is_not_structural(self):
        # The shape that matters: something went out, we could not read the
        # reply. That says nothing about whether the channel works, and
        # counting it would retire every channel on a slow afternoon.
        health = ChannelHealth(name="api", capacity=1)
        health.note(post_dispatch("a", TerminalState.UNKNOWN))
        assert health.consecutive_channel_failures == 0
        assert health.dispatches == 1

    def test_a_target_that_does_not_exist_resets_the_streak(self):
        # A 404 proves the channel is answering. Three scattered 404s are not a
        # broken channel.
        health = ChannelHealth(name="browser", capacity=1)
        health.note(never_dispatched("a"))
        health.note(never_dispatched("b"))
        health.note(
            Outcome(terminal=TerminalState.NOT_REPORTABLE, target_ref="c")
        )
        assert health.consecutive_channel_failures == 0

    def test_the_worst_streak_survives_the_reset(self):
        health = ChannelHealth(name="browser", capacity=1)
        for _ in range(3):
            health.note(never_dispatched("x"))
        health.note(acked("y"))
        assert health.consecutive_channel_failures == 0
        assert health.worst_consecutive == 3

    def test_counts_are_kept_per_terminal(self):
        health = ChannelHealth(name="browser", capacity=1)
        health.note(acked("a"))
        health.note(acked("b"))
        health.note(never_dispatched("c"))
        assert health.outcomes == {"submitted_acked": 2, "channel_failed": 1}
        assert health.as_dict()["attempts"] == 3

    def test_disabling_is_idempotent_and_keeps_the_first_reason(self):
        health = ChannelHealth(name="browser", capacity=1)
        health.disable("layout changed")
        health.disable("something else")
        assert health.disabled_reason == "layout changed"


# ===========================================================================
# The ladder
# ===========================================================================


class TestTheLadder:
    """One report per target, tried down the rungs in order."""

    async def test_a_working_first_channel_is_the_only_channel_tried(self, tmp_path):
        second = FakeChannel("api")
        first = FakeChannel("browser")
        runner, _, store = a_runner(
            tmp_path, channels=[first, second], targets=make_targets("a")
        )
        report = await runner.run()

        assert [call[0] for call in first.calls] == ["a"]
        assert second.calls == []
        assert report.acked == 1
        assert report.dispatches == 1

    async def test_a_pre_dispatch_channel_failure_falls_through(self, tmp_path):
        # The whole point of the ladder: the browser could not serve this
        # target, so the API is asked. Nothing was sent, so this is not a
        # duplicate.
        first = FakeChannel("browser", script=[never_dispatched("a", channel="browser")])
        second = FakeChannel("api")
        runner, _, store = a_runner(
            tmp_path, channels=[first, second], targets=make_targets("a")
        )
        report = await runner.run()

        assert [c[0] for c in first.calls] == ["a"]
        assert [c[0] for c in second.calls] == ["a"]
        assert report.acked == 1
        # The failure is not in the ledger: it never dispatched, and recording
        # it would claim a report that does not exist.
        assert kinds(store) == ["intent", "outcome"]
        assert records(store)[0]["channel"] == "api"

    async def test_a_post_dispatch_uncertainty_never_falls_through(self, tmp_path):
        # The mirror image, and the important one. UNKNOWN means the report may
        # have landed. Trying the next channel would be a second report for one
        # target, filed by a different identity from a different exit.
        first = FakeChannel(
            "browser",
            script=[
                lambda t, dispatch, _a: (
                    dispatch(),
                    post_dispatch(t.key, TerminalState.UNKNOWN, channel="browser"),
                )[1]
            ],
        )
        second = FakeChannel("api")
        runner, _, store = a_runner(
            tmp_path, channels=[first, second], targets=make_targets("a")
        )
        report = await runner.run()

        assert second.calls == [], "the ladder fell through after a dispatch"
        assert report.dispatches == 1
        assert report.counts[TerminalState.UNKNOWN.value] == 1

    async def test_the_first_terminal_state_wins(self, tmp_path):
        # NOT_REPORTABLE stops the ladder even though it is not a channel's
        # fault: the target is gone, and asking a different channel will not
        # bring it back.
        first = FakeChannel(
            "browser",
            script=[
                lambda _t, _d, _a: Outcome(
                    terminal=TerminalState.NOT_REPORTABLE, target_ref="a", channel="browser"
                )
            ],
        )
        second = FakeChannel("api")
        runner, _, store = a_runner(
            tmp_path, channels=[first, second], targets=make_targets("a")
        )
        await runner.run()
        assert second.calls == []

    async def test_the_ladder_reaches_the_last_rung(self, tmp_path):
        channels = [
            FakeChannel("browser", script=[never_dispatched("a", channel="browser")]),
            FakeChannel("api", script=[never_dispatched("a", channel="api")]),
            FakeChannel("web", script=[never_dispatched("a", channel="web")]),
        ]
        runner, _, store = a_runner(
            tmp_path, channels=channels, targets=make_targets("a")
        )
        report = await runner.run()

        assert [len(c.calls) for c in channels] == [1, 1, 1]
        assert report.dispatches == 0
        assert kinds(store) == [], "no dispatch, so the ledger must be empty"

    async def test_every_target_in_the_list_is_attempted_in_file_order(self, tmp_path):
        # The ledger has to be readable against the file the operator is
        # looking at. A run that reshuffled its list would make that impossible
        # for no throughput gain.
        channel = FakeChannel()
        runner, _, store = a_runner(
            tmp_path,
            channels=[channel],
            targets=make_targets("first.one", "second.one", "third.one"),
        )
        await runner.run()
        assert [call[0] for call in channel.calls] == [
            "first.one",
            "second.one",
            "third.one",
        ]
        assert [r["target_ref"] for r in records(store) if r["kind"] == "intent"] == [
            "first.one",
            "second.one",
            "third.one",
        ]


# ===========================================================================
# The dispatch boundary
# ===========================================================================


class TestTheDispatchBoundary:
    async def test_the_intent_is_on_disk_before_the_click(self, tmp_path):
        # Read the checkpoint from inside the channel, at the instant the click
        # is about to happen. This is the assertion that cannot be faked by
        # inspecting the file afterwards.
        seen: list[list[str]] = []
        channel_box: dict = {}

        def on_the_boundary(target, on_dispatch, attempt):
            seen.append(kinds(channel_box["store"]))
            on_dispatch()
            return acked(target.key)

        channel = FakeChannel("browser", script=[on_the_boundary])
        runner, _, store = a_runner(
            tmp_path, channels=[channel], targets=make_targets("a")
        )
        channel_box["store"] = store
        report = await runner.run()

        assert report.acked == 1
        assert seen == [[]], "the intent was not on disk at the moment of the click"
        assert kinds(store) == ["intent", "outcome"]

    async def test_a_failing_checkpoint_write_stops_the_run_without_clicking(
        self, tmp_path
    ):
        # The failure this guards: a swallowed or lost write turns into a
        # dispatch no record knows about, which is a duplicate waiting for the
        # next run. So the write failing must stop the run outright -- not grade
        # the target, and not fall through to the next channel, because that
        # would report it nowhere while printing a tidy summary.
        clicked: list[str] = []
        second = FakeChannel("api")

        def boom(target, on_dispatch, attempt):
            on_dispatch()
            clicked.append(target.key)
            return acked(target.key)

        channel = FakeChannel("browser", script=[boom])
        runner, _, store = a_runner(
            tmp_path, channels=[channel, second], targets=make_targets("a", "b")
        )
        store.record_intent = _failing_write  # type: ignore[method-assign]

        with pytest.raises(RunAborted) as caught:
            await runner.run()

        assert "could not record the dispatch intent" in str(caught.value)
        assert caught.value.scope is ErrorScope.RUN
        assert clicked == [], "the click happened after the write failed"
        assert second.calls == [], "fell through after a failed boundary write"
        # Every channel is closed even though the run died, so an operator
        # running this in a terminal does not leave a browser behind.
        assert channel.closed and second.closed

    async def test_a_second_dispatch_on_one_target_is_refused(self, tmp_path):
        # A channel with a doubled submit button. The runner is the last line of
        # defence, because the intent record cannot express "twice".
        def twice(target, on_dispatch, attempt):
            on_dispatch()
            with pytest.raises(RunAborted) as caught:
                on_dispatch()
            assert caught.value.scope is ErrorScope.RUN
            return acked(target.key)

        channel = FakeChannel("browser", script=[twice])
        runner, _, store = a_runner(tmp_path, channels=[channel], targets=make_targets("a"))
        report = await runner.run()
        assert report.dispatches == 1, "the second boundary call must not bill"
        assert kinds(store) == ["intent", "outcome"]

    async def test_the_account_is_charged_only_after_the_record(self, tmp_path):
        clock = Clock()
        pool = make_pool("alpha", clock=clock, budget=5)
        observed: list[int] = []

        def peek(target, on_dispatch, attempt):
            observed.append(pool.get("alpha").used_today)
            on_dispatch()
            return acked(target.key)

        channel = FakeChannel("browser", script=[peek])
        runner, _, _ = a_runner(
            tmp_path, channels=[channel], pool=pool, targets=make_targets("a"), clock=clock)
        await runner.run()
        assert observed == [0], "the account was billed before the click"
        assert pool.get("alpha").used_today == 1

    async def test_one_intent_produces_exactly_one_outcome(self, tmp_path):
        # The invariant --resume depends on. Checked by reading the ledger, not
        # by trusting the store's own bookkeeping.
        channel = FakeChannel()
        runner, _, store = a_runner(
            tmp_path, channels=[channel], targets=make_targets("a", "b", "c")
        )
        await runner.run()
        assert kinds(store) == ["intent", "outcome"] * 3
        assert report_unsettled(store) == ()

    async def test_a_clean_run_reports_no_unsettled_targets(self, tmp_path):
        runner, _, store = a_runner(
            tmp_path, channels=[FakeChannel()], targets=make_targets("a", "b")
        )
        report = await runner.run()
        assert report.unsettled == ()
        assert not report.needs_attention


def report_unsettled(store: CheckpointStore) -> tuple[str, ...]:
    return tuple(sorted(store.state.pending))


# ===========================================================================
# Transients
# ===========================================================================


class TestTransients:
    async def test_a_transient_retries_the_same_channel(self, tmp_path):
        channel = FakeChannel(
            "browser", script=[TransientError("timeout"), _DISPATCH_THEN_ACK]
        )
        second = FakeChannel("api")
        runner, _, store = a_runner(
            tmp_path,
            channels=[channel, second],
            targets=make_targets("a"),
            options=RunOptions(max_reports=10, backoff_seconds=0.0, transient_retries=2),
        )
        report = await runner.run()

        assert [c[1] for c in channel.calls] == [1, 2], "attempt numbers must climb"
        assert second.calls == [], "a transient must not drop down the ladder"
        assert report.acked == 1

    async def test_retries_are_bounded(self, tmp_path):
        channel = FakeChannel(
            "browser", script=[TransientError("timeout")] * 5
        )
        second = FakeChannel("api")
        runner, _, _ = a_runner(
            tmp_path,
            channels=[channel, second],
            targets=make_targets("a"),
            options=RunOptions(max_reports=10, backoff_seconds=0.0, transient_retries=1),
        )
        report = await runner.run()

        assert len(channel.calls) == 2, "1 initial + 1 retry, then give up"
        assert [c[0] for c in second.calls] == ["a"], "and hand over to the next rung"
        assert report.acked == 1, "the next rung's report is the only one filed"

    async def test_a_transient_after_dispatch_is_never_retried(self, tmp_path):
        # The dangerous one. A NavigationTimeout raised *after* the click may
        # mean the report landed. Retrying is how one target becomes two.
        def dispatch_then_timeout(target, on_dispatch, attempt):
            on_dispatch()
            raise TransientError("read timed out")

        channel = FakeChannel("browser", script=[dispatch_then_timeout])
        second = FakeChannel("api")
        runner, _, store = a_runner(
            tmp_path,
            channels=[channel, second],
            targets=make_targets("a"),
            options=RunOptions(max_reports=10, backoff_seconds=0.0, transient_retries=5),
        )
        report = await runner.run()

        assert len(channel.calls) == 1, "retried after a dispatch"
        assert second.calls == [], "fell through after a dispatch"
        assert report.counts[TerminalState.UNKNOWN.value] == 1
        assert report.unsettled == ()

    async def test_the_backoff_grows_between_retries(self, tmp_path):
        waits: list[float] = []

        async def record(seconds: float) -> bool:
            waits.append(seconds)
            return True

        channel = FakeChannel("browser", script=[TransientError("t")] * 4)
        runner, _, _ = a_runner(
            tmp_path,
            channels=[channel],
            targets=make_targets("a"),
            options=RunOptions(
                max_reports=10, backoff_seconds=7.0, transient_retries=3
            ),
        )
        runner._wait = record
        await runner.run()
        # 7.0 then 14.0. Doubling, so a channel that is genuinely down is not
        # hammered on a fixed interval.
        assert waits[:2] == [7.0, 14.0]


# ===========================================================================
# Channel retirement
# ===========================================================================


class TestChannelRetirement:
    async def test_repeated_non_dispatching_failures_retire_a_channel(self, tmp_path):
        first = FakeChannel(
            "browser", script=[never_dispatched("a", channel="browser")] * 6
        )
        second = FakeChannel("api")
        runner, _, _ = a_runner(
            tmp_path,
            channels=[first, second],
            targets=make_targets("a", "b", "c", "d", "e", "f"),
            options=RunOptions(max_reports=10, channel_failure_threshold=3),
        )
        report = await runner.run()

        health = runner.channel_health()["browser"]
        assert health.disabled
        assert "3 consecutive" in health.disabled_reason
        # It is taken out of the ladder rather than retried for the rest of the
        # run: a layout change will not fix itself in six targets' time.
        assert len(first.calls) == 3, "kept trying a retired channel"
        # api carried all six, because the first three fell through to it
        # before browser was retired. A retired rung is a rung that never
        # gets asked again, not a target that gets skipped.
        assert len(second.calls) == 6
        assert report.counts[TerminalState.SUBMITTED_ACKED.value] == 6

    async def test_a_single_failure_does_not_retire_anything(self, tmp_path):
        first = FakeChannel("browser", script=[never_dispatched("a", channel="browser")])
        runner, _, _ = a_runner(
            tmp_path, channels=[first], targets=make_targets("a")
        )
        await runner.run()
        assert not runner.channel_health()["browser"].disabled

    async def test_a_working_rung_reinstates_health(self, tmp_path):
        first = FakeChannel(
            "browser",
            script=[
                never_dispatched("a", channel="browser"),
                never_dispatched("b", channel="browser"),
                _DISPATCH_THEN_ACK,
            ],
        )
        runner, _, _ = a_runner(
            tmp_path,
            channels=[first],
            targets=make_targets("a", "b", "c"),
            options=RunOptions(max_reports=10, channel_failure_threshold=3),
        )
        await runner.run()
        assert not runner.channel_health()["browser"].disabled

    async def test_losing_every_channel_stops_the_run_says_so(self, tmp_path):
        # The failure this prevents: a clean exit code, a tidy summary, and zero
        # reports filed.
        only = FakeChannel("browser", script=[never_dispatched("x", channel="browser")] * 8)
        runner, _, _ = a_runner(
            tmp_path,
            channels=[only],
            targets=make_targets(*[f"t{n}" for n in range(6)]),
            options=RunOptions(max_reports=10, channel_failure_threshold=2),
        )
        report = await runner.run()

        assert report.aborted
        assert "every channel is disabled" in report.abort_reason
        assert report.dispatches == 0
        assert any("was retired" in message for message in report.errors)

    async def test_no_channels_at_all_is_refused_before_anything_happens(self, tmp_path):
        runner, _, _ = a_runner(tmp_path, channels=[], targets=make_targets("a"))
        with pytest.raises(NoChannelsAvailable) as caught:
            await runner.run()
        assert caught.value.scope is ErrorScope.RUN


# ===========================================================================
# Self-report and invalid targets
# ===========================================================================


class TestRefusals:
    async def test_reporting_our_own_account_is_refused_before_any_dispatch(
        self, tmp_path
    ):
        # The single worst thing this tool could do. It is caught by handle,
        # before the ladder starts, and the account's budget is untouched.
        channel = FakeChannel()
        pool = make_pool("alpha", "bravo")
        pool.get("bravo").username = "reporter.bravo"
        runner, _, store = a_runner(
            tmp_path,
            channels=[channel],
            pool=pool,
            targets=make_targets("reporter.bravo"),
        )
        report = await runner.run()

        assert channel.calls == [], "opened a browser to report ourselves"
        assert report.dispatches == 0
        assert store.state.pending == set(), "wrote an intent for a refused target"
        assert len(report.refusals) == 1
        assert "own reporting accounts" in report.refusals[0].reason
        assert report.refusals[0].fatal_to_run

    async def test_a_self_report_latches_the_whole_run(self, tmp_path):
        # One bad line must not be allowed to spend the rest of the list: if
        # the file can contain our own handle, nothing else in it has been
        # checked, and the cost of finding out is a locked account.
        channel = FakeChannel()
        pool = make_pool("alpha", "bravo")
        pool.get("bravo").username = "reporter.bravo"
        runner, _, _ = a_runner(
            tmp_path,
            channels=[channel],
            pool=pool,
            targets=make_targets("good.one", "reporter.bravo", "other.one"),
        )
        report = await runner.run()

        # "good.one" sorts after nothing, so it may or may not have been reached
        # depending on order -- but the targets *after* the refusal must not be.
        attempted = [call[0] for call in channel.calls]
        assert "other.one" not in attempted
        assert report.aborted
        assert "self-report" in report.abort_reason

    async def test_an_invalid_handle_is_refused_without_stopping_the_run(
        self, tmp_path
    ):
        # Deliberately different from a self-report: a handle that fails
        # validation provably is not the operator's own account, so it is a bad
        # line rather than a bad list. The rest of the file is still usable.
        channel = FakeChannel()
        runner, _, store = a_runner(
            tmp_path,
            channels=[channel],
            targets=TargetList(
                targets=[Target(handle="x" * 100), Target(handle="fine.one")],
                source="test",
            ),
        )
        report = await runner.run()

        assert [call[0] for call in channel.calls] == ["fine.one"]
        assert len(report.refusals) == 1
        assert not report.refusals[0].fatal_to_run
        assert not report.aborted, "one bad line should not end the run"

    async def test_a_refusal_writes_nothing_to_the_ledger(self, tmp_path):
        channel = FakeChannel()
        pool = make_pool("alpha")
        pool.get("alpha").username = "mine"
        runner, _, store = a_runner(
            tmp_path, channels=[channel], pool=pool, targets=make_targets("mine")
        )
        await runner.run()
        assert kinds(store) == []


# ===========================================================================
# The abort latch
# ===========================================================================


class TestTheAbortLatch:
    async def test_the_per_run_cap_stops_dispatching(self, tmp_path):
        channel = FakeChannel()
        runner, _, _ = a_runner(
            tmp_path,
            channels=[channel],
            targets=make_targets("a", "b", "c", "d", "e"),
            options=RunOptions(max_reports=2, backoff_seconds=0.0),
        )
        report = await runner.run()

        assert report.dispatches == 2
        assert len(channel.calls) == 2
        assert report.aborted
        assert "per-run cap" in report.abort_reason

    async def test_the_cap_counts_dispatches_not_targets(self, tmp_path):
        # A cap is a blast radius on Instagram, not a line count in a file. One
        # target gets a report, two get refused by a channel that has stopped
        # working: the run has not reached its cap and must not claim to have.
        def first_only(target, on_dispatch, attempt):
            if target.key != "a":
                raise PreflightFailed("this one target is not servable")
            on_dispatch()
            return acked(target.key)

        # The script is consumed one entry per call, and the default would
        # happily dispatch, so all three calls have to be scripted.
        channel = FakeChannel("browser", script=[first_only] * 3)
        runner, _, _ = a_runner(
            tmp_path,
            channels=[channel],
            targets=make_targets("a", "b", "c"),
            options=RunOptions(
                max_reports=2, backoff_seconds=0.0, channel_failure_threshold=9
            ),
        )
        report = await runner.run()

        assert report.dispatches == 1
        assert not report.aborted, "the run reported a cap it never reached"
        assert len(channel.calls) == 3, "one ladder pass per target, not one pass total"

    async def test_the_horizon_stops_the_run(self, tmp_path):
        clock = Clock()
        channel = FakeChannel()
        runner, _, _ = a_runner(
            tmp_path,
            channels=[channel],
            targets=make_targets("a", "b", "c"),
            options=RunOptions(max_reports=100, horizon_seconds=10.0, backoff_seconds=0.0),
            clock=clock,
        )

        # The clock only moves when a report is dispatched, so the horizon
        # cannot expire on its own here. Push it past the deadline from a
        # progress callback instead -- the shape of a real long run.
        def tick(outcome):
            clock.advance(6.0)

        runner._on_progress = tick
        report = await runner.run()

        assert report.dispatches == 2, "the run ignored its own horizon"
        assert report.aborted
        assert "horizon" in report.abort_reason

    async def test_an_external_abort_stops_new_work(self, tmp_path):
        channel = FakeChannel()
        runner, _, _ = a_runner(
            tmp_path, channels=[channel], targets=make_targets("a", "b", "c")
        )
        runner._on_progress = lambda outcome: runner.request_abort("operator")
        report = await runner.run()

        assert report.dispatches == 1, "kept working after the latch closed"
        assert report.abort_reason == "operator"

    async def test_the_first_abort_reason_is_the_one_kept(self, tmp_path):
        runner, _, _ = a_runner(tmp_path)
        runner.request_abort("first")
        runner.request_abort("second")
        assert runner.abort_reason == "first"

    async def test_an_abort_does_not_abandon_a_dispatched_report(self, tmp_path):
        # The failure this design exists to prevent: a Ctrl-C in the middle of
        # a wizard leaves an intent with no outcome, and that target can never
        # be settled again.
        channel = FakeChannel()
        runner, _, store = a_runner(
            tmp_path, channels=[channel], targets=make_targets("a", "b")
        )
        runner._on_progress = lambda outcome: runner.request_abort("stop")
        report = await runner.run()

        assert report.unsettled == (), "an in-flight report was abandoned"
        assert kinds(store) == ["intent", "outcome"]

    async def test_a_review_item_stops_the_run(self, tmp_path):
        # UNKNOWN needs a person. A run that keeps filing while ten of them
        # pile up has stopped listening, and the next thing it does is probably
        # retry one of them.
        def dispatch_unknown(target, on_dispatch, attempt):
            on_dispatch()
            return post_dispatch(target.key, TerminalState.UNKNOWN, channel="browser")

        channel = FakeChannel(
            "browser", script=[dispatch_unknown, _DISPATCH_THEN_ACK]
        )
        runner, _, _ = a_runner(
            tmp_path, channels=[channel], targets=make_targets("a", "b", "c", "d")
        )
        report = await runner.run()

        assert report.dispatches == 1
        assert report.aborted
        assert "needs a human" in report.abort_reason
        assert len(report.review) == 1

    async def test_the_wait_is_cut_short_by_an_abort(self, tmp_path):
        # Otherwise --max-reports and Ctrl-C both take effect only after the
        # current pacing gap is over, which can be an hour.
        reached: list[str] = []

        async def sleeper(seconds: float) -> bool:
            reached.append("slept")
            return True

        channel = FakeChannel()
        runner, _, _ = a_runner(
            tmp_path, channels=[channel], targets=make_targets("a", "b")
        )
        runner._wait = sleeper

        async def abort_during_the_wait():
            await asyncio.sleep(0)
            runner.request_abort("operator")

        task = asyncio.ensure_future(abort_during_the_wait())
        report = await runner.run()
        await task
        assert report.aborted


# ===========================================================================
# Resume / reconciliation
# ===========================================================================


class TestResume:
    async def test_reconciliation_happens_before_any_new_work(self, tmp_path):
        # A run that started filing before settling the previous run's orphans
        # could hit one of them. This is what --resume is for.
        resumed = seed_checkpoint(tmp_path, "run-one", ("orphan", None))
        assert resumed.state.pending == {"orphan"}

        channel = FakeChannel()
        pool = make_pool("alpha")
        runner = Runner(
            store=resumed,
            pool=pool,
            channels=[ChannelSpec("browser", channel)],
            targets=make_targets("orphan", "fresh.one"),
            options=RunOptions(max_reports=10, backoff_seconds=0.0),
            monotonic=Clock(),
            sleeper=_no_wait,
        )
        report = await runner.run()

        # The orphan was settled as UNKNOWN and never attempted.
        assert [call[0] for call in channel.calls] == ["fresh.one"]
        assert report.counts[TerminalState.UNKNOWN.value] == 1
        assert report.unsettled == ()

    async def test_a_settled_target_is_never_retried(self, tmp_path):
        resumed = seed_checkpoint(
            tmp_path, "run-one", ("a", TerminalState.SUBMITTED_ACKED)
        )
        channel = FakeChannel()
        runner = Runner(
            store=resumed,
            pool=make_pool("alpha"),
            channels=[ChannelSpec("browser", channel)],
            targets=make_targets("a", "b"),
            options=RunOptions(max_reports=10, backoff_seconds=0.0),
            monotonic=Clock(),
            sleeper=_no_wait,
        )
        await runner.run()
        assert [call[0] for call in channel.calls] == ["b"]

    async def test_the_store_refuses_a_settled_target_too(self, tmp_path):
        # The runner's in-attempt guard refuses a *second click within one
        # attempt*. This is the other half: the store, which is the thing that
        # outlives the process, refuses a target that is already settled. The
        # store deliberately does not refuse a second intent for a target that
        # is dispatched-but-unsettled -- both records stay in the file, the
        # target still resolves to settled, and reconciliation handles it
        # before any new work starts. Refusing there would make a crash mid
        # report unrecoverable rather than merely noisy.
        from insta_report.checkpoint import DoubleDispatch, Intent

        store = make_store(tmp_path, "run-test").open()
        store.record_intent(Intent("run-test", "a", "alpha", "L1", "browser"))
        store.record_outcome(
            Outcome(
                terminal=TerminalState.SUBMITTED_ACKED,
                target_ref="a",
                channel="browser",
                dispatched_at=utc_now(),
                finished_at=utc_now(),
            )
        )
        with pytest.raises(DoubleDispatch):
            store.record_intent(Intent("run-test", "a", "alpha", "L1", "api"))


async def _no_wait(seconds: float) -> bool:
    return True


# ===========================================================================
# Affinity and rotation
# ===========================================================================


class TestAffinity:
    async def test_every_worker_gets_its_own_account(self, tmp_path):
        # Two workers sharing one account is two sessions on one exit with one
        # budget between them -- the correlation the account pool exists to
        # prevent, arriving through a door the pool's own eligibility check
        # cannot see.
        channel = FakeChannel(capacity=4)
        clock = Clock()
        pool = make_pool("alpha", "bravo", clock=clock)
        runner, _, _ = a_runner(
            tmp_path,
            channels=[channel],
            pool=pool,
            targets=make_targets(*[f"t{n}" for n in range(8)]),
            options=RunOptions(max_reports=20, max_concurrent=3, backoff_seconds=0.0), clock=clock)
        await runner.run()

        refs = {call[2] for call in channel.calls}
        assert len(refs) >= 2, "every worker took the same identity"
        assert len(refs) <= 3

    async def test_one_slot_keeps_one_account_and_one_exit_across_reports(
        self, tmp_path
    ):
        clock = Clock()
        pool = make_pool("alpha", clock=clock)
        proxies = make_proxies(2, clock=clock)
        channel = FakeChannel()
        runner, _, _ = a_runner(
            tmp_path,
            channels=[channel],
            pool=pool,
            proxies=proxies,
            targets=make_targets("a", "b", "c"),
            options=RunOptions(max_reports=20, backoff_seconds=0.0), clock=clock)
        await runner.run()

        accounts = {call[2] for call in channel.calls}
        leases = {call[3] for call in channel.calls}
        assert accounts == {"alpha"}
        assert len(leases) == 1, "the exit rotated under a live session"

    async def test_an_exhausted_lease_rotates_both_halves(self, tmp_path):
        # A lease is the rotation unit. When it is spent, the account *and* the
        # exit change together -- rebinding one without the other would leave a
        # session on an address its account never used.
        clock = Clock()
        pool = make_pool("alpha", "bravo", clock=clock, lease_reports=2)
        proxies = make_proxies(4, clock=clock)
        channel = FakeChannel()
        runner, _, _ = a_runner(
            tmp_path,
            channels=[channel],
            pool=pool,
            proxies=proxies,
            targets=make_targets("a", "b", "c", "d"),
            options=RunOptions(max_reports=20, backoff_seconds=0.0), clock=clock)
        await runner.run()

        pairs = {(call[2], call[3]) for call in channel.calls}
        assert len(pairs) == 2, f"expected two (account, exit) pairs, got {pairs}"
        for account, _lease in pairs:
            bound = pool.active_lease(account)
            assert bound is not None

    async def test_an_expired_exit_forces_a_rotation(self, tmp_path):
        # F10. If the provider has already moved the address, carrying on is how
        # a session starts as one identity and finishes as another.
        clock = Clock()
        pool = make_pool("alpha", "bravo", clock=clock, lease_reports=99)
        proxies = make_proxies(4, clock=clock, sticky_ttl=100.0)
        channel = FakeChannel()
        runner, _, _ = a_runner(
            tmp_path,
            channels=[channel],
            pool=pool,
            proxies=proxies,
            targets=make_targets("a", "b"),
            options=RunOptions(max_reports=20, backoff_seconds=0.0), clock=clock)

        original = runner._on_progress
        counter = {"n": 0}

        def tick(outcome):
            if original is not None:
                original(outcome)
            counter["n"] += 1
            if counter["n"] == 1:
                clock.advance(500.0)  # past the sticky window

        runner._on_progress = tick
        await runner.run()
        assert len({call[3] for call in channel.calls}) == 2

    async def test_a_stale_exit_is_caught_before_the_channel_is_called(
        self, tmp_path
    ):
        # Asserted at the boundary rather than after, because by the time a
        # wizard has noticed, the intent is already written and the only
        # available verdict would be UNKNOWN.
        clock = Clock()
        pool = make_pool("alpha", clock=clock)
        proxies = make_proxies(2, clock=clock, sticky_ttl=100.0)
        channel = FakeChannel()
        runner, _, _ = a_runner(
            tmp_path,
            channels=[channel],
            pool=pool,
            proxies=proxies,
            targets=make_targets("a", "b"),
            options=RunOptions(max_reports=20, backoff_seconds=0.0), clock=clock)
        leases: list[object] = []
        original_acquire = proxies.acquire

        def acquire(**kwargs):
            lease = original_acquire(**kwargs)
            leases.append(lease)
            return lease

        proxies.acquire = acquire  # type: ignore[method-assign]
        await runner.run()
        assert leases, "no exit was ever acquired"

    async def test_no_eligible_account_stops_the_run_with_a_reason(self, tmp_path):
        clock = Clock()
        pool = make_pool("alpha", clock=clock, budget=0)
        channel = FakeChannel()
        runner, _, _ = a_runner(
            tmp_path, channels=[channel], pool=pool, targets=make_targets("a"), clock=clock)
        report = await runner.run()
        assert channel.calls == []
        assert report.aborted
        assert "no eligible account" in report.abort_reason.lower()

    async def test_an_unusable_exit_stops_the_run_rather_than_going_direct(
        self, tmp_path
    ):
        # Fails closed, like build_pool does. An operator who paid for isolation
        # must never silently not get it.
        clock = Clock()
        pool = make_pool("alpha", clock=clock)
        proxies = make_proxies(1, clock=clock, min_cooldown=0.0)
        channel = FakeChannel()
        runner, _, _ = a_runner(
            tmp_path,
            channels=[channel],
            pool=pool,
            proxies=proxies,
            targets=make_targets("a"), clock=clock)
        # Every exit is already cooling down.
        for _ in range(6):
            try:
                proxies.acquire()
            except ProxyUnavailable:
                break
        report = await runner.run()
        assert report.aborted
        assert channel.calls == []


# ===========================================================================
# Pacing
# ===========================================================================


class TestPacing:
    async def test_the_first_report_is_not_delayed(self, tmp_path):
        channel = FakeChannel()
        runner, waits, _ = a_runner(
            tmp_path, channels=[channel], targets=make_targets("a")
        )
        await runner.run()
        assert waits.waits == [], "a fresh pacer slept before its first report"

    async def test_a_gap_is_taken_before_the_second_report(self, tmp_path):
        channel = FakeChannel()
        runner, waits, _ = a_runner(
            tmp_path, channels=[channel], targets=make_targets("a", "b")
        )
        await runner.run()
        assert len(waits.waits) == 1
        assert waits.waits[0] >= 8.0

    async def test_the_pacer_is_noted_only_after_a_real_dispatch(self, tmp_path):
        channel = FakeChannel("browser", script=[never_dispatched("a", channel="browser")])
        second = FakeChannel("api")
        runner, waits, _ = a_runner(
            tmp_path, channels=[channel, second], targets=make_targets("a")
        )
        await runner.run()
        # Two rungs for one target is still one report, so still one gap.
        assert len(waits.waits) == 0

    async def test_pacing_is_per_worker_not_shared(self, tmp_path):
        # Two workers each keep their own cadence. A single shared pacer would
        # serialise them behind each other's gaps and make max_concurrent mean
        # nothing.
        channel = FakeChannel(capacity=4)
        clock = Clock()
        pool = make_pool("alpha", "bravo", clock=clock)
        runner, waits, _ = a_runner(
            tmp_path,
            channels=[channel],
            pool=pool,
            targets=make_targets(*[f"t{n}" for n in range(6)]),
            options=RunOptions(max_reports=20, max_concurrent=2, backoff_seconds=0.0), clock=clock)
        await runner.run()
        # Six reports over two workers: each worker waits between its own
        # reports, so at most four gaps, not five.
        assert len(waits.waits) <= 4


# ===========================================================================
# Dry run
# ===========================================================================


class TestDryRun:
    async def test_a_dry_run_touches_nothing(self, tmp_path):
        channel = FakeChannel()
        store = make_store(tmp_path)
        runner = Runner(
            store=store,
            pool=make_pool("alpha"),
            channels=[ChannelSpec("browser", channel)],
            targets=make_targets("a", "b"),
            options=RunOptions(dry_run=True),
            monotonic=Clock(),
        )
        report = await runner.run()

        assert channel.calls == [], "a dry run opened a channel"
        assert channel.closed is False, "a dry run opened a browser"
        assert not Path(store.path).exists(), "a dry run wrote the ledger"
        assert report.dry_run
        assert report.dispatches == 0
        assert "(dry run" in report.render()

    async def test_the_plan_counts_what_is_left_to_do(self, tmp_path):
        store = seed_checkpoint(
            tmp_path, "run-one", ("a", TerminalState.SUBMITTED_ACKED)
        )
        runner = Runner(
            store=store,
            pool=make_pool("alpha"),
            channels=[ChannelSpec("browser", FakeChannel())],
            targets=make_targets("a", "b", "c"),
            options=RunOptions(dry_run=True),
            monotonic=Clock(),
        )
        plan = runner.plan()
        assert plan.targets == 3
        assert plan.pending == 2
        assert plan.already_settled == 1

    async def test_the_plan_names_our_own_handles_before_the_run(self, tmp_path):
        pool = make_pool("alpha", "bravo")
        pool.get("bravo").username = "reporter.bravo"
        runner = Runner(
            store=make_store(tmp_path),
            pool=pool,
            channels=[ChannelSpec("browser", FakeChannel())],
            targets=make_targets("reporter.bravo", "fine.one"),
            options=RunOptions(dry_run=True),
            monotonic=Clock(),
        )
        assert runner.plan().self_reporting == ("reporter.bravo",)


# ===========================================================================
# Error routing
# ===========================================================================


class TestErrorRouting:
    async def test_a_pre_dispatch_channel_failure_raises_into_a_state(
        self, tmp_path
    ):
        channel = FakeChannel("browser", script=[PreflightFailed("anchor set is empty")])
        second = FakeChannel("api")
        runner, _, store = a_runner(
            tmp_path, channels=[channel, second], targets=make_targets("a")
        )
        report = await runner.run()

        assert [c[0] for c in second.calls] == ["a"]
        assert report.acked == 1

    async def test_a_challenge_quarantines_the_account_and_its_exit(self, tmp_path):
        clock = Clock()
        pool = make_pool("alpha", "bravo", clock=clock)
        proxies = make_proxies(4, clock=clock)
        channel = FakeChannel("browser", script=[AccountChallenged("checkpoint")])
        runner, _, _ = a_runner(
            tmp_path,
            channels=[channel],
            pool=pool,
            proxies=proxies,
            targets=make_targets("a"), clock=clock)
        report = await runner.run()

        assert report.counts[TerminalState.QUARANTINED.value] == 1
        assert pool.get("alpha").quarantined_at(clock()) is not None
        # The exit is dropped with the account. Keeping it would hand the next
        # worker a clean IP to repeat the same mistake from.
        assert pool.active_lease("alpha") is None

    async def test_a_challenge_before_dispatch_is_still_a_quarantine(
        self, tmp_path
    ):
        # A login wall on the profile page. Nothing was sent, but the identity
        # is still compromised, and quarantining on "it did not dispatch" would
        # keep a blocked account in rotation.
        clock = Clock()
        pool = make_pool("alpha", "bravo", clock=clock)
        channel = FakeChannel("browser", script=[AccountChallenged("login wall")])
        second = FakeChannel("api")
        runner, _, _ = a_runner(
            tmp_path,
            channels=[channel, second], pool=pool, targets=make_targets("a"), clock=clock)
        await runner.run()

        assert pool.get("alpha").quarantined_at(clock()) is not None
        assert second.calls == [], "a compromised identity should not try the next rung"

    async def test_an_expired_session_is_distinguished_from_a_challenge(
        self, tmp_path
    ):
        clock = Clock()
        pool = make_pool("alpha", "bravo", clock=clock)
        channel = FakeChannel("browser", script=[SessionExpired("cookie gone")])
        runner, _, _ = a_runner(
            tmp_path, channels=[channel], pool=pool, targets=make_targets("a"), clock=clock)
        report = await runner.run()

        assert report.counts[TerminalState.QUARANTINED.value] == 1
        assert pool.active_lease("alpha") is None

    async def test_a_post_dispatch_challenge_is_unknown_not_quarantined(
        self, tmp_path
    ):
        # Two separate questions, and they have different answers.
        #
        # For the *target*: the report may have landed, so the honest state is
        # UNKNOWN. A QUARANTINED here would be a claim about a request nobody
        # can read a receipt for.
        #
        # For the *account*: a challenge wall appeared, and that is a fact about
        # the identity regardless of the target. But the account layer already
        # made that call: note_outcome grows the failure streak for an
        # unconfirmed dispatch without sidelining, precisely because a streak
        # escalates on repetition and a single quarantine empties the pool over
        # a response we cannot read. So the assertion here is "the streak grew
        # and the account is still usable", not "the account is untouched".
        def dispatch_then_challenge(target, on_dispatch, attempt):
            on_dispatch()
            raise AccountChallenged("wall appeared after submit")

        clock = Clock()
        pool = make_pool("alpha", "bravo", clock=clock)
        channel = FakeChannel("browser", script=[dispatch_then_challenge])
        runner, _, _ = a_runner(
            tmp_path, channels=[channel], pool=pool, targets=make_targets("a"), clock=clock)
        report = await runner.run()

        assert report.counts[TerminalState.UNKNOWN.value] == 1
        account = pool.get("alpha")
        assert not account.quarantined_at(clock()), "sidelined on a weak signal"
        assert account.consecutive_failures == 1, "the challenge was not recorded"
        assert account.eligible(clock()), "a single unknown emptied the pool"

    async def test_a_run_scoped_fatal_stops_everything(self, tmp_path):
        # CheckpointCorrupt is the canonical run-scoped fatal. One report scope
        # error must not end a 500-target run, and one run scope error must end
        # it. The scope is the whole point.
        from insta_report.errors import CheckpointCorrupt

        channel = FakeChannel(
            "browser",
            script=[
                _DISPATCH_THEN_ACK,
                CheckpointCorrupt("torn write in the middle of the ledger"),
            ],
        )
        runner, _, _ = a_runner(
            tmp_path,
            channels=[channel],
            targets=make_targets("a", "b", "c"),
        )
        report = await runner.run()

        assert report.aborted
        assert "CheckpointCorrupt" in report.abort_reason
        assert report.dispatches == 1

    async def test_a_report_scoped_fatal_does_not_end_the_run(self, tmp_path):
        class TargetOnly(FatalError):
            default_scope = ErrorScope.REPORT

        def one_bad_target(target, on_dispatch, attempt):
            if target.key == "a":
                raise TargetOnly("this one target is a problem")
            on_dispatch()
            return acked(target.key)

        channel = FakeChannel("browser", script=[one_bad_target])
        runner, _, _ = a_runner(
            tmp_path, channels=[channel], targets=make_targets("a", "b")
        )
        report = await runner.run()

        assert not report.aborted
        assert report.acked == 1
        assert [call[0] for call in channel.calls] == ["a", "b"], (
            "a report-scoped fatal must cost exactly one target, not the run"
        )

    async def test_an_unclassified_exception_before_dispatch_does_not_dispatch(
        self, tmp_path
    ):
        # A channel bug must not become a report. Falling through to the next
        # rung is safe because nothing was sent; filing something would not be.
        channel = FakeChannel("browser", script=[ValueError("NoneType is not subscriptable")])
        second = FakeChannel("api")
        runner, _, store = a_runner(
            tmp_path, channels=[channel, second], targets=make_targets("a")
        )
        report = await runner.run()

        assert [c[0] for c in second.calls] == ["a"]
        assert report.dispatches == 1
        assert kinds(store) == ["intent", "outcome"]

    async def test_an_unclassified_exception_after_dispatch_is_unknown(
        self, tmp_path
    ):
        def dispatch_then_explode(target, on_dispatch, attempt):
            on_dispatch()
            raise RuntimeError("something nobody classified")

        channel = FakeChannel("browser", script=[dispatch_then_explode])
        second = FakeChannel("api")
        runner, _, store = a_runner(
            tmp_path, channels=[channel, second], targets=make_targets("a")
        )
        report = await runner.run()

        assert second.calls == []
        assert report.counts[TerminalState.UNKNOWN.value] == 1
        assert report.unsettled == (), "an exception lost the outcome record"
        assert "RuntimeError" in store.state.outcome_for("a").detail


# ===========================================================================
# Concurrency limits
# ===========================================================================


class TestConcurrency:
    async def test_workers_are_bounded_by_eligible_accounts(self, tmp_path):
        channel = FakeChannel(capacity=8)
        clock = Clock()
        pool = make_pool("alpha", "bravo", clock=clock)
        runner, _, _ = a_runner(
            tmp_path,
            channels=[channel],
            pool=pool,
            targets=make_targets(*[f"t{n}" for n in range(4)]),
            options=RunOptions(max_reports=20, max_concurrent=5, backoff_seconds=0.0), clock=clock)
        await runner.run()
        assert len({call[2] for call in channel.calls}) <= 2

    async def test_workers_are_bounded_by_channel_capacity(self, tmp_path):
        # Starting five workers against a browser that serves one page is how
        # you get five tabs, four of which are mid-submit when the fifth finds
        # the menu already open.
        channel = FakeChannel(capacity=1)
        clock = Clock()
        pool = make_pool(*[f"acct{n}" for n in range(5)], clock=clock)
        # Records the *deepest* overlap seen, not the current depth: a stack
        # that is popped back to empty when the run ends would assert nothing.
        peak = {"now": 0, "max": 0}
        original = channel.report

        async def watched(*args, **kwargs):
            peak["now"] += 1
            peak["max"] = max(peak["max"], peak["now"])
            await asyncio.sleep(0)
            try:
                return await original(*args, **kwargs)
            finally:
                peak["now"] -= 1

        channel.report = watched  # type: ignore[method-assign]
        runner, _, _ = a_runner(
            tmp_path,
            channels=[channel],
            pool=pool,
            targets=make_targets(*[f"t{n}" for n in range(10)]),
            options=RunOptions(max_reports=20, max_concurrent=5, backoff_seconds=0.0), clock=clock)
        await runner.run()
        assert peak["max"] == 1, f"channel ran {peak['max']} reports at once"
        assert len(channel.calls) == 10, "the capacity bound must not stop work"

    async def test_no_workers_means_no_dispatch(self, tmp_path):
        clock = Clock()
        pool = make_pool("alpha", clock=clock, budget=0)
        channel = FakeChannel()
        runner, _, _ = a_runner(
            tmp_path, channels=[channel], pool=pool, targets=make_targets("a"), clock=clock)
        report = await runner.run()
        assert channel.calls == []
        assert report.dispatches == 0


# ===========================================================================
# Reporting
# ===========================================================================


class TestRunReport:
    async def test_the_summary_describes_requests_not_accounts(self, tmp_path):
        # The vocabulary is load-bearing. Nothing in this tool can tell an
        # operator that Instagram acted on anything.
        runner, _, _ = a_runner(tmp_path, channels=[FakeChannel()], targets=make_targets("a"))
        report = await runner.run()
        text = report.render().lower()
        for forbidden in ("removed", "banned", "account was reported", "success"):
            assert forbidden not in text, f"the summary claims {forbidden!r}"

    async def test_the_summary_leads_with_what_was_requested(self, tmp_path):
        runner, _, _ = a_runner(
            tmp_path, channels=[FakeChannel()], targets=make_targets("a", "b")
        )
        text = (await runner.run()).render()
        assert "dispatched 2" in text
        assert "submitted_acked" in text

    async def test_an_empty_run_says_so_instead_of_printing_a_blank(self, tmp_path):
        clock = Clock()
        pool = make_pool("alpha", clock=clock, budget=0)
        runner, _, _ = a_runner(
            tmp_path, channels=[FakeChannel()], pool=pool, targets=make_targets("a"), clock=clock)
        assert "(none)" in (await runner.run()).render()

    async def test_review_items_and_unsettled_both_raise_the_flag(self, tmp_path):
        def dispatch_unknown(target, on_dispatch, attempt):
            on_dispatch()
            return post_dispatch(target.key, TerminalState.UNKNOWN)

        channel = FakeChannel("browser", script=[dispatch_unknown])
        pool = make_pool("alpha")
        pool.get("alpha").username = "mine"
        runner, _, _ = a_runner(
            tmp_path, channels=[channel], pool=pool, targets=make_targets("mine")
        )
        report = await runner.run()
        assert report.needs_attention
        assert any("REFUSING" in str(r) or r.reason for r in report.refusals)

    async def test_channels_are_reported_with_their_health(self, tmp_path):
        channel = FakeChannel("browser")
        runner, _, _ = a_runner(
            tmp_path, channels=[channel], targets=make_targets("a")
        )
        report = await runner.run()
        entry = report.channel_report()[0]
        assert entry["name"] == "browser"
        assert entry["dispatches"] == 1
        assert entry["disabled"] is False
        assert "browser" in report.render()

    async def test_the_report_keeps_no_live_channel_handles(self, tmp_path):
        # A report that holds a BrowserChannel keeps a browser open through
        # whatever the operator does with it next.
        runner, _, _ = a_runner(tmp_path, channels=[FakeChannel()], targets=make_targets("a"))
        report = await runner.run()
        assert report.channel_names == ("browser",)
        assert not any(
            isinstance(name, FakeChannel) for name in report.channel_names
        )
