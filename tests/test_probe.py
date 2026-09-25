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
    Probe,
    ProbeResult,
    ProbeVerdict,
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


def test_no_default_probe_submits_a_report():
    """A probe must be incapable of harming a target.

    flag_user uses POST because its route shape is part of what is being
    tested, but the body is a bare marker and the account is not authenticated,
    so it cannot file anything.
    """
    for probe in DEFAULT_PROBES:
        if probe.method == "POST":
            assert probe.body in (None, "", "source_name=profile")
        assert "report" not in probe.url or "flag_user" in probe.url


def test_default_probes_are_read_only_against_targets():
    """No default probe may point at a submit endpoint."""
    for probe in DEFAULT_PROBES:
        assert "/report" not in probe.url.replace("flag_user", "")
        assert "flag_user" in probe.url or "/report" not in probe.url


# --- plumbing ---------------------------------------------------------------


def test_substitutions_replace_path_placeholders():
    probe = Probe(name="x", url="https://t.test/{username}/")
    assert probe.url.format(username="alice") == "https://t.test/alice/"


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
