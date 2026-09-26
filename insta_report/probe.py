"""T0: the discriminating probe.

Everything about the API channel is currently unobservable. The first probe
returned 429 with zero bytes, which flags the source IP. Every later probe came
from that same burned address, and a 404 from ``i.instagram.com`` without a
valid ``Authorization`` header is indistinguishable between three very
different worlds:

    1. the route is gone
    2. the route exists and this is the right response
    3. the request never got far enough to mean anything

The wrong conclusion was drawn from this earlier: "the endpoints are gone."
The honest conclusion is "unobservable from a burned IP." The API channel
(decision D5) is gated on resolving that, and this module is the resolution.

**The control call is the whole design.** A known-good authenticated request to
the *same host* runs first, from the *same* exit. If the control does not answer,
the probe reports ``INCONCLUSIVE`` and refuses to interpret anything else --
because a 404 next to a failed control tells you about your proxy, not about
Instagram. Skipping the control is the specific mistake that produced the
original wrong answer, so it is not optional and not skippable by flag.

Run it with::

    python -m insta_report.probe --config ~/insta-report.toml

It reads proxies from the config's ``[proxies]`` section and requires a
``[probe]`` section naming the control. It reports, it never acts: no target is
reported, nothing is written to a checkpoint, no account is touched.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import httpx

from .outcomes import NetworkVerdict, classify_network
from .support.redaction import get_registry

__all__ = [
    "ProbeVerdict",
    "ProbeResult",
    "ProbeReport",
    "run_probe",
    "DEFAULT_PROBES",
]

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 20.0

#: Matches the tier these probes actually talk to. Every default probe and the
#: control point at ``i.instagram.com``, which is the *mobile* API host, and
#: that tier answers ``400 {"message": "useragent mismatch"}`` to anything that
#: does not present as a mobile client.
#:
#: This used to be a desktop Chrome string, which meant the probe's shipped
#: defaults asked a mobile host a mobile question in a desktop's voice. The
#: failure was quiet and self-consistent: the control 500'd, the module
#: reported INCONCLUSIVE, and the natural reading -- "unobservable, so do not
#: build the channel" -- was wrong. The API was answering the whole time.
#:
#: The earlier comment here said a pinned default "goes stale silently", which
#: is not what was measured. Measured, on 2026-09-26 against a live session:
#: app versions 155.0.0.14.114, 219.0.0.12.117 and 302.0.0.23.113 all
#: returned byte-identical 200s, with and without ``X-IG-App-ID``, on both the
#: Android and iOS UA shapes. Only the *format* is checked. So this stays
#: pinned, and the config still overrides it -- but the reason to override is
#: to match a tier, not to chase a version.
DEFAULT_USER_AGENT = (
    "Instagram 302.0.0.23.113 Android (24/7.0; 640dpi; 1440x2560; samsung; "
    "SM-G930F; herolte; samsungexynos8890; en_US; 336201482)"
)


class ProbeVerdict(str, Enum):
    """What a probe run can honestly conclude."""

    #: Control answered and the endpoint answered readably. A real signal.
    OBSERVABLE = "observable"
    #: Control answered, endpoint gave something readable that is not success.
    #: The route exists; the request was refused or the shape is wrong.
    ANSWERED_NOT_OK = "answered_not_ok"
    #: Control answered, endpoint returned nothing interpretable. Cannot say
    #: whether the route exists.
    UNOBSERVABLE = "unobservable"
    #: The control itself failed. Nothing else in this run means anything.
    #: This is the outcome a burned IP produces, and it is the one that caused
    #: the original misdiagnosis.
    INCONCLUSIVE = "inconclusive"

    #: A route answered ``ok`` while the control did not. The API is real and
    #: reachable and the session is good -- that much is proven -- but the exit
    #: could not be shown to work, so nothing here will reproduce.
    #:
    #: This is a separate verdict rather than ``OBSERVABLE`` because the two
    #: answers lead to opposite actions. ``OBSERVABLE`` unblocks building the
    #: channel. This one says: the route exists, so the channel is worth
    #: building, but not from *this* exit -- go get a better one first.
    #:
    #: Observed for real, which is why it exists. One run returned ``403`` for
    #: the control and ``200 {"status": "ok"}`` for a probe against the same URL
    #: 600ms later, and the old rule reported INCONCLUSIVE and threw the
    #: success away. The success was the more informative of the two.
    OBSERVABLE_UNSTABLE_EXIT = "observable_unstable_exit"


@dataclass
class ProbeResult:
    """One HTTP call and everything needed to judge it."""

    name: str
    url: str
    status: int | None = None
    body: str = ""
    content_type: str | None = None
    elapsed_ms: int = 0
    egress_ip: str | None = None
    error: str | None = None
    #: Snippet of the body, redacted, for a human to eyeball.
    excerpt: str = ""

    @property
    def network_verdict(self) -> NetworkVerdict:
        return classify_network(
            status=self.status,
            body=self.body,
            content_type=self.content_type,
            timed_out=self.error == "timeout",
        )

    def to_row(self) -> dict[str, Any]:
        return {
            "probe": self.name,
            "status": self.status,
            "bytes": len(self.body),
            "content_type": self.content_type,
            "elapsed_ms": self.elapsed_ms,
            "egress_ip": self.egress_ip,
            "network_verdict": self.network_verdict.value,
            "error": self.error,
        }


@dataclass
class ProbeReport:
    """A full run: one exit, one control, N probes."""

    exit_ip: str | None
    control: ProbeResult | None
    results: list[ProbeResult] = field(default_factory=list)

    @property
    def control_ok(self) -> bool:
        """Did the known-good call answer readably?

        A statement about the **exit**, not about any route. That distinction is
        the whole design: the control exists so that a *negative* probe result
        can be attributed -- a 404 next to a working control is a missing route,
        and a 404 next to a failed control is an exit that cannot be trusted.
        Only negatives need that corroboration.
        """
        if self.control is None:
            return False
        return self.control.network_verdict is NetworkVerdict.OK

    @property
    def verdict(self) -> ProbeVerdict:
        verdicts = {r.network_verdict for r in self.results}

        if not self.control_ok:
            # A failed control suppresses *negative* results, and only negative
            # results. ``ok`` is not ambiguous: it cannot be produced by a
            # blocked exit, a broken proxy, or an absent credential. Measured on
            # 2026-09-26 -- the same route unauthenticated answers 404 -> 302 ->
            # the logged-out page, never an application error -- so a ``200
            # {"status": "ok"}`` is evidence about *this session* and not just
            # about the route.
            #
            # Discarding it is how a working API got reported as unobservable:
            # the run above had a 200 sitting in its own results and threw it
            # away because an earlier request to the same URL had failed.
            #
            # A control that never *ran* is a different case and is excluded.
            # ``OBSERVABLE_UNSTABLE_EXIT`` claims the exit was watched refusing
            # us; a missing control means nobody watched, so there is no
            # behaviour to report and the run is simply malformed.
            if self.control is not None and NetworkVerdict.OK in verdicts:
                return ProbeVerdict.OBSERVABLE_UNSTABLE_EXIT
            return ProbeVerdict.INCONCLUSIVE

        if not verdicts:
            return ProbeVerdict.UNOBSERVABLE
        if verdicts <= {NetworkVerdict.OK, NetworkVerdict.REJECTED}:
            # Every route answered with something readable. The endpoints are
            # real, which is the question D5 was gated on.
            return (
                ProbeVerdict.OBSERVABLE
                if NetworkVerdict.OK in verdicts
                else ProbeVerdict.ANSWERED_NOT_OK
            )
        return ProbeVerdict.UNOBSERVABLE

    def summary(self) -> str:
        lines = [
            f"exit ip          : {self.exit_ip or 'unknown'}",
            f"control          : {_control_line(self.control)}",
            f"OVERALL VERDICT  : {self.verdict.value.upper()}",
            "",
            f"{'probe':<22} {'status':>7} {'bytes':>7} {'ms':>6}  verdict",
            "-" * 62,
        ]
        for result in self.results:
            lines.append(
                f"{result.name:<22} {str(result.status or '-'):>7} "
                f"{len(result.body):>7} {result.elapsed_ms:>6}  "
                f"{result.network_verdict.value}"
            )
        lines.append("")
        lines.append(_interpretation(self.verdict))
        return "\n".join(lines)

    def to_json(self) -> dict[str, Any]:
        return {
            "egress_ip": self.exit_ip,
            "control_ok": self.control_ok,
            "verdict": self.verdict.value,
            "control": self.control.to_row() if self.control else None,
            "results": [r.to_row() for r in self.results],
        }


def _control_line(control: ProbeResult | None) -> str:
    if control is None:
        return "NOT RUN -- this is why the run is inconclusive"
    return (
        f"{control.status} {len(control.body)}B "
        f"{control.network_verdict.value} "
        f"({control.elapsed_ms}ms)"
        + (f" error={control.error}" if control.error else "")
    )


def _interpretation(verdict: ProbeVerdict) -> str:
    if verdict is ProbeVerdict.INCONCLUSIVE:
        return (
            "INCONCLUSIVE. The known-good control call did not answer, so these\n"
            "results say nothing about whether the routes exist. The usual cause is a\n"
            "flagged source IP -- an earlier probe returned 429 with zero bytes, which\n"
            "is what flags one. Re-run from clean residential exits. Do not conclude\n"
            "the endpoints are gone; that was the original misdiagnosis."
        )
    if verdict is ProbeVerdict.OBSERVABLE:
        return (
            "OBSERVABLE. The control answered and at least one route answered\n"
            "readably, from the same exit. The mobile API channel is real and worth\n"
            "building. Re-run from a second clean exit before committing, in case this\n"
            "one is unusual."
        )
    if verdict is ProbeVerdict.OBSERVABLE_UNSTABLE_EXIT:
        return (
            "OBSERVABLE, BUT THE EXIT IS NOT TRUSTWORTHY. At least one route\n"
            "answered 'ok' -- so the API exists, this session is good, and the channel\n"
            "is worth building -- while the control on the same exit did not answer.\n"
            "\n"
            "That combination is the interesting part. Both requests carried the same\n"
            "cookie from the same address moments apart, so something is refusing\n"
            "some requests and not others: a challenge threshold, a per-route throttle,\n"
            "or a flaky provider hop. A run on this exit would be unreliable, which is\n"
            "worse than no run at all, because it fails partway.\n"
            "\n"
            "Do not treat this as INCONCLUSIVE. It is the stronger of the two readings.\n"
            "Re-run from a clean residential exit; if the control answers there, the\n"
            "API channel is confirmed and the problem was the exit."
        )
    if verdict is ProbeVerdict.ANSWERED_NOT_OK:
        return (
            "ANSWERED, NOT OK. Every route returned something readable that is not a\n"
            "success. The routes exist. Whether the request is shaped correctly is a\n"
            "separate question this probe does not answer -- it makes no authenticated\n"
            "report call against a real target."
        )
    return (
        "UNOBSERVABLE. The control answered, but the routes returned nothing this can\n"
        "read. That is consistent with a moved route and with a shape we are sending\n"
        "wrong. It is not evidence of absence."
    )


# --- probe definitions ------------------------------------------------------


@dataclass(frozen=True)
class Probe:
    """One request to make. Read-only by construction -- no report is filed."""

    name: str
    url: str
    method: str = "GET"
    headers: Mapping[str, str] = field(default_factory=dict)
    body: str | None = None


#: Endpoints whose liveness T0 is trying to establish. Deliberately read-only:
#: nothing here submits a report, so a run cannot harm a target.
#:
#: Read-only is *enforced*, not merely intended, and it is enforced here because
#: the first version of this table was not. It included a POST to
#: ``/api/v1/users/{user_id}/flag_user/`` -- the live reporting endpoint -- which
#: was harmless only because ``{user_id}`` was never substituted, so the request
#: went to a literal ``{user_id}`` and 404'd. A safety property that holds
#: because a substitution nobody remembered to add is missing is not a safety
#: property. Supplying that substitution is the obvious next edit, and the result
#: would be a tool that flags a real account while its own docstring states it
#: files no reports -- the same shape of lie as the code this project replaced,
#: inverted: there it claimed to have reported when it had not, here it would
#: claim not to have reported when it had.
#:
#: So it is a GET. A GET to that route answers 405 when the route exists, which
#: is the liveness signal T0 wanted, and answers it without a method that can
#: change anything. ``_assert_read_only`` below refuses to load a probe set
#: containing a state-changing call, so the guarantee lives in code.
DEFAULT_PROBES: tuple[Probe, ...] = (
    Probe(
        name="account_form_data",
        url="https://i.instagram.com/api/v1/accounts/edit/web_form_data/",
    ),
    Probe(
        name="web_profile_info",
        url="https://i.instagram.com/api/v1/users/web_profile_info/?username={username}",
    ),
    Probe(
        name="web_profile_info_app_id",
        url="https://i.instagram.com/api/v1/users/web_profile_info/?username={username}",
        headers={"X-IG-App-ID": "936619743392459"},
    ),
    Probe(
        name="flag_user_route_exists",
        url="https://www.instagram.com/api/v1/users/{user_id}/flag_user/",
        # GET, and asserted. See the note above -- a 405 here still proves the
        # route is real, and nothing here can report anyone.
        method="GET",
    ),
    Probe(
        name="profile_html",
        url="https://www.instagram.com/{username}/",
    ),
    Probe(
        name="root",
        url="https://www.instagram.com/",
    ),
)

#: Methods that cannot change state on a remote server. The probe's contract --
#: "reports only, files no reports, touches no target" -- is only worth anything
#: if it is checked rather than asserted in a docstring.
READ_ONLY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _assert_read_only(probes: Sequence[Probe]) -> None:
    """Refuse a probe set that could change something.

    Raises rather than warns. A probe run is a diagnostic: its value is that the
    operator can trust it changed nothing, and a diagnostic that is trusted only
    because it was careful is not a diagnostic.
    """
    for probe in probes:
        if probe.method.upper() not in READ_ONLY_METHODS:
            raise ValueError(
                f"probe {probe.name!r} uses {probe.method!r}, which can change "
                f"state on {probe.url.split('?')[0]!r}. This module reports; it "
                f"does not act. If a state-changing call is genuinely needed, it "
                f"is not a probe and does not belong in DEFAULT_PROBES."
            )



def _egress_ip(proxy: str | None, timeout: float, user_agent: str) -> str | None:
    """The address the world sees, so a 'clean exit' claim can be checked.

    A proxy pool that silently hands back the same flagged address is the reason
    the original probe burned itself, and it is invisible without asking.

    This builds its **own** client rather than borrowing the caller's. Borrowing
    looked harmless and was not: the caller's client carries
    ``Cookie: sessionid=...`` at the client level, so the Instagram session was
    sent to ``api.ipify.org`` and ``ifconfig.me/ip`` on every single run. Those
    are unrelated third parties, and the operator consented to neither.

    The proxy is still threaded through, and must be: an egress check that
    bypasses the proxy reports the *direct* address, which is the one number
    this function exists to establish and the one number that would be wrong.

    So the credential is kept out of scope structurally -- it is not passed in,
    so it cannot be sent -- rather than by remembering to strip it later.
    """
    headers = {"User-Agent": user_agent, "Accept": "text/plain"}
    try:
        with httpx.Client(
            proxy=proxy, timeout=timeout, headers=headers
        ) as client:
            for service in ("https://api.ipify.org", "https://ifconfig.me/ip"):
                try:
                    response = client.get(service)
                except httpx.HTTPError:
                    continue
                if response.status_code == 200:
                    candidate = response.text.strip()
                    if candidate:
                        return candidate
    except httpx.HTTPError:
        # The proxy refused to connect at all. Every service would fail
        # identically, so there is no point trying the second one, and no
        # address to report: "unknown" is the honest answer, and it is
        # different from "the address is empty".
        log.debug("egress check could not connect through %r", proxy)
    return None


def render_url(url: str, substitutions: Mapping[str, str]) -> str:
    """Substitute ``{name}`` placeholders, leaving unknown ones in place.

    Uses ``str.replace`` rather than ``str.format`` deliberately. ``format``
    raises ``KeyError`` on a placeholder it has no value for, and this table
    contains one that is intentionally not substituted -- so a strict formatter
    would make the probe refuse to run, and the loose alternative of
    pre-formatting the table would hide which call was actually made.
    """
    for key, value in substitutions.items():
        url = url.replace("{" + key + "}", value)
    return url


def _do(
    client: httpx.Client, probe: Probe, substitutions: Mapping[str, str]
) -> ProbeResult:
    """Execute one probe and record what came back."""
    url = render_url(probe.url, substitutions)

    # ``probe.headers`` is the *request*, not a display value, so nothing here is
    # scrubbed. It was, and that was the worst bug in this module's history:
    # the control call carries ``Cookie: sessionid=...`` in its headers, the
    # registry replaced the sessionid with the redaction placeholder, and the
    # one request the entire module exists to trust went out unauthenticated.
    # It answered 403 on every run, and because the *probes* pass ``headers={}``
    # -- so the loop was a no-op for them, and they inherited the real cookie
    # from the client -- a probe against the control's own route answered 200
    # seconds later. The symptom read as "Instagram is flaky"; the cause was
    # this function.
    #
    # Redaction belongs on the way *out* to a human, not on the way out to the
    # network. It is applied where a value is recorded for display:
    # ``ProbeResult.excerpt`` and the JSON/table renderers.
    headers = dict(probe.headers)

    result = ProbeResult(name=probe.name, url=url)
    started = time.monotonic()
    try:
        response = client.request(
            probe.method,
            url,
            headers=headers,
            content=probe.body,
        )
        result.status = response.status_code
        result.body = response.text
        result.content_type = response.headers.get("content-type")
    except httpx.TimeoutException:
        result.error = "timeout"
    except httpx.HTTPError as exc:
        result.error = type(exc).__name__
        log.debug("probe %s failed: %s", probe.name, exc)
    result.elapsed_ms = int((time.monotonic() - started) * 1000)
    result.excerpt = get_registry().scrub(result.body[:200]).replace("\n", " ")
    return result


def run_probe(
    *,
    proxy: str | None,
    control_url: str,
    probes: Sequence[Probe] = DEFAULT_PROBES,
    substitutions: Mapping[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    user_agent: str = DEFAULT_USER_AGENT,
    extra_headers: Mapping[str, str] | None = None,
) -> ProbeReport:
    """Run the control, then the probes, from a single exit.

    Order matters and is not rearranged: the control establishes that this exit
    can get a readable answer at all. Running the probes first and checking the
    control afterwards would let a burned IP produce four confident-looking 404s
    and one failure, and the four are the ones that mislead.

    The read-only check runs *before* any network call, so a probe set that
    could report someone fails without having reported them.
    """
    _assert_read_only(probes)

    subs = dict(substitutions or {})
    headers = {"User-Agent": user_agent, **dict(extra_headers or {})}

    # The egress check runs first and outside the credentialed client, so the
    # order that matters -- control before probes -- is untouched, and the
    # session never travels to an IP-echo service. See ``_egress_ip``.
    report = ProbeReport(
        exit_ip=_egress_ip(proxy, timeout, user_agent), control=None
    )

    with httpx.Client(
        proxy=proxy,
        timeout=timeout,
        follow_redirects=True,
        headers=headers,
    ) as client:
        # The control adds no headers of its own. The credential is attached
        # once, on the client, and the control and the probes both inherit it.
        # They used to be attached twice -- once on the client and once on the
        # control's own ``Probe.headers`` -- and that duplication is how the
        # redaction loop came to scrub one copy and not the other.
        control = Probe(name="CONTROL (known-good)", url=control_url)
        report.control = _do(client, control, subs)

        if not report.control_ok:
            # True of the *negative* rows only. A row that answers ``ok`` does
            # not need the control's corroboration and will still be believed,
            # which is why this says what it actually means rather than the
            # reassuring-sounding "nothing here means anything".
            log.error(
                "control call failed (status=%s verdict=%s error=%s); negative "
                "probe results below cannot be attributed to a route rather "
                "than to this exit. A probe answering 'ok' is still evidence.",
                report.control.status,
                report.control.network_verdict.value,
                report.control.error,
            )

        report.results = [_do(client, probe, subs) for probe in probes]
        return report


def run_across_exits(
    exits: Iterable[str | None],
    **kwargs: Any,
) -> list[ProbeReport]:
    """Same probe from several addresses.

    One exit is an anecdote. The T0 decision needs at least two clean
    residential addresses, because a single unusual exit is exactly what a
    burned IP looks like from the inside.
    """
    return [run_probe(proxy=exit, **kwargs) for exit in exits]


# --- entry point ------------------------------------------------------------

#: Authenticated, read-only, and known to answer when credentials are good. This
#: is the control: if it does not answer, nothing else in the run means anything.
#:
#: It was ``/api/v1/accounts/current_v2/``, which is not a route. Measured on
#: 2026-09-26: 404 with Instagram's 20,942-byte logged-out HTML page for every
#: mobile user agent, and 500 for the desktop one. A control that answers 404
#: to a valid session cannot distinguish "this exit is blocked" from "this URL
#: is wrong" -- and it reported INCONCLUSIVE on an exit that was demonstrably
#: working, which is the expensive direction to be wrong in.
#:
#: ``/api/v1/accounts/edit/web_form_data/`` is the replacement. It exists, it
#: requires a session, it is a GET, and it answers ``{"status": "ok", ...}`` --
#: which is the one shape :func:`classify_body` is built to recognise. It also
#: fails *usefully*: an unauthenticated request gets 404 -> 302 -> the login
#: page, and a bad user agent gets a JSON ``useragent mismatch``. Both name the
#: actual problem, which is the property a control exists to provide.
DEFAULT_CONTROL_URL = "https://i.instagram.com/api/v1/accounts/edit/web_form_data/"

#: A username the operator controls. Probing an account you do not own is both
#: rude and less informative, since a genuine block looks like a missing one.
DEFAULT_PROBE_USERNAME = "instagram"


def _main(argv: Sequence[str] | None = None) -> int:
    import argparse

    from .config import ConfigError, load_config
    from .support.logging import setup_logging

    parser = argparse.ArgumentParser(
        prog="python -m insta_report.probe",
        description=(
            "Decide whether the mobile API channel is observable. Reports only; "
            "files no reports and touches no target's state."
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
        "--username",
        default=DEFAULT_PROBE_USERNAME,
        help="an account you control, used as the {username} substitution",
    )
    parser.add_argument(
        "--proxy",
        action="append",
        default=[],
        metavar="URL",
        help="repeat per exit; omit to probe the direct connection",
    )
    parser.add_argument(
        "--account",
        help="account ref supplying the sessionid for the control call",
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

    accounts = [a for a in config.accounts if a.enabled]
    if not accounts:
        print("no enabled accounts in config", file=sys.stderr)
        return 2
    chosen = next((a for a in accounts if a.ref == args.account), accounts[0])

    # Reading sessionid here is what registers it with the redactor, so any
    # excerpt printed below has already been scrubbed.
    headers = {"Cookie": f"sessionid={chosen.sessionid}"}
    if config.api.app_id:
        headers["X-IG-App-ID"] = config.api.app_id

    # The probe set targets the *mobile* API host, so the identity presented must
    # be a mobile one. Falling back to the web user agent here is what made the
    # first T0 run report INCONCLUSIVE on a perfectly reachable API: with
    # [api] mobile_user_agent unset, the run sent a desktop Chrome string to
    # i.instagram.com and got "useragent mismatch" back from every route.
    #
    # A wrong-tier identity is a configuration error, so it is named rather than
    # papered over. Falling back to the module default -- which is mobile, and
    # which the comment above explains -- is correct; falling back to the *web*
    # agent is not, and never was.
    if config.api.mobile_user_agent:
        probe_ua = config.api.mobile_user_agent
    else:
        probe_ua = DEFAULT_USER_AGENT
        if config.api.web_user_agent:
            log.warning(
                "[api] mobile_user_agent is unset; using the built-in mobile "
                "default %r. The web user agent is deliberately NOT used here: "
                "these probes target the mobile API tier, which rejects a "
                "desktop identity with 'useragent mismatch'.",
                DEFAULT_USER_AGENT.split(" Android ")[0] + " ...",
            )

    exits: list[str | None] = list(args.proxy) or [None]
    if len(exits) < 2:
        print(
            f"warning: probing {len(exits)} exit. The T0 decision needs at least "
            "two clean residential addresses -- pass --proxy more than once.",
            file=sys.stderr,
        )

    reports = run_across_exits(
        exits,
        control_url=args.control,
        substitutions={"username": args.username},
        timeout=args.timeout,
        user_agent=probe_ua,
        extra_headers=headers,
    )

    if args.json:
        print(json.dumps([r.to_json() for r in reports], indent=2))
    else:
        for index, report in enumerate(reports):
            if len(reports) > 1:
                print(f"\n{'=' * 64}\nEXIT {index + 1} of {len(reports)}\n{'=' * 64}")
            print(report.summary())

    # Exit code mirrors the strongest signal seen. 0 requires a *clean*
    # OBSERVABLE, not merely a route that answered: a run that proved the API
    # exists but could not prove its own exit worked has not cleared the gate,
    # because every report it later files would be filed through an exit it does
    # not trust. That is exit 1 -- finished, and the operator has reading to do.
    best = {r.verdict for r in reports}
    if ProbeVerdict.OBSERVABLE in best:
        return 0
    if ProbeVerdict.OBSERVABLE_UNSTABLE_EXIT in best or (
        ProbeVerdict.ANSWERED_NOT_OK in best
    ):
        return 1
    return 3


if __name__ == "__main__":
    raise SystemExit(_main())
