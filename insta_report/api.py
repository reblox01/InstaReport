"""The API report channel (D5).

D5 puts a mobile identity on the mobile API host as the primary path, with a web
identity as an **internal** fallback. Internal is the load-bearing word: the
runner does not know the fallback exists and must not, because a channel that
wanted to fall through on its own would be re-implementing the ladder -- the part
of this design that is hardest to test and the part a channel has no business
owning.

**Nothing about the request is verified.** The endpoint, the method, the body
shape and the ``IGT:2`` signature D5 names have never been observed answering,
because establishing them requires filing a real report against a real account
from a residential exit. So all of them are constructor arguments with no
defaults, and :class:`ApiEndpoint` refuses to exist unverified. A guess cannot
reach the network from here, and that is the entire point: the failure being
guarded against is not a crash, it is a confident-looking submission from an
endpoint nobody checked.

What *is* settled here is the classification -- how one response becomes one of
the six terminal states -- and it is conservative by construction:

* A 2xx is **not** an acknowledgement. F1 is the reason: Instagram renders
  success optimistically to reporters it does not trust, so its "yes" carries no
  information about whether anything was filed. Only a body an operator has
  explicitly marked as an ack shape -- read from a real submission -- promotes a
  response to ``SUBMITTED_ACKED``. With no markers, which is how this module
  ships, every dispatched request lands in ``SUBMITTED_UNCONFIRMED`` or
  ``UNKNOWN``. That is the correct answer to an unobserved endpoint, not a
  deficiency to be patched out by whoever runs the first real report.
* The internal identity fallback is permitted **only** on an outcome that is
  provably side-effect-free. See :data:`_NEVER_SENT` and
  :func:`_may_have_been_sent`; the reasoning is in :meth:`ApiChannel.report`.

Both properties are load-bearing, and both are the kind that fail silently: a
fallback permitted too eagerly files the same report twice, and a 2xx read as an
ack invents a success. The tests here exist mostly to hold those two shut.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar

import httpx

from .artifacts import ArtifactStore, FailureContext
from .outcomes import Outcome, TerminalState, utc_now
from .proxies import ProxyLease
from .support.urls import missing_placeholders, render_url
from .targets import Target

log = logging.getLogger(__name__)

#: Reported in the ledger, and matched by the runner's per-channel health
#: accounting. A constant rather than a ``name`` attribute read, because the
#: protocol declares ``name`` as a ClassVar and a second spelling of the same
#: string is a second thing to keep in step.
CHANNEL = "api"


class UnverifiedEndpoint(RuntimeError):
    """A reporting endpoint nobody has watched answer.

    Raised at construction, not at dispatch. A channel that discovers this on its
    first report has already been wired into the ladder, and the operator finds
    out at the moment the tool matters.
    """


class EndpointShapeError(ValueError):
    """The endpoint is not shaped like something a report can be filed against."""


#: Failures that provably happened *before any byte reached Instagram*.
#:
#: Read off httpx's hierarchy rather than guessed, and the distinction is the
#: single most consequential thing in this module. Everything here means the
#: request was never processed, so nothing was filed: the ladder may continue and
#: the next identity may try. ``ProxyError`` is here because a proxy that refuses
#: never opens a connection to the origin at all, and ``PoolTimeout`` because a
#: request that never left the pool never left the machine.
#:
#: ``LocalProtocolError`` is here too, and that is a judgement call: it means
#: httpx rejected the request itself, so nothing was sent, but it is the one entry
#: where a future httpx could make it mean something else. It is listed explicitly
#: rather than swept into "any other error", so that a change in meaning shows up
#: as a test failure rather than as a double report in production.
_NEVER_SENT: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ProxyError,
    httpx.LocalProtocolError,
)

#: Failures where bytes went out and we cannot tell whether they landed.
#:
#: Every one of these is ``UNKNOWN`` and terminal. A read timeout after the
#: request was written is the textbook case: the report may be sitting in
#: Instagram's queue, and the only honest record is one that says so. The
#: project's rule is that a lost report is recoverable and a double-filed one is
#: not, so every ambiguity resolves toward "do not send it again".
_MAY_HAVE_BEEN_SENT: tuple[type[Exception], ...] = (
    httpx.ReadTimeout,
    httpx.ReadError,
    httpx.WriteTimeout,
    httpx.WriteError,
    httpx.RemoteProtocolError,
)


@dataclass(frozen=True)
class ApiIdentity:
    """One way of presenting the reporting account.

    ``headers`` is a mapping rather than a ``signature_scheme`` enum on purpose.
    D5 names ``IGT:2``, but the scheme's actual shape -- the literal prefix, the
    fields covered, the encoding, whether it is signed at all -- has never been
    observed, and an enum would have to commit to a set of names that exist only
    in the plan. A mapping can hold whatever is discovered, and until something is
    discovered it holds nothing.
    """

    name: str
    user_agent: str
    app_id: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ApiEndpoint:
    """Where and how one report is filed.

    ``verified`` defaults to ``False`` and is not a configuration flag. It is a
    claim a person makes after watching a real submission, recorded in code where
    a reviewer will see it -- as opposed to ``[api] enabled``, which is a
    deployment switch someone can flip at 2am without reading anything.
    """

    url_template: str
    method: str = "POST"
    verified: bool = False
    #: Substrings that, if present in a 2xx body, mean the report was accepted.
    #: Empty means no 2xx is an acknowledgement. See the module docstring on F1.
    ack_markers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # A report is a write. Anything else is either a mistake or a probe, and
        # this is not the place for either -- the probe module is, and it enforces
        # the opposite invariant on the same idea.
        if self.method.upper() != "POST":
            raise EndpointShapeError(
                f"a reporting endpoint is filed with POST; {self.method!r} cannot "
                "file a report, so an endpoint using it is either a read (use the "
                "probe) or a mistake."
            )
        if not self.url_template:
            raise EndpointShapeError("url_template is required; an empty one is not a route")
        if not self.verified:
            raise UnverifiedEndpoint(
                f"{self.url_template!r} is not marked verified. Nothing in this "
                "module has been observed answering, because establishing it "
                "requires filing a real report from a residential exit. Mark it "
                "verified only after that has been done and the response shape "
                "recorded -- and record the ack markers in the same edit, so the "
                "claim and its evidence arrive together."
            )

    def url_for(self, target: Target) -> str:
        """The concrete URL for *target*, or raise.

        Refuses rather than substituting an empty id. ``.../users//flag_user/``
        is a syntactically valid request to a different, meaningless place, and
        the 404 it earns is indistinguishable from a target that does not exist.
        """
        if not target.user_id:
            raise EndpointShapeError(
                f"target {target.escaped()!r} has no resolved user_id, so there is "
                "nothing to address. Resolve it before dispatch, not here: a "
                "channel that resolved targets would be doing the runner's job."
            )
        url = render_url(self.url_template, {"user_id": target.user_id})
        missing = sorted(missing_placeholders(url, {"user_id"}))
        if missing:  # pragma: no cover - unreachable via url_for's own guard
            raise EndpointShapeError(
                f"{self.url_template!r} needs {missing}, which url_for cannot "
                "supply. Every placeholder in a reporting endpoint must be the "
                "target's id; anything else is a route this channel cannot file "
                "against."
            )
        return url


def _may_have_been_sent(exc: Exception) -> bool:
    """Whether *exc* leaves it genuinely unclear whether the report was filed.

    Unknown exception types answer ``True``. That is the conservative direction and
    it is deliberate: a future httpx release adding an error type this table has
    never heard of should make the tool record ``UNKNOWN`` and stop, not quietly
    decide the report was never sent and send it again.
    """
    if isinstance(exc, _NEVER_SENT):
        return False
    if isinstance(exc, _MAY_HAVE_BEEN_SENT):
        return True
    if isinstance(exc, httpx.RequestError):
        return True
    # Not an httpx failure at all -- a bug in here, most likely. Also ambiguous,
    # and also recorded as UNKNOWN rather than retried.
    return True


@dataclass(frozen=True)
class ApiResponse:
    """What came back, reduced to what the classification needs."""

    identity: str
    status: int | None
    body: str
    error: str | None = None

    @property
    def is_rejection(self) -> bool:
        """A 4xx: the server refused the request, so nothing was filed.

        429 is included, and deliberately. A throttle is a refusal to process, and
        refusing to process means no report exists. It is separated out below
        because a throttle is *not* an identity problem -- retrying the same
        session from the same address under a different user agent would earn the
        same 429 -- but it is a refusal nonetheless.
        """
        return self.status is not None and 400 <= self.status < 500

    @property
    def is_success(self) -> bool:
        return self.status is not None and 200 <= self.status < 300

    def is_acked(self, ack_markers: Sequence[str]) -> bool:
        if not ack_markers:
            return False
        return any(marker in self.body for marker in ack_markers)


def classify(response: ApiResponse, ack_markers: Sequence[str]) -> TerminalState:
    """One response to one terminal state.

    Split out from :meth:`ApiChannel.report` and free of side effects so the whole
    table can be read in one place and tested without a socket. The rows are:

    ==========================  ================================  ===============
    observed                    state                            filed?
    ==========================  ================================  ===============
    2xx, ack marker present     ``SUBMITTED_ACKED``               yes
    2xx, anything else          ``SUBMITTED_UNCONFIRMED``        maybe
    4xx                         ``CHANNEL_FAILED``               provably no
    5xx                         ``UNKNOWN``                      maybe
    read-stage failure          ``UNKNOWN``                      maybe
    connect-stage failure       ``CHANNEL_FAILED``               provably no
    ==========================  ================================  ===============

    Only ``SUBMITTED_ACKED`` is ever reached when ``ack_markers`` is empty, and it
    never is then. A 2xx with no verified ack shape is ``SUBMITTED_UNCONFIRMED``,
    because Instagram's success render is not evidence -- see F1 in the module
    docstring.
    """
    if response.error is not None:
        if _may_have_been_sent(_rehydrate(response.error)):
            return TerminalState.UNKNOWN
        return TerminalState.CHANNEL_FAILED
    if response.is_success:
        if response.is_acked(ack_markers):
            return TerminalState.SUBMITTED_ACKED
        return TerminalState.SUBMITTED_UNCONFIRMED
    if response.is_rejection:
        return TerminalState.CHANNEL_FAILED
    # 5xx, 3xx, and anything else unexpected. The request was processed by
    # something, and we cannot say what.
    return TerminalState.UNKNOWN


def _rehydrate(error: str) -> Exception:
    """Rebuild a typed exception from its recorded name.

    :class:`ApiResponse` carries the error as a string because it is a frozen
    record that gets serialised, and classification runs after that point. The
    alternative -- keeping the exception object on the response -- would mean the
    record cannot be written down, which is the property that matters more.

    An unrecognised name becomes a plain ``Exception``, and ``_may_have_been_sent``
    answers ``True`` for that, so a name from a future httpx is UNKNOWN rather
    than a false "never sent".
    """
    return _ERROR_TYPES.get(error, Exception)(error)


_ERROR_TYPES: Mapping[str, type[Exception]] = {
    cls.__name__: cls for cls in _NEVER_SENT + _MAY_HAVE_BEEN_SENT
}


def _excerpt(body: str, limit: int = 200) -> str:
    """A bounded, single-line, scrubbed view of a response body.

    Scrubbed here rather than at the display layer, because this string reaches an
    artifact file and an exception message, and the artifact is written to disk
    before anybody chooses to look at it.
    """
    from .support.redaction import get_registry

    return get_registry().scrub(body[:limit]).replace("\n", " ")


class ApiChannel:
    """Files one report over HTTP.

    Satisfies :class:`~insta_report.runner.ReportChannel`: a ClassVar ``name``, an
    async ``report``, and an async ``aclose``.
    """

    name: ClassVar[str] = CHANNEL

    def __init__(
        self,
        *,
        endpoint: ApiEndpoint,
        identities: Sequence[ApiIdentity],
        build_body: Callable[[Target], Mapping[str, str]],
        sessionid: str,
        app_id: str | None = None,
        timeout: float = 20.0,
        artifacts: ArtifactStore | None = None,
    ) -> None:
        if not identities:
            raise EndpointShapeError(
                "at least one identity is required. D5's ladder is mobile-first "
                "with a web fallback, so an empty ladder is not a "
                "configuration, it is a channel with nothing to say."
            )
        if endpoint.method.upper() != "POST":  # pragma: no cover - endpoint guards
            raise EndpointShapeError(f"not a write: {endpoint.method!r}")
        self.endpoint = endpoint
        # Materialised once, in order, because the order *is* D5. A dict or a set
        # here would make "mobile first" unrepresentable.
        self.identities = tuple(identities)
        self._build_body = build_body
        self._sessionid = sessionid
        self._app_id = app_id
        self._timeout = timeout
        self._artifacts = artifacts
        #: One client per identity, not one client. The identity *is* the
        #: User-Agent and the app id, and httpx binds those to the client at
        #: construction -- so a single cached client would send the fallback
        #: attempt wearing the primary identity's headers. That failure is silent
        #: and total: the web identity would be rejected for being the mobile one,
        #: and the reason recorded would name the wrong rung.
        self._clients: dict[str, httpx.AsyncClient] = {}

    # -- lifecycle --------------------------------------------------------

    def _headers(self, identity: ApiIdentity) -> dict[str, str]:
        headers = {
            "User-Agent": identity.user_agent,
            "Accept": "*/*",
            "Cookie": f"sessionid={self._sessionid}",
        }
        app_id = identity.app_id or self._app_id
        if app_id:
            headers["X-IG-App-ID"] = app_id
        headers.update(identity.headers)
        return headers

    async def _get_client(
        self, identity: ApiIdentity, lease: ProxyLease | None
    ) -> httpx.AsyncClient:
        client = self._clients.get(identity.name)
        if client is None:
            # ``endpoint.url`` and not ``endpoint.origin``. The origin is
            # ``host:port`` with the credentials removed so that it is safe to
            # log, which makes it useless as a connect target: an authenticated
            # residential proxy needs its user and password, and it needs a
            # scheme. The credential-bearing form is correct here and must never
            # reach a log line or an artifact -- which is why
            # ``FailureContext.proxy_origin`` is given the *other* field below.
            client = httpx.AsyncClient(
                proxy=lease.endpoint.url if lease else None,
                timeout=self._timeout,
                follow_redirects=False,
                headers=self._headers(identity),
            )
            self._clients[identity.name] = client
        return client

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()

    # -- the report -------------------------------------------------------

    async def report(
        self,
        target: Target,
        *,
        on_dispatch: Callable[[], None],
        account_ref: str | None = None,
        lease: ProxyLease | None = None,
        attempt: int = 1,
    ) -> Outcome:
        """File one report and return what was observed.

        The identity fallback is internal, and the rule governing it is the whole
        design of this method: **the next identity is tried only when the previous
        one provably filed nothing.** A 4xx or a connect-stage failure qualifies.
        A 5xx, a read timeout, or any 2xx does not -- in those cases the report may
        exist, and a second identity would be a second report against the same
        target. So the ladder stops at the first ambiguity and records ``UNKNOWN``.

        That is the asymmetry D5's fallback would otherwise paper over, and getting
        it wrong is invisible: two reports filed, ledger says one, budget says one.

        The boundary is crossed **once**, before the first request, and never
        again. Not once per identity: the runner's ``on_dispatch`` raises
        ``RunAborted`` on a second call, and rightly so -- but the reason it is
        right here is the same reason the fallback is internal. The identity
        ladder is one attempt at one report, so it has one boundary. Calling it
        per rung would assert two independent dispatches for one target, which is
        the exact shape of the bug the guard exists to catch.

        The cost of that choice is accepted rather than solved. If the primary
        identity is refused with a 4xx and the process dies before the fallback's
        request goes out, the checkpoint says dispatched and a resumed run treats
        the target as ``UNKNOWN`` -- when in fact nothing was ever filed. One lost
        report in exchange for never writing a second dispatch, which is the right
        side of the trade for a tool whose whole premise is that it cannot tell a
        filed report from a lost one.
        """
        try:
            url = self.endpoint.url_for(target)
            body = dict(self._build_body(target))
        except EndpointShapeError as exc:
            # Pre-dispatch and unambiguous: we never addressed anyone.
            return self._outcome(
                target,
                terminal=TerminalState.CHANNEL_FAILED,
                account_ref=account_ref,
                lease=lease,
                attempt=attempt,
                detail=str(exc),
            )

        crossed = False
        response: ApiResponse | None = None
        terminal = TerminalState.CHANNEL_FAILED

        for identity in self.identities:
            if not crossed:
                # Placed here rather than inside ``_attempt`` so that the
                # once-per-report rule is visible at the loop, where the ladder
                # is, instead of being a property of a helper it calls.
                on_dispatch()
                crossed = True
            response = await self._attempt(identity, url, body, lease)
            terminal = classify(response, self.endpoint.ack_markers)

            # Only a provably-side-effect-free failure may advance the ladder. See
            # the method docstring; this condition is the whole safety argument.
            if terminal is not TerminalState.CHANNEL_FAILED:
                break

            if not self._worth_another_identity(response):
                break
            log.info(
                "api: identity %r was refused (%s); trying the next identity in "
                "the internal ladder. Nothing was filed, so this is safe.",
                identity.name,
                _describe(response),
            )

        if response is None:  # pragma: no cover - identities is non-empty
            raise EndpointShapeError("the identity ladder produced no attempt")
        filed = terminal in {
            TerminalState.SUBMITTED_ACKED,
            TerminalState.SUBMITTED_UNCONFIRMED,
            TerminalState.UNKNOWN,
        }
        detail = (
            f"identity={response.identity} {_describe(response)}; "
            f"ack_markers={'set' if self.endpoint.ack_markers else 'none'}"
        )
        if terminal is TerminalState.SUBMITTED_UNCONFIRMED and not self.endpoint.ack_markers:
            detail += (
                "; no ack shape has been verified for this endpoint, so a 2xx is "
                "not treated as confirmation -- see the module docstring on F1"
            )

        outcome = self._outcome(
            target,
            terminal=terminal,
            account_ref=account_ref,
            lease=lease,
            attempt=attempt,
            detail=detail,
            dispatched_at=utc_now() if filed else None,
        )
        if terminal is not TerminalState.SUBMITTED_ACKED or not self.endpoint.ack_markers:
            # A bundle on every non-trivial outcome. A response body is the only
            # evidence this channel will ever produce, so it is written on the way
            # out rather than on the way to a failure somebody thought to look at.
            outcome = await self._capture(
                outcome, target, account_ref, lease, attempt, response
            )
        return outcome

    async def _attempt(
        self,
        identity: ApiIdentity,
        url: str,
        body: Mapping[str, str],
        lease: ProxyLease | None,
    ) -> ApiResponse:
        """One request, under one identity.

        The dispatch boundary is *not* here. It is in :meth:`report`, immediately
        before the first of these runs, because the boundary belongs to the report
        and not to any single request inside it -- see that method's docstring for
        why the fallback does not get its own.

        Only ``httpx.RequestError`` is caught. A wider net would swallow the bugs
        in this module and report them to the operator as Instagram refusing a
        report, which is the misdiagnosis this project exists to eliminate.
        """
        client = await self._get_client(identity, lease)
        try:
            response = await client.post(url, data=body)
        except httpx.RequestError as exc:
            return ApiResponse(
                identity=identity.name, status=None, body="", error=type(exc).__name__
            )
        return ApiResponse(
            identity=identity.name,
            status=response.status_code,
            body=response.text,
        )

    @staticmethod
    def _worth_another_identity(response: ApiResponse) -> bool:
        """Whether a refusal is worth retrying under a different identity.

        A 4xx is, with one exception. 429 is a throttle: it belongs to the address
        and the session, not to the identity's user agent, so the web identity
        would earn the identical 429 while spending the fallback. Anything else --
        including the connect-stage failures, which are not 4xx at all -- is a
        legitimate reason to try the next rung.
        """
        if response.status == 429:
            return False
        return True

    # -- outcomes ---------------------------------------------------------

    def _outcome(
        self,
        target: Target,
        *,
        terminal: TerminalState,
        account_ref: str | None,
        lease: ProxyLease | None,
        attempt: int,
        detail: str,
        dispatched_at: Any = None,
    ) -> Outcome:
        return Outcome(
            terminal=terminal,
            target_ref=target.key,
            channel=CHANNEL,
            account_ref=account_ref,
            lease_id=lease.lease_id if lease else None,
            attempt=attempt,
            resolved_user_id=target.user_id,
            dispatched_at=dispatched_at,
            finished_at=utc_now(),
            detail=detail,
        )

    async def _capture(
        self,
        outcome: Outcome,
        target: Target,
        account_ref: str | None,
        lease: ProxyLease | None,
        attempt: int,
        response: ApiResponse,
    ) -> Outcome:
        """Write the response body out as evidence.

        Never raises. A missing bundle costs an operator the ability to read the
        response after the fact, but raising here would replace a recorded
        outcome with an exception, and the recorded outcome is the part that is
        still correct.
        """
        if self._artifacts is None:
            return outcome
        try:
            artifact = self._artifacts.capture(
                FailureContext(
                    target_key=target.key,
                    target_display=target.escaped(),
                    channel=CHANNEL,
                    terminal=outcome.terminal,
                    detail=outcome.detail,
                    account_display=account_ref or "",
                    proxy_origin=lease.endpoint.origin if lease else "",
                    attempt=attempt,
                    submit_status=response.status,
                    submit_body_excerpt=_excerpt(response.body),
                    submit_url=self.endpoint.url_template,
                ),
                # ``capture`` writes its argument to page.html, which is a
                # misleading name for a JSON body -- but the alternative is a
                # second artefact kind for one field, and the file's content is
                # described in failure.json next to it.
                html=_wrap_body(response),
            )
        except Exception:  # noqa: BLE001 - see docstring
            log.warning(
                "api: could not write an evidence bundle for %s; the outcome is "
                "still recorded. Losing the bundle is a real loss, so it is logged "
                "at warning rather than swallowed.",
                target.escaped(),
                exc_info=True,
            )
            return outcome
        return outcome.with_evidence(*(str(path) for path in artifact.files()))


def _wrap_body(response: ApiResponse) -> str:
    """Present a response as a small readable document.

    JSON when it parses, verbatim when it does not. Pretty-printing is not
    cosmetic here: an operator reading a bundle at 3am should not have to guess
    whether a truncated body failed to parse or arrived truncated.
    """
    if not response.body:
        return ""
    try:
        return json.dumps(json.loads(response.body), indent=2, ensure_ascii=False)
    except (ValueError, TypeError):
        return response.body


def _describe(response: ApiResponse) -> str:
    if response.error is not None:
        return f"transport error {response.error}"
    return f"HTTP {response.status} {_excerpt(response.body, 120)!r}"
