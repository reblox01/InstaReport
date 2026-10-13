"""T9: preflight -- prove the API channel answers, before anything relies on it.

The channel itself is ``api.py``: a complete classifier attached to an endpoint
that refuses to exist unverified. This module is the third piece, answering the
one question neither of those two can: **does the channel's host answer this
account's identity, from an exit a run would actually use?**

One question, two requests. A known-good control call to the same host runs
first, for the probe's reason: a 404 next to a failed control says nothing
about Instagram and everything about the exit. Then a single GET to the
reporting route the channel would file against. A GET on that route answers
405 when the route exists -- the liveness signal -- and cannot change
anything, so the probe's read-only assertion runs before any request and
refuses a state-changing method outright.

**What a pass proves:** from this exit, with this session and this mobile
identity, the API host answers and the reporting route is reachable.

**What it does not prove:** that a report was filed (no POST leaves this
module), that the endpoint shape D5 names is correct (that claim still needs a
watched submission -- see ``ApiEndpoint.verified``), or that the ``IGT:2``
signature is right (never observed; still a constructor argument with no
default). A preflight is evidence about liveness, not a certificate of
readiness, and it is deliberately not persisted: it describes one exit at one
moment, so it expires with the exit it was measured from. No state file means
no stale file to trust.

Run it with::

    python -m insta_report.preflight --config ~/insta-report.toml [--proxy URL]

The gate for whoever wires the channel is :func:`assert_answers`: a ladder
falling into a channel nobody has watched answer is the exact failure
:class:`~insta_report.errors.PreflightFailed` exists to describe.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from .errors import PreflightFailed
from .probe import (
    DEFAULT_CONTROL_URL,
    DEFAULT_TIMEOUT,
    FLAG_ROUTE_URL,
    ChannelIdentity,
    IdentityError,
    Probe,
    ProbeReport,
    ProbeResult,
    ProbeVerdict,
    resolve_channel_identity,
    run_probe,
)

if TYPE_CHECKING:
    from .config import Config

__all__ = [
    "PreflightReport",
    "ANSWERING_VERDICTS",
    "run_preflight",
    "preflight_account",
    "assert_answers",
]

#: The one request besides the control: the reporting route, read-only. A 405
#: on it is the signal -- the route exists and wants a method GET does not
#: carry -- and a readable 200 answers too. Anything uninterpretable does not.
_PREFLIGHT_PROBES: tuple[Probe, ...] = (
    Probe(name="flag_user_route_exists", url=FLAG_ROUTE_URL, method="GET"),
)

#: The verdicts that mean the channel answered. Both require the control to
#: have answered first -- that is the probe's design and it is not optional
#: here either -- and then accept the route either succeeding (``OBSERVABLE``)
#: or refusing the GET readably (``ANSWERED_NOT_OK``): on a POST-only route a
#: readable refusal *is* the route answering.
#:
#: ``OBSERVABLE_UNSTABLE_EXIT`` is deliberately absent. The route may well
#: have answered, but this exit could not be shown to work, so nothing from
#: this run will reproduce -- and a run built on it would fail partway, which
#: is worse than not starting.
ANSWERING_VERDICTS = frozenset(
    {ProbeVerdict.OBSERVABLE, ProbeVerdict.ANSWERED_NOT_OK}
)


def _interpretation(verdict: ProbeVerdict) -> str:
    """What to conclude, in preflight's terms rather than the probe's.

    The probe's interpretation talks about *building* the channel; this talks
    about *relying* on it, which is a different question with different
    instructions -- and the same refusal to call a failed control a finding
    about the route.
    """
    if verdict is ProbeVerdict.INCONCLUSIVE:
        return (
            "The known-good control did not answer, so this run says nothing "
            "about the channel. The usual cause is a flagged source IP. Re-run "
            "from a clean residential exit, and do not conclude the route is "
            "gone -- that was the original misdiagnosis."
        )
    if verdict is ProbeVerdict.OBSERVABLE:
        return (
            "The control answered and the reporting route answered readably "
            "from the same exit. The channel answers: this is the observation "
            "the ladder needs before the channel may be relied on."
        )
    if verdict is ProbeVerdict.ANSWERED_NOT_OK:
        return (
            "The control answered and the reporting route refused the GET "
            "readably. On a POST-only route a readable refusal is the route "
            "answering: it exists. The channel answers from this exit."
        )
    if verdict is ProbeVerdict.UNOBSERVABLE:
        return (
            "The control answered, but the reporting route returned nothing "
            "interpretable (a timeout or an error page). This exit cannot be "
            "shown to reach the channel; re-run from a clean residential exit."
        )
    return (
        "The reporting route answered but the control did not. The session "
        "reaches the API; this exit could not be shown to work, so nothing "
        "here will reproduce. Re-run from a different exit -- the route being "
        "real and this address being usable are separate findings."
    )


@dataclass(frozen=True)
class PreflightReport:
    """Control plus reporting route from one exit, reduced to one question.

    Wraps the probe's own :class:`~insta_report.probe.ProbeReport` rather than
    re-deriving it: the control-first ordering, the read-only assertion and the
    placeholder assertion are the probe's invariants, and preflight inherits
    them by construction instead of reimplementing them and hoping the two
    stay honest together.
    """

    account_ref: str
    proxy: str | None
    report: ProbeReport

    @property
    def verdict(self) -> ProbeVerdict:
        return self.report.verdict

    @property
    def answers(self) -> bool:
        """The single bit the ladder cares about. See :data:`ANSWERING_VERDICTS`."""
        return self.verdict in ANSWERING_VERDICTS

    @property
    def route(self) -> ProbeResult | None:
        return self.report.results[0] if self.report.results else None

    def summary(self) -> str:
        control = self.report.control
        control_line = "not run"
        if control is not None:
            control_line = f"{control.status or '-'} {control.network_verdict.value}"
            if control.error:
                control_line += f" ({control.error})"
        route = self.route
        route_line = "not run"
        if route is not None:
            route_line = f"{route.status or '-'} {route.network_verdict.value}"
            if route.error:
                route_line += f" ({route.error})"
        exit_line = self.proxy or "direct"
        if self.report.exit_ip:
            exit_line += f" (ip {self.report.exit_ip})"
        state = "ANSWERS" if self.answers else "DOES NOT ANSWER"
        lines = [
            f"account          : {self.account_ref}",
            f"exit             : {exit_line}",
            f"control          : {control_line}",
            f"reporting route  : {route_line}",
            "",
            f"PREFLIGHT        : {state} [{self.verdict.value}]",
            "",
            _interpretation(self.verdict),
        ]
        return "\n".join(lines)

    def to_json(self) -> dict[str, Any]:
        """Machine-readable, and carrying no more than the table does.

        Rows rather than bodies, deliberately: this is the artifact a wrapper
        parses, and a response body in it is a response body somebody will
        eventually paste somewhere. ``excerpt`` stays out for the same reason.
        """
        control = self.report.control
        route = self.route
        return {
            "kind": "preflight",
            "account_ref": self.account_ref,
            "exit": self.proxy,
            "exit_ip": self.report.exit_ip,
            "answers": self.answers,
            "verdict": self.verdict.value,
            "control": control.to_row() if control is not None else None,
            "route": route.to_row() if route is not None else None,
        }


def run_preflight(
    identity: ChannelIdentity,
    *,
    proxy: str | None,
    control_url: str = DEFAULT_CONTROL_URL,
    timeout: float = DEFAULT_TIMEOUT,
) -> PreflightReport:
    """Ask one question of one exit: does this identity's channel answer?

    Everything enforceable is enforced by :func:`run_probe` before any request
    goes out -- read-only, placeholders substituted, control first. What is not
    enforced is *which* exit, and deliberately so: the caller supplies it
    because the caller is the one who knows which exit a run would use, and a
    preflight from an exit the run will not use proves nothing the run needs.
    """
    inner = run_probe(
        proxy=proxy,
        control_url=control_url,
        probes=_PREFLIGHT_PROBES,
        substitutions={"user_id": identity.user_id},
        timeout=timeout,
        user_agent=identity.user_agent,
        extra_headers=identity.headers,
    )
    return PreflightReport(account_ref=identity.account_ref, proxy=proxy, report=inner)


def preflight_account(
    config: Config,
    *,
    proxy: str | None,
    account_ref: str | None = None,
    control_url: str = DEFAULT_CONTROL_URL,
    timeout: float = DEFAULT_TIMEOUT,
) -> PreflightReport:
    """Resolve one account's channel identity, then ask whether it answers.

    The one call an operator-facing check needs. The identity assembly lives in
    :func:`~insta_report.probe.resolve_channel_identity` so the identity
    observed answering here is the very same assembly the probe sends -- one
    code path, no drift, and an ``IdentityError`` before any network call when
    the config cannot say who is asking.
    """
    identity = resolve_channel_identity(config, account_ref=account_ref)
    return run_preflight(
        identity, proxy=proxy, control_url=control_url, timeout=timeout
    )


def assert_answers(preflight: PreflightReport) -> None:
    """The gate: raise unless the channel answered from the exit measured.

    Whoever wires ``ApiChannel`` into the ladder calls this one line first. A
    fallback nobody has watched answer is a black hole -- every browser failure
    would drop into it silently -- and the message names the verdict rather
    than saying "failed" because the three non-answering verdicts need three
    different fixes: a flagged exit, an unreachable route, and a control that
    never stood up.
    """
    if not preflight.answers:
        raise PreflightFailed(
            f"preflight for account {preflight.account_ref!r} from "
            f"{preflight.proxy or 'the direct connection'} did not answer: "
            f"verdict {preflight.verdict.value}. The channel is not trusted at "
            "all until a fresh preflight answers; run "
            "'python -m insta_report.preflight' from a clean residential exit "
            "and re-check."
        )


# --- entry point ------------------------------------------------------------


def _main(argv: Sequence[str] | None = None) -> int:
    import argparse

    from .config import ConfigError, load_config
    from .support.logging import setup_logging

    parser = argparse.ArgumentParser(
        prog="python -m insta_report.preflight",
        description=(
            "Prove the API channel answers. Read-only: files no reports and "
            "touches no target's state."
        ),
    )
    parser.add_argument(
        "--config", required=True, type=Path, help="path to the operator config"
    )
    parser.add_argument(
        "--control",
        default=DEFAULT_CONTROL_URL,
        help="known-good authenticated URL used to prove the exit works",
    )
    parser.add_argument(
        "--proxy",
        action="append",
        default=[],
        metavar="URL",
        help="repeat per exit; omit to preflight the direct connection",
    )
    parser.add_argument(
        "--account",
        help="account ref whose identity is preflighted (default: first enabled)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON, not a table")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    args = parser.parse_args(argv)

    setup_logging(verbose=False)

    try:
        config = load_config(args.config)
    except (ConfigError, OSError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    exits: list[str | None] = list(args.proxy) or [None]
    reports: list[PreflightReport] = []
    for exit_url in exits:
        try:
            reports.append(
                preflight_account(
                    config,
                    proxy=exit_url,
                    account_ref=args.account,
                    control_url=args.control,
                    timeout=args.timeout,
                )
            )
        except IdentityError as exc:
            print(f"config error: {exc}", file=sys.stderr)
            return 2

    if args.json:
        print(json.dumps([r.to_json() for r in reports], indent=2))
    else:
        for index, report in enumerate(reports):
            if len(reports) > 1:
                print(f"\n{'=' * 64}\nEXIT {index + 1} of {len(reports)}\n{'=' * 64}")
            print(report.summary())

    # Exit 0 only when *every* named exit answers. "At least one works" is the
    # probe's standard for discovery; this is a gate, and a gate that passes
    # when part of what it checked failed is not a gate. 1 means finished with
    # reading to do; 2 means the config never let a request out.
    return 0 if all(r.answers for r in reports) else 1


if __name__ == "__main__":
    raise SystemExit(_main())
