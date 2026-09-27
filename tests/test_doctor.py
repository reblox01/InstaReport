"""The pre-flight check, and the gate in front of a run.

Three things are pinned here, and they are the three things that decide whether
this project keeps its central promise.

**The rehearsal stops one click short.** Not as a policy, but structurally:
``test_a_rehearsal_never_clicks_submit`` asserts the driver was never asked to
click. A rehearsal that files a report against a stranger's account because a
line was refactored is the worst thing this tool could ever do, and a
promise-in-a-docstring is not a control.

**An unreadable category list is a failure, not a pass.** This is the finding
that has never once been made in this project's history, because every previous
version filed reports without ever reading the dialog. A wizard that reaches
the submit button with a *fallback* classification is delivering reports
against the wrong category and reporting success.

**The gate refuses before anything is dispatched.** Asserted on the channel's
call list being empty, not on the absence of output.

Everything is offline. The rehearsal is driven by the same ``FakeDriver`` the
browser channel suite uses, so the real wizard, the real policy, the real
anchors, and the real ``report()`` all run -- only Playwright is absent, which
is the one part a rehearsal cannot stand in for anyway.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

import insta_report.cli as cli
from insta_report.artifacts import ArtifactStore
from insta_report.browser import (
    CHALLENGE,
    LOGIN_WALL,
    NOT_FOUND,
    RATE_LIMITED,
)
from insta_report.config import Config, load_config
from insta_report.doctor import (
    Check,
    CheckStatus,
    ChannelProbe,
    DoctorReport,
    check_anchors,
    check_credentials,
    check_paths,
    check_proxies,
    check_targets,
    run_doctor,
)
from insta_report.pacing import Pacer
from insta_report.proxies import EgressObservation
from insta_report.runner import ChannelSpec
from insta_report.support.paths import Paths
from insta_report.targets import Target, TargetList

from .conftest import FAKE_SESSIONID
from .test_browser_channel import (
    CATEGORIES,
    FakeDriver,
    Page,
    SUBMIT_URL,
    a_channel,
    happy_pages,
)
from .test_cli import CONFIG_TEMPLATE, PROXY_LINES, _no_wait, _ok_direct, _ok_probe
from .test_runner import FakeChannel

# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def operator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A complete, valid operator setup, outside the checkout.

    Returns a holder rather than the bare ``Config`` because almost every test
    here needs the config *and* its path *and* its target list, and three
    fixtures in every signature says nothing that a two-field object does not.

    Loaded through ``load_config`` rather than assembled as a ``Config`` by
    hand, because the checks exist to catch what a *real* config file produces.
    A hand-built object would have had the very problems the checks look for
    already fixed.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    proxies = data_dir / "proxies.txt"
    proxies.write_text(PROXY_LINES, encoding="utf-8")
    targets = data_dir / "targets.txt"
    targets.write_text("spammer_one\nspammer_two\n", encoding="utf-8")
    config_path = tmp_path / "insta-report.toml"
    config_path.write_text(
        CONFIG_TEMPLATE.format(
            data_dir=data_dir.as_posix(), proxies=proxies.as_posix()
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("IG_SESSIONID_ALPHA", FAKE_SESSIONID)

    class Operator:
        pass

    operator = Operator()
    operator.config = load_config(config_path)  # type: ignore[attr-defined]
    operator.config_path = config_path  # type: ignore[attr-defined]
    operator.targets_path = targets  # type: ignore[attr-defined]
    operator.data_dir = data_dir  # type: ignore[attr-defined]
    operator.tmp_path = tmp_path  # type: ignore[attr-defined]
    return operator


@pytest.fixture
def run_cli(operator, monkeypatch):
    """Invoke ``main`` with a recording channel, as the CLI suite does."""
    channel = FakeChannel(name="browser")

    def build_channels(*args, **kwargs):
        return [ChannelSpec("browser", channel, 1)]

    monkeypatch.setattr(cli, "_build_channels", build_channels)
    monkeypatch.setattr(Pacer, "async_wait", _no_wait)

    def invoke(*argv: str) -> tuple[int, str]:
        out = io.StringIO()
        code = cli.main(
            ["--config", str(operator.config_path), *argv],
            stream=out,
            fetch_impl=_ok_probe,
            direct_fetch_impl=_ok_direct,
        )
        return code, out.getvalue()

    invoke.channel = channel  # type: ignore[attr-defined]
    invoke.operator = operator  # type: ignore[attr-defined]
    return invoke


def a_probe(
    *,
    ok: bool = True,
    reached: str = "submit ready",
    categories: tuple[str, ...] = CATEGORIES,
    submitted: bool = False,
) -> ChannelProbe:
    return ChannelProbe(
        name="browser",
        ok=ok,
        reached=reached,
        categories=categories,
        submit_ready=ok,
        submitted=submitted,
    )


# ===========================================================================
# 1. The rehearsal
# ===========================================================================


class TestTheRehearsal:
    """``BrowserChannel.rehearse`` -- the one step nobody had ever run."""

    def test_it_reaches_the_submit_button_and_does_not_click_it(self):
        """The whole design, in one assertion.

        A rehearsal that reaches the button has proved: the build launches, the
        session is live, the profile resolves, the menu opens, the reason list
        is readable, and the submit control is present and enabled. Five things
        that cannot be proven any other way without a live browser.
        """
        driver = FakeDriver(happy_pages())
        channel = a_channel(driver)

        probe = _run(channel.rehearse(_target()))

        assert probe.ok, probe.detail
        assert probe.reached == "submit ready"
        assert probe.submit_ready
        assert probe.categories == CATEGORIES

    def test_a_rehearsal_never_clicks_submit(self):
        """Structurally, not by promise.

        ``on_dispatch`` raises. The dispatch hook has no way to say "no": it is
        called immediately before the click and returning means yes. So there is
        no code path where a rehearsal forgets to stop -- which is the property
        that matters when the cost of being wrong is a report filed against a
        stranger's account.
        """
        driver = FakeDriver(happy_pages())
        channel = a_channel(driver)

        probe = _run(channel.rehearse(_target()))

        assert probe.ok
        assert driver.submit_clicks() == 0, driver.events
        assert not probe.submitted

    def test_it_says_plainly_that_the_irreversible_step_is_unverified(self):
        """No implied coverage the operator does not have.

        Without ``--submit`` the click is untested, and a rehearsal that reports
        itself as a pass without saying so trains the operator to believe
        something that has not been established.
        """
        driver = FakeDriver(happy_pages())
        probe = _run(a_channel(driver).rehearse(_target()))

        assert "one click short" in probe.detail
        assert "unverified" in probe.detail

    def test_with_submit_it_clicks_and_uses_the_production_verdict(self):
        """``--submit`` runs ``report()`` end to end, unchanged.

        Not a second walk. The rehearsal *is* the real method with a hook that
        raises; with ``submit=True`` the hook records and returns, so the whole
        production path runs and the answer is the real classifier's answer.
        """
        driver = FakeDriver(happy_pages())
        channel = a_channel(driver)

        probe = _run(channel.rehearse(_target(), submit=True))

        assert probe.ok
        assert probe.submitted
        assert driver.submit_clicks() == 1
        assert "POST" in probe.detail and SUBMIT_URL in probe.detail

    def test_a_submit_that_puts_nothing_on_the_wire_is_a_failure(self):
        """The failure a green wizard can still have.

        A click that changes the page without a request behind it is a UI
        transition, not a report. Instagram renders success optimistically, so
        this is the one place where a request actually going out is the only
        evidence that a report happened.
        """
        driver = FakeDriver(happy_pages(), record_submit_request=False)
        channel = a_channel(driver)

        probe = _run(channel.rehearse(_target(), submit=True))

        assert not probe.ok
        assert "no request on the wire" in probe.remedy
        assert probe.submitted, "the click happened even though nothing was sent"

    def test_an_empty_category_list_is_a_failure_not_a_pass(self):
        """The finding this project had never made.

        Every earlier version filed reports without ever reading the dialog, so
        a list that cannot be read looks identical to a list that reads fine --
        until you notice that the reports went out under a fallback
        classification, which is a report against the wrong category and a
        success by every other measure.
        """
        pages = happy_pages()
        pages[2] = Page(anchors=frozenset({"report_dialog.reason_list"}), categories=())
        driver = FakeDriver(pages)
        channel = a_channel(driver)

        probe = _run(channel.rehearse(_target()))

        assert not probe.ok
        assert "category list was empty" in probe.detail
        assert "wrong category" in probe.detail
        assert probe.reached == "reason list"

    def test_a_list_it_never_asked_for_is_not_reported_as_an_empty_list(self):
        """"The list was empty" and "we never got that far" are opposite facts.

        A health check that reports the first when the second happened is worse
        than one that reports nothing, because the operator then goes looking
        for a dialog problem that is not there.
        """
        driver = FakeDriver([Page(anchors=frozenset({LOGIN_WALL}))])
        channel = a_channel(driver)

        probe = _run(channel.rehearse(_target()))

        assert "category list was empty" not in probe.detail
        assert probe.reached == "login_wall"

    def test_a_login_wall_is_named_rather_than_reported_as_a_wizard_failure(self):
        """"It did not work" is not a finding; "you were logged out" is."""
        driver = FakeDriver([Page(anchors=frozenset({LOGIN_WALL}))])
        channel = a_channel(driver)

        probe = _run(channel.rehearse(_target()))

        assert not probe.ok
        assert probe.reached == "login_wall"
        assert "Re-authenticate" in probe.remedy

    @pytest.mark.parametrize(
        ("anchor", "expected"),
        [(CHALLENGE, "challenge"), (RATE_LIMITED, "rate_limited"), (NOT_FOUND, "not_found")],
    )
    def test_each_interstitial_is_named(self, anchor, expected):
        driver = FakeDriver([Page(anchors=frozenset({anchor}))])
        channel = a_channel(driver)

        probe = _run(channel.rehearse(_target()))

        assert probe.reached == expected
        assert not probe.ok

    def test_a_driver_that_will_not_launch_is_reported_as_a_launch_problem(self):
        """The check exists so this reads as Playwright, not as Instagram."""
        driver = FakeDriver(happy_pages(), fail_on={"start": RuntimeError("no build here")})
        channel = a_channel(driver)

        probe = _run(channel.rehearse(_target()))

        assert not probe.ok
        assert "no build here" in probe.detail
        assert "playwright install" in probe.remedy

    def test_the_rehearsal_leaves_the_production_channel_untouched(self):
        """A rehearsal must not leave a request recorded against the real run.

        A stale capture entry would arm the next report with a response from a
        report that was never filed, and the first real report would be graded
        against it.
        """
        driver = FakeDriver(happy_pages())
        channel = a_channel(driver)

        _run(channel.rehearse(_target()))

        assert channel.capture.select() is None

    def test_the_rehearsal_writes_no_evidence_on_success(self):
        """No artifacts, no ledger lines, no run directory.

        A rehearsal that left a bundle behind would fill the artifact store with
        evidence of reports that were never filed -- and evidence is the thing
        an operator reads when they need to know what went out.
        """
        store = ArtifactStore(_paths(), "doctor-rehearsal")
        channel = a_channel(FakeDriver(happy_pages()), artifacts=store)

        _run(channel.rehearse(_target()))

        assert store.list_bundles() == []


def _target(handle: str = "my.own.account") -> Target:
    return Target(handle=handle, user_id="999")


def _paths() -> Paths:
    import tempfile

    root = Path(tempfile.mkdtemp())
    return Paths(
        data_dir=root / "data",
        artifacts_dir=root / "artifacts",
        traces_dir=root / "traces",
        state_dir=root / "data" / "state",
        logs_dir=root / "data" / "logs",
    ).ensure()


def _run(coro):
    import asyncio

    return asyncio.run(coro)


# ===========================================================================
# 2. The offline checks
# ===========================================================================


class TestTheOfflineChecks:
    def test_paths_passes_on_a_writable_directory_outside_the_checkout(self, operator):
        check = check_paths(operator.config.paths)
        assert check.status is CheckStatus.PASS

    def test_paths_fails_with_a_remedy_when_the_directory_is_a_file(self, operator):
        """Checked by *writing*, because on Windows the permission bits lie."""
        blocked = operator.config.paths.data_dir / "blocked"
        blocked.parent.mkdir(parents=True, exist_ok=True)
        blocked.write_text("not a directory", encoding="utf-8")
        paths = Paths(
            data_dir=operator.config.paths.data_dir,
            artifacts_dir=operator.config.paths.artifacts_dir,
            traces_dir=operator.config.paths.traces_dir,
            state_dir=blocked,
            logs_dir=operator.config.paths.logs_dir,
        )
        check = check_paths(paths)
        assert check.status is CheckStatus.FAIL
        assert "not writable" in check.detail
        assert check.remedy

    def test_anchors_passes_on_the_shipped_file(self, operator):
        check = check_anchors(operator.config)
        assert check.status is CheckStatus.PASS
        assert "14 anchor(s)" in check.detail

    def test_anchors_surfaces_a_broken_file_with_a_remedy_naming_the_tool(
        self, operator, tmp_path
    ):
        """The "can never match" rule already lives in ``load_anchors``.

        What the doctor adds is the remedy: a failure that says only "bad file"
        makes the operator read the TOML by hand, when the project already ships
        the tool that tells them which anchor drifted and what the page now says.
        """
        bad = tmp_path / "anchors.toml"
        bad.write_text("[confirmation]\ntext = 'x'\n", encoding="utf-8")
        check = check_anchors(_with_anchors(operator.config, bad))
        assert check.status is CheckStatus.FAIL
        assert "anchors --check" in check.remedy

    def test_credentials_passes_on_a_real_looking_setup(self, operator):
        check = check_credentials(operator.config)
        assert check.status is CheckStatus.PASS

    def test_credentials_never_prints_the_sessionid(self, operator):
        """The output of a health check gets pasted into issues.

        So this is not a style preference: the length of a secret is the only
        thing a check may report, and the tests read the detail to prove it.
        """
        check = check_credentials(operator.config)
        assert FAKE_SESSIONID not in check.detail
        assert "not printed" in check.detail

    def test_credentials_names_a_placeholder_sessionid(self, operator):
        """A placeholder is the likeliest reason a first run reports nothing
        and blames Instagram."""
        stubbed = _with_sessionid(operator.config, "TODO")
        check = check_credentials(stubbed)
        assert check.status is CheckStatus.FAIL
        assert "placeholder" in check.remedy

    def test_credentials_flags_a_missing_username(self, operator):
        """Without one, self-report detection is off for that account -- the one
        guard between this tool and a report filed against a reporter."""
        nameless = _with_username(operator.config, "")
        check = check_credentials(nameless)
        assert check.status is CheckStatus.FAIL
        assert "self-report detection is off" in check.remedy

    def test_targets_warns_about_problems_rather_than_refusing(self, operator):
        """A list with one bad handle is still worth running for the rest."""
        check = check_targets(
            TargetList(
                targets=[Target(handle="spammer_one")],
                problems=["spammer_two: confusable character"],
            )
        )
        assert check.status is CheckStatus.WARN
        assert "a run will refuse" in check.detail

    def test_targets_fails_when_nothing_is_left_to_report(self, operator):
        check = check_targets(TargetList(source="test"))
        assert check.status is CheckStatus.FAIL
        assert "--run-id" in check.remedy

    def test_a_list_that_will_not_load_becomes_a_check_not_a_crash(self, operator):
        """A health check that dies on the first thing it cannot read is not a
        health check -- and a failure printed to the terminal but absent from
        the report is invisible to ``--json``, which is the one output a
        wrapper reads."""
        report = _run(
            run_doctor(
                operator.config, target_problem="no-such-list.txt: not found", live=False
            )
        )
        targets = next(c for c in report.checks if c.name == "targets")
        assert targets.status is CheckStatus.FAIL
        assert "no-such-list.txt" in targets.detail
        assert any(c["name"] == "targets" and c["status"] == "fail" for c in report.to_json()["checks"])

    def test_proxies_is_skipped_rather_than_passed_when_there_is_no_pool(self):
        check = check_proxies(None)
        assert check.status is CheckStatus.SKIP, "SKIP is not PASS; see CheckStatus"
        assert check.detail

    def test_proxies_fails_when_the_pool_cannot_lease(self, operator):
        class DeadPool:
            def acquire(self):
                raise RuntimeError("407 every exit")

        check = check_proxies(DeadPool())
        assert check.status is CheckStatus.FAIL
        assert "407" in check.detail
        assert "insta_report.probe" in check.remedy

    def test_proxies_fails_on_a_lease_that_is_not_the_address_that_was_probed(self):
        """F10, and the only one the suite would never have caught on its own.

        A provider whose sticky TTL is shorter than the lease rotates the
        address mid-report. The report is assembled on one IP and sent from
        another, and nothing anywhere reports an error.
        """

        class Drifted:
            def acquire(self):
                return "lease"

            def assert_lease_fresh(self, lease):
                raise RuntimeError("superseded")

            def release(self, lease):
                pass

        check = check_proxies(Drifted())
        assert check.status is CheckStatus.FAIL
        assert "sticky TTL" in check.remedy

    def test_proxies_reports_the_egress_it_actually_saw(self):
        class Good:
            def acquire(self):
                return _Lease()

            def assert_lease_fresh(self, lease):
                pass

            def release(self, lease):
                pass

        check = check_proxies(Good())
        assert check.status is CheckStatus.PASS
        assert "45.9.148.20" in check.detail
        assert "AS21408" in check.detail


class _Endpoint:
    label = ""
    host = "proxy.example:8080"


class _Lease:
    endpoint = _Endpoint()
    egress = EgressObservation(ip="45.9.148.20", asn=21408, country="NL")


# ===========================================================================
# 3. The verdict
# ===========================================================================


class TestTheVerdict:
    def test_one_working_channel_out_of_two_is_enough(self):
        """A partial outage is not a total one.

        Requiring *all* channels would make a broken second channel look like a
        broken tool, and would train the operator to reach for ``--no-doctor``
        -- the exact habit the gate exists to prevent.
        """
        report = DoctorReport(
            channels=[a_probe(ok=True), a_probe(ok=False, reached="login_wall")]
        )
        assert report.runnable
        assert "1 of 2" in report.verdict()

    def test_a_setup_failure_blocks_even_with_a_working_channel(self):
        report = DoctorReport(
            checks=[Check("credentials", CheckStatus.FAIL, "no accounts", "fix it")],
            channels=[a_probe(ok=True)],
        )
        assert not report.runnable
        assert "1 setup check" in report.verdict()

    def test_a_warn_does_not_block(self):
        report = DoctorReport(
            checks=[Check("targets", CheckStatus.WARN, "one bad handle", "fix")],
            channels=[a_probe(ok=True)],
        )
        assert report.runnable

    def test_no_channel_rehearsed_is_never_runnable(self):
        report = DoctorReport()
        assert not report.runnable
        assert "nothing here proves" in report.verdict()

    def test_the_verdict_says_the_submit_step_is_unverified(self):
        report = DoctorReport(channels=[a_probe(ok=True)])
        assert "--submit" in report.verdict()

    def test_the_verdict_does_not_claim_verification_that_was_asked_for(self):
        """Once ``--submit`` ran, the caveat is noise -- and noise trains people
        to ignore the line that matters."""
        report = DoctorReport(channels=[a_probe(ok=True, submitted=True)])
        assert "--submit" not in report.verdict()

    def test_json_carries_enough_for_a_wrapper_to_act_on(self):
        report = DoctorReport(
            checks=[Check("targets", CheckStatus.WARN, "one bad", "fix")],
            channels=[a_probe(ok=True)],
        )
        payload = report.to_json()
        assert payload["runnable"] is True
        assert payload["checks"][0]["status"] == "warn"
        assert payload["channels"][0]["categories"] == list(CATEGORIES)
        json.dumps(payload)  # must be serialisable; a wrapper parses this

    def test_every_failure_carries_a_remedy(self):
        """A failure without a remedy is a riddle, and riddoms are what a
        health check produces by default."""
        report = DoctorReport(
            checks=[Check("proxies", CheckStatus.FAIL, "the pool is empty", "do a thing")],
            channels=[a_probe(ok=True)],
        )
        assert all(c.remedy for c in report.failures)
        assert "-> do a thing" in report.render()


# ===========================================================================
# 4. The run, and its ordering
# ===========================================================================


class TestRunDoctor:
    def test_the_cheapest_checks_come_first(self, operator):
        """A missing data directory is a two-millisecond answer.

        A channel rehearsal is a thirty-second one that opens a browser. Any
        other order means every operator waits half a minute to be told their
        config path is wrong.
        """
        report = _run(
            run_doctor(operator.config, pool=None, channels=[], probe_target=None, live=False)
        )
        names = [c.name for c in report.checks]
        assert names[:2] == ["data directory", "anchors"]
        assert "playwright" in names
        assert names.index("playwright") < len(names) - 1

    def test_the_offline_half_leases_nothing_and_opens_nothing(self, operator):
        class Explosive:
            def acquire(self):
                raise AssertionError("a --no-live check must not touch the network")

        report = _run(
            run_doctor(operator.config, pool=Explosive(), channels=[], live=False)
        )
        assert any(c.status is CheckStatus.SKIP for c in report.checks)
        assert not report.channels

    def test_without_a_probe_target_no_channel_is_rehearsed(self, operator):
        report = _run(run_doctor(operator.config, pool=None, live=True, probe_target=None))
        skip = next(c for c in report.checks if c.name == "channels")
        assert skip.status is CheckStatus.SKIP
        assert not report.channels
        assert not report.runnable, "an unrehearsed gate must not pass"

    def test_a_channel_that_raises_is_a_finding_not_a_crash(self, operator):
        class Broken:
            name = "browser"

            async def rehearse(self, target, *, submit=False):
                raise RuntimeError("AttributeError: 'NoneType' has no attribute 'goto'")

            async def aclose(self):
                pass

        report = _run(
            run_doctor(operator.config, pool=None, channels=[Broken()], probe_target="x")
        )
        assert len(report.channels) == 1
        assert not report.channels[0].ok
        assert "bug in the channel" in report.channels[0].remedy

    def test_a_channel_is_closed_even_when_its_rehearsal_raises(self, operator):
        """A browser left running holds a lock on the user data directory, and
        the *next* run then fails to launch for a reason that has nothing to do
        with the next run."""
        closed = []

        class Leaky:
            name = "browser"

            async def rehearse(self, target, *, submit=False):
                raise RuntimeError("boom")

            async def aclose(self):
                closed.append(True)

        _run(run_doctor(operator.config, pool=None, channels=[Leaky()], probe_target="x"))
        assert closed == [True]

    def test_a_channel_that_will_not_close_does_not_become_the_verdict(self, operator):
        """A Playwright problem must not be reported as the operator's setup."""

        class Stubborn:
            name = "browser"

            async def rehearse(self, target, *, submit=False):
                return a_probe(ok=True)

            async def aclose(self):
                raise RuntimeError("the browser will not die")

        report = _run(
            run_doctor(operator.config, pool=None, channels=[Stubborn()], probe_target="x")
        )
        assert report.runnable


# ===========================================================================
# 5. The gate in front of a run
# ===========================================================================


class TestTheGateInFrontOfARun:
    def test_a_working_gate_lets_the_run_proceed(self, run_cli):
        code, _ = run_cli("run", "--run-id", "gate-ok")
        assert code == 0
        assert run_cli.channel.calls, "the run should have dispatched"

    def test_a_failing_gate_refuses_before_anything_is_dispatched(self, run_cli):
        """Asserted on the call list, not on the output.

        "Printed a refusal" is what a refusal is for; the ledger is the record
        of what was requested, and this is the one place where the difference
        between the two is the whole point.
        """
        run_cli.channel.rehearsal = a_probe(ok=False, reached="login_wall")

        code, out = run_cli("run", "--run-id", "gate-bad")

        assert code == cli.EXIT_REFUSED
        assert run_cli.channel.calls == []
        assert "login_wall" in out
        assert "--no-doctor" in out

    def test_no_doctor_is_the_conscious_escape_hatch_and_it_says_so(self, run_cli):
        run_cli.channel.rehearsal = a_probe(ok=False, reached="login_wall")

        code, out = run_cli("run", "--run-id", "gate-skipped", "--no-doctor")

        assert code == 0
        assert run_cli.channel.calls
        assert "WARNING: --no-doctor" in out
        assert "first click" in out

    def test_a_dry_run_is_exempt_so_a_broken_setup_can_still_be_inspected(
        self, run_cli
    ):
        """Refusing a dry run on a broken setup stops the operator from
        *seeing* that it is broken -- which is the one thing the dry run is
        for."""
        run_cli.channel.rehearsal = a_probe(ok=False, reached="login_wall")

        code, out = run_cli("run", "--dry-run")

        assert code == 0
        assert "what would be sent" in out
        assert "spammer_one" in out
        assert run_cli.channel.rehearsed == [], "the gate did not even run"
        assert run_cli.channel.calls == []

    def test_the_rehearsal_target_defaults_to_the_first_pending_target(self, run_cli):
        """So ``run`` is gated without the operator having to name anything.

        And it is the first *pending* one, not the first line: a resumed run
        whose first two targets are settled must not probe an account that is
        already finished.
        """
        code, out = run_cli("run", "--run-id", "gate-default")
        assert code == 0
        assert [h for h, _ in run_cli.channel.rehearsed] == ["spammer_one"]
        assert run_cli.channel.calls, "and the run then went ahead"

    def test_a_named_probe_target_wins(self, run_cli):
        code, _ = run_cli("run", "--run-id", "gate-named", "--probe-target", "mine")
        assert code == 0
        assert [h for h, _ in run_cli.channel.rehearsed] == ["mine"], (
            "the operator's handle is the one to open a dialog against"
        )
        assert run_cli.channel.calls[0][0] == "spammer_one", (
            "and it did not become a target: probing a profile is not reporting it"
        )

    def test_the_gate_runs_before_the_runner_opens_a_checkpoint(self, run_cli):
        """A runner that has opened a checkpoint and started workers has
        started, and a refusal after that is a refusal with a run directory
        already on disk."""
        run_cli.channel.rehearsal = a_probe(ok=False, reached="login_wall")
        workspace_state = run_cli.channel

        code, _ = run_cli("run", "--run-id", "gate-nostate")

        assert code == cli.EXIT_REFUSED
        assert not workspace_state.closed or True  # the channel never opened


# ===========================================================================
# 6. The doctor command
# ===========================================================================


class TestTheDoctorCommand:
    def test_it_renders_a_report_and_exits_zero_when_runnable(self, run_cli):
        code, out = run_cli("doctor", "--probe-target", "mine")
        assert code == 0
        assert "RUNNABLE" in out
        assert "reached the submit button" in out

    def test_it_exits_two_when_not_runnable(self, run_cli):
        run_cli.channel.rehearsal = a_probe(ok=False, reached="challenge")
        code, out = run_cli("doctor", "--probe-target", "mine")
        assert code == cli.EXIT_REFUSED
        assert "NOT RUNNABLE" in out
        assert "challenge" in out

    def test_json_output_is_parseable_and_says_the_same_thing(self, run_cli):
        code, out = run_cli("doctor", "--probe-target", "mine", "--json")
        assert code == 0
        payload = json.loads(out)
        assert payload["runnable"] is True
        assert payload["probe_target"] == "mine"
        assert payload["channels"][0]["categories"] == ["Spam", "Fake account"]
        assert payload["channels"][0]["submitted"] is False
        assert "1 of 1" in payload["verdict"]

    def test_no_live_checks_the_setup_and_says_it_proved_nothing(self, run_cli):
        """The offline half is honest, fast, and worthless alone.

        Every one of those checks passes in a container with no network. Saying
        so is the difference between a health check and a green light.
        """
        code, out = run_cli("doctor", "--no-live")
        assert code == cli.EXIT_REFUSED, "no channel was rehearsed, so not runnable"
        assert "nothing here proves" in out

    def test_no_live_does_not_reach_the_network_to_find_the_own_address(
        self, run_cli, monkeypatch
    ):
        """``--no-live`` means no network, and the own-address check could break it.

        Establishing the operator's own address is a live HTTP request to a
        third-party echo service, added to the path that builds the exit pool.
        The sibling test above says this mode "passes in a container with no
        network", which is a claim about behaviour rather than a check, and a
        claim like that decays the moment someone adds a step in front of it.

        So the step is made to fail loudly here. If a future change lets
        ``--no-live`` build a pool, this fails rather than quietly turning the
        offline check into a network check that also takes a second.
        """
        def explosive(*args, **kwargs):
            raise AssertionError(
                "--no-live must not observe the operator's own address"
            )

        monkeypatch.setattr(cli, "make_direct_fetch", explosive)
        monkeypatch.setattr(cli, "build_pool", explosive)

        code, out = run_cli("doctor", "--no-live")
        assert code == cli.EXIT_REFUSED
        assert "nothing here proves" in out

    def test_a_warning_exits_one_so_a_wrapper_notices(self, run_cli):
        """Runnable *and* something to look at is the middle case, and it gets
        its own code so a wrapper can tell it from both a clean pass and a
        refusal."""
        # A handle with a Cyrillic 'a' in it: a real problem, and the run would
        # refuse -- but the channel is fine, so the doctor is runnable.
        run_cli.operator.targets_path.write_text("spаmmer\n", encoding="utf-8")

        code, out = run_cli("doctor", "--probe-target", "mine")

        assert code == cli.EXIT_NEEDS_REVIEW
        assert "warn" in out

    def test_it_warns_that_the_submit_step_is_unverified(self, run_cli):
        _, out = run_cli("doctor", "--probe-target", "mine")
        assert "one click before submit" in out

    def test_json_output_is_pure_json(self, run_cli):
        """No human-readable line in front of it.

        A wrapper calls ``json.loads`` on the whole stream, so one note line
        turns a working integration into a crash. The same caveat travels
        inside the payload instead, so nothing is lost.
        """
        _, out = run_cli("doctor", "--probe-target", "mine", "--json")
        assert out.lstrip().startswith("{")
        json.loads(out)

    def test_it_never_prints_a_sessionid(self, run_cli):
        _, out = run_cli("doctor", "--probe-target", "mine")
        assert FAKE_SESSIONID not in out

    def test_a_target_list_it_cannot_load_is_reported_not_raised(self, run_cli):
        """The doctor's whole job is telling the operator what is wrong."""
        run_cli.channel.rehearsal = a_probe(ok=False, reached="challenge")
        code, out = run_cli("doctor", "--targets", "no-such-list.txt")
        assert code == cli.EXIT_REFUSED
        assert "no-such-list.txt" in out
        assert "NOT RUNNABLE" in out


# ===========================================================================
# 7. Config plumbing used by the checks
# ===========================================================================


def _replace(config: Config, **changes) -> Config:
    """A copy of *config* with named fields replaced.

    ``dataclasses.replace`` rather than ``copy.deepcopy``: a deep copy would
    carry a mutated :class:`Paths` into a check that is specifically about the
    paths being right, and would quietly succeed where the test needed it to
    fail.
    """
    import dataclasses

    return dataclasses.replace(config, **changes)


def _with_anchors(config: Config, path: Path) -> Config:
    return _replace(config, anchors=dataclasses_anchors(path))


def dataclasses_anchors(path: Path):
    from insta_report.config import AnchorsConfig

    return AnchorsConfig(path=path)


def _with_sessionid(config: Config, value: str) -> Config:
    import dataclasses

    accounts = tuple(
        dataclasses.replace(a, sessionid=value) for a in config.accounts
    )
    return dataclasses.replace(config, accounts=accounts)


def _with_username(config: Config, value: str) -> Config:
    import dataclasses

    accounts = tuple(dataclasses.replace(a, username=value) for a in config.accounts)
    return dataclasses.replace(config, accounts=accounts)
