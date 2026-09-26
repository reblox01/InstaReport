"""T10: the pre-flight check, and the gate in front of a run.

**Why this exists.** Every other layer of this tool reports what it observes,
and every one of them observes *after* the damage. A browser that will not
launch is discovered at the first click. A session cookie Instagram has
invalidated is discovered as a login wall, on the account that gets challenged
for it. A Chromium build that was never downloaded is discovered halfway through
a target list. Each of those is a truthful report, and each of them arrives too
late to be worth acting on.

So the checks live here, in one place, ordered cheapest-first, and a run is
refused before it dispatches anything if no channel survives them.

**What the channel rehearsal does and does not do.** It stops one click short of
submitting, because that is the only point at which verification is free:

```
  launch  ->  profile  ->  menu  ->  reason list  ->  category  ->  SUBMIT
                                                                  |
                                          the doctor stops here, and does not click
```

Everything to the left of that line is verified against a real, live page: that
the build launches, that the session is still good, that the report dialog
opens, and -- the thing that has never once been verified in this project's
history -- that the category list is *readable*. A category list that cannot be
read means every report is filed under a fallback classification, which is a
report against the wrong category rather than a failure anyone would notice.

The submit click is the one step whose verification costs something real, so it
is opt-in: ``--submit`` files a real report against a target the operator
controls. Without the flag the boundary is respected absolutely, and the report
says so rather than implying more coverage than it has.

**CI cannot run this.** The offline checks are honest, fast, and worthless
alone -- every one of them passes in a container with no network. Only
``doctor`` on the operator's machine proves anything, which is exactly why it
gates the run instead of living in the test suite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, Sequence, runtime_checkable

from .config import Config
from .support.paths import Paths

__all__ = [
    "CheckStatus",
    "Check",
    "ChannelProbe",
    "DoctorReport",
    "Rehearsable",
    "run_doctor",
    "check_paths",
    "check_anchors",
    "check_credentials",
    "check_targets",
    "check_playwright",
    "check_proxies",
]


class CheckStatus(str, Enum):
    """How a check ended. Four states, and none of them is "probably fine"."""

    #: It did the thing.
    PASS = "pass"
    #: It did not, and the operator has to fix something. Always carries a
    #: remedy, because a failure without one is a riddle.
    FAIL = "fail"
    #: It worked, and something about the setup deserves a look. Never blocks.
    WARN = "warn"
    #: Deliberately not checked on this invocation. Distinct from PASS on
    #: purpose: "I did not look" and "I looked and it was fine" are different
    #: findings, and a health check that reports them the same way is lying
    #: about how much it knows.
    SKIP = "skip"


@dataclass(frozen=True)
class Check:
    name: str
    status: CheckStatus
    detail: str
    #: What to do about it. Empty for PASS and SKIP, and for a WARN it is a
    #: suggestion rather than an instruction.
    remedy: str = ""

    def line(self) -> str:
        """One row, for a human reading a terminal."""
        marker = {
            CheckStatus.PASS: "ok  ",
            CheckStatus.FAIL: "FAIL",
            CheckStatus.WARN: "warn",
            CheckStatus.SKIP: "skip",
        }[self.status]
        row = f"  {marker}  {self.name:<20} {self.detail}"
        if self.remedy and self.status in (CheckStatus.FAIL, CheckStatus.WARN):
            row += f"\n        -> {self.remedy}"
        return row


@dataclass(frozen=True)
class ChannelProbe:
    """One channel's rehearsal, and how far it got."""

    name: str
    ok: bool
    #: The last step that succeeded, in wizard order. The most useful single
    #: fact in the whole report: "it got as far as choosing a category" locates
    #: a drift exactly, where "the channel failed" does not.
    reached: str
    detail: str = ""
    remedy: str = ""
    #: The categories read from the live dialog, if the flow got that far.
    #: Reported because a list that is *empty* is a different failure from one
    #: that is absent, and the operator can only tell them apart if shown it.
    categories: tuple[str, ...] = ()
    #: Whether the submit button was found and enabled, without clicking it.
    submit_ready: bool = False
    #: Set only when the operator passed ``--submit``.
    submitted: bool = False

    def line(self) -> str:
        marker = "ok  " if self.ok else "FAIL"
        row = f"  {marker}  {self.name:<16} reached: {self.reached}"
        if self.detail:
            row += f"\n        {self.detail}"
        if self.remedy:
            row += f"\n        -> {self.remedy}"
        if self.categories:
            shown = ", ".join(self.categories[:6])
            more = " ..." if len(self.categories) > 6 else ""
            row += f"\n        {len(self.categories)} category(s) read: {shown}{more}"
        return row


@runtime_checkable
class Rehearsable(Protocol):
    """A channel that can prove it reaches the submit button.

    A protocol rather than an import, so the doctor holds no browser knowledge
    and a test can hand it a channel that has never seen Playwright. The browser
    implementation lives in :mod:`insta_report.browser`, the only module in the
    project allowed to import Playwright.
    """

    name: str

    async def rehearse(self, target: Any, *, submit: bool = False) -> ChannelProbe: ...


# ===========================================================================
# 1. The offline checks. Cheap, local, and worthless alone.
# ===========================================================================


def check_paths(paths: Paths) -> Check:
    """The data directory exists, is writable, and is not the checkout.

    Checked by *doing* rather than by inspecting permission bits, because on
    Windows those bits say almost nothing and the only reliable test is a write.
    """
    name = "data directory"
    probe = paths.state_dir / ".doctor-write-probe"
    try:
        paths.state_dir.mkdir(parents=True, exist_ok=True)
        probe.write_text("doctor", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return Check(
            name,
            CheckStatus.FAIL,
            f"{paths.data_dir} is not writable: {exc}",
            "Point [run] data_dir somewhere writable, or fix its permissions.",
        )
    return Check(
        name, CheckStatus.PASS, f"{paths.data_dir} is writable and outside the checkout"
    )


def check_anchors(config: Config) -> Check:
    """The anchor file loads, and says what it contains.

    The interesting failure -- an anchor with neither a selector nor a text, so
    it can never match and every report waits out the full timeout before
    truthfully reporting that it was not there -- is raised by
    :func:`~insta_report.anchors.load_anchors` itself. It is *not* re-checked
    here. A health check that repeats a validation rule is a second copy of it,
    and the copy is the one that will fall behind.
    """
    name = "anchors"
    from .anchors import load_anchors  # noqa: PLC0415

    try:
        anchors = load_anchors(config.anchors.path)
    except Exception as exc:  # noqa: BLE001
        return Check(
            name,
            CheckStatus.FAIL,
            f"{config.anchors.path}: {type(exc).__name__}: {exc}",
            "Fix the file, or point [anchors] at a known-good copy. Compare it "
            "against a live page with: insta-report anchors --check <dump.json>",
        )

    every = anchors.all_anchors()
    with_text = sum(1 for a in every if a.texts or a.alternate_texts)
    return Check(
        name,
        CheckStatus.PASS,
        f"{len(every)} anchor(s) from {config.anchors.path.name}; "
        f"{with_text} text-checked, {len(every) - with_text} selector-only",
    )


def check_credentials(config: Config) -> Check:
    """Every enabled account has a session, and none of them is printed.

    ``load_config`` has already refused an unset or empty variable, so reaching
    this point means they are present. What is checked here is what survives a
    valid-looking config: a sessionid that is a placeholder, and an account with
    no username, which silently disables self-report detection -- the one guard
    between the tool and a report filed against a reporting account.
    """
    name = "credentials"
    active = config.active_accounts
    if not active:
        return Check(
            name,
            CheckStatus.FAIL,
            "no enabled accounts",
            "Set enabled = true on at least one [[accounts]] entry.",
        )

    problems: list[str] = []
    for account in active:
        # The length and the shape, never the value. A sessionid eleven
        # characters long is a placeholder, and a placeholder is the most likely
        # reason a first run reports nothing and blames Instagram.
        if len(account.sessionid) < 20:
            problems.append(
                f"{account.ref}: sessionid is {len(account.sessionid)} character(s), "
                "which is a placeholder rather than a session"
            )
        if not account.username:
            problems.append(
                f"{account.ref}: no username, so self-report detection is off for it"
            )
        if account.daily_budget < 1:
            problems.append(
                f"{account.ref}: daily_budget is {account.daily_budget}, so it can "
                "never file a report"
            )

    if problems:
        return Check(
            name,
            CheckStatus.FAIL,
            f"{len(problems)} problem(s) across {len(active)} account(s)",
            " | ".join(problems),
        )
    return Check(
        name,
        CheckStatus.PASS,
        f"{len(active)} account(s), each with a sessionid of plausible length "
        "(values not printed)",
    )


def check_targets(targets: Any) -> Check:
    """The target list, as far as it can be judged without the network.

    Reportable rather than fatal. A list with one confusable handle in it is
    still worth running for the other 399, and refusing the whole thing because
    of one is a worse answer than naming the one -- though a *run* will refuse
    it, which is the correct place for that rule to live.
    """
    name = "targets"
    pending = list(targets.pending())
    problems = list(targets.problems)
    if not pending:
        return Check(
            name,
            CheckStatus.FAIL,
            "nothing to report: every target is already settled or attempted",
            "This is what a --resume of a finished run looks like. Use a new "
            "--run-id for new targets.",
        )
    if problems:
        return Check(
            name,
            CheckStatus.WARN,
            f"{len(pending)} reportable, {len(problems)} problem(s) -- a run will "
            "refuse until these are fixed",
            " | ".join(problems[:3]),
        )
    return Check(
        name,
        CheckStatus.PASS,
        f"{len(pending)} reportable target(s) from {len(targets)}",
    )


async def check_playwright() -> Check:
    """The Chromium build the config names is actually installed.

    Cheap, entirely local, and it catches a failure otherwise discovered at the
    first click of the first report -- where the runner files it as a channel
    failure, which reads as "Instagram rejected us" and is not remotely true.

    Uses the *async* API because this runs inside the run's event loop, and
    Playwright's sync API raises rather than nest when entered from a running
    loop. Asking for ``executable_path`` resolves the path without launching.
    """
    name = "playwright"
    try:
        from playwright.async_api import async_playwright  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - the package is a dependency
        return Check(
            name,
            CheckStatus.FAIL,
            f"playwright is not importable: {exc}",
            "pip install -e .",
        )

    try:
        async with async_playwright() as playwright:
            path = str(playwright.chromium.executable_path)
    except Exception as exc:  # noqa: BLE001
        return Check(
            name,
            CheckStatus.FAIL,
            f"could not ask playwright where chromium is: {type(exc).__name__}: {exc}",
            "pip install -e . then: python -m playwright install chromium",
        )

    if not path or not Path(path).exists():
        return Check(
            name,
            CheckStatus.FAIL,
            f"the chromium build is not installed (expected at {path or 'an unknown path'})",
            "python -m playwright install chromium",
        )
    return Check(name, CheckStatus.PASS, f"chromium at {path}")


def check_proxies(pool: Any) -> Check:
    """The pool builds, and one lease can actually be taken and read back.

    Acquiring a lease is the only part of the offline checks that touches the
    network, and it is here because a pool that builds but cannot lease is
    indistinguishable from a pool with no exits until the first report -- at
    which point the channel failure is reported against the wrong thing.
    """
    name = "proxies"
    if pool is None:
        return Check(
            name,
            CheckStatus.SKIP,
            "no pool (direct connection)",
            "Set [proxies] to route through residential exits; a direct "
            "connection exposes the operator's own address to every report.",
        )
    try:
        lease = pool.acquire()
    except Exception as exc:  # noqa: BLE001
        return Check(
            name,
            CheckStatus.FAIL,
            f"the pool built but no exit could be leased: "
            f"{type(exc).__name__}: {exc}",
            "Check the proxy credentials and that the addresses answer. "
            "'python -m insta_report.probe --proxy <url>' tests one exit directly.",
        )

    try:
        # Asserted, not merely read. A lease whose address has already drifted
        # is the one failure that would assemble a report on one IP and submit
        # it from another, and it is invisible without this check.
        pool.assert_lease_fresh(lease)
    except Exception as exc:  # noqa: BLE001
        return Check(
            name,
            CheckStatus.FAIL,
            f"leased an exit that is not the one that was probed: {exc}",
            "A provider whose sticky TTL is shorter than the lease will do this. "
            "Raise its stickiness, or use a file of one-exit-per-lease proxies.",
        )
    finally:
        pool.release(lease)

    detail = f"leased {lease.endpoint.label or lease.endpoint.host}"
    if lease.egress is not None:
        detail += f" -> egress {lease.egress.ip}"
        if lease.egress.country:
            detail += f" ({lease.egress.country})"
        if lease.egress.asn:
            detail += f" AS{lease.egress.asn}"
    return Check(name, CheckStatus.PASS, detail)


# ===========================================================================
# 2. The report
# ===========================================================================


@dataclass
class DoctorReport:
    checks: list[Check] = field(default_factory=list)
    channels: list[ChannelProbe] = field(default_factory=list)
    probe_target: str | None = None

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.status is CheckStatus.FAIL]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.status is CheckStatus.WARN]

    @property
    def passed_channels(self) -> list[ChannelProbe]:
        return [c for c in self.channels if c.ok]

    @property
    def runnable(self) -> bool:
        """May a run start? One surviving channel is the bar.

        Not "all channels". One working channel is a working tool; refusing
        because the *second* channel is broken would make a partial outage look
        like a total one, and would train the operator to reach for
        ``--no-doctor``.
        """
        return bool(self.passed_channels) and not self.failures

    def add(self, check: Check) -> None:
        self.checks.append(check)

    def render(self) -> str:
        lines: list[str] = []
        if self.checks:
            lines.append("setup")
            lines.extend(c.line() for c in self.checks)
        if self.channels:
            lines.append("")
            lines.append("channels")
            lines.extend(c.line() for c in self.channels)
        lines.append("")
        lines.append(self.verdict())
        return "\n".join(lines)

    def verdict(self) -> str:
        if not self.channels:
            if self.failures:
                return (
                    "NOT RUNNABLE. The setup is wrong, and nothing was checked against "
                    "a live channel. Fix the failures above and re-run with "
                    "--probe-target HANDLE for an account you control."
                )
            return (
                "SETUP OK, but no channel was rehearsed -- so nothing here proves a "
                "report can actually be filed. Pass --probe-target to find out."
            )

        passing = len(self.passed_channels)
        total = len(self.channels)
        if passing == 0:
            return (
                f"NOT RUNNABLE. {total} channel(s) tried, none reached the submit "
                "button. A run now would file nothing and report the failure against "
                "Instagram rather than against your setup."
            )
        if self.failures:
            return (
                f"NOT RUNNABLE. {passing} of {total} channel(s) reached the submit "
                f"button, but {len(self.failures)} setup check(s) failed."
            )
        suffix = (
            ""
            if self.channels[0].submitted
            else "  The submit click itself is unverified unless you pass --submit "
            "against an account you control."
        )
        return (
            f"RUNNABLE. {passing} of {total} channel(s) reached the submit button, "
            f"and no setup check failed.{suffix}"
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "runnable": self.runnable,
            "verdict": self.verdict(),
            "probe_target": self.probe_target,
            "checks": [
                {
                    "name": c.name,
                    "status": c.status.value,
                    "detail": c.detail,
                    "remedy": c.remedy,
                }
                for c in self.checks
            ],
            "channels": [
                {
                    "name": c.name,
                    "ok": c.ok,
                    "reached": c.reached,
                    "detail": c.detail,
                    "remedy": c.remedy or None,
                    "categories": list(c.categories),
                    "submit_ready": c.submit_ready,
                    "submitted": c.submitted,
                }
                for c in self.channels
            ],
        }


# ===========================================================================
# 3. The run
# ===========================================================================


async def run_doctor(
    config: Config,
    *,
    targets: Any = None,
    target_problem: str = "",
    pool: Any = None,
    channels: Sequence[Any] = (),
    probe_target: str | None = None,
    submit: bool = False,
    live: bool = True,
) -> DoctorReport:
    """Every check, cheapest first, then the channel rehearsals.

    Order is not cosmetic. A missing data directory is a two-millisecond
    answer; a channel rehearsal is a thirty-second one that opens a browser.
    Any other order means every operator waits half a minute to be told their
    config path is wrong.

    *live=False* runs the offline half only. It is what a wrapper can safely
    call on every invocation, and it is honest about what it did not do: the
    report says no channel was rehearsed, so nobody mistakes a passing config
    check for a working tool.

    *target_problem* is how a list that could not even be *loaded* gets
    reported. It is a parameter rather than an exception because a health
    check that dies on the first thing it cannot read is not a health check --
    and because a check printed to the terminal but absent from the report
    would be invisible to ``--json``, which is the one output a wrapper reads.
    """
    report = DoctorReport(probe_target=probe_target)

    report.add(check_paths(config.paths))
    report.add(check_anchors(config))
    report.add(check_credentials(config))
    if target_problem:
        report.add(
            Check(
                "targets",
                CheckStatus.FAIL,
                target_problem,
                "Fix the path, or remove the file. Nothing can be reported from "
                "a list this tool cannot read.",
            )
        )
    elif targets is not None:
        report.add(check_targets(targets))
    report.add(await check_playwright())

    if not live:
        report.add(
            Check(
                "exits",
                CheckStatus.SKIP,
                "not leased: the offline half of the check only",
                "This run verified the setup. It did not prove a report can be sent.",
            )
        )
        return report

    report.add(check_proxies(pool))

    if not probe_target:
        report.add(
            Check(
                "channels",
                CheckStatus.SKIP,
                "not rehearsed: no --probe-target given",
                "Pass --probe-target with an account you control to prove a channel "
                "can reach the submit button. A run will not start without one.",
            )
        )
        return report

    from .targets import Target  # noqa: PLC0415

    target = Target(handle=probe_target)
    for channel in channels:
        report.channels.append(
            await _rehearse_one(channel, target, submit=submit)
        )
    return report


async def _rehearse_one(
    channel: Any, target: Any, *, submit: bool
) -> ChannelProbe:
    """Rehearse one channel, and close it afterwards whatever happened.

    The close is in a ``finally`` and its own failure is swallowed. A browser
    that will not close is a Playwright problem, and it must not become the
    doctor's verdict on the operator's setup.
    """
    name = getattr(channel, "name", "unknown")
    try:
        return await channel.rehearse(target, submit=submit)
    except Exception as exc:  # noqa: BLE001
        # A channel that raises out of its own rehearsal is itself a finding.
        # Swallowing it would leave the operator with a channel that silently
        # does not exist, which is the failure this whole task exists to stop.
        return ChannelProbe(
            name=name,
            ok=False,
            reached="launch",
            detail=f"the rehearsal itself raised: {type(exc).__name__}: {exc}",
            remedy="This is a bug in the channel rather than in your setup. "
            "Re-run with --verbose for the traceback.",
        )
    finally:
        close = getattr(channel, "aclose", None)
        if close is not None:
            try:
                await close()
            except Exception:  # noqa: BLE001
                pass
