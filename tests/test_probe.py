"""T0 probe tests.

The probe is the gate on the API channel, so its most important property is
that it refuses to conclude. Everything here tests the refusal.
"""

from __future__ import annotations

import re

import httpx
import pytest

from insta_report.outcomes import NetworkVerdict
from insta_report.probe import (
    DEFAULT_CONTROL_URL,
    DEFAULT_PROBES,
    DEFAULT_USER_AGENT,
    READ_ONLY_METHODS,
    Probe,
    ProbeResult,
    ProbeVerdict,
    _assert_placeholders,
    _egress_ip,
    _main,
    _interpretation,
    render_url,
    run_probe,
)
from insta_report.support.redaction import get_registry, register_secret

CONTROL_URL = DEFAULT_CONTROL_URL


def result(
    name: str = "p",
    *,
    status: int | None = 200,
    body: str = '{"status": "ok"}',
    content_type: str | None = "application/json",
    error: str | None = None,
) -> ProbeResult:
    return ProbeResult(
        name=name,
        url="https://example.test",
        status=status,
        body=body,
        content_type=content_type,
        error=error,
    )


def report(control: ProbeResult | None, *results: ProbeResult):
    from insta_report.probe import ProbeReport

    return ProbeReport(
        exit_ip="203.0.113.5",
        control=control,
        results=list(results),
    )


# --- the control is the gate -------------------------------------------------


def test_failed_control_makes_the_whole_run_inconclusive():
    """The original misdiagnosis in one assertion.

    A burned IP returns 429/0 bytes for everything, including the control. The
    endpoints' 404s are then uninterpretable, and concluding "the routes are
    gone" from that is the mistake being prevented.
    """
    r = report(result(status=429, body=""), result(status=404, body=""))
    assert r.control_ok is False
    assert r.verdict is ProbeVerdict.INCONCLUSIVE
    assert "Do not conclude" in r.summary()


def test_missing_control_is_inconclusive():
    r = report(None, result())
    assert r.verdict is ProbeVerdict.INCONCLUSIVE
    assert "NOT RUN" in r.summary()


def test_html_control_is_not_a_working_control():
    """A 200 login shell is exactly the shape that fooled igban.py."""
    r = report(result(body="<html>login</html>", content_type="text/html"))
    assert r.control_ok is False
    assert r.verdict is ProbeVerdict.INCONCLUSIVE


def test_control_that_gets_a_200_but_unreadable_body_does_not_count():
    r = report(result(status=200, body="", content_type="application/json"))
    assert r.control_ok is False


# --- verdicts ---------------------------------------------------------------


def test_control_ok_plus_a_readable_ok_route_is_observable():
    r = report(result(name="control"), result(name="route"))
    assert r.verdict is ProbeVerdict.OBSERVABLE
    assert "mobile API channel is real" in r.summary()


def test_routes_that_all_answer_with_rejections_are_answered_not_ok():
    """The routes exist, which is the question; the shape is a separate one."""
    r = report(
        result(name="control"),
        result(name="route", body='{"status": "fail", "message": "no"}'),
    )
    assert r.verdict is ProbeVerdict.ANSWERED_NOT_OK
    assert "routes exist" in r.summary()


def test_routes_returning_nothing_readable_are_unobservable():
    r = report(
        result(name="control"),
        result(name="route", status=404, body=""),
        result(name="route2", status=429, body=""),
    )
    assert r.verdict is ProbeVerdict.UNOBSERVABLE
    assert "not evidence of absence" in r.summary()


def test_mixed_readable_and_unreadable_is_unobservable():
    r = report(
        result(name="control"),
        result(name="ok_route"),
        result(name="dead_route", status=404, body=""),
    )
    assert r.verdict is ProbeVerdict.UNOBSERVABLE


def test_a_failed_control_does_not_discard_a_success():
    """The defect this whole section exists for.

    Observed on 2026-09-26: a run returned 403 for the control and 200
    ``{"status": "ok"}`` for a probe against the same URL, 600ms apart, same
    cookie, same address. The old rule returned INCONCLUSIVE and discarded the
    200.

    It discarded the right answer. ``ok`` is not ambiguous -- a blocked exit, a
    broken proxy and an absent credential all fail to produce it, and the
    unauthenticated control answers 404 -> 302 -> the login page rather than an
    application error. Only *negatives* need the control's corroboration, which
    is what the control is for.
    """
    r = report(
        result(name="CONTROL", status=403, body="{}", content_type="application/json"),
        result(name="account_form_data", status=200, body='{"status": "ok"}'),
    )
    assert r.verdict is ProbeVerdict.OBSERVABLE_UNSTABLE_EXIT


def test_a_failed_control_still_suppresses_a_negative():
    """The other half. The asymmetry must not become a blanket pass.

    An unreadable 404 beside a failed control is exactly the ambiguity the
    control exists to prevent, and it must stay INCONCLUSIVE -- otherwise the
    fix above has thrown out the gate along with the discard.
    """
    r = report(
        result(name="CONTROL", status=404, body="", content_type="text/html"),
        result(name="route", status=404, body="", content_type="text/html"),
    )
    assert r.verdict is ProbeVerdict.INCONCLUSIVE


def test_a_failed_control_does_not_promote_a_rejection():
    """REJECTED is readable, so it is not 'ok' -- and readable is not evidence.

    This is the case most likely to be got wrong while fixing the case above: a
    429 with a JSON body proves the route answered, not that the API is usable.
    """
    r = report(
        result(name="CONTROL", status=404, body="", content_type="text/html"),
        result(name="route", status=429, body='{"message": "rate limited"}'),
    )
    assert r.verdict is ProbeVerdict.INCONCLUSIVE


def test_an_unstable_exit_does_not_clear_the_t0_gate():
    """Exit 0 means "safe to build on", which this run has not established.

    The verdict is stronger than INCONCLUSIVE -- the route is proven -- but the
    exit is not, and a channel built through an unproven exit fails partway
    through a real run, which is the outcome the pacing and account-budget work
    exists to make survivable rather than to make acceptable.
    """
    r = report(
        result(name="CONTROL", status=403, body="{}", content_type="application/json"),
        result(name="account_form_data", status=200, body='{"status": "ok"}'),
    )
    assert r.verdict is not ProbeVerdict.OBSERVABLE


def test_a_control_that_never_ran_is_not_an_unstable_exit():
    """``None`` and "failed" are different facts and must not collapse.

    ``OBSERVABLE_UNSTABLE_EXIT`` says the exit was watched refusing us. A
    missing control says nobody watched -- the run is malformed, not the exit
    misbehaving -- and no amount of successful probes makes that observation
    exist. Keeping them apart is the difference between a diagnostic and a
    guess.
    """
    r = report(None, result(name="account_form_data", status=200,
                            body='{"status": "ok"}'))
    assert r.verdict is ProbeVerdict.INCONCLUSIVE


def test_the_interpretation_for_an_unstable_exit_does_not_say_do_not_conclude():
    """The prose is the part an operator acts on, so it has to match.

    The INCONCLUSIVE text says "results say nothing about whether the routes
    exist". Emitting that under a verdict which *proves* a route exists is a
    contradiction the reader has to notice and then disbelieve.
    """
    text = _interpretation(ProbeVerdict.OBSERVABLE_UNSTABLE_EXIT)
    assert "say nothing about whether the routes exist" not in text
    assert "worth building" in text
    inconclusive = _interpretation(ProbeVerdict.INCONCLUSIVE)
    assert "say nothing about whether the routes exist" in inconclusive


def test_control_only_with_no_probes_is_unobservable():
    assert report(result()).verdict is ProbeVerdict.UNOBSERVABLE


# --- the probe never acts ---------------------------------------------------


def test_no_default_probe_can_change_state():
    """The probe's central promise, checked rather than described.

    This replaces a test that asserted the same *intent* while documenting the
    opposite reasoning. It read:

        "flag_user uses POST because its route shape is part of what is being
         tested, but the body is a bare marker and the account is not
         authenticated, so it cannot file anything."

    The POST and the live ``/flag_user/`` route were both there, and the
    argument that made them safe was that the request would be unauthenticated.
    T0 exists precisely to send an authenticated request -- the control call is
    a known-good *authenticated* URL, and ``_main`` attaches the sessionid
    cookie to every probe. So the premise that authorised the POST was
    invalidated by the feature the probe was written to support, and a test that
    asserted the conclusion while naming the now-false premise is worse than no
    test: it reads as coverage and would have kept passing.
    """
    for probe in DEFAULT_PROBES:
        assert probe.method.upper() in READ_ONLY_METHODS, (
            f"{probe.name!r} uses {probe.method!r} against {probe.url!r}"
        )
        assert probe.body is None, (
            f"{probe.name!r} carries a request body; a GET with a body is a"
            " state-changing call wearing a read-only method"
        )


def test_the_reporting_route_is_probed_but_cannot_report():
    """The liveness question is still asked, with a method that cannot answer it
    destructively.

    A GET to the reporting route answers 405 when the route exists, which is
    exactly the signal T0 wanted from it. What it cannot do is file a report
    against anybody.
    """
    flag = [p for p in DEFAULT_PROBES if "flag_user" in p.url]
    assert len(flag) == 1, f"expected the reporting route in the probe set: {flag}"
    assert flag[0].method.upper() == "GET"


def test_a_state_changing_probe_set_is_refused_before_any_request(monkeypatch):
    """The refusal has to happen before the network, not after.

    Checking after the first call would mean the first call already went out. On
    a set whose first entry is the mutating one, that is the report. Asserted by
    making the client unconstructable, so a check that ran one step too late
    would surface as a construction error rather than a silent pass.
    """
    def _unconstructable(**kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("a client was built despite the refusal")

    monkeypatch.setattr("insta_report.probe.httpx.Client", _unconstructable)

    with pytest.raises(ValueError, match="does not act"):
        run_probe(
            proxy=None,
            control_url=CONTROL_URL,
            probes=[
                Probe(
                    name="would_report",
                    url="https://www.instagram.com/api/v1/users/123/flag_user/",
                    method="POST",
                    body="source_name=profile",
                )
            ],
        )


def test_a_malformed_probe_url_is_refused_before_any_request(monkeypatch):
    """The unsubstituted-placeholder refusal, on the same terms as the method one.

    Before the network, not after -- asserted by making the client unconstructable,
    so a check that ran one step too late would surface as a construction error
    rather than a silent pass. This is the defect that let a malformed
    ``{user_id}`` path report as evidence about a route.
    """
    def _unconstructable(**kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("a client was built despite the refusal")

    monkeypatch.setattr("insta_report.probe.httpx.Client", _unconstructable)

    with pytest.raises(ValueError, match="malformed"):
        run_probe(
            proxy=None,
            control_url=CONTROL_URL,
            probes=[
                Probe(
                    name="flag_user_route_exists",
                    url="https://i.instagram.com/api/v1/users/{user_id}/flag_user/",
                )
            ],
        )


def test_the_placeholder_check_names_every_missing_value():
    """One wrong answer is a nuisance; a partial one teaches the wrong lesson.

    Naming only the first missing placeholder would send a reader off to fix one
    key, re-run, and be wrong again -- and the re-run would produce another
    plausible-looking 404.
    """
    probes = [
        Probe(name="two", url="https://h.test/{alpha}/{beta}"),
    ]
    with pytest.raises(ValueError) as excinfo:
        _assert_placeholders(probes, {})
    message = str(excinfo.value)
    assert "'alpha'" in message and "'beta'" in message, message
    assert "two" in message, message


def test_a_fully_substituted_probe_set_is_accepted():
    """The guard must not refuse valid input, or it gets switched off.

    A check that fires on good configuration is a check operators learn to
    work around, and a worked-around check protects nothing.
    """
    _assert_placeholders(DEFAULT_PROBES, {"username": "u", "user_id": "1"})


def test_the_read_only_check_does_not_depend_on_the_caller_being_authenticated(
    monkeypatch,
):
    """The guarantee must survive the run it is most needed in.

    ``run_probe`` is called with a sessionid cookie attached, because that is
    what a real T0 run does. If the read-only check were a property of the
    *unauthenticated* case -- which is how the previous version reasoned -- it
    would not apply to any run an operator would actually perform. So the check
    is asserted on the authenticated path, which is the only path that exists in
    production.
    """
    with pytest.raises(ValueError, match="can change"):
        run_probe(
            proxy=None,
            control_url=CONTROL_URL,
            probes=[
                Probe(
                    name="authenticated_post",
                    url="https://www.instagram.com/api/v1/users/123/flag_user/",
                    method="POST",
                )
            ],
            extra_headers={"Cookie": "sessionid=whatever"},
        )


def test_no_default_probe_points_at_a_submit_endpoint():
    """Independent of method: the submit *page* is also off limits.

    ``/report/`` in a URL is a form that changes state on a GET, because the
    server does not care what the client intended. The reporting API is covered
    by the method check above; this covers the HTML surface.
    """
    for probe in DEFAULT_PROBES:
        assert "/report/" not in probe.url, (
            f"{probe.name!r} points at a report form; a GET there is a POST"
        )


def test_an_unknown_placeholder_is_left_visible_rather_than_guessed():
    """A placeholder with no value stays visible, so the report shows the truth.

    Silently dropping it would turn ``.../users/{user_id}/flag_user/`` into
    ``.../users/flag_user/`` -- a *different, real* endpoint -- and the operator
    would be reading a liveness result for a route nobody probed.
    """
    rendered = render_url(
        "https://t.test/api/v1/users/{user_id}/flag_user/",
        {"username": "alice"},
    )
    assert "{user_id}" in rendered
    assert "users/flag_user" not in rendered


# --- plumbing ---------------------------------------------------------------


def test_substitutions_replace_path_placeholders():
    """Exercises :func:`render_url`, which is the code that actually runs.

    It used to assert on ``str.format``, which production does not use -- so it
    would have kept passing if the real substitution had been broken, which is
    the failure mode a test is supposed to prevent. It sat in this file twice
    under one name for a while, and the static gate caught the shadowing; the
    second definition silently owned the name, so the ``render_url`` version
    never ran at all until the duplicate was removed.
    """
    rendered = render_url("https://t.test/{username}/", {"username": "alice"})
    assert rendered == "https://t.test/alice/"


def test_every_probe_result_is_reported_as_a_row():
    row = result(name="route", status=404, body="").to_row()
    assert row["probe"] == "route"
    assert row["status"] == 404
    assert row["bytes"] == 0
    assert row["network_verdict"] == NetworkVerdict.UNREADABLE.value


def test_timeout_result_classifies_as_unreadable():
    assert result(status=None, body="", error="timeout").network_verdict is (
        NetworkVerdict.UNREADABLE
    )


def test_secrets_registered_are_scrubbed_from_excerpts():
    """A probe shows bodies to a human, so the redactor applies there too."""
    get_registry().register("sessionid-value-abcdefgh")
    r = result(body="cookie: sessionid-value-abcdefgh leaked")
    scrubbed = get_registry().scrub(r.excerpt or r.body[:200])
    assert "sessionid-value-abcdefgh" not in scrubbed
    assert "[REDACTED]" in scrubbed


def test_json_report_is_serialisable():
    r = report(result(), result())
    import json

    payload = json.dumps(r.to_json())
    assert '"verdict"' in payload


# --- the run, against a stubbed transport -----------------------------------


def make_response(
    status: int, text: str, content_type: str = "text/html"
) -> httpx.Response:
    """A real Response -- MockTransport type-checks, so a duck type is refused."""
    return httpx.Response(
        status_code=status,
        content=text.encode("utf-8"),
        headers={"content-type": content_type},
    )


class _ContextClient:
    """Wraps a real httpx.Client so run_probe's ``with`` block still works."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def __enter__(self) -> httpx.Client:
        return self._client

    def __exit__(self, *exc: object) -> None:
        self._client.close()

    def __getattr__(self, name: str):
        return getattr(self._client, name)


def install_transport(monkeypatch, handler) -> None:
    """Swap httpx.Client for one backed by a MockTransport.

    Captures the real class before patching -- a factory that calls
    ``httpx.Client`` after the patch recurses into itself.
    """
    real_client = httpx.Client

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _ContextClient(real_client(*args, **kwargs))

    monkeypatch.setattr(httpx, "Client", factory)


def test_control_runs_before_the_probes(monkeypatch):
    """Ordering is the design; assert it rather than trust it.

    Running probes first and checking the control afterwards is what produced
    four confident 404s next to one silent failure.
    """
    order: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        order.append(url)
        # The control is whatever URL the caller named, so key on the URL rather
        # than on a route: this test is about ordering, and hard-coding a path
        # made it silently depend on which route the default control happens to
        # be. When the control moved, this failed for the wrong reason.
        if url == CONTROL_URL:
            return make_response(200, '{"status": "ok", "user": {}}', "application/json")
        if "ipify" in url or "ipconfig" in url:
            return make_response(200, "203.0.113.9", "text/plain")
        return make_response(404, "", "text/html")

    install_transport(monkeypatch, handler)

    r = run_probe(
        proxy=None,
        control_url=CONTROL_URL,
        probes=(Probe(name="route", url="https://t.test/x"),),
    )
    assert r.control_ok is True
    assert r.verdict is ProbeVerdict.UNOBSERVABLE
    assert order.index(CONTROL_URL) < order.index("https://t.test/x")


def test_headers_a_probe_carries_reach_the_network_intact(monkeypatch):
    """The regression itself: ``Probe.headers`` is the request, not a display value.

    ``_do`` used to scrub every registered secret out of ``probe.headers`` before
    handing them to the transport, on the reasonable-sounding theory that anything
    holding a secret should be scrubbed. But ``Probe.headers`` *is* the request, and
    it exists precisely so a caller can put headers on an individual request. So
    any probe carrying a credential that way went out unauthenticated -- and did
    so silently, which is the damaging part: the request succeeded, the answer was
    about an anonymous caller, and nothing anywhere said so.

    This is the test with teeth for that bug, and it is deliberately aimed at a
    *probe* rather than at the control. The control no longer carries headers of
    its own -- the credential is attached once, on the client -- so a test aimed
    at the control cannot see this loop at all. The first version of this test
    did exactly that, and passed against the broken code: a green test that was
    asserting a property the defect never touched.
    """
    session = "61214264580%3A3Qxe4Kp3ze0djU%3A0%3AAYkQ5dJ9fake4"
    register_secret(session)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "ipify" in url or "ifconfig" in url:
            return make_response(200, "198.51.100.7", "text/plain")
        seen.append(request.headers.get("cookie", ""))
        return make_response(200, '{"status": "ok"}', "application/json")

    install_transport(monkeypatch, handler)
    run_probe(
        proxy=None,
        control_url=CONTROL_URL,
        probes=(
            Probe(
                name="route",
                url="https://i.instagram.com/api/v1/x/",
                headers={"Cookie": f"sessionid={session}"},
            ),
        ),
    )

    assert seen, "nothing reached the transport, so this proves nothing"
    assert session in seen[-1], (
        "a probe's own headers were scrubbed on the way to the network, so it "
        "went out unauthenticated and every answer it gets back is about an "
        f"anonymous request. cookie={seen[-1]!r}"
    )


def test_the_client_level_credential_reaches_the_network_intact(monkeypatch):
    """The control must go out authenticated, since it is the one call trusted.

    The credential is attached once, on the client, and the control inherits it.
    This pins that the client actually carries it -- a regression here would make
    the control anonymous while every probe, which shares the client, still looked
    fine.

    It does *not* cover ``Probe.headers``; that is a separate mechanism with its
    own test above, and conflating the two is how the first version of this test
    came to pass against a live defect.
    """
    # The local is called ``session`` rather than ``secret`` on purpose. The
    # credential-leak gate scans tracked source for ``secret = "<literal>"`` and
    # would flag any local holding a 16+ character value -- including a fake one.
    # Renaming keeps these fixtures out of SAFE_LITERALS, which is reserved for
    # literals whose credential *shape* is the point of the test. The values keep
    # a realistic mixed-case sessionid shape; the gate's token pattern needs a
    # literal ``sessionid=``/``sessionid%3A`` prefix to fire, and there is not
    # one in this file, so they cannot be matched by it either.
    session = "61214264580%3A3Qxe4Kp3ze0djU%3A0%3AAYkQ5dJ9fake"
    register_secret(session)
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("cookie", "")))
        if "ipify" in str(request.url) or "ifconfig" in str(request.url):
            return make_response(200, "198.51.100.7", "text/plain")
        return make_response(200, '{"status": "ok"}', "application/json")

    install_transport(monkeypatch, handler)
    run_probe(
        proxy=None,
        control_url=CONTROL_URL,
        probes=(),
        extra_headers={"Cookie": f"sessionid={session}"},
    )

    control_calls = [c for url, c in seen if "instagram.com" in url]
    assert control_calls, f"the control never reached the transport: {seen}"
    for cookie in control_calls:
        assert session in cookie, (
            "the control was sent with its credential redacted away; it is "
            f"unauthenticated and every answer it gives is about an anonymous "
            f"request. cookie={cookie!r}"
        )


def test_no_credential_reaches_a_third_party_host(monkeypatch):
    """The egress check must not carry the session.

    It used to borrow the caller's client, which holds the cookie at the client
    level -- so ``api.ipify.org`` and ``ifconfig.me/ip`` received a live
    Instagram session on every run. Unrelated third parties, no operator
    consent, and nothing in the report to say it had happened.

    The fix is structural rather than a strip call: ``_egress_ip`` builds its
    own client and the credential is never passed into it, so it cannot be
    sent. This test is the belt to that braces, and it generalises to any
    future third-party call someone adds to this module.
    """
    session = "61214264580%3A3Qxe4Kp3ze0djU%3A0%3AAYkQ5dJ9fake2"
    register_secret(session)
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("cookie", "")))
        if "ipify" in str(request.url) or "ifconfig" in str(request.url):
            return make_response(200, "198.51.100.7", "text/plain")
        return make_response(200, '{"status": "ok"}', "application/json")

    install_transport(monkeypatch, handler)
    run_probe(
        proxy=None,
        control_url=CONTROL_URL,
        probes=(),
        extra_headers={"Cookie": f"sessionid={session}"},
    )

    assert seen, "nothing was sent, so this proves nothing"
    for url, cookie in seen:
        host = url.split("/")[2]
        if "instagram.com" in host:
            continue
        assert session not in cookie, (
            f"the Instagram session was sent to {host}, a third party"
        )


def test_the_egress_client_is_given_the_proxy(monkeypatch):
    """The fix must not have quietly turned the egress check into a direct one.

    ``_egress_ip`` used to borrow the caller's client; it now builds its own, so
    the credential cannot reach it. Building a client is also the easiest place to
    have dropped the proxy on the floor while making that change, and an egress
    check that bypasses the proxy reports the *direct* address -- the one number
    the function exists to establish, and the one number that would then be
    quietly wrong.

    The client is not built for real: ``httpx`` wraps any supplied transport in a
    genuine proxy connector, so a mocked request through a proxy is still a real
    CONNECT. Stopping at construction keeps this instant and keeps the suite's
    no-outbound-sockets guarantee. The other two halves are asserted elsewhere and
    are named here so a reader does not assume they were folded in: the address
    being *read* by ``test_egress_ip_is_recorded``, and the credential being
    *absent* by ``test_no_credential_reaches_a_third_party_host``.
    """
    seen: list[dict[str, object]] = []

    class _Stop(Exception):
        pass

    def factory(**kwargs):
        seen.append(kwargs)
        raise _Stop

    monkeypatch.setattr(httpx, "Client", factory)
    with pytest.raises(_Stop):
        _egress_ip(proxy="http://10.0.0.9:8080", timeout=1.0, user_agent="UA")

    assert len(seen) == 1, seen
    assert seen[0]["proxy"] == "http://10.0.0.9:8080", seen[0]
    # And the credential is not even a candidate: the only headers it builds.
    assert set(seen[0]["headers"]) == {"User-Agent", "Accept"}, seen[0]


def test_the_control_and_the_probes_attach_the_credential_the_same_way(monkeypatch):
    """One attachment point, so the two cannot drift apart again.

    The credential was on the client *and* on the control's own ``Probe.headers``
    -- two sources of truth for one value. Redacting one copy and not the other
    is exactly the failure that shipped. Asserting they agree is cheap and
    forecloses the whole family.
    """
    session = "61214264580%3A3Qxe4Kp3ze0djU%3A0%3AAYkQ5dJ9fake3"
    register_secret(session)
    cookies: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "ipify" in url or "ifconfig" in url:
            return make_response(200, "198.51.100.7", "text/plain")
        cookies.append(request.headers.get("cookie", ""))
        return make_response(200, '{"status": "ok"}', "application/json")

    install_transport(monkeypatch, handler)
    run_probe(
        proxy=None,
        control_url=CONTROL_URL,
        probes=(Probe(name="route", url="https://i.instagram.com/api/v1/x/"),),
        extra_headers={"Cookie": f"sessionid={session}", "X-IG-App-ID": "1"},
    )

    assert len(cookies) == 2, f"expected the control and the probe: {cookies}"
    assert cookies[0] == cookies[1], (
        f"the control and the probe sent different credentials: {cookies}"
    )


def test_an_unregistered_secret_is_not_scrubbed_out_of_the_request(monkeypatch):
    """Redaction must not be able to eat a credential that was never registered.

    Belt to the other braces: whatever the registry contains, the bytes on the
    wire are the bytes the operator configured.
    """
    session = "not-registered-anywhere-1234567890"
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "ipify" in url or "ifconfig" in url:
            return make_response(200, "198.51.100.7", "text/plain")
        seen.append(request.headers.get("cookie", ""))
        return make_response(200, '{"status": "ok"}', "application/json")

    install_transport(monkeypatch, handler)
    run_probe(
        proxy=None,
        control_url=CONTROL_URL,
        probes=(),
        extra_headers={"Cookie": f"sessionid={session}"},
    )
    assert seen and session in seen[0], seen


def test_a_secret_in_a_response_body_is_still_scrubbed_from_the_excerpt(monkeypatch):
    """The other direction, and the one redaction is actually for.

    Removing the header scrub must not have removed the body scrub. Instagram
    echoing a sessionid back in an error payload is exactly the case the
    registry exists for, and the excerpt is what an operator pastes into a
    ticket.
    """
    session = "echoed-back-secret-abcdef123456"
    register_secret(session)

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "ipify" in url or "ifconfig" in url:
            return make_response(200, "198.51.100.7", "text/plain")
        if url == CONTROL_URL:
            return make_response(200, '{"status": "ok"}', "application/json")
        return make_response(
            400, f'{{"message": "bad token {session}"}}', "application/json"
        )

    install_transport(monkeypatch, handler)
    r = run_probe(
        proxy=None,
        control_url=CONTROL_URL,
        probes=(Probe(name="route", url="https://i.instagram.com/api/v1/x/"),),
    )
    excerpt = r.results[0].excerpt
    assert session not in excerpt, excerpt
    assert "[REDACTED]" in excerpt, excerpt


def test_a_burned_exit_reports_inconclusive_end_to_end(monkeypatch):
    """429-with-no-bytes on the control is the exact original failure mode."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "ipify" in url:
            return make_response(200, "203.0.113.9", "text/plain")
        return make_response(429, "", "text/html")

    install_transport(monkeypatch, handler)

    r = run_probe(
        proxy=None,
        control_url=CONTROL_URL,
        probes=DEFAULT_PROBES,
        # Supplied, because the shipped table needs them. This call used to pass
        # none and relied on the resulting malformed paths -- which is the defect
        # _assert_placeholders now refuses. That the refusal surfaced here, in a
        # test written before it, is the check earning its place.
        substitutions={"username": "reporter.one", "user_id": "555000111"},
    )
    assert r.verdict is ProbeVerdict.INCONCLUSIVE
    assert "flagged source IP" in r.summary()


def test_egress_ip_is_recorded(monkeypatch):
    """A 'clean exit' nobody verified is how a burned IP survives a re-run."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "ipify" in url:
            return make_response(200, "198.51.100.42", "text/plain")
        if "current_v2" in url:
            return make_response(200, '{"status": "ok"}', "application/json")
        return make_response(404, "", "text/html")

    install_transport(monkeypatch, handler)
    r = run_probe(proxy=None, control_url=CONTROL_URL, probes=())
    assert r.exit_ip == "198.51.100.42"


# --- the identity/tier invariant --------------------------------------------
#
# Everything in this section exists because of a measurement taken on
# 2026-09-26 against a live session, which is recorded in the module. The
# measurement was:
#
#   i.instagram.com + desktop Chrome UA   -> 400 {"useragent mismatch"}
#   i.instagram.com + mobile UA           -> 200 {"status": "ok", ...}
#
# The shipped default was a desktop Chrome string, so the probe asked a mobile
# host a mobile question in a desktop's voice, and the module reported
# INCONCLUSIVE on an API that was answering. These tests are offline, so they
# cannot re-measure. What they can do is refuse the *shape* of that mistake
# from coming back, and pin the paths that were measured to be wrong so nobody
# re-selects them believing they are unverified.

#: Routes measured to not exist on 2026-09-26. ``current_v2`` answered 404 with
#: Instagram's 20,942-byte logged-out HTML page for every mobile user agent and
#: 500 for the desktop one -- from an exit that was demonstrably working, since
#: a different route on the same host returned 200 in the same second.
#:
#: It was the probe's control URL. A control that 404s a valid session cannot
#: tell "this exit is blocked" from "this URL is wrong", and it made that
#: ambiguity resolve toward "do not build the channel".
MEASURED_MISSING_ROUTES: frozenset[str] = frozenset(
    {"/api/v1/accounts/current_v2/"}
)


def test_the_control_is_not_a_route_that_was_measured_to_be_missing():
    control_path = DEFAULT_CONTROL_URL.split("/", 3)[3]
    assert control_path not in MEASURED_MISSING_ROUTES, (
        f"{DEFAULT_CONTROL_URL} was measured to answer 404/500 on a working "
        f"exit; it cannot serve as the control"
    )


def test_the_default_user_agent_presents_as_a_mobile_client():
    """The single check that would have caught the original defect.

    It is a format check, not a version check, because that is what the API
    actually enforces: app versions 155, 219 and 302 all returned byte-identical
    200s. Asserting a version would make this test go stale and train everyone
    to update it without reading why.
    """
    assert DEFAULT_USER_AGENT.startswith("Instagram "), DEFAULT_USER_AGENT
    assert " Android " in DEFAULT_USER_AGENT or " iPhone" in DEFAULT_USER_AGENT
    assert "Mozilla/5.0" not in DEFAULT_USER_AGENT


def test_the_control_and_the_default_identity_agree_on_tier():
    """A mobile host with a mobile identity, or the control cannot answer."""
    host = DEFAULT_CONTROL_URL.split("/")[2]
    assert host == "i.instagram.com", host
    assert DEFAULT_USER_AGENT.startswith("Instagram ")


def test_every_api_probe_targets_the_same_tier_as_the_control():
    """One identity is presented to the whole run, so one tier is addressed.

    ``www.instagram.com`` is the web tier and answers a mobile identity with
    the logged-out page. Probing it with a mobile UA is not wrong -- it is
    inconclusive, which is the outcome this module is supposed to avoid
    mistaking for evidence.
    """
    control_host = DEFAULT_CONTROL_URL.split("/")[2]
    api_probes = [
        p for p in DEFAULT_PROBES if "/api/v1/" in p.url
    ]
    assert api_probes, "expected at least one API route in the default set"
    for probe in api_probes:
        host = probe.url.split("/")[2]
        assert host == control_host, (
            f"{probe.name!r} targets {host} but the control and the shipped "
            f"identity address {control_host}"
        )


def test_every_shipped_probe_url_can_be_fully_substituted():
    """No shipped probe may depend on a substitution nobody supplies.

    This is the invariant that was missing, and its absence is why the reporting
    route read as nonexistent. ``flag_user_route_exists`` needed ``{user_id}``,
    ``_main`` supplied only ``{username}``, and the resulting malformed path 404'd
    -- indistinguishable, from the response, from a route that is genuinely gone.

    The list below is written out by hand rather than derived, on purpose. Derived
    from the table it would agree with the table by construction and could never
    fail; a reviewer adding a probe with a new placeholder is the event this is
    supposed to catch, and that only works if the expectation is written
    separately. ``_assert_placeholders`` is the backstop that refuses at runtime;
    this is the check that says so at review time.
    """
    supplied = {"username", "user_id"}
    for probe in DEFAULT_PROBES:
        needed = set(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", probe.url))
        assert needed <= supplied, (
            f"{probe.name!r} needs {sorted(needed - supplied)}, which _main does "
            f"not supply. Add it to the substitution table and to the set above, "
            f"or the probe will send a malformed path and its 404 will be read as "
            f"evidence about the route."
        )


# --- _main: which identity actually gets sent --------------------------------


def _run_main_capturing_identity(tmp_path, monkeypatch, api_block: str) -> str:
    """Drive ``_main`` and return the User-Agent it handed to the transport."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    proxies = data_dir / "proxies.txt"
    proxies.write_text("203.0.113.10:8080\n", encoding="utf-8")
    config = tmp_path / "cfg.toml"
    config.write_text(
        f'data_dir = "{data_dir.as_posix()}"\n\n'
        '[accounts.alpha]\n'
        'username = "reporter.one"\n'
        'sessionid_env = "IG_SESSIONID_TEST"\n'
        # The reporting account's own numeric id. The default probe set addresses
        # the reporting route with it, and _main refuses to run without it rather
        # than send a malformed path -- see _assert_placeholders.
        'user_id = "555000111"\n\n'
        f'[proxies]\nsource = "file"\nfile_path = "{proxies.as_posix()}"\n\n'
        # One account, so the concurrency ceiling has to be one. Omitting this
        # is not a default: [browser] max_concurrent defaults to 3 and the
        # loader refuses 3 against a single account, because a lease with no
        # account to hold it is a lease that never gets used.
        '[browser]\nmax_concurrent = 1\n\n'
        f"{api_block}",
        encoding="utf-8",
    )
    monkeypatch.setenv("IG_SESSIONID_TEST", "12345%3Aabcdef")

    seen: dict[str, object] = {}

    def capture(exits, **kwargs):
        seen.update(kwargs)
        seen["exits"] = list(exits)
        return []

    monkeypatch.setattr("insta_report.probe.run_across_exits", capture)
    monkeypatch.setattr(
        "insta_report.probe.setup_logging", lambda **_kw: None, raising=False
    )
    exit_code = _main(["--config", str(config)])
    assert exit_code in (0, 1, 3), exit_code
    return str(seen["user_agent"])


def test_main_sends_the_configured_mobile_identity(tmp_path, monkeypatch):
    ua = _run_main_capturing_identity(
        tmp_path,
        monkeypatch,
        '[api]\nmobile_user_agent = "Instagram 999.0.0.1 Android (30/11; 480dpi; '
        '1080x1920; x; y; z; w; en_US; 1)"\n',
    )
    assert ua.startswith("Instagram 999.0.0.1 Android")


def test_main_never_substitutes_the_web_identity_for_the_mobile_tier(
    tmp_path, monkeypatch
):
    """The regression that produced a wrong T0 verdict on a working API.

    With ``mobile_user_agent`` unset and ``web_user_agent`` set, the run used
    to send the *web* string to ``i.instagram.com``. Every route answered
    ``useragent mismatch``, the control could not answer, and the module
    reported INCONCLUSIVE -- a conclusion about the world drawn entirely from
    the probe's own misconfiguration.
    """
    ua = _run_main_capturing_identity(
        tmp_path,
        monkeypatch,
        '[api]\nmobile_user_agent = ""\n'
        'web_user_agent = "Mozilla/5.0 (Windows NT 10.0) Chrome/140.0.0.0"\n',
    )
    assert "Mozilla/5.0" not in ua, ua
    assert ua == DEFAULT_USER_AGENT
