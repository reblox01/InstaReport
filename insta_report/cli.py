"""The command line.

Four commands, and the split is by what they *change*:

```
  insta-report run        touches Instagram. Writes a checkpoint.
  insta-report status     reads a checkpoint. Never writes.
  insta-report targets    reads a target list. Never writes.
  insta-report anchors    reads the anchor file. Never writes.

  `doctor` is T10 and gates `run` by exit code. It is not here yet, so this
  module makes no claim to check anything before dispatching -- the browser
  channel reports a failed launch as a channel failure, which is a truthful
  but late place to find out.
```

**Exit codes are part of the interface**, because a script wrapping this needs
to tell "finished, nothing to look at" from "finished, look at this" without
parsing prose:

| code | meaning                                                          |
|-----:|------------------------------------------------------------------|
|    0 | finished; nothing needs a human                                  |
|    1 | finished, but the output needs reading -- unsettled, a refusal, a challenge, an abort |
|    2 | could not start: bad usage, bad config, unusable target list, no channel |
|  130 | interrupted; the run stopped cleanly at a report boundary         |

The distinction between 1 and 2 is the one that matters most. Code 2 means
*nothing was sent* and it is safe to fix the input and try again. Code 1 means
requests may have gone out. A wrapper that treats them the same will eventually
re-run a target whose report already landed.

**Ctrl-C latches, it does not cancel.** The first signal asks the runner to
stop starting new work; the report in flight is finished and settled, because
abandoning it leaves an intent with no outcome and that target can then never
be settled by anything. A second signal raises, because by then the operator is
telling us something is wedged.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence, TextIO

from .accounts import Account, AccountPool
from .anchors import AnchorSet, load_anchors, report_drift
from .artifacts import ArtifactStore
from .browser import BrowserChannel, BrowserDriver, SubmitPolicy
from .checkpoint import CheckpointStore
from .config import Config, ConfigError, load_config
from .narrative import NarrativeBuilder, build_builder
from .pacing import Pacer, PacingConfig
from .proxies import ProxyPool, build_pool
from .runner import ChannelHealth, ChannelSpec, Refusal, RunOptions, RunReport, Runner
from .support.logging import setup_logging
from .support.paths import RunIdError, check_run_id
from .targets import TargetList, TargetProblem, load_targets
from .transport import fetch

__all__ = [
    "main",
    "build_parser",
    "EXIT_OK",
    "EXIT_NEEDS_REVIEW",
    "EXIT_REFUSED",
    "EXIT_INTERRUPTED",
]

EXIT_OK = 0
EXIT_NEEDS_REVIEW = 1
EXIT_REFUSED = 2
EXIT_INTERRUPTED = 130

#: Looked for, in order, when ``--config`` is absent. Named rather than
#: discovered, so "which file did it use" is answerable from the source.
DEFAULT_CONFIG_NAMES = ("insta-report.toml", "config.toml")


# -- small helpers ---------------------------------------------------------


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _resolve_config(explicit: str | None) -> Path:
    """Find the config, or say precisely what was looked for.

    An operator who mistypes ``--config`` and gets "file not found" from an
    auto-discovery path has no way to tell which of the two was the mistake.
    Naming the candidates turns a 2am puzzle into a one-line fix.
    """
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
        return path

    for name in DEFAULT_CONFIG_NAMES:
        candidate = Path.cwd() / name
        if candidate.is_file():
            return candidate

    raise ConfigError(
        "no config file found. Pass --config, or create one of "
        + ", ".join(DEFAULT_CONFIG_NAMES)
        + " in the current directory. Copy config.example.toml to start."
    )


def _load_anchors(config: Config) -> AnchorSet:
    """Read the anchor file, reporting a bad one as a config error.

    An ``AnchorMissing`` escaping to the top level would print a traceback for
    what is a file with a typo in it.
    """
    try:
        return load_anchors(config.anchors.path)
    except Exception as exc:  # noqa: BLE001 - any failure is the same report
        raise ConfigError(
            f"the anchor file at {config.anchors.path} could not be read: {exc}"
        ) from exc


def _load_targets_from(
    config: Config, explicit: str | None, *, require_usable: bool = True
) -> TargetList:
    """Load the target list, refusing a file the operator did not name.

    The default sits under the data directory rather than the working
    directory, so a run started from anywhere resolves the same file. A run
    that silently picked up a different list depending on the shell's cwd has a
    blast radius that depends on where it was launched.

    *require_usable* is the difference between "refuse a bad list" and "show me
    a bad list". A run cannot proceed with one, so it gets the raise. The
    ``targets`` command exists to *diagnose* one, so it passes ``False`` and
    prints the findings itself -- otherwise the diagnostic raises before it can
    report, and the one command whose whole job is naming the problems names
    them as a traceback.
    """
    path = (
        Path(explicit).expanduser()
        if explicit
        else config.paths.data_dir / "targets.txt"
    )
    if not path.is_file():
        raise ConfigError(
            f"target list not found: {path}\n"
            "Point at one with --targets, or create it: one handle per line, "
            "with blank lines and # comments ignored."
        )
    anchors = _load_anchors(config)
    targets = load_targets(path, confusables=anchors.confusable_codepoints)
    if not targets:
        raise ConfigError(
            f"{path} contains no targets. An empty list is almost always a path "
            "that resolved to the wrong file, and running it would report "
            "nothing while looking like it worked."
        )
    if require_usable:
        targets.require_usable()
    return targets


def _build_accounts(config: Config) -> AccountPool:
    """The pool, from the enabled accounts.

    ``config.load_config`` has already registered every ``sessionid`` with the
    redaction registry, so a value that reaches a log line through here is
    scrubbed rather than printed.
    """
    accounts = [
        Account(
            ref=entry.ref,
            username=entry.username,
            sessionid=entry.sessionid,
            daily_budget=entry.daily_budget,
            enabled=entry.enabled,
        )
        for entry in config.active_accounts
    ]
    if not accounts:
        raise ConfigError(
            "every configured account is disabled. There is no anonymous path "
            "by design: a report must be filed from a real, trusted session."
        )
    return AccountPool(accounts)


def _build_proxies(config: Config, fetch_impl: Callable[..., Any]) -> ProxyPool:
    """Build the exit pool, refusing to start a run without a usable one.

    ``build_pool`` already fails closed on an empty list. This adds the failure
    the pool cannot see: a provider key that is not in the environment reads as
    "no exits available", and the operator debugging that at 2am needs to be
    told the variable is unset rather than that a proxy is down.
    """
    if config.proxies.source == "provider" and not config.proxies.resolved_key():
        variable = config.proxies.provider_key_env or "(no provider_key_env set)"
        raise ConfigError(
            f"[proxies] source='provider' needs the API key in {variable}, and "
            "that variable is unset or empty."
        )
    return build_pool(config.proxies, fetch=fetch_impl)


def _build_channels(
    config: Config, anchors: AnchorSet, artifacts: ArtifactStore
) -> list[ChannelSpec]:
    """One browser channel per enabled account.

    A channel per account rather than one shared channel, because a
    ``BrowserDriver`` owns exactly one persistent context and one page. Two
    accounts through one driver would be two identities in one browser
    profile, which is the correlation the account pool exists to prevent and
    the reason the rotation unit is the lease rather than the report.

    Playwright's persistent context launches and owns its own browser, so
    these are N browser processes. Sharing one would need a different context
    strategy and would cost the user-data directory that makes a real session
    reproducible; the concurrency here is small enough that it does not matter.
    """
    specs: list[ChannelSpec] = []
    for entry in config.active_accounts:
        driver = BrowserDriver(
            user_data_dir=config.browser.user_data_dir / entry.ref,
            headless=config.browser.headless,
            locale=config.browser.locale,
            timezone_id=config.browser.timezone,
            navigation_timeout_ms=config.browser.navigation_timeout_ms,
            confirmation_timeout_ms=config.browser.confirmation_timeout_ms,
        )
        specs.append(
            ChannelSpec(
                BrowserChannel.name,
                BrowserChannel(
                    driver=driver,
                    anchors=anchors,
                    policy=SubmitPolicy(),
                    artifacts=artifacts,
                ),
                capacity=1,
            )
        )
    return specs


def _pacer_factory(config: Config) -> Callable[[], Pacer]:
    """One pacer per worker, all from the same numbers.

    Per worker because a pacer holds its own "last dispatch" clock: a shared
    one would make two workers serialised behind each other's gap, which is a
    throughput limit nobody asked for, and would also mean one worker's
    dispatch reset the other's wait.
    """
    pacing = PacingConfig(
        floor_gap=config.run.floor_gap_seconds,
        jitter_fraction=config.run.jitter_fraction,
        horizon_fraction=config.run.horizon_fraction,
        default_horizon_seconds=config.run.horizon_seconds,
    )
    return lambda: Pacer(pacing)


# -- narratives ------------------------------------------------------------


def _attach_narratives(
    targets: TargetList, narratives: NarrativeBuilder, *, stream: TextIO
) -> None:
    """Give every pending target the narrative it will be filed with.

    Rendered here, once, before the run, rather than inside the channel, for
    two reasons. The templates are code and not config -- an operator-editable
    template is a template nobody has reviewed -- so a narrative is a pure
    function of the target, and a *retried* target must produce byte-identical
    text or the same report lands twice under two classifications. And a
    ``--dry-run`` shows exactly what the real run will send, because both go
    through this function.

    A target that already carries a ``category`` or ``detail`` is left alone:
    an operator who wrote the text meant it, and overwriting it with a
    template would be the tool deciding something they already decided.
    """
    for target in targets.pending():
        if target.detail or target.category:
            continue
        try:
            narrative = narratives.build(target)
        except Exception as exc:  # noqa: BLE001
            # Not fatal. A target whose narrative will not render is a target
            # whose detail stays empty, which Instagram accepts; refusing the
            # whole run over it would cost the operator every other target.
            print(f"  {target.escaped():<32} narrative not rendered: {exc}", file=stream)
            continue
        target.detail = narrative.text
        target.category = narrative.category


# -- run -------------------------------------------------------------------


def _print_narrative_preview(targets: TargetList, *, stream: TextIO) -> None:
    """Show what would be written into each report, for a dry run to read.

    The classification is the part of a report an operator most wants to check
    before anything is sent, and it is the part that cannot be checked after:
    once a report is filed, finding it was filed under the wrong category means
    finding it by hand on a page Instagram does not give us. So a dry run that
    shows a count and not the text has skipped the only part of itself an
    operator can act on.
    """
    print("", file=stream)
    print("what would be sent:", file=stream)
    for target in targets.pending():
        category = target.category or "(category chosen from the live dialog)"
        print(f"  {target.escaped()}", file=stream)
        print(f"    category: {category}", file=stream)
        detail = target.detail or "(no note attached)"
        # Wrapped rather than printed on one line: a note is prose, and prose
        # truncated by a terminal width is a note the operator never read.
        # 84 keeps the printed line, indent included, inside 88 columns.
        for line in _wrap(detail, 84):
            print(f"    {line}", file=stream)
    print("", file=stream)


def _wrap(text: str, width: int) -> list[str]:
    """Wrap on whitespace, never mid-word.

    ``textwrap`` would do this, but it also breaks long words with hyphens and
    collapses whitespace, and a report note is something the operator may copy
    verbatim out of this output. A note that comes back wrapped-and-hyphenated
    is a note that no longer matches what would be sent.
    """
    words = text.split()
    if not words:
        return [text]
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        if len(current) + 1 + len(word) <= width:
            current = f"{current} {word}"
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _self_report_refusals(
    targets: TargetList, plan: dict[str, Any], pool: AccountPool
) -> tuple[Refusal, ...]:
    """The targets that would be refused before dispatch, in report form.

    A dry run has to show these. A run that silently drops a self-report from
    its own output and only mentions it in a log line is a run whose target
    count and outcome count do not add up, and the operator is left deciding
    whether that is a rounding error or a bug.

    The keys come from the plan rather than from a second call to
    ``self_reporting``, so this cannot disagree with the run about who is
    being refused.
    """
    refusals = []
    for key in plan["self_reporting"]:
        target = targets.get(str(key))
        refusals.append(
            Refusal(
                target_key=str(key),
                display=target.escaped() if target else str(key),
                reason="this is one of our own accounts",
                fatal_to_run=True,
            )
        )
    return tuple(refusals)


def _dry_run_report(runner: Runner, targets: TargetList, pool: AccountPool) -> RunReport:
    """The report a ``--dry-run`` prints.

    Built from ``plan()`` -- the run's own work-queue computation -- so it
    cannot disagree with the run it describes. A plan built from a looser
    notion of "pending" is how an operator is told forty targets are left and
    watches five reports happen.
    """
    plan = runner.plan()
    now = datetime.now(timezone.utc)
    return RunReport(
        run_id=str(plan["run_id"]),
        started_at=now,
        finished_at=now,
        dispatches=0,
        targets_considered=int(plan["pending"]),
        counts={},
        refusals=_self_report_refusals(targets, plan, pool),
        channel_names=tuple(plan["channels"]),  # type: ignore[arg-type]
        # Zeroed health per planned channel, so the summary's channel section
        # shows which channels *would* have been used. Without this a dry run
        # prints the bare heading "channels:" with nothing under it, which
        # reads as "the tool found no channels" rather than "nothing was
        # attempted".
        health=tuple(
            ChannelHealth(name=str(name), capacity=1) for name in plan["channels"]
        ),
        review=(),
        unsettled=(),
        aborted=False,
        dry_run=True,
    )


def _exit_code_for(report: RunReport) -> int:
    """Which of the two non-zero codes this run earned.

    ``unsettled`` and ``review`` lead: a target that was dispatched and never
    resolved is the one result that cannot be acted on automatically in either
    direction, and a script wrapping this needs it distinguishable from a
    quiet refusal.
    """
    if report.unsettled or report.review or report.errors:
        return EXIT_NEEDS_REVIEW
    if report.needs_attention or report.aborted:
        return EXIT_NEEDS_REVIEW
    return EXIT_OK


def _install_sigint(runner: Runner) -> Callable[[], None]:
    """Latch on the first Ctrl-C, raise on the second. Returns a restorer.

    ``signal.signal`` rather than ``loop.add_signal_handler``, on purpose: the
    latter is not implemented on the Windows event loop this project runs on,
    and a Ctrl-C that silently does nothing is the worst possible failure mode
    for a control an operator is relying on to stop a run.
    """
    state = {"count": 0}

    def handler(signum: int, frame: Any) -> None:
        state["count"] += 1
        if state["count"] == 1:
            runner.request_abort("interrupted at the operator's request (Ctrl-C)")
            return
        raise KeyboardInterrupt("second interrupt; abandoning the run")

    try:
        previous = signal.signal(signal.SIGINT, handler)
    except ValueError:
        # Not the main thread, so there is no signal to catch and nothing to
        # latch. The run still works, just without a graceful stop -- which is
        # worth knowing, so it says so rather than pretending.
        print(
            "note: not the main thread, so Ctrl-C will not stop this run "
            "cleanly. Stop it with SIGTERM or kill it.",
            file=sys.stderr,
        )
        return lambda: None

    def restore() -> None:
        try:
            signal.signal(signal.SIGINT, previous)
        except ValueError:
            pass

    return restore


async def _run(
    config: Config,
    targets: TargetList,
    options: RunOptions,
    *,
    run_id: str,
    resume: bool,
    verbose: bool,
    stream: TextIO,
    fetch_impl: Callable[..., Any] | None = None,
) -> int:
    """Assemble the parts, run once, print the summary, return the exit code.

    Every collaborator is built here and closed in the ``finally``: a Playwright
    process left running after a crashed run holds a lock on the user data
    directory, and the next run then fails to launch for a reason that has
    nothing to do with the next run.
    """
    setup_logging(verbose=verbose, stream=stream)

    run_dir = config.paths.run_dir(run_id)
    store = CheckpointStore(run_dir / "checkpoint.jsonl", run_id)
    if resume:
        settled = len(store.state.settled)
        print(
            f"resuming run {run_id}: {settled} target(s) already settled, "
            "none of them retried",
            file=stream,
        )
    elif store.state.dispatched:
        print(
            f"note: run {run_id} already has {len(store.state.dispatched)} "
            "dispatched target(s) on disk. Use --resume to continue that run, "
            "or pass a different --run-id.",
            file=stream,
        )

    pool = _build_accounts(config)
    proxies = _build_proxies(config, fetch_impl or fetch)
    anchors = _load_anchors(config)
    artifacts = ArtifactStore(config.paths, run_id, allow_trace=False)
    channels = _build_channels(config, anchors, artifacts)
    narratives = build_builder(config.run, anchors=anchors)

    runner = Runner(
        store=store,
        pool=pool,
        channels=channels,
        targets=targets,
        options=options,
        pacer_factory=_pacer_factory(config),
        proxies=proxies,
        on_progress=lambda outcome: print(
            f"  {outcome.target_ref:<28} {outcome.terminal.value:<22} "
            f"via {outcome.channel}",
            file=stream,
        ),
    )

    restore_sigint = _install_sigint(runner)
    try:
        # Before the branch, so a dry run previews exactly the narratives the
        # real run would file rather than a second rendering of them.
        _attach_narratives(targets, narratives, stream=stream)
        if options.dry_run:
            _print_narrative_preview(targets, stream=stream)
            report = _dry_run_report(runner, targets, pool)
        else:
            report = await runner.run()
    finally:
        restore_sigint()
        for spec in channels:
            # Best effort: a driver that will not close is a Playwright problem
            # and must not replace the run's own result with a traceback.
            try:
                await spec.channel.aclose()
            except Exception as exc:  # noqa: BLE001
                print(f"warning: a browser did not close cleanly: {exc}", file=stream)
        store.close()

    print(file=stream)
    print(report.render(), file=stream)
    return _exit_code_for(report)


# -- status ----------------------------------------------------------------


def _status(config: Config, run_id: str | None, *, stream: TextIO) -> int:
    """Read a run's checkpoint and describe what may have gone out.

    Answers the question a ledger exists for -- *may a report have landed?* --
    and is explicit that the answer is about requests. Read-only: a status
    command that rewrites state is a status command nobody trusts while
    diagnosing.
    """
    if run_id is None:
        state_dir = config.paths.state_dir
        runs = sorted(
            (p.name for p in state_dir.glob("*") if (p / "checkpoint.jsonl").is_file()),
            reverse=True,
        )
        if not runs:
            print(f"no runs found in {state_dir}", file=stream)
            return EXIT_OK
        print(f"runs in {state_dir} (newest first):", file=stream)
        for name in runs[:20]:
            store = CheckpointStore(state_dir / name / "checkpoint.jsonl", name)
            unsettled = len(store.state.pending)
            print(
                f"  {name:<28} dispatched={len(store.state.dispatched):<4} "
                f"settled={len(store.state.settled):<4} unsettled={unsettled}",
                file=stream,
            )
        return EXIT_NEEDS_REVIEW if any(
            CheckpointStore(state_dir / n / "checkpoint.jsonl", n).state.pending
            for n in runs[:20]
        ) else EXIT_OK

    ledger = config.paths.state_dir / run_id / "checkpoint.jsonl"
    if not ledger.is_file():
        raise ConfigError(f"no checkpoint for run {run_id} at {ledger}")

    store = CheckpointStore(ledger, run_id)
    state = store.state
    print(f"run {run_id}", file=stream)
    print(f"  ledger      {ledger}", file=stream)
    print(f"  dispatched  {len(state.dispatched)}", file=stream)
    print(f"  settled     {len(state.settled)}", file=stream)

    unsettled = sorted(state.pending)
    print(f"  unsettled   {len(unsettled)}", file=stream)

    if unsettled:
        # Leads the output, because it is the one result that cannot be acted
        # on in either direction: not retried automatically, not declared
        # failed.
        print("", file=stream)
        print("  MAY HAVE LANDED (dispatched, no recorded outcome):", file=stream)
        for key in unsettled:
            print(f"    {key}", file=stream)

    by_state: dict[str, int] = {}
    for key in state.dispatched:
        outcome = state.outcome_for(key)
        if outcome is not None:
            by_state[outcome.terminal.value] = by_state.get(outcome.terminal.value, 0) + 1
    if by_state:
        print("", file=stream)
        print("  recorded outcomes:", file=stream)
        for name, count in sorted(by_state.items()):
            print(f"    {name:<24} {count}", file=stream)

    review = state.outcomes_needing_review()
    if review:
        print("", file=stream)
        print("  needs a human:", file=stream)
        for outcome in review:
            detail = f" -- {outcome.detail}" if outcome.detail else ""
            print(
                f"    {outcome.target_ref:<28}{outcome.terminal.value}{detail}",
                file=stream,
            )

    return EXIT_NEEDS_REVIEW if unsettled or review else EXIT_OK


# -- targets ---------------------------------------------------------------


def _targets(config: Config, explicit: str | None, *, stream: TextIO) -> int:
    """Validate a target list and print exactly what is wrong with it.

    This is the command to run *before* a run, not after, and it is noisy on
    purpose. A confusable handle is reported as invalid rather than as a
    warning, because the failure it prevents is a report filed against a
    different, real account.
    """
    try:
        # ``require_usable=False`` -- this command's product is the diagnosis,
        # so it must be allowed to reach the printing below with an unusable
        # list in hand. Asking for the raise here made every line of it
        # unreachable for exactly the lists an operator runs it on.
        targets = _load_targets_from(config, explicit, require_usable=False)
    except ConfigError as exc:
        print(f"cannot use the target list: {exc}", file=stream)
        return EXIT_REFUSED

    print(f"targets from {targets.source}", file=stream)
    print(f"  {len(targets)} target(s), {len(targets.problems)} problem(s)", file=stream)
    for problem in targets.problems:
        print(f"  PROBLEM  {problem}", file=stream)

    print("", file=stream)
    for target in targets:
        problems = target.validate()
        notes = ""
        if target.user_id:
            notes = f"  id={target.user_id}"
        if target.category:
            notes += f"  category={target.category!r}"
        print(
            f"  {'ok ' if not problems else 'BAD'}  {target.escaped():<32}{notes}",
            file=stream,
        )
        for problem in problems:
            print(f"        {problem}", file=stream)

    print("", file=stream)
    print(
        f"  {len(targets.pending())} pending, "
        f"{len(targets.attempted())} previously attempted",
        file=stream,
    )
    if not targets.usable:
        print("the list is not usable. Fix the problems above and re-run.", file=stream)
        return EXIT_REFUSED
    return EXIT_OK


# -- anchors ---------------------------------------------------------------


def _anchors(config: Config, path: str | None, *, stream: TextIO) -> int:
    """Show the anchor file.

    Separated from ``--check`` because the same file answers two different
    questions and an operator who has to copy it to a second place to look at
    it will not.
    """
    resolved = Path(path).expanduser() if path else config.anchors.path
    try:
        anchors = load_anchors(resolved)
    except Exception as exc:  # noqa: BLE001
        print(f"cannot read the anchor file: {exc}", file=stream)
        return EXIT_REFUSED

    print(f"anchors from {anchors.path}", file=stream)
    for anchor in anchors.all_anchors():
        if anchor.texts:
            shown = " | ".join(anchor.texts)
        else:
            # Not a gap in the file: an anchor with no configured text is
            # matched by its selector alone, and its marker is its own name.
            shown = f"(no text; {len(anchor.selectors)} selector(s))"
        print(f"  {anchor.name:<26} {shown}", file=stream)

    print("", file=stream)
    print("  confusable characters (a target containing one is invalid):", file=stream)
    for entry in anchors.confusable_codepoints.values():
        print(
            f"    {entry.codepoint}  {entry.name:<18} looks like {entry.ascii_twin}",
            file=stream,
        )
    return EXIT_OK


def _anchors_check(config: Config, path: str | None, dump: str, *, stream: TextIO) -> int:
    """Compare a captured page against the anchors and print the drift.

    The diagnostic that matters when Instagram has moved something and no
    report is being filed: every anchor that did not match, and what was there
    instead.
    """
    resolved = Path(path).expanduser() if path else config.anchors.path
    try:
        anchors = load_anchors(resolved)
    except Exception as exc:  # noqa: BLE001
        print(f"cannot read the anchor file: {exc}", file=stream)
        return EXIT_REFUSED

    dump_path = Path(dump).expanduser()
    try:
        observed = json.loads(dump_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read {dump_path}: {exc}", file=stream)
        return EXIT_REFUSED
    if not isinstance(observed, dict):
        print(
            f"{dump_path} must be a JSON object of anchor name -> observed text",
            file=stream,
        )
        return EXIT_REFUSED

    try:
        text_by_anchor = _observed_text(observed, dump_path)
    except ConfigError as exc:
        print(str(exc), file=stream)
        return EXIT_REFUSED

    report = report_drift(anchors, text_by_anchor)
    print(report.render(), file=stream)
    return EXIT_OK if report.clean else EXIT_NEEDS_REVIEW


def _observed_text(observed: dict[str, Any], dump_path: Path) -> dict[str, str]:
    """Coerce a dump's values to the text ``report_drift`` compares.

    Two shapes are accepted, because both are what a capture script actually
    produces: a bare string, and a list of strings for a region that matched
    more than once (a list is joined, since the anchors match on a substring).

    Anything else is **refused**. It used to be passed through ``str()``, which
    turned a nested object into its Python repr and then graded that repr as a
    real observation -- so a dump written in the wrong shape produced a
    confident drift report about a page nobody had looked at. Silently
    coercing bad input into a plausible answer is the exact failure this whole
    tool is built to avoid; a refusal is worth more than a wrong answer here,
    because the answer an operator acts on is "update anchors.toml".
    """
    out: dict[str, str] = {}
    for name, value in observed.items():
        if isinstance(value, str):
            out[str(name)] = value
        elif isinstance(value, (list, tuple)) and all(
            isinstance(item, str) for item in value
        ):
            out[str(name)] = "\n".join(value)
        else:
            raise ConfigError(
                f"{dump_path}: {name!r} must be a string, or a list of strings "
                f"(the text found in that region). Got {type(value).__name__}. "
                "Coerced, it would be graded as an observation nobody made."
            )
    return out


# -- argument parsing ------------------------------------------------------


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="config file (default: ./insta-report.toml or ./config.toml)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="insta-report",
        description=(
            "Deliver fraud reports to Instagram from real, authenticated "
            "sessions. Reports what it requested; cannot know what Instagram "
            "did with them."
        ),
        epilog="Exit codes: 0 clean, 1 needs reading, 2 could not start, 130 interrupted.",
    )
    _add_common(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser(
        "run",
        help="deliver reports",
        description=(
            "Deliver reports. Writes a durable checkpoint before every submit, "
            "so an interrupted run can be finished without double-reporting."
        ),
    )
    run.add_argument("--targets", metavar="PATH", help="default: <data_dir>/targets.txt")
    run.add_argument("--run-id", metavar="ID", help="default: a UTC timestamp")
    run.add_argument(
        "--resume",
        metavar="ID",
        help="continue run ID; its settled targets are never retried",
    )
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan and the narratives; send nothing",
    )
    run.add_argument(
        "--max-reports",
        type=int,
        metavar="N",
        help="cap dispatches for this run (overrides [run] max_reports)",
    )
    run.add_argument(
        "--horizon-hours",
        type=float,
        metavar="H",
        help="stop starting new work after H hours",
    )
    run.add_argument(
        "--max-concurrent",
        type=int,
        metavar="N",
        help="concurrent workers; bounded by eligible accounts anyway",
    )
    run.set_defaults(handler=_cmd_run)

    status = sub.add_parser(
        "status",
        help="describe a run's checkpoint (read-only)",
        description=(
            "Read a run's checkpoint. Lists the targets that were dispatched "
            "and never resolved -- the ones that may have been reported."
        ),
    )
    status.add_argument("--run", metavar="ID", help="default: list every run")
    status.set_defaults(handler=_cmd_status)

    targets = sub.add_parser(
        "targets",
        help="validate a target list (read-only)",
        description=(
            "Check a target list for confusable characters, self-reports and "
            "duplicates. Sends nothing."
        ),
    )
    targets.add_argument("--targets", metavar="PATH", help="the list to check")
    targets.set_defaults(handler=_cmd_targets)

    anchors = sub.add_parser(
        "anchors",
        help="show the anchors, or check a page against them (read-only)",
        description=(
            "Show the anchor file, or compare a captured page against it. When "
            "Instagram moves something, this is how you find out what to."
        ),
    )
    anchors.add_argument("--anchors-path", metavar="PATH", help="an alternative file")
    anchors.add_argument(
        "--check",
        metavar="DUMP.json",
        help="compare a JSON map of anchor -> observed text, and report drift",
    )
    anchors.set_defaults(handler=_cmd_anchors)

    return parser


# -- handlers --------------------------------------------------------------


def _cmd_run(
    args: argparse.Namespace, *, stream: TextIO, fetch_impl: Callable[..., Any] | None
) -> int:
    if args.resume and args.run_id:
        raise ConfigError("--resume and --run-id name the same thing; pass one")

    config = load_config(_resolve_config(args.config))
    # "Not supplied" and "supplied as an empty string" are different, and the
    # difference is the whole point. ``args.run_id or default`` conflates them,
    # so an operator whose shell variable expanded to nothing
    # (``--run-id "$RUN_ID"`` with RUN_ID unset) would get a fresh timestamped
    # run instead of a refusal -- and a fresh run re-reports every target in
    # the list, including any whose report already landed. Compared against
    # ``None`` rather than tested for truthiness, for that reason alone.
    if args.resume is not None:
        run_id = args.resume
    elif args.run_id is not None:
        run_id = args.run_id
    else:
        run_id = f"run-{_utc_stamp()}"
    # Refused here, before anything is created, and through the same function
    # ``Paths.run_dir`` uses -- so the CLI and the artifact store cannot
    # disagree about what a usable id is. This used to be a second copy of the
    # rule inline, which meant the Windows cases (a trailing space or dot, a
    # control character) passed here and failed later as an ``OSError`` from a
    # ``mkdir``, which is the same bug reported from three files further down.
    #
    # Refused rather than sanitised, deliberately: a sanitised id points at a
    # *different* run's directory, and resuming the wrong run is worse than not
    # resuming at all.
    try:
        check_run_id(run_id)
    except RunIdError as exc:
        raise ConfigError(str(exc)) from exc

    # Targets are loaded before any browser or pool is built, so a typo in a
    # handle costs a second rather than a Chromium launch.
    targets = _load_targets_from(config, args.targets)

    run_cfg = config.run
    options = RunOptions(
        max_reports=(
            args.max_reports if args.max_reports is not None else run_cfg.max_reports
        ),
        horizon_seconds=(
            args.horizon_hours * 3600.0
            if args.horizon_hours is not None
            else run_cfg.horizon_seconds
        ),
        transient_retries=run_cfg.transient_retries,
        backoff_seconds=run_cfg.backoff_seconds,
        exit_rotations=run_cfg.exit_rotations,
        channel_failure_threshold=run_cfg.channel_failure_threshold,
        max_concurrent=(
            args.max_concurrent
            if args.max_concurrent is not None
            else (run_cfg.max_concurrent or config.browser.max_concurrent)
        ),
        dry_run=args.dry_run,
    )

    try:
        return asyncio.run(
            _run(
                config,
                targets,
                options,
                run_id=run_id,
                resume=bool(args.resume),
                verbose=args.verbose,
                stream=stream,
                fetch_impl=fetch_impl,
            )
        )
    except KeyboardInterrupt:
        print(
            "\ninterrupted; the report in flight was allowed to finish and settle",
            file=stream,
        )
        return EXIT_INTERRUPTED


def _cmd_status(args: argparse.Namespace, *, stream: TextIO, fetch_impl: Any = None) -> int:
    setup_logging(verbose=args.verbose, stream=stream)
    return _status(load_config(_resolve_config(args.config)), args.run, stream=stream)


def _cmd_targets(
    args: argparse.Namespace, *, stream: TextIO, fetch_impl: Any = None
) -> int:
    setup_logging(verbose=args.verbose, stream=stream)
    return _targets(load_config(_resolve_config(args.config)), args.targets, stream=stream)


def _cmd_anchors(
    args: argparse.Namespace, *, stream: TextIO, fetch_impl: Any = None
) -> int:
    setup_logging(verbose=args.verbose, stream=stream)
    config = load_config(_resolve_config(args.config))
    if args.check:
        return _anchors_check(config, args.anchors_path, args.check, stream=stream)
    return _anchors(config, args.anchors_path, stream=stream)


def main(
    argv: Sequence[str] | None = None,
    *,
    stream: TextIO | None = None,
    fetch_impl: Callable[..., Any] | None = None,
) -> int:
    """Entry point. Returns an exit code rather than calling ``sys.exit``.

    Returning rather than exiting is what lets the tests drive the real
    argument parser and the real dispatch and assert on the code, instead of
    asserting on a captured ``SystemExit``.
    """
    out = stream or sys.stdout
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        return int(args.handler(args, stream=out, fetch_impl=fetch_impl))
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=out)
        return EXIT_REFUSED
    except TargetProblem as exc:
        # Every problem in the list at once, already rendered by the targets
        # module with the remedy in the message. Catching it here is the backstop
        # for any path that reaches ``require_usable()`` without a handler of its
        # own: a traceback naming a list an operator is trying to fix costs them
        # the diagnosis they ran the command to get.
        print(f"cannot use the target list: {exc}", file=out)
        return EXIT_REFUSED
    except KeyboardInterrupt:
        print("\ninterrupted", file=out)
        return EXIT_INTERRUPTED
    except OSError as exc:
        # A missing Playwright build, an unwritable data directory, a locked
        # ledger. All real, all the operator's problem, and none of them a
        # traceback's problem.
        print(f"could not continue: {exc}", file=out)
        return EXIT_REFUSED


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
