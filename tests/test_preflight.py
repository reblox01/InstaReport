"""The API channel's preflight: what "answers" means, and what a pass refuses.

Every network test runs against a stubbed transport -- the suite's offline
guard refuses real sockets anyway -- and the ones with teeth assert what does
*not* happen: no method that could change state leaves this module, no request
leaves before the identity is complete, and no non-answering verdict is
allowed to look like a pass. The entry-point tests pin the exit-code contract
(0 answers, 1 finished with reading to do, 2 the config never let a request
out) because that is what a wrapper -- and the operator at 2am -- keys on.
"""

from __future__ import annotations

import json
import httpx
import pytest

from insta_report.errors import PreflightFailed
from insta_report.preflight import (
    ANSWERING_VERDICTS,
    PreflightReport,
    _main,
    assert_answers,
    preflight_account,
    run_preflight,
)
from insta_report.probe import (
    DEFAULT_USER_AGENT,
    ChannelIdentity,
    ProbeReport,
    ProbeResult,
    ProbeVerdict,
)

from .test_probe import install_transport, make_response

IDENTITY = ChannelIdentity(
    account_ref="alpha",
    user_id="555000111",
    user_agent=DEFAULT_USER_AGENT,
    headers={
        "Cookie": "sessionid=12345%3Aabcdef",
        "X-IG-App-ID": "936619743392459",
    },
)

#: The two body shapes the classifier can actually read: a control that means
#: it, and a refusal. classify_network returns UNREADABLE for an empty or
#: HTML body no matter what the status says, so a fixture that wants REJECTED
#: has to speak JSON -- the same rule the live tier is held to.
CONTROL_JSON = '{"status": "ok", "user": {}}'
REFUSAL_JSON = '{"message": "Method Not Allowed"}'


def _is_ip_echo(url: str) -> bool:
    return "ipify" in url or "ifconfig" in url


def a_preflight(*, answers: bool) -> PreflightReport:
    """A crafted report, for the tests that judge the verdict and never the
    network. Not-answering is the inconclusive shape: a control that refused
    and a route that said nothing readable."""
    if answers:
        control = ProbeResult(
            name="CONTROL (known-good)",
            url="https://i.instagram.com/api/v1/accounts/edit/web_form_data/",
            status=200,
            body=CONTROL_JSON,
            content_type="application/json",
        )
        route = ProbeResult(
            name="flag_user_route_exists",
            url="https://i.instagram.com/api/v1/users/555000111/flag_user/",
            status=405,
            body=REFUSAL_JSON,
            content_type="application/json",
        )
    else:
        control = ProbeResult(
            name="CONTROL (known-good)",
            url="https://i.instagram.com/api/v1/accounts/edit/web_form_data/",
            status=403,
            body="forbidden",
            content_type="text/html",
        )
        route = ProbeResult(
            name="flag_user_route_exists",
            url="https://i.instagram.com/api/v1/users/555000111/flag_user/",
            status=404,
            body="",
            content_type="text/html",
        )
    return PreflightReport(
        account_ref="alpha",
        proxy=None,
        report=ProbeReport(exit_ip="203.0.113.9", control=control, results=[route]),
    )


# --- what "answers" means, against a stubbed transport ----------------------


class TestWhatAnsweringMeans:
    def test_a_readable_refusal_on_the_reporting_route_is_answering(
        self, monkeypatch
    ):
        """405 is the best answer a GET can get from a POST-only route.

        The route exists, it wants a method the preflight does not carry, and
        nothing was changed finding that out. Refusing to call it a pass would
        make the gate demand success where the healthy response is a refusal.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if _is_ip_echo(url):
                return make_response(200, "203.0.113.9", "text/plain")
            if "flag_user" in url:
                return make_response(405, REFUSAL_JSON, "application/json")
            return make_response(200, CONTROL_JSON, "application/json")

        install_transport(monkeypatch, handler)
        report = run_preflight(IDENTITY, proxy=None)
        assert report.verdict is ProbeVerdict.ANSWERED_NOT_OK
        assert report.answers is True

    def test_a_readable_ok_on_the_reporting_route_is_answering(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if _is_ip_echo(url):
                return make_response(200, "203.0.113.9", "text/plain")
            return make_response(200, CONTROL_JSON, "application/json")

        install_transport(monkeypatch, handler)
        report = run_preflight(IDENTITY, proxy=None)
        assert report.verdict is ProbeVerdict.OBSERVABLE
        assert report.answers is True

    def test_a_route_that_times_out_is_not_answered(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if _is_ip_echo(url):
                return make_response(200, "203.0.113.9", "text/plain")
            if "flag_user" in url:
                raise httpx.ConnectTimeout("the route never came back")
            return make_response(200, CONTROL_JSON, "application/json")

        install_transport(monkeypatch, handler)
        report = run_preflight(IDENTITY, proxy=None)
        assert report.verdict is ProbeVerdict.UNOBSERVABLE
        assert report.answers is False
        assert report.route is not None and report.route.error == "timeout"

    def test_a_failed_control_makes_the_run_inconclusive(self, monkeypatch):
        """The rule the whole design rests on, applied to the channel's question.

        A dead control plus a missing route says everything about the exit and
        nothing about Instagram. Passing here would hand the ladder evidence
        drawn entirely from an exit that could not stand up its own control.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if _is_ip_echo(url):
                return make_response(200, "203.0.113.9", "text/plain")
            if "flag_user" in url:
                return make_response(404, "", "text/html")
            return make_response(403, "forbidden", "text/html")

        install_transport(monkeypatch, handler)
        report = run_preflight(IDENTITY, proxy=None)
        assert report.verdict is ProbeVerdict.INCONCLUSIVE
        assert report.answers is False

    def test_an_unstable_exit_does_not_pass_the_gate(self, monkeypatch):
        """The route answered; the exit still does not get a pass.

        The route being real and this address being usable are separate
        findings, and only the second one lets a run reproduce. The probe
        keeps this verdict apart from OBSERVABLE for the same reason.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if _is_ip_echo(url):
                return make_response(200, "203.0.113.9", "text/plain")
            if "flag_user" in url:
                return make_response(200, CONTROL_JSON, "application/json")
            return make_response(403, "forbidden", "text/html")

        install_transport(monkeypatch, handler)
        report = run_preflight(IDENTITY, proxy=None)
        assert report.verdict is ProbeVerdict.OBSERVABLE_UNSTABLE_EXIT
        assert report.answers is False


# --- what preflight refuses to do -------------------------------------------


class TestWhatPreflightRefusesToDo:
    def test_every_request_that_leaves_is_a_read_method(self, monkeypatch):
        """The module's central claim, asserted on the wire rather than in prose.

        The first version of this tool's probe table carried a POST to the
        reporting route. It was harmless only because a substitution nobody
        remembered was missing -- so the guarantee now lives in code, twice:
        run_probe's assertion before any call, and this test's record of what
        actually left.
        """
        methods: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            methods.append(request.method)
            url = str(request.url)
            if _is_ip_echo(url):
                return make_response(200, "203.0.113.9", "text/plain")
            if "flag_user" in url:
                return make_response(405, REFUSAL_JSON, "application/json")
            return make_response(200, CONTROL_JSON, "application/json")

        install_transport(monkeypatch, handler)
        run_preflight(IDENTITY, proxy=None)
        assert methods, "no request observed, so nothing was proven"
        assert set(methods) <= {"GET", "HEAD", "OPTIONS"}, methods
        assert "POST" not in methods

    def test_the_route_is_addressed_by_the_numeric_id_not_a_placeholder(
        self, monkeypatch
    ):
        """A rendered URL is a precondition for an interpretable response.

        A path containing a literal ``{user_id}`` 404s, and that 404 sitting
        next to a healthy control looks exactly like evidence the route is
        gone. Both the probe and this module refuse the unrendered form; this
        pins the end-to-end rendering for the preflight's own probe.
        """
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "flag_user" in url:
                seen.append(url)
            if _is_ip_echo(url):
                return make_response(200, "203.0.113.9", "text/plain")
            if "flag_user" in url:
                return make_response(405, REFUSAL_JSON, "application/json")
            return make_response(200, CONTROL_JSON, "application/json")

        install_transport(monkeypatch, handler)
        run_preflight(IDENTITY, proxy=None)
        assert seen, "the reporting route was never addressed"
        url = seen[0]
        assert "555000111" in url
        assert "{user_id}" not in url and "%7B" not in url

    def test_the_identity_reaches_the_transport_intact(self, monkeypatch):
        """The session, the app id and the tier's user agent, as one client.

        Bundled because they describe a single client: this module's whole
        question is whether *this* identity answers, and a request that
        silently lost the cookie would answer as nobody.
        """
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "flag_user" in url:
                captured["cookie"] = request.headers.get("cookie", "")
                captured["user-agent"] = request.headers.get("user-agent", "")
                captured["app-id"] = request.headers.get("x-ig-app-id", "")
            if _is_ip_echo(url):
                return make_response(200, "203.0.113.9", "text/plain")
            if "flag_user" in url:
                return make_response(405, REFUSAL_JSON, "application/json")
            return make_response(200, CONTROL_JSON, "application/json")

        install_transport(monkeypatch, handler)
        run_preflight(IDENTITY, proxy=None)
        assert captured["cookie"] == "sessionid=12345%3Aabcdef"
        assert captured["user-agent"] == DEFAULT_USER_AGENT
        assert captured["app-id"] == "936619743392459"


# --- the gate ----------------------------------------------------------------


class TestTheGate:
    def test_a_passing_report_is_not_refused(self):
        assert_answers(a_preflight(answers=True))

    def test_a_failing_report_names_its_verdict(self):
        """The verdict is the fix.

        A flag needs a different response than an unreachable route, and an
        error message that says only "failed" makes the operator guess which.
        """
        with pytest.raises(PreflightFailed, match="inconclusive"):
            assert_answers(a_preflight(answers=False))

    def test_the_gate_is_binary_and_matches_the_report(self):
        """One definition of answering-ness, asserted as a set.

        The five verdicts split exactly into the two that pass and the three
        that cannot -- no third category to be argued about later. And a
        report whose control never stood up never answers, so the gate
        refuses it like any other failure rather than special-casing it.
        """
        assert ANSWERING_VERDICTS == frozenset(
            {ProbeVerdict.OBSERVABLE, ProbeVerdict.ANSWERED_NOT_OK}
        )
        empty = PreflightReport(
            account_ref="alpha",
            proxy=None,
            report=ProbeReport(exit_ip=None, control=None, results=[]),
        )
        assert empty.answers is False
        with pytest.raises(PreflightFailed):
            assert_answers(empty)


# --- the recorded evidence ---------------------------------------------------


class TestTheRecordedEvidence:
    def test_summary_says_answers_and_carries_no_session(self):
        summary = a_preflight(answers=True).summary()
        assert "ANSWERS" in summary
        assert "inconclusive" not in summary
        assert "sessionid" not in summary

    def test_summary_says_does_not_answer_when_it_does_not(self):
        summary = a_preflight(answers=False).summary()
        assert "DOES NOT ANSWER" in summary
        assert "control did not answer" in summary

    def test_json_is_serialisable_and_carries_answers(self):
        payload = a_preflight(answers=True).to_json()
        assert payload["answers"] is True
        assert payload["verdict"] == "answered_not_ok"
        assert payload["kind"] == "preflight"
        json.dumps(payload)  # a wrapper parses this; it must not raise

    def test_json_carries_rows_not_bodies(self):
        """The artifact a wrapper reads is not a place response bodies live.

        Rows have no body and no excerpt by construction, so the only way a
        response could travel here is if someone added it -- and this test
        fails the day they do.
        """
        text = json.dumps(a_preflight(answers=True).to_json())
        assert "body" not in json.loads(text)["control"]
        assert '"status": "ok"' not in text
        assert "sessionid" not in text


# --- preflight_account: resolve, then ask -------------------------------------


class TestPreflightAccount:
    def test_it_resolves_the_named_account_then_asks_that_exit(self, monkeypatch):
        """The two halves stay in their own functions so the forwarding is
        visible: which account, which exit, in that order."""
        seen: dict[str, object] = {}

        def fake_resolve(config, *, account_ref=None):
            seen["account_ref"] = account_ref
            return IDENTITY

        def fake_run(identity, *, proxy, control_url, timeout):
            seen["identity"] = identity
            seen["proxy"] = proxy
            return a_preflight(answers=True)

        monkeypatch.setattr(
            "insta_report.preflight.resolve_channel_identity", fake_resolve
        )
        monkeypatch.setattr("insta_report.preflight.run_preflight", fake_run)

        report = preflight_account(object(), proxy="http://10.0.0.9:8080", account_ref="bravo")
        assert seen["account_ref"] == "bravo"
        assert seen["identity"] is IDENTITY
        assert seen["proxy"] == "http://10.0.0.9:8080"
        assert report.answers is True


# --- the entry point ---------------------------------------------------------


def _with_user_id(base_config_toml: str) -> str:
    """The shared config with the one key this identity needs."""
    return base_config_toml.replace(
        "[accounts.alpha]",
        '[accounts.alpha]\nuser_id = "555000111"',
    )


class TestTheEntryPoint:
    def test_a_missing_config_file_exits_2(self, tmp_path, capsys):
        code = _main(["--config", str(tmp_path / "nope.toml")])
        assert code == 2
        assert "config error" in capsys.readouterr().err

    def test_an_incomplete_identity_exits_2_without_a_request(
        self,
        base_config_toml,
        session_env,
        proxy_file,
        write_config,
        monkeypatch,
        capsys,
    ):
        """No user_id, no request: the refusal costs nothing but a read.

        The transport is armed to fail the test if touched -- the point of
        refusing *before* the network is that a config error can never leave
        the house and come back looking like a route finding.
        """

        def explode(request: httpx.Request) -> httpx.Response:
            raise AssertionError(
                "no request may leave before the identity is complete"
            )

        install_transport(monkeypatch, explode)
        config = write_config(base_config_toml)
        code = _main(["--config", str(config)])
        assert code == 2
        err = capsys.readouterr().err
        assert "[accounts.alpha]" in err
        assert "user_id" in err

    def test_an_unknown_account_ref_exits_2(
        self,
        base_config_toml,
        session_env,
        proxy_file,
        write_config,
        monkeypatch,
        capsys,
    ):
        """A mislabeled identity is worse than none: account A's evidence
        wearing account B's name would send the operator to fix the wrong
        account. So an unknown ref refuses instead of falling back."""

        def explode(request: httpx.Request) -> httpx.Response:
            raise AssertionError("an unknown ref must refuse before the network")

        install_transport(monkeypatch, explode)
        config = write_config(_with_user_id(base_config_toml))
        code = _main(["--config", str(config), "--account", "charlie"])
        assert code == 2
        assert "charlie" in capsys.readouterr().err

    def test_when_the_channel_answers_the_exit_is_0(
        self,
        base_config_toml,
        session_env,
        proxy_file,
        write_config,
        monkeypatch,
        capsys,
    ):
        monkeypatch.setattr(
            "insta_report.preflight.preflight_account",
            lambda *a, **kw: a_preflight(answers=True),
        )
        config = write_config(_with_user_id(base_config_toml))
        code = _main(["--config", str(config)])
        assert code == 0
        out = capsys.readouterr().out
        assert "ANSWERS" in out

    def test_when_it_does_not_the_exit_is_1(
        self,
        base_config_toml,
        session_env,
        proxy_file,
        write_config,
        monkeypatch,
        capsys,
    ):
        monkeypatch.setattr(
            "insta_report.preflight.preflight_account",
            lambda *a, **kw: a_preflight(answers=False),
        )
        config = write_config(_with_user_id(base_config_toml))
        code = _main(["--config", str(config)])
        assert code == 1
        out = capsys.readouterr().out
        assert "DOES NOT ANSWER" in out

    def test_json_output_parses_and_carries_answers(
        self,
        base_config_toml,
        session_env,
        proxy_file,
        write_config,
        monkeypatch,
        capsys,
    ):
        monkeypatch.setattr(
            "insta_report.preflight.preflight_account",
            lambda *a, **kw: a_preflight(answers=True),
        )
        config = write_config(_with_user_id(base_config_toml))
        code = _main(["--config", str(config), "--json"])
        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload[0]["answers"] is True
        assert payload[0]["account_ref"] == "alpha"

    def test_one_failing_exit_among_several_fails_the_gate(
        self,
        base_config_toml,
        session_env,
        proxy_file,
        write_config,
        monkeypatch,
    ):
        """A gate that passes when part of what it checked failed is not a
        gate -- the probe's "strongest signal wins" rule is for discovery;
        this is a decision, and decisions are conservative."""
        answers = iter([True, False])
        monkeypatch.setattr(
            "insta_report.preflight.preflight_account",
            lambda *a, **kw: a_preflight(answers=next(answers)),
        )
        config = write_config(_with_user_id(base_config_toml))
        code = _main(
            [
                "--config",
                str(config),
                "--proxy",
                "http://10.0.0.9:8080",
                "--proxy",
                "http://10.0.0.10:8080",
            ]
        )
        assert code == 1
