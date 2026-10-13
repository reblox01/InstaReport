"""Pacing.

The original code slept ``random.uniform(1.5, 4.0)`` between reports. That is
a guess with a plausible-looking number attached: it does not know how much
budget the account has left, how much of the run remains, or whether the
account is about to be throttled into uselessness.

The rule here is that an account should **slow down before it stops**, not hit a
wall. That means the gap is derived from how much allowance is left and how much
time is left to spend it in, so the last report of a budget arrives at the same
cadence as the first rather than in a final scramble that looks exactly like an
account being flagged.

```
  budget left                     time left in horizon
  ┌──────────────┐                ┌──────────────┐
  │ 20/20  ████  │  floor ──────► │ 1.5s         │  even spread, whole day
  │  5/20  █     │  rising ────► │ 6.4s         │  stretched to fit
  │  1/20  ░     │  steep  ────► │ 120s         │  crawling, not slamming
  └──────────────┘                └──────────────┘
```

Everything is measured with :func:`time.monotonic`. A wall clock would let an NTP
correction or a DST transition double a gap or collapse one to zero, and a
collapsed gap is indistinguishable from not pacing at all. The clock is
injectable so the tests can run years of schedule in microseconds.

On top of the derivation sits the part that is not arithmetic: uniform jitter,
and — with :attr:`PacingConfig.pause_chance` — an occasional pause a person
would take. A schedule with a ceiling on every gap is periodic however wide
the band, and periodicity is the signal, not slowness.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Callable

__all__ = [
    "Pacer",
    "PacingConfig",
    "MIN_GAP_SECONDS",
    "PAUSE_STRETCH_MIN",
    "PAUSE_STRETCH_MAX",
]

log = logging.getLogger(__name__)

#: Absolute floor. Whatever the arithmetic says, two reports are never sent back
#: to back, because that is the shape of a burst and bursts are what get an
#: account challenged.
MIN_GAP_SECONDS = 8.0

#: Never wait longer than this between reports within a run.
#:
#: This has to sit comfortably *above* the spacing a normal budget produces, or
#: the "will not last" warning fires on the default configuration and operators
#: learn to ignore it -- at which point it is useless for the case that matters.
#: A 20-report budget spread over the 6-hour default horizon derives ~15 minutes
#: per report, so an hour is generous headroom that only a genuinely
#: over-provisioned run reaches.
MAX_GAP_SECONDS = 3600.0

#: The pause multiplier band. When a pause fires (see
#: :attr:`PacingConfig.pause_chance`) the gap is multiplied by a uniform draw
#: from this range. Multiplicative so it scales with the cadence it lands on:
#: a pause on top of a 15-minute gap is a coffee break, on top of the 8-second
#: floor it is a glance away. The pair (chance, stretch) is chosen so the
#: throughput cost stays near 15% -- see ``compute_gap``.
PAUSE_STRETCH_MIN = 1.5
PAUSE_STRETCH_MAX = 3.5


@dataclass(frozen=True)
class PacingConfig:
    """Operator-tunable pacing knobs.

    ``floor_gap`` replaces the old ``random.uniform(1.5, 4.0)`` for the
    well-provisioned case, where there is budget and time to spare. It is a
    floor, not a constant: the derived figure is always at least this.

    ``horizon_fraction`` is a per-step damping factor, *not* the fraction of the
    horizon the run will consume. Because the gap is recomputed against a
    shrinking horizon each step, the factor compounds: a 20-report budget with
    ``horizon_fraction = 0.85`` consumes about 98% of its horizon, not 85%.
    That is the wanted outcome -- an attended run should not idle out the last
    quarter of its window -- but the number is a knob on per-step stretch, not
    a budget utilisation target, and reading it as the latter is how someone
    ends up halving it expecting the run to finish halfway instead.
    """

    floor_gap: float = 30.0
    jitter_fraction: float = 0.25
    #: Probability that a gap is stretched by a human-sized factor
    #: (:data:`PAUSE_STRETCH_MIN`..:data:`PAUSE_STRETCH_MAX`), drawn after
    #: the jitter so a pause is a pause on top of the normal variation.
    #:
    #: Zero here on purpose. This is the library component, and a default that
    #: draws from the generator would change every seeded schedule in every
    #: test and every incident replay -- a coin no test can pin. The *product*
    #: default is 0.10, set where operator config is parsed (``[run]
    #: pause_chance``): the tool humanizes by default, the component does it
    #: only when asked, and the two defaults are one line apart in cli.py.
    pause_chance: float = 0.0
    #: Share of the remaining horizon one report's gap is allowed to claim.
    horizon_fraction: float = 0.85
    #: Used when no horizon is supplied -- an attended run is assumed to be
    #: several hours, not a minute.
    default_horizon_seconds: float = 6 * 3600.0


class Pacer:
    """Computes and waits out the gap between reports.

    Wait, then report -- not the reverse. ``wait()`` is called *before* a
    dispatch, and returns immediately if enough time has already passed, so a
    slow target does not add a second delay on top of the one that is about to
    be spent loading it.
    """

    def __init__(
        self,
        config: PacingConfig | None = None,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.config = config or PacingConfig()
        self._monotonic = monotonic
        self._sleep = sleeper
        self._rng = rng or random.Random()
        self._last_dispatch: float | None = None
        self._last_computed: float = 0.0
        self._gaps: list[float] = []

    # -- arithmetic -----------------------------------------------------

    def compute_gap(
        self,
        *,
        budget_remaining: int,
        budget_total: int,
        horizon_remaining: float | None = None,
    ) -> float:
        """Seconds to wait before the next report.

        ``budget_remaining`` of zero means the account is done; the answer is
        infinity, which the caller should treat as "do not report" rather than
        as a very long sleep.
        """
        if budget_remaining <= 0:
            return float("inf")

        cfg = self.config
        if horizon_remaining is None:
            horizon_remaining = cfg.default_horizon_seconds
        horizon_remaining = max(0.0, horizon_remaining) * cfg.horizon_fraction

        # The natural spacing if the remaining allowance were spread evenly
        # over the time that is actually left. This is the term that makes an
        # account slow down before it runs out: as budget_remaining falls and
        # the denominator holds, the quotient rises.
        if horizon_remaining > 0:
            even = horizon_remaining / budget_remaining
        else:
            # No time left to spread into. Fall back to the floor rather than
            # to zero -- a zero gap is a burst.
            even = cfg.floor_gap

        base = max(cfg.floor_gap, even, MIN_GAP_SECONDS)

        if base > MAX_GAP_SECONDS:
            # Only warn when the cap is actually binding. A figure a hair over
            # the cap is not a budget problem, and warning on it would make the
            # warning meaningless.
            shortfall = base - MAX_GAP_SECONDS
            log.warning(
                "derived gap %.0fs exceeds the %.0fs cap by %.0fs: %d reports "
                "left for %.0fs of horizon. The run will stop before the horizon "
                "ends. Raise the budget, extend the run, or accept the cap.",
                base,
                MAX_GAP_SECONDS,
                shortfall,
                budget_remaining,
                horizon_remaining,
            )
            base = MAX_GAP_SECONDS

        jitter = base * cfg.jitter_fraction
        gap = base + self._rng.uniform(-jitter, jitter)

        # The part arithmetic cannot derive: a person steps away from the
        # task. Without this every gap sits inside one narrow band -- however
        # wide the band -- and a band has a ceiling, which is periodicity with
        # better manners. With probability pause_chance the gap stretches by
        # a human-sized factor instead. Multiplicative, so the pause scales
        # with the cadence it lands on. At the shipped pairing (0.10 chance,
        # 1.5-3.5x) this costs about 15% of throughput: the price of not
        # being periodic, against a budget that is self-imposed anyway.
        if cfg.pause_chance > 0.0 and self._rng.random() < cfg.pause_chance:
            gap *= self._rng.uniform(PAUSE_STRETCH_MIN, PAUSE_STRETCH_MAX)

        # Both clamps on the final figure rather than the base, because the
        # base is not what the schedule waits: jitter can push a capped base
        # back over MAX, and a pause multiplies whatever it is handed. The
        # contract is about the gap, so the gap is what gets bounded.
        return min(MAX_GAP_SECONDS, max(MIN_GAP_SECONDS, gap))

    # -- waiting --------------------------------------------------------

    def time_since_dispatch(self) -> float:
        if self._last_dispatch is None:
            return float("inf")
        return self._monotonic() - self._last_dispatch

    def wait(
        self,
        *,
        budget_remaining: int,
        budget_total: int,
        horizon_remaining: float | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> float:
        """Sleep until the next dispatch is due. Returns the gap applied.

        Returns immediately when enough time has already elapsed, so pacing
        never adds delay on top of work that was slow anyway.
        """
        due = self._due(
            budget_remaining=budget_remaining,
            budget_total=budget_total,
            horizon_remaining=horizon_remaining,
        )
        if due is None:
            return 0.0
        target_gap, remaining = due
        if remaining > 0:
            (sleeper or self._sleep)(remaining)
            self._gaps.append(target_gap)
        self._last_computed = target_gap
        return max(0.0, remaining)

    async def async_wait(
        self,
        *,
        budget_remaining: int,
        budget_total: int,
        horizon_remaining: float | None = None,
        sleeper: Callable[[float], Any] | None = None,
    ) -> float:
        """Await the gap instead of blocking on it.

        Same arithmetic and the same return contract as :meth:`wait`, because
        the runner is asyncio end to end and a ``time.sleep`` here would freeze
        every other worker for the length of the gap. With a derived gap that
        can reach :data:`MAX_GAP_SECONDS`, that is not a rounding error -- it is
        every other lease sitting idle for an hour.

        *sleeper* may be a coroutine function, in which case it is awaited, so a
        test can run a whole schedule without real time passing.
        """
        due = self._due(
            budget_remaining=budget_remaining,
            budget_total=budget_total,
            horizon_remaining=horizon_remaining,
        )
        if due is None:
            return 0.0
        target_gap, remaining = due
        if remaining > 0:
            if sleeper is not None:
                result = sleeper(remaining)
                if inspect.isawaitable(result):
                    await result
            else:
                await asyncio.sleep(remaining)
            self._gaps.append(target_gap)
        self._last_computed = target_gap
        return max(0.0, remaining)

    def _due(
        self,
        *,
        budget_remaining: int,
        budget_total: int,
        horizon_remaining: float | None,
    ) -> tuple[float, float] | None:
        """Shared due-time arithmetic. ``None`` means "do not report at all".

        Both wait paths call this so the sync and async versions cannot drift
        into disagreeing about when a report is due.
        """
        target_gap = self.compute_gap(
            budget_remaining=budget_remaining,
            budget_total=budget_total,
            horizon_remaining=horizon_remaining,
        )
        if target_gap == float("inf"):
            return None
        return target_gap, target_gap - self.time_since_dispatch()

    def note_dispatch(self) -> None:
        """Called immediately after a report goes out.

        Separate from ``wait()`` so the measured interval covers the dispatch
        itself rather than only the gap: the clock starts when the report lands,
        not when the sleep finished.
        """
        self._last_dispatch = self._monotonic()

    def reset(self) -> None:
        """Forget the last dispatch. For a fresh run, never mid-run."""
        self._last_dispatch = None
        self._gaps.clear()

    # -- introspection --------------------------------------------------

    @property
    def last_gap(self) -> float:
        return self._last_computed

    def observed_gaps(self) -> list[float]:
        return list(self._gaps)

    def describe(
        self,
        *,
        budget_remaining: int,
        budget_total: int,
        horizon_remaining: float | None = None,
    ) -> str:
        gap = self.compute_gap(
            budget_remaining=budget_remaining,
            budget_total=budget_total,
            horizon_remaining=horizon_remaining,
        )
        if gap == float("inf"):
            return f"no reports left ({budget_remaining} of {budget_total})"
        return (
            f"next report in ~{gap:.0f}s "
            f"({budget_remaining}/{budget_total} left, "
            f"{horizon_remaining or self.config.default_horizon_seconds:.0f}s of horizon)"
        )
