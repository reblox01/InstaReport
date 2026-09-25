"""Pacing tests.

The property that matters is stated in the module docstring: an account must
**slow down before it stops**. A fixed sleep cannot do that -- it is the same
gap whether the account has one report left or twenty -- so the central test
here sweeps a whole budget and asserts the gap rises as allowance falls.

Two regimes exist and the tests name which one they are in, because conflating
them is what made the first draft of this file fail:

```
  derived  = horizon * 0.85 / budget_remaining     <- dominates when time is tight
  floor_gap= operator's floor                      <- dominates when time is ample
  gap      = max(floor_gap, derived, MIN_GAP)      <- then jittered
```

A 20-report budget over the 6-hour default horizon derives ~15 minutes per
report, so the floor is irrelevant there; the floor only wins when there is far
more budget than horizon. Tests that want the floor therefore supply a short
horizon or a large budget on purpose.

Jitter is seeded in every test asserting an exact figure, because a randomised
assertion is a flaky assertion.
"""

from __future__ import annotations

import random

import pytest

from insta_report.pacing import (
    MAX_GAP_SECONDS,
    MIN_GAP_SECONDS,
    Pacer,
    PacingConfig,
)

HOUR = 3600.0


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_pacer(clock: FakeClock | None = None, **config) -> Pacer:
    clock = clock or FakeClock()
    return Pacer(
        PacingConfig(**config),
        monotonic=clock,
        sleeper=clock.sleep,
        rng=random.Random(1234),
    )


# --- the headline property --------------------------------------------------


def test_the_gap_rises_as_the_budget_depletes():
    """Slow down before stopping, not at the wall.

    A constant sleep is the old behaviour and it is why an account burns its
    allowance in the first hour and then has nothing left. The horizon here is
    short enough that no figure reaches the cap, so the growth is the real
    derived curve rather than a plateau.
    """
    pacer = make_pacer(jitter_fraction=0.0, floor_gap=1.0)
    gaps = [
        pacer.compute_gap(budget_remaining=n, budget_total=20, horizon_remaining=HOUR)
        for n in range(20, 0, -1)
    ]
    assert gaps == sorted(gaps), f"gap must never shrink as budget falls: {gaps}"
    assert all(b > a for a, b in zip(gaps, gaps[1:])), f"gap must rise strictly: {gaps}"
    # Growth is 1/n, so the ratio across the budget is the budget itself.
    assert gaps[-1] / gaps[0] == pytest.approx(20, rel=0.01)


def test_the_gap_grows_steeply_at_the_tail():
    """The last few reports must be far apart, not merely different.

    Linear growth would mean a fixed sleep with a different name. The final
    report of a budget is the one most likely to look like a flagged account, so
    it is the one that must be slowest.
    """
    pacer = make_pacer(jitter_fraction=0.0, floor_gap=1.0)
    horizon = HOUR
    full = pacer.compute_gap(budget_remaining=20, budget_total=20, horizon_remaining=horizon)
    tail = pacer.compute_gap(budget_remaining=2, budget_total=20, horizon_remaining=horizon)
    assert tail / full == pytest.approx(10, rel=0.01)


def test_the_gap_is_monotonic_across_a_simulated_budget_spend():
    """End to end: spend a budget with the pacer driving, and gaps rise.

    The schedule is allowed to consume *more* than ``horizon_fraction`` of the
    horizon, because the gap is recomputed against a shrinking horizon each
    step and the factor compounds -- a 20-report budget uses about 98% of its
    window. What it may never do is exceed the horizon, or stop early while
    reports remain, so both bounds are asserted here.
    """
    clock = FakeClock()
    pacer = make_pacer(clock, jitter_fraction=0.0, floor_gap=1.0)
    total = 20
    horizon = HOUR

    observed = []
    for used in range(total):
        gap = pacer.compute_gap(
            budget_remaining=total - used,
            budget_total=total,
            horizon_remaining=horizon,
        )
        observed.append(gap)
        pacer.note_dispatch()
        clock.advance(gap)
        horizon = max(0.0, horizon - gap)

    assert observed == sorted(observed)
    assert observed[0] < observed[-1]
    assert sum(observed) <= HOUR, "the schedule overspent its own horizon"
    assert sum(observed) > HOUR * 0.8, "the schedule left the horizon mostly unused"


def test_a_recomputed_schedule_consumes_most_of_its_horizon():
    """Pins the compounding behaviour the docstring describes.

    Without this, a future change to ``horizon_fraction`` could quietly halve
    horizon utilisation -- or double it into an overspend -- and the docstring
    would keep claiming whatever the author remembered.
    """
    clock = FakeClock()
    pacer = make_pacer(clock, jitter_fraction=0.0, floor_gap=1.0)
    horizon = HOUR
    spent = 0.0
    for remaining in range(20, 0, -1):
        gap = pacer.compute_gap(
            budget_remaining=remaining,
            budget_total=20,
            horizon_remaining=horizon,
        )
        spent += gap
        horizon = max(0.0, horizon - gap)

    assert spent / HOUR == pytest.approx(0.987, abs=0.02)


def test_a_default_budget_spreads_over_the_default_horizon():
    """The out-of-the-box schedule is a whole afternoon, not a burst.

    20 reports over 6 hours works out to roughly 15 minutes apart, which is the
    point: report weight comes from a trusted account filing correct reports
    over time, and the old 1.5-4s sleep was the behaviour this replaces.
    """
    pacer = make_pacer(jitter_fraction=0.0)
    gap = pacer.compute_gap(budget_remaining=20, budget_total=20)
    assert gap == pytest.approx(6 * HOUR * 0.85 / 20, rel=0.01)
    assert gap > 600.0


def test_the_default_configuration_does_not_trigger_the_cap_warning(caplog):
    """The warning has to stay meaningful, so it must be silent on the defaults.

    An earlier cap of 900s sat right at the default configuration's 918s figure,
    which meant the very first report of a normal run shouted about a budget
    problem that did not exist. A warning that fires on the common case is a
    warning nobody reads.
    """
    pacer = make_pacer(jitter_fraction=0.0)
    with caplog.at_level("WARNING"):
        gap = pacer.compute_gap(budget_remaining=20, budget_total=20)
    assert caplog.records == []
    assert gap < MAX_GAP_SECONDS


# --- floors and caps --------------------------------------------------------


def test_zero_budget_means_never_not_a_very_long_sleep():
    """Infinity, because the caller's correct response is to stop, not to sleep."""
    pacer = make_pacer()
    assert pacer.compute_gap(budget_remaining=0, budget_total=20) == float("inf")


def test_negative_budget_also_means_never():
    pacer = make_pacer()
    assert pacer.compute_gap(budget_remaining=-1, budget_total=20) == float("inf")


def test_the_configured_floor_wins_when_there_is_far_more_budget_than_time():
    """Floor regime: 500 reports to fit in 10 minutes needs no stretching."""
    pacer = make_pacer(floor_gap=45.0, jitter_fraction=0.0)
    gap = pacer.compute_gap(
        budget_remaining=500, budget_total=500, horizon_remaining=10 * 60.0
    )
    assert gap == 45.0


def test_the_derived_schedule_wins_when_budget_is_the_binding_constraint():
    pacer = make_pacer(floor_gap=45.0, jitter_fraction=0.0)
    gap = pacer.compute_gap(budget_remaining=2, budget_total=20, horizon_remaining=HOUR)
    assert gap > 45.0
    assert gap == pytest.approx(HOUR * 0.85 / 2, rel=0.01)


def test_the_absolute_minimum_is_never_breached():
    """Two reports are never sent back to back, whatever the config says."""
    pacer = make_pacer(floor_gap=0.0, jitter_fraction=0.9)
    for n in (1, 2, 50):
        gap = pacer.compute_gap(
            budget_remaining=n, budget_total=50, horizon_remaining=0.0
        )
        assert gap >= MIN_GAP_SECONDS


def test_a_binding_cap_warns_and_quantifies_the_shortfall(caplog):
    """Only warn when the cap actually costs the run time.

    A figure a hair over the cap is not a budget problem; warning on it would
    make the warning noise.
    """
    pacer = make_pacer(floor_gap=1.0, jitter_fraction=0.0)
    with caplog.at_level("WARNING"):
        gap = pacer.compute_gap(
            budget_remaining=1,
            budget_total=20,
            horizon_remaining=MAX_GAP_SECONDS * 10,
        )
    assert gap == MAX_GAP_SECONDS
    messages = [r.message for r in caplog.records]
    assert len(messages) == 1
    assert "exceeds" in messages[0] and "stop before the horizon ends" in messages[0]


def test_no_horizon_uses_the_configured_default():
    pacer = make_pacer(
        default_horizon_seconds=2 * HOUR, jitter_fraction=0.0, floor_gap=1.0
    )
    explicit = pacer.compute_gap(budget_remaining=10, budget_total=20, horizon_remaining=2 * HOUR)
    implied = pacer.compute_gap(budget_remaining=10, budget_total=20)
    assert explicit == implied


def test_a_squeezed_horizon_never_produces_a_zero_gap():
    """No time left to spread into means the floor, not zero. Zero is a burst."""
    pacer = make_pacer(floor_gap=20.0, jitter_fraction=0.0)
    gap = pacer.compute_gap(
        budget_remaining=500, budget_total=500, horizon_remaining=0.0
    )
    assert gap == 20.0


def test_a_zero_horizon_fraction_leaves_only_the_floor():
    pacer = make_pacer(floor_gap=33.0, jitter_fraction=0.0, horizon_fraction=0.0)
    gap = pacer.compute_gap(budget_remaining=10, budget_total=20, horizon_remaining=HOUR)
    assert gap == 33.0


# --- jitter -----------------------------------------------------------------


def test_jitter_stays_within_its_fraction():
    """Floor regime, so the band is centred on the configured floor."""
    pacer = Pacer(
        PacingConfig(floor_gap=100.0, jitter_fraction=0.25),
        monotonic=FakeClock(),
        rng=random.Random(7),
    )
    gaps = [
        pacer.compute_gap(
            budget_remaining=500, budget_total=500, horizon_remaining=10 * 60.0
        )
        for _ in range(200)
    ]
    assert all(75.0 <= g <= 125.0 for g in gaps), (min(gaps), max(gaps))


def test_jitter_perturbs_the_derived_schedule_too():
    """Jitter is not only a floor-regime decoration."""
    pacer = Pacer(
        PacingConfig(floor_gap=1.0, jitter_fraction=0.25),
        monotonic=FakeClock(),
        rng=random.Random(7),
    )
    gaps = {
        pacer.compute_gap(budget_remaining=20, budget_total=20, horizon_remaining=HOUR)
        for _ in range(50)
    }
    assert len(gaps) > 1, "jitter should not be a constant"
    base = HOUR * 0.85 / 20
    assert all(base * 0.75 <= g <= base * 1.25 for g in gaps)


def test_jitter_is_reproducible_with_a_seeded_generator():
    """Two pacers with the same seed must schedule identically.

    Otherwise an incident cannot be replayed, which is the whole reason the
    generator is injectable.
    """
    a = Pacer(PacingConfig(), monotonic=FakeClock(), rng=random.Random(99))
    b = Pacer(PacingConfig(), monotonic=FakeClock(), rng=random.Random(99))
    assert [a.compute_gap(budget_remaining=5, budget_total=20) for _ in range(5)] == [
        b.compute_gap(budget_remaining=5, budget_total=20) for _ in range(5)
    ]


def test_jitter_never_pushes_below_the_absolute_minimum():
    pacer = Pacer(
        PacingConfig(floor_gap=MIN_GAP_SECONDS, jitter_fraction=0.9),
        monotonic=FakeClock(),
        rng=random.Random(3),
    )
    for _ in range(300):
        gap = pacer.compute_gap(
            budget_remaining=500, budget_total=500, horizon_remaining=0.0
        )
        assert gap >= MIN_GAP_SECONDS


# --- waiting ----------------------------------------------------------------


def test_the_first_wait_after_a_lease_returns_immediately():
    """Nothing has been dispatched yet, so nothing needs to be waited out."""
    clock = FakeClock()
    pacer = make_pacer(clock)
    assert pacer.wait(budget_remaining=20, budget_total=20) == 0.0
    assert clock.slept == []


def test_wait_sleeps_the_remainder_after_a_dispatch():
    clock = FakeClock()
    pacer = make_pacer(clock, floor_gap=30.0, jitter_fraction=0.0)
    pacer.note_dispatch()

    slept = pacer.wait(
        budget_remaining=500, budget_total=500, horizon_remaining=10 * 60.0
    )
    assert slept == 30.0
    assert clock.slept == [30.0]


def test_wait_returns_immediately_when_enough_time_already_passed():
    """Pacing must not add delay on top of work that was slow anyway."""
    clock = FakeClock()
    pacer = make_pacer(clock, floor_gap=30.0, jitter_fraction=0.0)
    pacer.note_dispatch()
    clock.advance(120.0)

    assert (
        pacer.wait(
            budget_remaining=500, budget_total=500, horizon_remaining=10 * 60.0
        )
        == 0.0
    )
    assert clock.slept == []


def test_wait_counts_only_the_untouched_remainder():
    clock = FakeClock()
    pacer = make_pacer(clock, floor_gap=100.0, jitter_fraction=0.0)
    pacer.note_dispatch()
    clock.advance(70.0)

    assert (
        pacer.wait(
            budget_remaining=500, budget_total=500, horizon_remaining=10 * 60.0
        )
        == 30.0
    )
    assert clock.slept == [30.0]


def test_wait_on_an_exhausted_budget_sleeps_nothing():
    clock = FakeClock()
    pacer = make_pacer(clock)
    pacer.note_dispatch()
    assert pacer.wait(budget_remaining=0, budget_total=20) == 0.0
    assert clock.slept == []


def test_note_dispatch_starts_the_interval_from_the_dispatch_not_the_sleep():
    """The measured gap must cover the dispatch itself.

    Starting the clock when the sleep ended would make the effective gap
    sleep+dispatch, which is drift the operator never asked for.
    """
    clock = FakeClock()
    pacer = make_pacer(clock, floor_gap=10.0, jitter_fraction=0.0)
    pacer.wait(budget_remaining=500, budget_total=500)  # no-op: nothing dispatched
    pacer.note_dispatch()

    clock.advance(5.0)  # the report itself took 5s
    assert pacer.time_since_dispatch() == 5.0
    assert (
        pacer.wait(budget_remaining=500, budget_total=500, horizon_remaining=10 * 60.0)
        == 5.0
    )


def test_a_custom_sleeper_overrides_the_injected_one():
    calls: list[float] = []
    clock = FakeClock()
    pacer = make_pacer(clock, floor_gap=12.0, jitter_fraction=0.0)
    pacer.note_dispatch()
    pacer.wait(
        budget_remaining=500,
        budget_total=500,
        horizon_remaining=10 * 60.0,
        sleeper=calls.append,
    )
    assert calls == [12.0]
    assert clock.slept == []


# --- reset ------------------------------------------------------------------


def test_reset_forgets_the_last_dispatch():
    clock = FakeClock()
    pacer = make_pacer(clock, floor_gap=30.0, jitter_fraction=0.0)
    pacer.note_dispatch()
    pacer.reset()
    assert pacer.wait(budget_remaining=20, budget_total=20) == 0.0


def test_reset_clears_observed_gaps():
    pacer = make_pacer()
    pacer._gaps.append(1.0)  # noqa: SLF001
    pacer.reset()
    assert pacer.observed_gaps() == []


def test_observed_gaps_are_a_copy():
    pacer = make_pacer()
    pacer._gaps.append(1.0)  # noqa: SLF001
    pacer.observed_gaps().append(2.0)
    assert pacer.observed_gaps() == [1.0]


# --- clock discipline -------------------------------------------------------


def test_pacing_uses_monotonic_time_only():
    """A wall clock would let an NTP correction collapse a gap to zero.

    There is no wall-clock parameter to pass and ``time_since_dispatch`` reads
    the injected monotonic clock, so the guarantee is structural rather than a
    matter of trusting the arithmetic.
    """
    clock = FakeClock(start=987_654.321)
    pacer = make_pacer(clock, floor_gap=30.0, jitter_fraction=0.0)
    pacer.note_dispatch()
    assert pacer.time_since_dispatch() == 0.0
    clock.advance(45.0)
    assert pacer.time_since_dispatch() == 45.0


def test_computing_a_gap_never_sleeps_or_reads_the_clock():
    """``compute_gap`` is pure arithmetic.

    If it consulted the clock or slept, the same inputs could give different
    answers at different moments, and the schedule would not be reproducible
    from a recorded config.
    """
    import inspect

    from insta_report import pacing

    source = inspect.getsource(pacing.Pacer.compute_gap)
    assert "self._sleep" not in source
    assert "self._monotonic" not in source
    assert "time." not in source


def test_the_injected_sleeper_is_the_only_thing_that_waits():
    """A default ``time.sleep`` anywhere else would make tests slow and the
    schedule un-replayable."""
    import inspect

    from insta_report import pacing

    source = inspect.getsource(pacing)
    # The only bare time.sleep is the default argument of __init__.
    assert source.count("time.sleep") == 1
    assert "sleeper: Callable[[float], None] = time.sleep" in source


# --- diagnostics ------------------------------------------------------------


def test_describe_reports_the_next_gap():
    pacer = make_pacer(floor_gap=30.0, jitter_fraction=0.0, default_horizon_seconds=6 * HOUR)
    text = pacer.describe(budget_remaining=10, budget_total=20)
    assert "next report in" in text
    assert "10/20" in text


def test_describe_says_so_when_there_is_nothing_left():
    pacer = make_pacer()
    assert "no reports left" in pacer.describe(budget_remaining=0, budget_total=20)


def test_last_gap_reflects_the_last_computation():
    clock = FakeClock()
    pacer = make_pacer(clock, floor_gap=55.0, jitter_fraction=0.0)
    pacer.wait(
        budget_remaining=500, budget_total=500, horizon_remaining=10 * 60.0
    )
    assert pacer.last_gap == 55.0
