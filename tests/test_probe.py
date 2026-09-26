"""T0 probe tests.

The probe is the gate on the API channel, so its most important property is
that it refuses to conclude. Everything here tests the refusal.
"""

from __future__ import annotations

import httpx
import pytest

from insta_report.outcomes import NetworkVerdict
from insta_report.probe import (
    DEFAULT_PROBES,
    READ_ONLY_METHODS,
    Probe,
    ProbeResult,
    ProbeVerdict,
    render_url,
    run_probe,
)
from insta_report.support.redaction import get_registry

CONTROL_URL = "https://i.instagram.com/api/v1/accounts/current_v2/"


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
        if "current_v2" in url:
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


def test_a_burned_exit_reports_inconclusive_end_to_end(monkeypatch):
    """429-with-no-bytes on the control is the exact original failure mode."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "ipify" in url:
            return make_response(200, "203.0.113.9", "text/plain")
        return make_response(429, "", "text/html")

    install_transport(monkeypatch, handler)

    r = run_probe(proxy=None, control_url=CONTROL_URL, probes=DEFAULT_PROBES)
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
