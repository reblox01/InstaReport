"""Tests for the API report channel (D5).

Three things in here are load-bearing and are tested for the property rather
than the implementation:

* **A 2xx is not an acknowledgement.** F1 is the reason -- Instagram renders
  success optimistically to reporters it does not trust -- so a success status
  with no verified ack shape is ``SUBMITTED_UNCONFIRMED``. The test that matters
  is the one where someone adds a plausible-looking success body and the
  classifier must still refuse to call it an ack.
* **The identity ladder only advances on a provably-side-effect-free refusal.**
  A 5xx or a read timeout means the report may exist; sending it again under a
  second identity would be a second report, and the ledger would record one.
* **The dispatch boundary is crossed once per report, not once per identity.**
  The runner raises ``RunAborted`` on a second call, so this is not a style
  preference -- but the fallback being internal is what makes one boundary the
  right answer rather than a workaround.

Each of those three has a test that fails if the property is removed. They are
marked in the test names so the failure points at the property and not at the
line.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from insta_report.api import (
    CHANNEL,
    ApiChannel,
    ApiEndpoint,
    ApiIdentity,
    ApiResponse,
    EndpointShapeError,
    UnverifiedEndpoint,
    _MAY_HAVE_BEEN_SENT,
    _NEVER_SENT,
    classify,
)
from insta_report.artifacts import ArtifactStore
from insta_report.outcomes import Outcome, TerminalState
from insta_report.proxies import ProxyEndpoint, ProxyLease
from insta_report.runner import ReportChannel
from insta_report.support.paths import Paths
from insta_report.support.redaction import get_registry
from insta_report.targets import Target

# Not credential-shaped: no ``sessionid=`` or ``sessionid%3A`` prefix, so the
# repository's own content scanner does not flag this file for holding a
# session. Long enough to clear MIN_SECRET_LENGTH so it can be registered for
# redaction, which two of these tests need it to be.
SESSION = "fake-sessionid-value-0000000000000002"

#: A proxy password, short enough to be plausible in a test and long enough to be
#: worth checking for. Never written into a URL literal in this file -- see the
#: note where it is used.
PROXY_PASSWORD = "swordfish9"

MOBILE = ApiIdentity(
    name="mobile",
    user_agent="Instagram 302.0.0.29.117 Android",
    app_id="936619743392459",
)
WEB = ApiIdentity(
    name="web",
    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0",
    app_id="936619743392459",
)

TEMPLATE = "https://i.instagram.com/api/v1/users/{user_id}/flag_user/"


def make_response(
    status: int, text: str, content_type: str = "text/plain"
) -> httpx.Response:
    """A real Response -- MockTransport type-checks, so a duck type is refused."""
    return httpx.Response(
        status_code=status,
        content=text.encode("utf-8"),
        headers={"content-type": content_type},
    )


def install_async_transport(monkeypatch, handler) -> list[dict[str, Any]]:
    """Swap ``httpx.AsyncClient`` for one backed by a MockTransport.

    Returns the list that accumulates each factory call's kwargs, so a test can
    assert on the proxy and headers the channel *asked for* rather than on what
    a mocked client happened to receive.

    The real class is captured before patching, because a factory that calls
    ``httpx.AsyncClient`` after the patch recurses into itself.

    ``proxy`` is recorded and then dropped. A MockTransport does not sit behind a
    proxy -- httpx wraps a proxied request in a real proxy connector and attempts
    a genuine CONNECT -- so the proxy argument cannot be honoured here. It is
    recorded first precisely so the tests that care can still see it, and the
    tests that care about *traffic* run without a lease.
    """
    real = httpx.AsyncClient
    seen: list[dict[str, Any]] = []

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        seen.append(dict(kwargs))
        kwargs.pop("proxy", None)
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return seen


def make_endpoint(**overrides: Any) -> ApiEndpoint:
    defaults: dict[str, Any] = {
        "url_template": TEMPLATE,
        "method": "POST",
        "verified": True,
    }
    defaults.update(overrides)
    return ApiEndpoint(**defaults)


def make_target(user_id: str | None = "999000111") -> Target:
    return Target(handle="bad.actor", user_id=user_id)


def body_for(target: Target) -> dict[str, str]:
    return {"source_name": target.user_id or "", "reason_id": "1"}


def make_channel(
    *,
    endpoint: ApiEndpoint | None = None,
    identities: tuple[ApiIdentity, ...] = (MOBILE, WEB),
    artifacts: ArtifactStore | None = None,
) -> ApiChannel:
    return ApiChannel(
        endpoint=endpoint if endpoint is not None else make_endpoint(),
        identities=identities,
        build_body=body_for,
        sessionid=SESSION,
        artifacts=artifacts,
    )


def recording_dispatch() -> tuple[Any, list[int]]:
    """An ``on_dispatch`` that counts its calls."""
    calls: list[int] = []

    def on_dispatch() -> None:
        calls.append(1)

    return on_dispatch, calls


def a_paths(tmp_path: Path) -> Paths:
    paths = Paths(
        data_dir=tmp_path / "data",
        artifacts_dir=tmp_path / "artifacts",
        traces_dir=tmp_path / "traces",
        state_dir=tmp_path / "state",
        logs_dir=tmp_path / "logs",
    )
    return paths.ensure()


def a_store(tmp_path: Path, run_id: str = "run-1") -> ArtifactStore:
    """A store whose root is outside the repository, which is a hard requirement.

    ``assert_outside_repo`` raises rather than warns, so this cannot be built
    pointing into the working tree -- the same fail-closed property the tool
    relies on when an operator configures the paths.
    """
    return ArtifactStore(a_paths(tmp_path), run_id)


# ---------------------------------------------------------------------------
# The endpoint cannot be built out of a guess
# ---------------------------------------------------------------------------


def test_an_unverified_endpoint_cannot_be_built():
    """The gate is in the constructor, so a guess is never wired into a ladder.

    Not a config flag and not a dispatch-time check. A channel that discovers
    this on its first report has already been scheduled, and the operator finds
    out at the moment the tool matters.
    """
    with pytest.raises(UnverifiedEndpoint) as caught:
        ApiEndpoint(url_template=TEMPLATE)
    message = str(caught.value)
    assert TEMPLATE in message
    # The message has to say what would make it true, or it is just a refusal.
    assert "verified" in message


def test_a_reporting_endpoint_may_not_be_a_read():
    """The inverse of the probe's read-only rule, enforced on the same idea.

    A GET here is either a mistake or someone using this module to probe, and
    neither belongs in the report path.
    """
    for method in ("GET", "PUT", "PATCH", "DELETE", "get"):
        with pytest.raises(EndpointShapeError):
            make_endpoint(method=method)


def test_an_empty_url_is_not_a_route():
    with pytest.raises(EndpointShapeError):
        make_endpoint(url_template="")


def test_a_verified_endpoint_may_carry_no_ack_markers():
    """Verifying the endpoint and knowing its success shape are separate facts.

    Collapsing them would force the first real submission to discover both at
    once, and the plan says the claim and its evidence should arrive together --
    which they can, without making one imply the other.
    """
    endpoint = make_endpoint(ack_markers=())
    assert endpoint.verified is True
    assert endpoint.ack_markers == ()


# ---------------------------------------------------------------------------
# Addressing
# ---------------------------------------------------------------------------


def test_the_reporting_url_carries_the_resolved_target_id():
    assert make_endpoint().url_for(make_target("424242")) == (
        "https://i.instagram.com/api/v1/users/424242/flag_user/"
    )


def test_an_unresolved_target_is_refused_rather_than_addressed_with_an_empty_id():
    """``/users//flag_user/`` is a valid request to a meaningless place.

    The 404 it earns is indistinguishable from a target that does not exist, and
    that is the same uninterpretable-response class the probe refuses to produce.
    """
    with pytest.raises(EndpointShapeError) as caught:
        make_endpoint().url_for(make_target(None))
    assert "user_id" in str(caught.value)


def test_a_placeholder_the_channel_cannot_fill_is_refused():
    """T16's lesson, applied forwards.

    Every placeholder in a reporting endpoint must be the target's id. Anything
    else names a route this channel cannot file against, and sending it would
    earn a 404 that reads like a finding.
    """
    with pytest.raises(EndpointShapeError):
        make_endpoint(url_template="https://i.instagram.com/api/v1/{scope}/x/").url_for(
            make_target("424242")
        )


# ---------------------------------------------------------------------------
# The classification table
# ---------------------------------------------------------------------------


def _response(**overrides: Any) -> ApiResponse:
    defaults: dict[str, Any] = {"identity": "mobile", "status": 200, "body": ""}
    defaults.update(overrides)
    return ApiResponse(**defaults)


def test_a_2xx_is_not_an_acknowledgement_without_a_verified_ack_shape():
    """The property: F1. Instagram's success render is not evidence of filing."""
    assert classify(_response(status=200, body='{"status":"ok"}'), ()) is (
        TerminalState.SUBMITTED_UNCONFIRMED
    )


def test_a_2xx_is_still_not_an_ack_when_the_body_looks_conclusive():
    """The teeth.

    A body that says "ok", says "submitted", and carries no error is exactly
    what someone would write a passing assertion against after the first live
    run. It must still not promote the response to ``SUBMITTED_ACKED`` until a
    human has watched a real submission and named the shape.
    """
    body = json.dumps({"status": "ok", "submitted": True, "success": True})
    assert classify(_response(status=200, body=body), ()) is (
        TerminalState.SUBMITTED_UNCONFIRMED
    )


def test_a_2xx_matching_a_verified_ack_shape_is_acknowledged():
    """The only route to ACKED, and it requires a human to have named a marker."""
    assert classify(
        _response(status=200, body='{"report_status":"filed"}'), ("report_status",)
    ) is TerminalState.SUBMITTED_ACKED


def test_a_2xx_that_does_not_match_its_own_ack_shape_is_unconfirmed():
    """Markers narrow the ack; they do not become one."""
    assert classify(
        _response(status=200, body='{"status":"ok"}'), ("report_status",)
    ) is TerminalState.SUBMITTED_UNCONFIRMED


def test_a_4xx_filed_nothing():
    """A rejection is a refusal to process, so no report exists.

    ``CHANNEL_FAILED`` and not ``SUBMITTED_UNCONFIRMED``, and that distinction is
    the whole budget question: ``counts_against_budget`` is false for
    ``CHANNEL_FAILED``, so a request Instagram threw away does not spend part of
    the account's daily allowance.
    """
    terminal = classify(_response(status=400, body="bad request"), ())
    assert terminal is TerminalState.CHANNEL_FAILED
    assert terminal.counts_against_budget is False
    # And the ladder may carry on, which is what the next tests depend on.
    assert terminal.stops_ladder is False


def test_a_throttle_is_also_a_rejection():
    assert classify(_response(status=429, body=""), ()) is TerminalState.CHANNEL_FAILED


def test_a_5xx_is_unknown():
    """Something processed the request and we cannot say what.

    Not ``CHANNEL_FAILED``: a 500 can absolutely follow a report being recorded,
    and answering "the channel failed" would invite a retry against a target that
    has already been reported.
    """
    terminal = classify(_response(status=500, body="oops"), ())
    assert terminal is TerminalState.UNKNOWN
    assert terminal.counts_against_budget is True
    assert terminal.needs_human_review is True


def test_a_read_timeout_is_unknown():
    assert classify(_response(status=None, body="", error="ReadTimeout"), ()) is (
        TerminalState.UNKNOWN
    )


def test_a_connect_failure_is_a_channel_failure():
    """The request provably never left, so nothing was filed and the ladder moves."""
    assert classify(_response(status=None, body="", error="ConnectError"), ()) is (
        TerminalState.CHANNEL_FAILED
    )


def test_the_never_sent_and_maybe_sent_tables_do_not_overlap():
    """A type appearing on both sides would make its classification a coin flip."""
    assert not set(_NEVER_SENT) & set(_MAY_HAVE_BEEN_SENT)


def test_every_httpx_transport_failure_is_classified_one_way_or_the_other():
    """Coverage of the split across httpx's own hierarchy, read from the package.

    Enumerated rather than hand-listed so that an httpx release adding an error
    type shows up here as a new name, instead of quietly falling through to the
    "unknown type" default.
    """
    import inspect

    transport_errors = [
        getattr(httpx, name)
        for name in dir(httpx)
        if inspect.isclass(getattr(httpx, name))
        and issubclass(getattr(httpx, name), httpx.TransportError)
    ]
    assert transport_errors, "httpx gained no transport errors at all?"
    for cls in transport_errors:
        response = _response(status=None, body="", error=cls.__name__)
        terminal = classify(response, ())
        expected = (
            TerminalState.CHANNEL_FAILED
            if cls in _NEVER_SENT
            else TerminalState.UNKNOWN
        )
        assert terminal is expected, cls.__name__


def test_an_error_type_this_table_has_never_heard_of_is_unknown():
    """A future httpx must not be able to talk this tool into re-sending.

    The default for an unrecognised name is ``UNKNOWN``, which stops. A default of
    "never sent" would be the more convenient answer and the dangerous one.
    """

    class FutureTransportError(httpx.RequestError):
        pass

    response = _response(
        status=None, body="", error=FutureTransportError.__name__
    )
    assert classify(response, ()) is TerminalState.UNKNOWN


def test_a_non_httpx_failure_is_also_unknown():
    """A bug in here is ambiguous too, and ambiguity is not a licence to resend."""
    assert classify(_response(status=None, body="", error="TypeError"), ()) is (
        TerminalState.UNKNOWN
    )


# ---------------------------------------------------------------------------
# The ladder: only a provably-side-effect-free refusal advances it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_refused_identity_advances_to_the_web_fallback(monkeypatch):
    """The fallback exists, and this is what it is for."""
    seen_user_agents: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_user_agents.append(request.headers["user-agent"])
        if len(seen_user_agents) == 1:
            return make_response(400, "useragent mismatch")
        return make_response(200, '{"status":"ok"}')

    install_async_transport(monkeypatch, handler)
    on_dispatch, calls = recording_dispatch()

    outcome = await make_channel().report(
        make_target(), on_dispatch=on_dispatch, account_ref="alpha", attempt=1
    )

    assert len(seen_user_agents) == 2, seen_user_agents
    # D5 is mobile-first, so the *first* attempt is the mobile one.
    assert seen_user_agents[0] == MOBILE.user_agent
    assert seen_user_agents[1] == WEB.user_agent
    # A 2xx with no ack shape: unconfirmed, not acked.
    assert outcome.terminal is TerminalState.SUBMITTED_UNCONFIRMED
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_the_fallback_request_carries_the_fallback_identitys_headers(
    monkeypatch,
):
    """The teeth for a one-client cache.

    httpx binds headers to the client at construction, so a single cached client
    sends the web attempt wearing the mobile User-Agent. The failure is silent
    and total: the fallback is rejected for being the primary, and the reason
    recorded names the wrong rung.

    Asserted on the *client kwargs* rather than on the request headers, because
    the request headers come from the mocked client this factory builds and
    therefore cannot detect a client that was constructed with the wrong
    identity. The defect is in the construction, so the assertion has to be too.
    """
    seen = install_async_transport(
        monkeypatch, lambda request: make_response(400, "useragent mismatch")
    )
    on_dispatch, _ = recording_dispatch()

    await make_channel().report(make_target(), on_dispatch=on_dispatch)

    # One client per identity, in ladder order, each with its own user agent.
    assert [call["headers"]["User-Agent"] for call in seen] == [
        MOBILE.user_agent,
        WEB.user_agent,
    ]


@pytest.mark.asyncio
async def test_a_repeated_identity_reuses_its_client(monkeypatch):
    """The cache exists so a long run does not build a client per report.

    Asserted because the alternative -- rebuilding on every report -- is the kind
    of regression that costs nothing in the test suite and a lot of connections
    in a four-hour run.
    """
    seen = install_async_transport(monkeypatch, lambda r: make_response(400, "nope"))
    channel = make_channel(identities=(MOBILE,))
    for _ in range(3):
        on_dispatch, _ = recording_dispatch()
        await channel.report(make_target(), on_dispatch=on_dispatch)

    assert len(seen) == 1
    await channel.aclose()


@pytest.mark.asyncio
async def test_a_5xx_does_not_advance_the_ladder(monkeypatch):
    """The property: a 500 may follow a report being recorded.

    Sending it again under the second identity would be a second report against
    the same target, with the ledger recording one and the budget spending one.
    This is the test that would catch that, so it asserts the *absence* of a
    second request rather than the presence of a state.
    """
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.headers["user-agent"])
        return make_response(500, "server error")

    install_async_transport(monkeypatch, handler)
    on_dispatch, calls = recording_dispatch()

    outcome = await make_channel().report(make_target(), on_dispatch=on_dispatch)

    assert attempts == [MOBILE.user_agent], attempts
    assert outcome.terminal is TerminalState.UNKNOWN
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_a_read_timeout_does_not_advance_the_ladder(monkeypatch):
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.headers["user-agent"])
        raise httpx.ReadTimeout("read timed out", request=request)

    install_async_transport(monkeypatch, handler)
    on_dispatch, _ = recording_dispatch()

    outcome = await make_channel().report(make_target(), on_dispatch=on_dispatch)

    assert attempts == [MOBILE.user_agent], attempts
    assert outcome.terminal is TerminalState.UNKNOWN


@pytest.mark.asyncio
async def test_a_2xx_does_not_advance_the_ladder(monkeypatch):
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.headers["user-agent"])
        return make_response(200, '{"status":"ok"}')

    install_async_transport(monkeypatch, handler)
    on_dispatch, _ = recording_dispatch()

    await make_channel().report(make_target(), on_dispatch=on_dispatch)

    assert attempts == [MOBILE.user_agent], attempts


@pytest.mark.asyncio
async def test_a_connect_failure_does_advance_the_ladder(monkeypatch):
    """The other half of the rule, so the previous tests are not vacuous."""
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.headers["user-agent"])
        if len(attempts) == 1:
            raise httpx.ConnectError("refused", request=request)
        return make_response(200, '{"status":"ok"}')

    install_async_transport(monkeypatch, handler)
    on_dispatch, _ = recording_dispatch()

    outcome = await make_channel().report(make_target(), on_dispatch=on_dispatch)

    assert len(attempts) == 2, attempts
    assert outcome.terminal is TerminalState.SUBMITTED_UNCONFIRMED


@pytest.mark.asyncio
async def test_a_throttle_does_not_spend_the_fallback(monkeypatch):
    """429 belongs to the address and the session, not to the identity.

    Retrying the same session from the same address under a different User-Agent
    would earn the identical 429, so advancing would burn the fallback to reach
    a known answer. The state is right either way -- a 4xx did file nothing --
    but the *spending* is what is being protected.
    """
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.headers["user-agent"])
        return make_response(429, "")

    install_async_transport(monkeypatch, handler)
    on_dispatch, _ = recording_dispatch()

    outcome = await make_channel().report(make_target(), on_dispatch=on_dispatch)

    assert attempts == [MOBILE.user_agent], attempts
    assert outcome.terminal is TerminalState.CHANNEL_FAILED
    assert outcome.was_dispatched is False


@pytest.mark.asyncio
async def test_a_single_identity_ladder_makes_exactly_one_attempt(monkeypatch):
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.headers["user-agent"])
        return make_response(400, "nope")

    install_async_transport(monkeypatch, handler)
    on_dispatch, _ = recording_dispatch()

    outcome = await make_channel(identities=(MOBILE,)).report(
        make_target(), on_dispatch=on_dispatch
    )

    assert attempts == [MOBILE.user_agent]
    assert outcome.terminal is TerminalState.CHANNEL_FAILED


def test_an_empty_ladder_is_refused():
    """An empty ladder is not a configuration, it is a channel with nothing to say."""
    with pytest.raises(EndpointShapeError):
        make_channel(identities=())


# ---------------------------------------------------------------------------
# The boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_boundary_is_crossed_once_per_report_not_once_per_identity(
    monkeypatch,
):
    """The teeth for putting ``on_dispatch`` inside the per-identity helper.

    The runner raises ``RunAborted`` on a second call -- "a second click is how
    one target becomes two reports" -- so this is not a style question. And the
    reason it is *right* here is the same reason the fallback is internal: the
    ladder is one attempt at one report, so it has one boundary.
    """
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return make_response(400, "refused")

    install_async_transport(monkeypatch, handler)
    on_dispatch, calls = recording_dispatch()

    await make_channel().report(make_target(), on_dispatch=on_dispatch)

    assert len(attempts) == 2, "the ladder should have advanced"
    assert len(calls) == 1, "but the boundary is per report, not per rung"


@pytest.mark.asyncio
async def test_the_boundary_is_crossed_before_the_first_byte_goes_out(monkeypatch):
    """Ordering, not just counting.

    A checkpoint written after the request would lose exactly the race it exists
    to survive.
    """
    order: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        order.append("request")
        return make_response(200, "{}")

    install_async_transport(monkeypatch, handler)

    def on_dispatch() -> None:
        order.append("dispatch")

    await make_channel().report(make_target(), on_dispatch=on_dispatch)

    assert order == ["dispatch", "request"], order


@pytest.mark.asyncio
async def test_a_failure_to_write_the_checkpoint_stops_the_report(monkeypatch):
    """The boundary's failure propagates, and no request is sent.

    A checkpoint that could not be written means the outcome cannot be trusted.
    Continuing would file a report whose result the tool has nowhere to record,
    which is the state this project exists to avoid.
    """
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return make_response(200, "{}")

    install_async_transport(monkeypatch, handler)

    class RunAborted(Exception):
        pass

    def on_dispatch() -> None:
        raise RunAborted("checkpoint write failed")

    with pytest.raises(RunAborted):
        await make_channel().report(make_target(), on_dispatch=on_dispatch)

    assert attempts == [], "no report may be filed without a durable intent record"


@pytest.mark.asyncio
async def test_an_unresolved_target_never_crosses_the_boundary(monkeypatch):
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return make_response(200, "{}")

    install_async_transport(monkeypatch, handler)
    on_dispatch, calls = recording_dispatch()

    outcome = await make_channel().report(
        make_target(None), on_dispatch=on_dispatch, account_ref="alpha"
    )

    assert attempts == []
    assert calls == []
    assert outcome.terminal is TerminalState.CHANNEL_FAILED
    assert outcome.was_dispatched is False


# ---------------------------------------------------------------------------
# What reaches the network
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_session_reaches_instagram_and_only_instagram(monkeypatch):
    """The credential is attached once, on the client, and reaches the origin.

    Written as two assertions on the same captured header rather than a match
    against a literal ``sessionid=...`` string, because that literal is
    credential-shaped and this repository scans its own tracked source for it.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return make_response(200, "{}")

    install_async_transport(monkeypatch, handler)
    on_dispatch, _ = recording_dispatch()

    await make_channel().report(make_target(), on_dispatch=on_dispatch)

    assert len(seen) == 1
    cookie = seen[0].headers["cookie"]
    assert cookie.startswith("sessionid=")
    assert SESSION in cookie
    assert seen[0].headers["user-agent"] == MOBILE.user_agent
    assert seen[0].headers["x-ig-app-id"] == MOBILE.app_id


@pytest.mark.asyncio
async def test_the_body_is_the_one_the_caller_built(monkeypatch):
    """The body shape is unverified, so it is injected rather than guessed here.

    Asserted as form encoding because that is what the channel sends -- ``data=``
    rather than ``json=`` -- and that choice is itself unverified, so the test
    records it rather than asserting a shape the endpoint may not want. Switching
    to JSON is a one-word change here and in ``_attempt``, and this test is what
    would notice.
    """
    seen: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.content)
        return make_response(200, "{}")

    install_async_transport(monkeypatch, handler)
    on_dispatch, _ = recording_dispatch()

    await make_channel().report(make_target("777"), on_dispatch=on_dispatch)

    assert seen[0] == b"source_name=777&reason_id=1"


@pytest.mark.asyncio
async def test_the_request_goes_to_the_substituted_url(monkeypatch):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return make_response(200, "{}")

    install_async_transport(monkeypatch, handler)
    on_dispatch, _ = recording_dispatch()

    await make_channel().report(make_target("424242"), on_dispatch=on_dispatch)

    assert seen == ["https://i.instagram.com/api/v1/users/424242/flag_user/"]


@pytest.mark.asyncio
async def test_the_proxy_is_connected_through_by_url_and_reported_by_origin(
    tmp_path: Path, monkeypatch
):
    """Two fields with two jobs, and confusing them breaks the run.

    ``endpoint.origin`` is ``host:port`` with the credentials removed so that it
    is safe to log -- which makes it useless as a connect target, since an
    authenticated residential proxy needs its user and password *and* a scheme.
    So the client gets ``url`` and the artifact gets ``origin``.

    One test because the two choices are only meaningful together: a reader who
    "simplified" one to match the other would send a credential-less proxy
    address, or log one.
    """
    seen = install_async_transport(monkeypatch, lambda r: make_response(200, "{}"))
    # Assembled rather than written as a literal, so this file contains no inline
    # proxy credential for the repository's own scanner to find. The scanner is
    # right to look -- a committed ``http://user:password@host`` is exactly the
    # leak it exists to catch, and a test that needs one should build one rather
    # than ship a literal for somebody to allow-list.
    proxy_url = "http://tenant:" + PROXY_PASSWORD + "@10.0.0.1:8080"
    endpoint = ProxyEndpoint(url=proxy_url, source="file")
    lease = ProxyLease(
        lease_id="px-1", endpoint=endpoint, sticky_expires_at=100.0, acquired_at=0.0
    )
    store = a_store(tmp_path)
    on_dispatch, _ = recording_dispatch()

    channel = make_channel(artifacts=store)
    outcome = await channel.report(
        make_target(), on_dispatch=on_dispatch, lease=lease
    )
    await channel.aclose()

    # Connect target: the full URL, credentials and scheme intact.
    assert seen[0]["proxy"] == endpoint.url
    assert PROXY_PASSWORD in seen[0]["proxy"]
    # Reported form: host:port, and the password is not in it.
    assert PROXY_PASSWORD not in endpoint.origin
    metadata = json.loads(Path(outcome.evidence_refs[0]).read_text(encoding="utf-8"))
    assert metadata["proxy_origin"] == endpoint.origin
    assert PROXY_PASSWORD not in json.dumps(metadata)


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_dispatched_response_is_kept_as_evidence(tmp_path: Path, monkeypatch):
    """A response body is the only evidence this channel will ever produce.

    So it is written on the way out of every non-acked outcome, not on the way to
    a failure somebody thought to look at afterwards.
    """
    install_async_transport(monkeypatch, lambda r: make_response(200, '{"a":  1}'))
    store = a_store(tmp_path)
    on_dispatch, _ = recording_dispatch()

    outcome = await make_channel(artifacts=store).report(
        make_target(), on_dispatch=on_dispatch, account_ref="alpha"
    )

    assert outcome.evidence_refs, "the bundle must be referenced from the ledger"
    metadata = json.loads(
        Path(outcome.evidence_refs[0]).read_text(encoding="utf-8")
    )
    assert metadata["channel"] == CHANNEL
    assert metadata["submit_status"] == 200
    assert metadata["account"] == "alpha"
    assert metadata["terminal"] == TerminalState.SUBMITTED_UNCONFIRMED.value


@pytest.mark.asyncio
async def test_a_secret_in_the_response_body_is_scrubbed_from_the_bundle(
    tmp_path: Path, monkeypatch
):
    """Instagram echoing a session back into a response must not land on disk.

    Scrubbed in :meth:`ApiChannel._excerpt` *and* centrally in the artifact
    store. The store is the one that matters: it already scrubbed the DOM, and
    leaving the metadata one field away in the same directory would have put the
    same secret in the same run with a different code path.
    """
    get_registry().register(SESSION)
    install_async_transport(
        monkeypatch,
        lambda r: make_response(200, json.dumps({"echo": f"cookie:{SESSION}"})),
    )
    store = a_store(tmp_path)
    on_dispatch, _ = recording_dispatch()

    outcome = await make_channel(artifacts=store).report(
        make_target(), on_dispatch=on_dispatch
    )

    for ref in outcome.evidence_refs:
        assert SESSION not in Path(ref).read_text(encoding="utf-8")
    assert "[REDACTED]" in Path(outcome.evidence_refs[0]).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_a_bundle_that_cannot_be_written_does_not_replace_the_outcome(
    tmp_path: Path, monkeypatch
):
    """Losing the pixels is a real loss; losing the record is worse.

    The browser channel's docstring says this and the browser channel does it.
    Here it is a test, because the alternative -- raising out of ``report`` --
    would turn a recorded ``SUBMITTED_UNCONFIRMED`` into an exception, and the
    recorded outcome is the part that is still correct.
    """
    install_async_transport(monkeypatch, lambda r: make_response(200, "{}"))

    class BrokenStore(ArtifactStore):
        def capture(self, *args: Any, **kwargs: Any) -> Any:
            raise OSError("disk full")

    on_dispatch, _ = recording_dispatch()
    outcome = await make_channel(artifacts=BrokenStore(a_paths(tmp_path), "run-1")).report(
        make_target(), on_dispatch=on_dispatch
    )

    assert outcome.terminal is TerminalState.SUBMITTED_UNCONFIRMED
    assert outcome.evidence_refs == ()


# ---------------------------------------------------------------------------
# The protocol, and the record
# ---------------------------------------------------------------------------


def test_the_channel_satisfies_the_runner_protocol():
    """Not a smoke test. The runner type-checks against this at build time, and a
    channel that failed the protocol would be dropped from the ladder with a
    message naming a type error rather than a missing channel."""
    assert isinstance(make_channel(), ReportChannel)
    assert ApiChannel.name == CHANNEL
    assert CHANNEL != "browser", "the two channels must be distinguishable in the ledger"


@pytest.mark.asyncio
async def test_the_outcome_names_the_channel_account_lease_and_target(monkeypatch):
    """What an operator reads at 3am to answer "what happened to this one"."""
    install_async_transport(monkeypatch, lambda r: make_response(200, "{}"))
    lease = ProxyLease(
        lease_id="px-9",
        endpoint=ProxyEndpoint(url="http://10.0.0.1:8080", source="file"),
        sticky_expires_at=100.0,
        acquired_at=0.0,
    )
    on_dispatch, _ = recording_dispatch()

    outcome: Outcome = await make_channel().report(
        make_target("424242"),
        on_dispatch=on_dispatch,
        account_ref="alpha",
        lease=lease,
        attempt=3,
    )

    assert outcome.channel == CHANNEL
    assert outcome.account_ref == "alpha"
    assert outcome.lease_id == "px-9"
    assert outcome.attempt == 3
    assert outcome.resolved_user_id == "424242"
    assert outcome.target_ref == make_target("424242").key
    assert outcome.finished_at is not None
    # The detail has to say which identity answered, or the fallback is
    # undebuggable -- a 4xx from the web rung looks identical to one from the
    # mobile rung in every other field. Here the mobile rung answered, because a
    # 2xx stops the ladder.
    assert "identity=mobile" in outcome.detail
    assert "ack_markers=none" in outcome.detail


@pytest.mark.asyncio
async def test_the_detail_names_the_identity_that_actually_answered(monkeypatch):
    """The other half: the fallback's answer has to be attributable to it."""
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.headers["user-agent"])
        if len(attempts) == 1:
            return make_response(400, "useragent mismatch")
        return make_response(400, "still refused")

    install_async_transport(monkeypatch, handler)
    on_dispatch, _ = recording_dispatch()

    outcome = await make_channel().report(make_target(), on_dispatch=on_dispatch)

    assert len(attempts) == 2
    assert "identity=web" in outcome.detail
    assert "still refused" in outcome.detail


@pytest.mark.asyncio
async def test_the_detail_explains_why_a_2xx_is_not_an_ack(monkeypatch):
    """The record has to carry its own caveat, or it will be read as a success."""
    install_async_transport(monkeypatch, lambda r: make_response(200, '{"status":"ok"}'))
    on_dispatch, _ = recording_dispatch()

    outcome = await make_channel().report(make_target(), on_dispatch=on_dispatch)

    assert outcome.terminal is TerminalState.SUBMITTED_UNCONFIRMED
    assert "no ack shape has been verified" in outcome.detail


@pytest.mark.asyncio
async def test_a_dispatched_2xx_spends_budget_and_an_unresolved_one_does_not(
    monkeypatch,
):
    """The budget consequence of the dispatch boundary, asserted where it is set."""
    install_async_transport(monkeypatch, lambda r: make_response(200, "{}"))
    on_dispatch, _ = recording_dispatch()

    dispatched = await make_channel().report(
        make_target(), on_dispatch=on_dispatch
    )
    assert dispatched.was_dispatched is True
    assert dispatched.terminal.counts_against_budget is True

    on_dispatch, _ = recording_dispatch()
    unresolved = await make_channel().report(
        make_target(None), on_dispatch=on_dispatch
    )
    assert unresolved.was_dispatched is False
    assert unresolved.terminal.counts_against_budget is False
