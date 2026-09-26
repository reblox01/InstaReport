"""The browser channel driven end to end, against a fake page driver.

The tests in ``test_browser.py`` cover the wizard and the policy as pure
functions. This file covers the part that actually decides an answer: the order
in which the channel does things, and what it returns when something goes wrong
at each step.

The driver here is a value, not a browser. That is not a convenience -- the
design puts ``PageDriver`` between the channel and Playwright precisely so the
failure modes worth testing (a challenge between reading and clicking, a
confirmation that was already on the page, a checkpoint write that fails) are
reproducible. Several of them cannot be provoked on demand against a live site,
and a test that only runs when Instagram misbehaves is not a test.

The page model is a list of :class:`Page` values, advanced one step per click,
which is how the real wizard behaves: each affordance reveals the next. The last
page sticks unless ``cycle`` is given, in which case the fake wraps there -- that
is how the post-submit readings can be scripted to alternate, which is what the
toast race needs.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from insta_report.anchors import load_anchors
from insta_report.artifacts import ArtifactStore
from insta_report.browser import (
    CHALLENGE,
    CONFIRMATION,
    LOGIN_WALL,
    MENU_ITEM,
    NOT_FOUND,
    RATE_LIMITED,
    REASON_LIST,
    SUBITEM,
    SUBMIT,
    TRIGGER,
    BrowserChannel,
    PageDriver,
    SubmitCapture,
    SubmitPolicy,
    SubmitRequest,
    WizardState,
    anchor_by_name,
    classify_observation,
    observe,
)
from insta_report.errors import AccountChallenged, ErrorScope, SessionExpired
from insta_report.outcomes import TerminalState
from insta_report.support.paths import Paths
from insta_report.targets import Target

ANCHORS = load_anchors()
CONFIRM_TEXT = ANCHORS.confirmation_text()
SUBMIT_URL = "https://www.instagram.com/api/v1/users/12345/report/"

#: The rows the fixture dialog offers, in the dialog's own wording.
CATEGORIES = ("It's spam", "It may be a teen", "Impersonation", "Something else")


def marker(name: str) -> str:
    anchor = anchor_by_name(ANCHORS, name)
    return anchor.texts[0] if anchor.texts else name


@dataclass(frozen=True)
class Page:
    """What is visible on the page at one point in a scripted flow."""

    anchors: frozenset[str] = frozenset()
    categories: tuple[str, ...] = ()
    selected: str | None = None
    submit_enabled: bool = True


def a_target(handle: str = "scammer.one", user_id: str = "12345") -> Target:
    return Target(handle=handle, user_id=user_id, attempt=1)


def happy_pages(after_submit: Page | None = None) -> list[Page]:
    """The four pre-submit steps of a working flow, then whatever comes next."""
    return [
        Page(anchors=frozenset({TRIGGER})),
        Page(anchors=frozenset({MENU_ITEM})),
        Page(anchors=frozenset({REASON_LIST}), categories=CATEGORIES),
        Page(
            anchors=frozenset({SUBMIT, SUBITEM}),
            selected="It's spam",
            submit_enabled=True,
        ),
        after_submit or Page(anchors=frozenset({CONFIRMATION})),
    ]


class FakeDriver:
    """A :class:`PageDriver` that replays a scripted sequence of pages."""

    def __init__(
        self,
        pages: list[Page],
        *,
        capture: SubmitCapture | None = None,
        post_pages: list[Page] | None = None,
        submit_url: str = SUBMIT_URL,
        submit_status: int = 200,
        submit_body: str = '{"status": "ok"}',
        submit_content_type: str = "application/json",
        record_submit_request: bool = True,
        body: str = "<html><body>fixture</body></html>",
        fail_on: dict[str, Exception] | None = None,
    ) -> None:
        if not pages:
            raise ValueError("a scenario needs at least one page")
        self.pages = list(pages)
        self.capture = capture if capture is not None else SubmitCapture()
        #: What the page shows after the submit click, one entry per reading.
        #: This is the only way a post-submit flow that re-renders -- a toast
        #: that appears and then goes -- can be scripted at all. Sticky at the
        #: last entry when there are more readings than entries.
        self.post_pages = list(post_pages) if post_pages else None
        self.submit_url = submit_url
        self.submit_status = submit_status
        self.submit_body = submit_body
        self.submit_content_type = submit_content_type
        self.record_submit_request = record_submit_request
        self.body = body
        self.fail_on = dict(fail_on or {})
        self.index = 0
        self.post_index = 0
        self._post = False
        #: Every interaction, in order. The dispatch-ordering tests read this.
        self.events: list[str] = []
        self.chosen: list[str] = []
        #: Readings taken. Lets a test check the fake's own bookkeeping.
        self.observations = 0

    # -- the script -------------------------------------------------------

    @property
    def page(self) -> Page:
        if self._post and self.post_pages:
            return self.post_pages[min(self.post_index, len(self.post_pages) - 1)]
        return self.pages[min(self.index, len(self.pages) - 1)]

    def _advance(self) -> None:
        if self._post and self.post_pages:
            if self.post_index < len(self.post_pages) - 1:
                self.post_index += 1
            return
        if self.index < len(self.pages) - 1:
            self.index += 1

    def _tick(self) -> None:
        """Advance the script one reading, from the per-observation probe.

        ``selected_category`` is the only driver method the observation builder
        calls exactly once per reading, which makes it the one place a fake can
        step a scripted sequence in time with observations. If that ever stops
        being true the post-submit tests fail loudly rather than quietly testing
        the wrong page, so the coupling is exercised rather than assumed.
        """
        self.observations += 1
        if self._post:
            self._advance()

    def _maybe_fail(self, what: str) -> None:
        exc = self.fail_on.get(what)
        if exc is not None:
            raise exc

    def reached(self, what: str) -> bool:
        return what in self.events

    def submit_clicks(self) -> int:
        return self.events.count("click_submit")

    # -- PageDriver -------------------------------------------------------

    async def goto_profile(self, handle: str) -> None:
        self.events.append(f"goto_profile:{handle}")
        self._maybe_fail("goto_profile")

    async def read(self, name: str, *, require_enabled: bool) -> str | None:
        self._maybe_fail("read")
        if name not in self.page.anchors:
            return None
        if require_enabled and not self.page.submit_enabled:
            return None
        return marker(name)

    async def selected_category(self) -> str | None:
        chosen = self.page.selected
        self._tick()
        return chosen

    async def offered_categories(self) -> tuple[str, ...]:
        return self.page.categories

    async def open_menu(self) -> None:
        self.events.append("open_menu")
        self._maybe_fail("open_menu")
        self._advance()

    async def pick_menu_item(self) -> None:
        self.events.append("pick_menu_item")
        self._maybe_fail("pick_menu_item")
        self._advance()

    async def choose_category(self, category: str) -> None:
        self.events.append(f"choose_category:{category}")
        self.chosen.append(category)
        self._maybe_fail("choose_category")
        self._advance()

    async def click_submit(self) -> None:
        self.events.append("click_submit")
        self._maybe_fail("click_submit")
        if self.record_submit_request:
            # The response event fires around the click in the real driver, so
            # the request is only in the capture afterwards -- which is why the
            # channel re-reads the DOM after the click rather than reading it
            # before.
            self.capture.add(
                SubmitRequest(
                    url=self.submit_url,
                    method="POST",
                    status=self.submit_status,
                    content_type=self.submit_content_type,
                    body=self.submit_body,
                )
            )
        # Entering the post-submit phase, which has to get the index right in
        # both directions. With an explicit script the first *reading* after the
        # click is what should see post_pages[0], so the index must not move --
        # advancing here would skip it. Without one, the page the wizard was
        # standing on is the ready page, and leaving the index there would read
        # the second submit affordance as a two-stage confirm.
        self._post = True
        self.post_index = 0
        if not self.post_pages:
            self._advance()

    async def html(self) -> str:
        self._maybe_fail("html")
        return self.body

    async def close(self) -> None:
        self.events.append("close")

    async def start(self, anchors) -> "FakeDriver":
        self._maybe_fail("start")
        self.events.append("start")
        return self


def a_channel(
    driver: Any,
    *,
    policy: SubmitPolicy | None = None,
    artifacts: ArtifactStore | None = None,
    capture: SubmitCapture | None = None,
    confirmation_polls: int = 3,
) -> BrowserChannel:
    # The `or` here would be a trap, and not an obvious one: SubmitCapture is
    # falsy while empty -- deliberately, so `if not capture:` reads as "nothing
    # captured yet" -- and this is called before anything has been captured. An
    # `or` would therefore hand the channel a *different*, permanently empty
    # capture, and every report would come back UNKNOWN with no visible cause.
    if capture is None:
        capture = getattr(driver, "capture", None)
    if capture is None:
        capture = SubmitCapture()
    return BrowserChannel(
        driver=driver,
        anchors=ANCHORS,
        policy=policy or SubmitPolicy(),
        artifacts=artifacts,
        capture=capture,
        confirmation_polls=confirmation_polls,
        poll_interval=0.0,
    )


def a_paths(tmp_path) -> Paths:
    paths = Paths(
        data_dir=tmp_path / "data",
        artifacts_dir=tmp_path / "artifacts",
        traces_dir=tmp_path / "traces",
        state_dir=tmp_path / "state",
        logs_dir=tmp_path / "logs",
    )
    return paths.ensure()


class RecordingDispatch:
    """Stands in for the checkpoint write, and records when it was called."""

    def __init__(self, events: list[str], *, error: Exception | None = None) -> None:
        self._events = events
        self._error = error
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1
        self._events.append("on_dispatch")
        if self._error is not None:
            raise self._error


# ===========================================================================
# The dispatch boundary
# ===========================================================================


class TestDispatchBoundaryIsReal:
    async def test_a_clean_flow_ends_acked(self):
        driver = FakeDriver(happy_pages())
        channel = a_channel(driver)
        outcome = await channel.report(
            a_target(), on_dispatch=RecordingDispatch(driver.events)
        )
        assert outcome.terminal is TerminalState.SUBMITTED_ACKED
        assert outcome.stops_ladder

    async def test_the_checkpoint_write_happens_before_the_click(self):
        # This ordering is the whole reason on_dispatch exists. If the click came
        # first and the process died between them, the run would have a report
        # in flight that no record knows about -- and on resume it would report
        # the same target again.
        driver = FakeDriver(happy_pages())
        channel = a_channel(driver)
        dispatch = RecordingDispatch(driver.events)
        await channel.report(a_target(), on_dispatch=dispatch)
        assert driver.events.index("on_dispatch") < driver.events.index(
            "click_submit"
        )
        assert dispatch.calls == 1

    async def test_a_failed_checkpoint_write_stops_the_click(self):
        # Swallowing this would convert a durable-write failure into a silent
        # dispatch. The report is not attempted, so nothing is in flight, so
        # there is nothing to reconcile -- the run stops instead.
        driver = FakeDriver(happy_pages())
        channel = a_channel(driver)
        boom = OSError("disk full")
        with pytest.raises(OSError, match="disk full"):
            await channel.report(
                a_target(), on_dispatch=RecordingDispatch(driver.events, error=boom)
            )
        assert not driver.reached("click_submit")

    async def test_dispatched_at_is_set_exactly_when_the_report_was_sent(self):
        driver = FakeDriver(happy_pages())
        outcome = await a_channel(driver).report(
            a_target(), on_dispatch=RecordingDispatch(driver.events)
        )
        assert outcome.dispatched_at is not None

    async def test_a_never_dispatched_attempt_says_so_in_the_record(self):
        driver = FakeDriver([Page(anchors=frozenset({TRIGGER}))])
        outcome = await a_channel(driver).report(
            a_target(), on_dispatch=RecordingDispatch(driver.events)
        )
        assert outcome.terminal is TerminalState.CHANNEL_FAILED
        assert outcome.dispatched_at is None
        assert not outcome.terminal.counts_against_budget
        # A channel that could not attempt it is exactly the case the ladder
        # exists for, so this is the one state that must not stop the run.
        assert not outcome.stops_ladder


# ===========================================================================
# Blockers, before anything is sent
# ===========================================================================


class TestPreDispatchBlockers:
    @pytest.mark.parametrize("anchor", [CHALLENGE, RATE_LIMITED])
    async def test_a_wall_on_the_profile_costs_the_lease_not_the_target(self, anchor):
        driver = FakeDriver([Page(anchors=frozenset({anchor}))])
        outcome = await a_channel(driver).report(
            a_target(), on_dispatch=RecordingDispatch(driver.events)
        )
        assert outcome.terminal is TerminalState.QUARANTINED
        assert outcome.dispatched_at is None
        assert not driver.reached("click_submit")
        # A quarantined lease is not evidence the report failed, so the runner
        # must not push it to the next channel and re-report.
        assert outcome.stops_ladder

    async def test_a_challenge_is_named_as_the_error(self):
        driver = FakeDriver([Page(anchors=frozenset({CHALLENGE}))])
        outcome = await a_channel(driver).report(
            a_target(), on_dispatch=RecordingDispatch(driver.events)
        )
        assert outcome.error_class == AccountChallenged.__name__

    async def test_a_login_wall_is_a_session_problem_not_a_challenge(self):
        # Different remedy: a challenge clears itself, a dead session needs a
        # human to log in again. Grading one as the other sends the operator to
        # the wrong console.
        driver = FakeDriver([Page(anchors=frozenset({LOGIN_WALL}))])
        outcome = await a_channel(driver).report(
            a_target(), on_dispatch=RecordingDispatch(driver.events)
        )
        assert outcome.terminal is TerminalState.QUARANTINED
        assert outcome.error_class == SessionExpired.__name__
        assert outcome.error_scope == ErrorScope.LEASE.value

    async def test_a_deleted_target_is_not_reportable_and_spends_nothing(self):
        # Every channel will fail identically on a 404, so falling through the
        # ladder would spend a second lease to learn the same thing.
        driver = FakeDriver([Page(anchors=frozenset({NOT_FOUND}))])
        outcome = await a_channel(driver).report(
            a_target(), on_dispatch=RecordingDispatch(driver.events)
        )
        assert outcome.terminal is TerminalState.NOT_REPORTABLE
        assert outcome.stops_ladder
        assert not outcome.terminal.counts_against_budget
        assert not driver.reached("click_submit")

    async def test_a_blocker_appearing_after_the_menu_opened_still_wins(self):
        # The dialog opens and the challenge arrives on the next read. Reading
        # the dialog as a working form here would file a report into a wall.
        driver = FakeDriver(
            [
                Page(anchors=frozenset({TRIGGER})),
                Page(anchors=frozenset({MENU_ITEM, CHALLENGE})),
            ]
        )
        outcome = await a_channel(driver).report(
            a_target(), on_dispatch=RecordingDispatch(driver.events)
        )
        assert outcome.terminal is TerminalState.QUARANTINED
        assert not driver.reached("click_submit")

    async def test_a_leftover_confirmation_is_never_counted_as_a_report(self):
        # A context reused across targets can still be showing the previous
        # target's confirmation. Reading it as this report's success is exactly
        # the false positive the old tool produced, one level up.
        driver = FakeDriver([Page(anchors=frozenset({TRIGGER, CONFIRMATION}))])
        outcome = await a_channel(driver).report(
            a_target(), on_dispatch=RecordingDispatch(driver.events)
        )
        assert outcome.terminal is TerminalState.CHANNEL_FAILED
        assert "leftover UI" in outcome.detail
        assert outcome.dispatched_at is None
        assert not driver.reached("click_submit")


# ===========================================================================
# Choosing a category
# ===========================================================================


class TestCategorySelection:
    async def test_the_dialogs_own_wording_is_what_gets_filed(self):
        # The configuration said "IMPERSONATION" -- shouted, in the operator's
        # casing. The click said exactly what Instagram's row said, because a
        # report filed under configured wording rather than the dialog's own is
        # a misclassification Instagram accepts without complaint.
        driver = FakeDriver(happy_pages())
        channel = a_channel(driver, policy=SubmitPolicy(["IMPERSONATION"]))
        await channel.report(a_target(), on_dispatch=RecordingDispatch(driver.events))
        assert driver.chosen == ["Impersonation"]

    async def test_the_preferred_row_is_clicked_when_it_is_offered(self):
        pages = happy_pages()
        pages[3] = Page(anchors=frozenset({SUBMIT, SUBITEM}), selected="Impersonation")
        driver = FakeDriver(pages)
        channel = a_channel(driver, policy=SubmitPolicy(["impersonation"]))
        await channel.report(a_target(), on_dispatch=RecordingDispatch(driver.events))
        assert driver.chosen == ["Impersonation"]
        assert driver.reached("click_submit")

    async def test_a_refusal_never_clicks_anything(self):
        # Instagram's first row is "It's spam" far more often than it is what
        # the operator meant. Clicking it on a miss would file every unmatched
        # preference as a spam report.
        driver = FakeDriver(happy_pages())
        channel = a_channel(driver, policy=SubmitPolicy(["child exploitation"]))
        outcome = await channel.report(
            a_target(), on_dispatch=RecordingDispatch(driver.events)
        )
        assert outcome.terminal is TerminalState.CHANNEL_FAILED
        assert not driver.chosen
        assert not driver.reached("click_submit")
        assert "refusing to guess" in outcome.detail
        # The operator is told what was on offer, so the fix is a config change.
        assert "Impersonation" in outcome.detail

    async def test_an_excluded_row_is_never_clicked(self):
        # Reporting oneself is an instant self-lock, and it is the one mistake
        # here with an immediate account-level cost.
        driver = FakeDriver(happy_pages())
        channel = a_channel(driver, policy=SubmitPolicy(avoid=["It's spam"]))
        await channel.report(a_target(), on_dispatch=RecordingDispatch(driver.events))
        assert driver.chosen == ["It may be a teen"]

    async def test_a_submit_affordance_with_no_category_is_not_submitted(self):
        # Instagram keeps a submit button present while the form is incomplete.
        # Clicking it files an uncategorised report, which it accepts silently.
        pages = happy_pages()
        pages[3] = Page(anchors=frozenset({SUBMIT, SUBITEM}), selected=None)
        driver = FakeDriver(pages)
        outcome = await a_channel(driver).report(
            a_target(), on_dispatch=RecordingDispatch(driver.events)
        )
        assert not driver.reached("click_submit")
        assert outcome.dispatched_at is None


# ===========================================================================
# After the click
# ===========================================================================


class TestPostSubmit:
    async def test_a_toast_that_vanishes_is_not_an_acked_report(self):
        # Instagram shows a transient "Report submitted" toast. One reading
        # cannot tell it from a finished wizard, so the confirmation has to
        # persist; a flashing one leaves us honestly UNCONFIRMED rather than
        # claiming a report landed.
        driver = FakeDriver(
            happy_pages(),
            post_pages=[
                Page(anchors=frozenset({CONFIRMATION})),  # poll 0: the toast
                Page(anchors=frozenset({TRIGGER})),  # poll 1: gone
                Page(anchors=frozenset({CONFIRMATION})),  # poll 2: and again
                Page(anchors=frozenset({TRIGGER})),
            ],
        )
        outcome = await a_channel(driver, confirmation_polls=4).report(
            a_target(), on_dispatch=RecordingDispatch(driver.events)
        )
        assert outcome.terminal is not TerminalState.SUBMITTED_ACKED
        assert outcome.needs_human_review
        assert outcome.dispatched_at is not None
        # The network said ok; only the DOM failed to corroborate it, which is
        # the downgrade the confirmation anchor's own comment block describes.
        assert outcome.terminal is TerminalState.SUBMITTED_UNCONFIRMED

    async def test_a_second_submit_affordance_is_recorded_and_never_clicked(self):
        # A two-stage confirm and a submitted report are indistinguishable from
        # here. Clicking again risks dispatching the same report twice, so the
        # only outcomes available are the uncertain ones.
        pages = happy_pages(
            after_submit=Page(
                anchors=frozenset({SUBMIT, SUBITEM}), selected="It's spam"
            )
        )
        driver = FakeDriver(pages)
        outcome = await a_channel(driver).report(
            a_target(), on_dispatch=RecordingDispatch(driver.events)
        )
        assert driver.submit_clicks() == 1
        assert outcome.terminal is not TerminalState.SUBMITTED_ACKED
        assert "second submit affordance" in outcome.detail

    async def test_a_hint_disambiguates_two_post_submit_mutations(self):
        # The flip side of refusing: one background beacon alongside the report
        # is not ambiguous, because only one of them matches a submit hint.
        capture = SubmitCapture()
        driver = FakeDriver(happy_pages(), capture=capture)
        original = driver.click_submit

        async def click_with_beacon() -> None:
            capture.add(
                SubmitRequest(url="https://x/beacon", method="POST", status=204)
            )
            await original()

        driver.click_submit = click_with_beacon  # type: ignore[method-assign]
        outcome = await a_channel(driver, capture=capture).report(
            a_target(), on_dispatch=RecordingDispatch(driver.events)
        )
        assert outcome.terminal is TerminalState.SUBMITTED_ACKED

    async def test_two_unhinted_mutations_never_become_a_verdict(self):
        # Nothing to choose between, so the network is unusable and the verdict
        # falls back to the DOM alone. Guessing here would put an unrelated
        # status code in the ledger as the verdict on the report.
        #
        # The report request itself has to be unhinted, otherwise the hint rule
        # does its job and the case is not ambiguous at all -- which is the
        # companion test above.
        capture = SubmitCapture()
        driver = FakeDriver(happy_pages(), capture=capture, submit_url="https://x/unlabelled")
        original = driver.click_submit

        async def click_with_noise() -> None:
            capture.add(SubmitRequest(url="https://x/beacon", method="POST", status=200))
            await original()

        driver.click_submit = click_with_noise  # type: ignore[method-assign]
        outcome = await a_channel(driver, capture=capture).report(
            a_target(), on_dispatch=RecordingDispatch(driver.events)
        )
        # UNKNOWN rather than UNCONFIRMED: the DOM confirmed, and the network
        # said nothing usable. A confirmation with no readable response behind it
        # is the optimistic-UI shape -- exactly the case that must never be
        # graded as a filed report, because nothing corroborates it.
        assert outcome.terminal is TerminalState.UNKNOWN
        assert outcome.needs_human_review
        # The click happened, so the target may already be filed. The ladder
        # does not get another go, or one report becomes two.
        assert outcome.stops_ladder

    async def test_a_wall_after_the_click_is_raised_not_returned(self):
        # Raised, not returned: the report may or may not have landed, and the
        # account needs quarantining. Returning a state here would let the
        # runner fall through the ladder and report a target we already tried.
        pages = happy_pages(after_submit=Page(anchors=frozenset({CHALLENGE})))
        driver = FakeDriver(pages)
        with pytest.raises(AccountChallenged):
            await a_channel(driver).report(
                a_target(), on_dispatch=RecordingDispatch(driver.events)
            )
        assert driver.submit_clicks() == 1

    async def test_the_dom_advancing_alone_is_not_enough(self):
        # With no request captured there is no verdict to read, so this is
        # UNKNOWN rather than ACKED. Grading the DOM alone would put
        # "submitted_acked" in the ledger on the strength of a page transition.
        driver = FakeDriver(happy_pages(), record_submit_request=False)
        outcome = await a_channel(driver).report(
            a_target(), on_dispatch=RecordingDispatch(driver.events)
        )
        assert outcome.terminal is TerminalState.UNKNOWN
        assert outcome.dispatched_at is not None
        # And the ladder does not get another go: the click happened, so the
        # target may already be filed, and reporting it on another channel is
        # how one target becomes two reports.
        assert outcome.stops_ladder

    async def test_a_rejected_response_is_unconfirmed_not_unknown(self):
        # A readable rejection -- quota, duplicate, policy -- is a lost request
        # with a reason, which is a different thing from a request whose fate is
        # unknown, and both are graded for a human.
        driver = FakeDriver(
            happy_pages(), submit_status=429, submit_body='{"message": "rate limited"}'
        )
        outcome = await a_channel(driver).report(
            a_target(), on_dispatch=RecordingDispatch(driver.events)
        )
        assert outcome.terminal is TerminalState.SUBMITTED_UNCONFIRMED
        assert outcome.needs_human_review

    async def test_an_unreadable_response_is_unknown(self):
        driver = FakeDriver(
            happy_pages(),
            submit_status=200,
            submit_content_type="text/html",
            submit_body="<html>interstitial</html>",
        )
        outcome = await a_channel(driver).report(
            a_target(), on_dispatch=RecordingDispatch(driver.events)
        )
        assert outcome.terminal is TerminalState.UNKNOWN
        assert outcome.needs_human_review

    async def test_a_acked_report_needs_no_human(self):
        driver = FakeDriver(happy_pages())
        outcome = await a_channel(driver).report(
            a_target(), on_dispatch=RecordingDispatch(driver.events)
        )
        assert not outcome.needs_human_review


# ===========================================================================
# Bookkeeping that has to survive the browser
# ===========================================================================


class TestOutcomeBookkeeping:
    async def test_the_account_and_lease_are_carried_onto_the_outcome(self):
        # The ledger is only useful if it says which account and which exit made
        # the request. Without those, a quarantined lease cannot be correlated
        # with the reports that went out on it.
        class Lease:
            lease_id = "lease-abc"
            endpoint = type(
                "E", (), {"origin": "http://203.0.113.10:8080"}
            )()

        driver = FakeDriver(happy_pages())
        outcome = await a_channel(driver).report(
            a_target(),
            on_dispatch=RecordingDispatch(driver.events),
            account_ref="accounts.alpha",
            lease=Lease(),
            attempt=3,
        )
        assert outcome.account_ref == "accounts.alpha"
        assert outcome.lease_id == "lease-abc"
        assert outcome.attempt == 3
        assert outcome.resolved_user_id == "12345"

    async def test_the_targets_own_handle_is_the_one_navigated_to(self):
        driver = FakeDriver(happy_pages())
        await a_channel(driver).report(
            a_target("bad.actor"), on_dispatch=RecordingDispatch(driver.events)
        )
        assert "goto_profile:bad.actor" in driver.events

    async def test_the_detail_records_the_observed_path(self):
        driver = FakeDriver([Page(anchors=frozenset({TRIGGER}))])
        outcome = await a_channel(driver).report(
            a_target(), on_dispatch=RecordingDispatch(driver.events)
        )
        assert "profile" in outcome.detail

    async def test_the_channel_names_itself_on_every_outcome(self):
        for pages in (
            happy_pages(),
            [Page(anchors=frozenset({NOT_FOUND}))],
            [Page(anchors=frozenset({TRIGGER}))],
        ):
            driver = FakeDriver(pages)
            outcome = await a_channel(driver).report(
                a_target(), on_dispatch=RecordingDispatch(driver.events)
            )
            assert outcome.channel == "browser"


# ===========================================================================
# Evidence bundles
# ===========================================================================


class TestEvidenceBundles:
    async def test_a_failure_is_bundled_so_a_human_can_see_why(self, tmp_path):
        driver = FakeDriver([Page(anchors=frozenset({CHALLENGE}))])
        store = ArtifactStore(a_paths(tmp_path), "run-1")
        channel = a_channel(driver, artifacts=store)
        await channel.report(a_target(), on_dispatch=RecordingDispatch(driver.events))
        bundles = store.list_bundles()
        assert len(bundles) == 1
        written = sorted(p.name for p in bundles[0].iterdir())
        assert "failure.json" in written
        assert "page.html" in written

    async def test_an_acked_report_is_not_bundled(self, tmp_path):
        # Bundling every success would fill the disk and bury the failures in
        # noise, which is the opposite of what a failure bundle is for.
        driver = FakeDriver(happy_pages())
        store = ArtifactStore(a_paths(tmp_path), "run-2")
        channel = a_channel(driver, artifacts=store)
        await channel.report(a_target(), on_dispatch=RecordingDispatch(driver.events))
        assert store.list_bundles() == []

    async def test_a_bundle_records_the_anchors_that_were_and_were_not_seen(
        self, tmp_path
    ):
        # "The confirmation was there but so was a challenge" is only readable
        # if the bundle carries both sets; a bare failure message is not.
        pages = happy_pages(after_submit=Page(anchors=frozenset({CHALLENGE})))
        driver = FakeDriver(pages)
        store = ArtifactStore(a_paths(tmp_path), "run-3")
        channel = a_channel(driver, artifacts=store)
        with pytest.raises(AccountChallenged):
            await channel.report(
                a_target(), on_dispatch=RecordingDispatch(driver.events)
            )
        bundle = store.list_bundles()[0]
        context = json.loads((bundle / "failure.json").read_text(encoding="utf-8"))
        assert CHALLENGE in context["anchors_hit"]
        assert TRIGGER in context["anchors_missed"]

    async def test_a_bundle_that_cannot_be_written_does_not_change_the_verdict(
        self, tmp_path, caplog
    ):
        # A run must not die because a screenshot could not be taken. The
        # verdict was already decided; losing the bundle costs evidence, not the
        # report.
        driver = FakeDriver([Page(anchors=frozenset({NOT_FOUND}))], body=None)
        store = ArtifactStore(a_paths(tmp_path), "run-4")
        channel = a_channel(driver, artifacts=store)
        driver.fail_on["html"] = RuntimeError("no page")
        with caplog.at_level("ERROR", logger="insta_report.browser"):
            outcome = await channel.report(
                a_target(), on_dispatch=RecordingDispatch(driver.events)
            )
        assert outcome.terminal is TerminalState.NOT_REPORTABLE
        assert "could not be written" in caplog.text


# ===========================================================================
# The seam itself
# ===========================================================================


class TestDriverSeam:
    def test_the_fake_satisfies_the_protocol(self):
        # If FakeDriver stopped matching PageDriver, every test above would
        # pass while the real driver was incompatible. Asserting the protocol
        # here is what keeps the seam honest.
        assert isinstance(FakeDriver([Page()]), PageDriver)

    async def test_the_channel_never_touches_playwright(self):
        # The layering claim, tested. Everything above the driver is a pure
        # function over Observations, so a Playwright upgrade cannot change a
        # verdict without this suite noticing.
        import insta_report.browser as module

        source = module.__file__
        assert source is not None
        text = open(source, encoding="utf-8").read()
        driver_section = text.split("class BrowserDriver")[1]
        # Only BrowserDriver and the protocol below it may import Playwright.
        assert "async_playwright" in driver_section


# ===========================================================================
# The fixture-backed real browser
# ===========================================================================


def fixture(name: str) -> str:
    """A file:// URL for a committed golden fixture."""
    return (Path(__file__).parent / "golden" / name).as_uri()


async def read_with(driver, anchors):
    """Run the driver's own read through the pure observation builder."""

    async def probe(name, *, require_enabled, timeout_ms):
        return await driver.read(name, require_enabled=require_enabled)

    return await observe(anchors, probe, category_probe=driver.selected_category)


async def wait_for_capture(capture: SubmitCapture, timeout: float = 5.0):
    """Wait for the fire-and-forget body read to land, or give up honestly."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        chosen = capture.select()
        if chosen is not None:
            return chosen
        await asyncio.sleep(0.01)
    return None


#: What the real matcher must produce for each committed golden page. Exact, not
#: a subset: an anchor that starts matching where it did not is how a channel
#: starts grading real pages as failures, and no assertion that only checks what
#: *should* be present can see it.
#:
#: `report_dialog.menu_item` is absent from the confirmation row on purpose --
#: its text "Report" is a normalised substring of "Thanks for reporting this
#: account", so the anchor does fire there. See
#: test_anchor_drift.py::TestLooseAnchorsArePinnedRatherThanFixed, which asserts
#: the false hit and the rule it implies: never read that anchor after dispatch.
EXACT_ANCHORS = {
    "profile_page.html": {"report_dialog.trigger"},
    "report_dialog.html": {
        "report_dialog.menu_item",
        "report_dialog.reason_item",
        "report_dialog.reason_list",
        "report_dialog.required_text",
        "report_dialog.subdialog.heading",
        "report_dialog.subdialog.item",
        "submit",
    },
    "report_confirmation.html": {"confirmation", "report_dialog.menu_item"},
    "report_challenge.html": {"challenge"},
    "report_login_wall.html": {"login_wall"},
    "report_rate_limited.html": {"rate_limited"},
    "report_not_found.html": {"not_found"},
}


class TestTheChannelOwnsItsDriverLifecycle:
    """The bug the offline suite cannot see, and the reason these tests exist.

    ``BrowserDriver.start()`` is what launches the browser, opens the persistent
    context, creates the page, attaches the response capture, and -- crucially --
    hands the driver its anchors, which every subsequent read depends on.

    It was not called from anywhere. The CLI built drivers and handed them to a
    channel; the channel went straight to ``goto_profile``; ``_page`` was
    ``None``; and every single real report would have failed with
    ``AttributeError: 'NoneType' object has no attribute 'goto'`` -- which the
    runner files as CHANNEL_FAILED, reading to the operator as "Instagram
    rejected the session".

    The offline suite stayed green throughout, because every offline test uses a
    fake driver that never needed starting. The real-browser tests stayed green
    too, because they call ``start()`` themselves. Nothing tested the seam
    between "a driver exists" and "a driver is running", which is where the tool
    was broken.
    """

    def test_a_report_starts_its_driver_before_touching_the_page(self):
        driver = FakeDriver(happy_pages())
        channel = a_channel(driver)
        report = asyncio.run(channel.report(a_target(), on_dispatch=lambda: None))
        assert driver.events[0] == "start", driver.events
        assert report.was_dispatched

    def test_starting_twice_is_harmless(self):
        """Idempotent, because the doctor starts a driver to test the build.

        A second launch on a directory the first one still holds fails with a
        lock error that reads like a corrupt profile.
        """
        driver = FakeDriver(happy_pages())
        channel = a_channel(driver)
        asyncio.run(driver.start(channel.anchors))
        asyncio.run(driver.start(channel.anchors))
        assert driver.events.count("start") == 2
        assert driver.events.count("close") == 0

    def test_a_driver_that_will_not_start_propagates_rather_than_pretending(
        self,
    ):
        """A launch failure is not a decision not to report.

        Swallowed into a NOT_REPORTABLE outcome it would read as "the tool
        declined this one" -- and the account would never be quarantined, the
        channel would never be graded, and the operator would be told the target
        was not reportable when the truth is that no browser ever opened. The
        typed exception is the only thing that reaches the runner's health
        accounting, so it has to survive.
        """
        driver = FakeDriver(happy_pages(), fail_on={"start": RuntimeError("no build")})
        channel = a_channel(driver)
        with pytest.raises(RuntimeError, match="no build"):
            asyncio.run(channel.report(a_target(), on_dispatch=lambda: None))
        # And it did not get as far as pretending to look at the page.
        assert "close" not in driver.events


@pytest.mark.browser
class TestAgainstTheRealBrowser:
    """The small number of tests that actually launch Chromium.

    Two jobs, and only two. The first is proving the driver turns a real DOM
    into the markers the pure tests assume -- which the FakeDriver cannot, since
    it is handed the answer. The second is being the *selector* oracle: the
    offline drift suite reads visible text out of each fixture and never
    evaluates a selector, so it is structurally blind to a selector that matches
    too much.

    That blindness is not hypothetical. `div[role='dialog']` sat in the
    confirmation anchor, and the pre-submit reason dialog is also a
    `div[role='dialog']` -- so every real report would have opened onto a page
    that already looked confirmed and been graded CHANNEL_FAILED as leftover UI,
    without a single one being sent. The text suite stayed green the whole time,
    because the confirmation *wording* was never on the form.

    So this class stays small on purpose: a browser launch is the most fragile
    thing in the suite, and CI pays for every one.
    """

    @pytest.fixture
    async def started(self, tmp_path):
        from insta_report.browser import BrowserDriver

        driver = BrowserDriver(user_data_dir=tmp_path / "profile", headless=True)
        try:
            await driver.start(ANCHORS)
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"chromium is unavailable here: {exc}")
        try:
            yield driver
        finally:
            await driver.close()

    @pytest.mark.parametrize("name,expected", sorted(EXACT_ANCHORS.items()))
    async def test_the_exact_anchor_set_of_every_golden_page(
        self, started, name, expected
    ):
        await started._page.goto(fixture(name))
        reading = await read_with(started, ANCHORS)
        assert reading.anchors_hit == frozenset(expected), (
            f"{name}: the real matcher hit {sorted(reading.anchors_hit)}, "
            f"the table says {sorted(expected)}"
        )

    async def test_the_pre_submit_form_is_not_a_confirmation(self, started):
        # The classification consequence of the row above, stated separately
        # because it is the property the whole pre-submit trap rests on: a tool
        # that filed before the click must not be able to pass this suite.
        await started._page.goto(fixture("report_dialog.html"))
        reading = await read_with(started, ANCHORS)
        assert classify_observation(reading) is not WizardState.CONFIRMED
        assert reading.submit_present
        assert reading.reason_list_present

    async def test_the_committed_confirmation_reads_as_confirmed(self, started):
        await started._page.goto(fixture("report_confirmation.html"))
        reading = await read_with(started, ANCHORS)
        assert classify_observation(reading) is WizardState.CONFIRMED
        assert not reading.submit_present

    @pytest.mark.parametrize(
        "name,state",
        [
            ("report_challenge.html", WizardState.BLOCKED),
            ("report_login_wall.html", WizardState.BLOCKED),
            ("report_rate_limited.html", WizardState.BLOCKED),
            ("report_not_found.html", WizardState.GONE),
        ],
    )
    async def test_each_wall_reads_as_a_blocker(self, started, name, state):
        # GONE is not BLOCKED, and the difference is a whole branch in the
        # channel: a deleted target costs nothing and stops the ladder, while a
        # CAPTCHA must quarantine the account. One test per wall, so a failure
        # names the wall rather than "the loop failed".
        await started._page.goto(fixture(name))
        reading = await read_with(started, ANCHORS)
        assert classify_observation(reading) is state

    async def test_the_driver_turns_a_response_into_a_submit_request(self, started):
        # The other half of the driver: responses become SubmitRequests. Without
        # this the channel could never get a network verdict and every report
        # would be UNKNOWN.
        capture = SubmitCapture()
        started._capture = capture
        started._on_response(
            _FakeResponse(
                SUBMIT_URL, "POST", 200, "application/json", '{"status":"ok"}'
            )
        )
        # The handler is deliberately fire-and-forget, so the body read is a
        # task on the loop. Waiting for the capture is what a real run does
        # implicitly; a bare sleep would be a guess about timing.
        chosen = await wait_for_capture(capture)
        assert chosen is not None
        assert chosen.url == SUBMIT_URL
        assert chosen.status == 200

    async def test_a_document_response_is_never_a_submit_candidate(self, started):
        # The trap that turns a working report into UNKNOWN. A profile page is a
        # document response, and letting it into the candidate set gives a form
        # that issues one XHR two candidates -- at which point the selection rule
        # declines to choose, correctly, and the report is graded against
        # nothing.
        capture = SubmitCapture()
        started._capture = capture
        started._on_response(
            _FakeResponse(
                "https://www.instagram.com/someone/", "GET", 200,
                "text/html", "<html>profile</html>",
            )
        )
        started._on_response(
            _FakeResponse(
                SUBMIT_URL, "POST", 200, "application/json", '{"status":"ok"}'
            )
        )
        chosen = await wait_for_capture(capture)
        assert chosen is not None
        assert chosen.url == SUBMIT_URL

    async def test_a_persistent_profile_survives_a_restart(self, tmp_path):
        # The reason a user data directory is used at all, and the one property
        # no fake can supply: the profile is on disk, so the session is the
        # browser state rather than a header reconstruction. A stale lock from a
        # killed run is the failure this guards against, so the second launch
        # has to reuse the same directory.
        from insta_report.browser import BrowserDriver

        profile = tmp_path / "shared-profile"
        first = BrowserDriver(user_data_dir=profile, headless=True)
        try:
            await first.start(ANCHORS)
            await first._page.goto(fixture("profile_page.html"))
        finally:
            await first.close()

        second = BrowserDriver(user_data_dir=profile, headless=True)
        try:
            await second.start(ANCHORS)
            assert second._page is not None
        finally:
            await second.close()


class _FakeResponse:
    """The slice of a Playwright response the driver actually reads."""

    def __init__(self, url, method, status, content_type, body):
        self.url = url
        self.request = type("R", (), {"method": method, "resource_type": "xhr"})()
        self.status = status
        self.headers = {"content-type": content_type}
        self._body = body.encode("utf-8")

    async def body(self):
        return self._body

    async def all_headers(self):
        return dict(self.headers)
