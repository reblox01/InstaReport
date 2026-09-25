"""The browser channel's state machine, its submit capture, and its policy.

Every test here runs without a browser. That is the point of the layering in
``browser.py``: the wizard, the classifier, and the capture are pure functions
over :class:`~insta_report.browser.Observation` values, and the traps the design
names -- pre-submit anchor, multi-step transition, toast race, normalisation --
all live in them. A test that needs a browser to prove a set difference is a test
that will not run in CI.

The fixtures below build a page as a *value* and read anchors through the real
committed anchor file, so a rename in ``anchors.toml`` breaks these tests exactly
as it would break the drift test. Nothing here reimplements anchor matching.
"""

from __future__ import annotations

import pytest

from insta_report.anchors import load_anchors
from insta_report.browser import (
    ACCOUNT_BLOCKERS,
    BLOCKERS,
    CHALLENGE,
    CONFIRMATION,
    LOGIN_WALL,
    MENU_ITEM,
    NOT_FOUND,
    PENDING,
    RATE_LIMITED,
    REASON_LIST,
    REQUIRED_TEXT,
    SUBITEM,
    SUBMIT,
    TARGET_BLOCKERS,
    TRIGGER,
    Action,
    Observation,
    ReportWizard,
    SubmitCapture,
    SubmitPolicy,
    SubmitRequest,
    WizardState,
    anchor_by_name,
    classify_blocked,
    classify_observation,
    observe,
    submit_url_hints,
)
from insta_report.errors import SelectorDrift
from insta_report.outcomes import TerminalState, classify_browser

ANCHORS = load_anchors()
CONFIRM_TEXT = ANCHORS.confirmation_text()


def marker(name: str) -> str:
    """The marker a driver returns for a satisfied anchor.

    Read off the real anchor file rather than written out here, so the strings
    the classifier compares are the strings Instagram's own anchor definitions
    would produce. A test that hardcoded them would keep passing after someone
    edited the confirmation wording in the TOML -- which is precisely the drift
    the drift test exists to catch, and it would have caught it here too.
    """
    anchor = anchor_by_name(ANCHORS, name)
    return anchor.texts[0] if anchor.texts else name


def page(
    *names: str,
    selected: str | None = None,
    at: float = 0.0,
) -> Observation:
    """One reading of a page where exactly *names* are visible.

    The three derived booleans are filled in the way ``observe`` would, rather
    than left for the caller. Leaving them to the caller would mean a test could
    build a reading claiming a menu is open while the menu anchor missed, and
    every assertion built on such a reading would be testing the test's own
    mistake.
    """
    return Observation(
        anchors_hit=frozenset(names),
        anchor_texts=frozenset(marker(n) for n in names),
        menu_open=MENU_ITEM in names,
        reason_list_present=REASON_LIST in names,
        category_selected=selected,
        submit_present=SUBMIT in names,
        at=at,
    )


# ===========================================================================
# 1. Reading one page
# ===========================================================================


class TestClassifyObservation:
    def test_a_page_with_nothing_on_it_is_empty(self):
        assert classify_observation(page()) is WizardState.EMPTY

    def test_the_trigger_alone_reads_as_a_profile(self):
        assert classify_observation(page(TRIGGER)) is WizardState.PROFILE

    def test_the_open_menu_reads_as_the_menu(self):
        assert classify_observation(page(MENU_ITEM)) is WizardState.MENU_OPEN

    def test_the_reason_list_reads_as_the_reason_dialog(self):
        assert classify_observation(page(REASON_LIST)) is WizardState.REASON_DIALOG

    def test_a_selected_category_without_submit_is_a_subdialog(self):
        # A category is chosen but the form is not submittable yet. Reaching
        # READY here would mean clicking submit on a page that cannot submit.
        assert (
            classify_observation(page(SUBITEM, selected="Spam"))
            is WizardState.SUBDIALOG
        )

    def test_category_and_submit_together_is_ready(self):
        assert (
            classify_observation(page(SUBMIT, SUBITEM, selected="Spam"))
            is WizardState.READY
        )

    def test_the_confirmation_anchor_reads_as_confirmed(self):
        assert classify_observation(page(CONFIRMATION)) is WizardState.CONFIRMED

    @pytest.mark.parametrize("anchor", [CHALLENGE, LOGIN_WALL, RATE_LIMITED])
    def test_account_side_blockers_read_as_blocked(self, anchor):
        assert classify_observation(page(anchor)) is WizardState.BLOCKED

    def test_a_missing_target_reads_as_gone_not_blocked(self):
        # GONE and BLOCKED are separate because they call for opposite handling:
        # a deleted account is a fact about the target and no other channel will
        # do better, while a challenge is a fact about the lease. Collapsing them
        # would quarantine every deleted account or fail to quarantine a CAPTCHA.
        assert classify_observation(page(NOT_FOUND)) is WizardState.GONE

    def test_a_blocker_beats_a_confirmation_on_the_same_page(self):
        # The dangerous direction. If a challenge could be read as a
        # confirmation, a CAPTCHA would be graded as a filed report.
        assert classify_observation(page(CHALLENGE, CONFIRMATION)) is WizardState.BLOCKED

    def test_a_blocker_beats_a_ready_dialog(self):
        # Equally dangerous: a dialog that looks submittable while a challenge is
        # up is a dialog whose submit button leads to a wall.
        state = classify_observation(
            page(CHALLENGE, SUBMIT, SUBITEM, selected="Spam")
        )
        assert state is WizardState.BLOCKED

    def test_a_gone_target_beats_a_confirmation(self):
        assert classify_observation(page(NOT_FOUND, CONFIRMATION)) is WizardState.GONE

    def test_a_confirmation_beats_a_pending_dialog_anchor(self):
        # Both on one page is contradictory, and the confirmation is the more
        # consequential claim, so it is the one that survives.
        assert (
            classify_observation(page(CONFIRMATION, SUBITEM))
            is WizardState.CONFIRMED
        )

    def test_blockers_and_pending_partition_the_anchor_table(self):
        # Guards the constants against each other. If a future anchor were added
        # to BLOCKERS and to PENDING, the precedence rules above would silently
        # depend on list order rather than on intent.
        assert not set(BLOCKERS) & set(PENDING)
        assert set(ACCOUNT_BLOCKERS) | set(TARGET_BLOCKERS) == set(BLOCKERS)
        assert not set(ACCOUNT_BLOCKERS) & set(TARGET_BLOCKERS)

    def test_classify_blocked_ignores_anchors_it_does_not_understand(self):
        assert classify_blocked({CONFIRMATION, SUBMIT, "some.new.anchor"}) == frozenset()


class TestObserve:
    """The anchor-checking read, which is the only thing touching the driver."""

    async def test_it_reports_the_complete_set_not_just_the_first_hit(self):
        # A reading that stopped at the first hit could not say "a challenge
        # appeared while the confirmation was still on screen", which is the
        # case where stopping early costs most.
        wanted = {CHALLENGE, CONFIRMATION, SUBMIT}

        async def probe(name, *, require_enabled, timeout_ms):
            return marker(name) if name in wanted else None

        reading = await observe(ANCHORS, probe, at=7.5)
        assert reading.anchors_hit == wanted
        # The marker set carries texts, not names. Asserting on CONFIRM_TEXT
        # rather than CONFIRMATION is the point: the classifier is handed
        # strings, so a test that checked the name would pass even if the
        # marker plumbing were wrong.
        assert CONFIRM_TEXT in reading.anchor_texts
        assert reading.at == 7.5

    async def test_a_probe_that_raises_counts_as_a_miss_not_a_crash(self):
        # The page is mid-navigation constantly. A probe that raises must cost
        # one anchor, not the whole reading -- and it must not be mistaken for a
        # blocker, which would end the run.
        async def probe(name, *, require_enabled, timeout_ms):
            if name == SUBMIT:
                raise RuntimeError("execution context was destroyed")
            return marker(name) if name == TRIGGER else None

        reading = await observe(ANCHORS, probe)
        assert reading.anchors_hit == frozenset({TRIGGER})
        assert classify_observation(reading) is WizardState.PROFILE

    async def test_it_passes_the_anchors_own_enabled_requirement_through(self):
        # Instagram keeps a disabled submit button on the page while the form is
        # incomplete. If the requirement were dropped, an unsubmittable dialog
        # would read as READY and the run would click into nothing.
        seen: list[tuple[str, bool]] = []

        async def probe(name, *, require_enabled, timeout_ms):
            seen.append((name, require_enabled))
            return None

        await observe(ANCHORS, probe)
        requirements = dict(seen)
        assert requirements[SUBMIT] is True
        assert requirements[CONFIRMATION] is False
        assert requirements[TRIGGER] is False

    async def test_the_submit_affordance_and_the_reason_list_are_derived(self):
        # Deriving these from the hit set rather than asking separately is what
        # keeps Observation internally consistent: there is no way to construct
        # a reading that claims a menu is open while the menu anchor missed.
        async def probe(name, *, require_enabled, timeout_ms):
            return marker(name) if name in {MENU_ITEM, REASON_LIST, SUBMIT} else None

        reading = await observe(ANCHORS, probe)
        assert reading.menu_open is True
        assert reading.reason_list_present is True
        assert reading.submit_present is True

    async def test_the_category_probe_result_becomes_the_selected_category(self):
        async def probe(name, *, require_enabled, timeout_ms):
            return marker(name) if name in {SUBMIT, SUBITEM} else None

        async def category():
            return "It's spam"

        reading = await observe(ANCHORS, probe, category_probe=category)
        assert reading.category_selected == "It's spam"
        assert classify_observation(reading) is WizardState.READY

    async def test_a_category_probe_that_raises_leaves_no_category(self):
        async def probe(name, *, require_enabled, timeout_ms):
            return marker(name) if name == SUBMIT else None

        async def category():
            raise RuntimeError("detached from page")

        reading = await observe(ANCHORS, probe, category_probe=category)
        assert reading.category_selected is None
        # Submit present but no category: not READY. A submit affordance with
        # nothing selected is exactly the state a report gets filed from by
        # mistake, so it must not read as ready.
        assert classify_observation(reading) is not WizardState.READY

    def test_an_unknown_anchor_name_is_drift_not_a_silent_miss(self):
        with pytest.raises(SelectorDrift, match="no anchor named"):
            anchor_by_name(ANCHORS, "report_dialog.invented")


# ===========================================================================
# 2. The wizard
# ===========================================================================


class TestWizardPath:
    def test_a_fresh_wizard_waits_rather_than_clicking_into_nothing(self):
        assert ReportWizard().action() is Action.WAIT

    def test_the_happy_path_reaches_submit_through_every_affordance(self):
        wizard = ReportWizard()
        seen: list[tuple[WizardState, Action]] = []
        for reading in (
            page(TRIGGER),
            page(MENU_ITEM),
            page(REASON_LIST),
            page(SUBMIT, SUBITEM, selected="It's spam"),
        ):
            seen.append((wizard.observe(reading), wizard.action()))
        assert seen == [
            (WizardState.PROFILE, Action.OPEN_MENU),
            (WizardState.MENU_OPEN, Action.PICK_MENU_ITEM),
            (WizardState.REASON_DIALOG, Action.CHOOSE_CATEGORY),
            (WizardState.READY, Action.SUBMIT),
        ]

    def test_a_dialog_with_no_reason_list_still_reaches_ready(self):
        # Instagram sometimes goes straight from the menu entry to submit. A
        # machine that required the full sequence would fail a working flow.
        wizard = ReportWizard()
        wizard.observe(page(TRIGGER))
        wizard.observe(page(MENU_ITEM))
        wizard.observe(page(SUBMIT, SUBITEM, selected="Spam"))
        assert wizard.state is WizardState.READY
        assert wizard.action() is Action.SUBMIT

    def test_a_terminal_state_offers_no_action_at_all(self):
        wizard = ReportWizard()
        wizard.observe(page(CONFIRMATION))
        assert wizard.action() is Action.NONE

    def test_the_budget_is_a_real_bound_not_a_formality(self):
        wizard = ReportWizard(max_observations=4)
        for index in range(3):
            wizard.observe(page(TRIGGER, at=float(index)))
        assert not wizard.budget_exhausted()
        wizard.observe(page(TRIGGER, at=3.0))
        assert wizard.budget_exhausted()

    def test_a_budget_too_small_to_hold_the_flow_is_refused_at_construction(self):
        # Below four there is no room for a profile, a menu, a dialog, and one
        # post-submit reading. A wizard that gave up on a working flow would look
        # exactly like a channel failure, so it cannot be configured into one.
        with pytest.raises(ValueError, match="max_observations"):
            ReportWizard(max_observations=3)


class TestTransitionRecords:
    def test_appeared_is_measured_against_what_the_wizard_already_knew(self):
        # If this were computed after merging, the first reading of every page
        # would look like a transition, and "it advanced" would be unfalsifiable.
        wizard = ReportWizard()
        first = wizard.observe(page(TRIGGER))
        assert first is WizardState.PROFILE
        assert wizard.transitions[0]["appeared"] == [TRIGGER]

        wizard.observe(page(TRIGGER, MENU_ITEM))
        assert wizard.transitions[1]["appeared"] == [MENU_ITEM]

    def test_a_repeated_anchor_is_not_reported_as_appearing_again(self):
        wizard = ReportWizard()
        wizard.observe(page(TRIGGER))
        wizard.observe(page(TRIGGER))
        assert wizard.transitions[1]["appeared"] == []

    def test_disappeared_names_anchors_that_left_the_page(self):
        wizard = ReportWizard()
        wizard.observe(page(TRIGGER, MENU_ITEM))
        wizard.observe(page(REASON_LIST))
        assert wizard.transitions[1]["disappeared"] == [MENU_ITEM, TRIGGER]

    def test_the_boundary_record_is_not_a_reading(self):
        # It shares the "to" field, so leaving it in path() makes one READY look
        # like two -- which reads as the multi-step trap on a clean flow.
        wizard = ReportWizard()
        wizard.observe(page(TRIGGER))
        wizard.declare_dispatch()
        wizard.observe(page(REASON_LIST, at=1))
        assert wizard.path() == ["profile", "reason_dialog"]
        assert any(t.get("dispatched") for t in wizard.transitions)


class TestDispatchBoundary:
    def test_it_is_set_once_and_never_cleared(self):
        wizard = ReportWizard()
        wizard.observe(page(SUBMIT, SUBITEM, selected="Spam"))
        assert not wizard.dispatched
        wizard.declare_dispatch()
        wizard.declare_dispatch()
        wizard.declare_dispatch()
        assert wizard.dispatched

    def test_only_one_boundary_record_is_written_however_often_it_is_called(self):
        wizard = ReportWizard()
        wizard.observe(page(TRIGGER))
        for _ in range(4):
            wizard.declare_dispatch()
        boundaries = [t for t in wizard.transitions if t.get("dispatched")]
        assert len(boundaries) == 1

    def test_anchors_seen_before_the_click_are_frozen_at_the_boundary(self):
        # The trap: if the confirmation text was already on the page before
        # submit, matching it after submit proves nothing. The snapshot has to be
        # taken before the click, because after it the DOM may already hold the
        # answer the snapshot exists to be tested against.
        wizard = ReportWizard()
        wizard.observe(page(TRIGGER, CONFIRMATION))
        wizard.declare_dispatch()
        wizard.observe(page(CONFIRMATION, SUBMIT, at=1))
        assert CONFIRM_TEXT in wizard.pre_submit
        # The reading after the click did not widen the pre-submit snapshot.
        assert wizard.pre_submit == frozenset({marker(TRIGGER), CONFIRM_TEXT})

    def test_the_pre_submit_snapshot_keeps_marker_texts_not_anchor_names(self):
        # The classifier compares marker texts. Mixing names in here would make a
        # renamed anchor look like a clean page -- drift would read as success.
        #
        # REQUIRED_TEXT is used because it is configured with real wording. An
        # anchor with no texts is its own marker, so it could not distinguish the
        # two schemes and the test would pass either way.
        assert marker(REQUIRED_TEXT) != REQUIRED_TEXT, (
            "this test is only meaningful while the anchor has configured text"
        )
        wizard = ReportWizard()
        wizard.observe(page(REQUIRED_TEXT))
        wizard.declare_dispatch()
        assert REQUIRED_TEXT not in wizard.pre_submit
        assert marker(REQUIRED_TEXT) in wizard.pre_submit


class TestPreSubmitAnchorTrap:
    def test_confirmation_present_before_submit_does_not_count_as_advancing(self):
        wizard = ReportWizard()
        wizard.observe(page(TRIGGER, CONFIRMATION))
        wizard.declare_dispatch()
        wizard.observe(page(CONFIRMATION, at=1))
        wizard.observe(page(CONFIRMATION, at=2))
        assert not wizard.evidence().dom_advanced

    def test_confirmation_absent_before_and_present_after_does_advance(self):
        wizard = ReportWizard()
        wizard.observe(page(TRIGGER))
        wizard.declare_dispatch()
        wizard.observe(page(CONFIRMATION, at=1))
        wizard.observe(page(CONFIRMATION, at=2))
        assert wizard.evidence().dom_advanced


class TestConfirmationStability:
    """The toast race.

    Instagram shows a transient "Report submitted" toast that satisfies the
    confirmation anchor and then vanishes. One reading cannot tell that apart
    from the wizard having genuinely finished, so the confirmation has to be seen
    twice in a row.
    """

    def test_one_reading_of_confirmation_is_not_stable(self):
        wizard = ReportWizard()
        wizard.observe(page(TRIGGER))
        wizard.declare_dispatch()
        wizard.observe(page(CONFIRMATION, at=1))
        assert not wizard.confirmation_stable

    def test_two_consecutive_readings_are_stable(self):
        wizard = ReportWizard()
        wizard.observe(page(TRIGGER))
        wizard.declare_dispatch()
        wizard.observe(page(CONFIRMATION, at=1))
        wizard.observe(page(CONFIRMATION, at=2))
        assert wizard.confirmation_stable

    def test_a_toast_that_vanishes_never_becomes_stable(self):
        wizard = ReportWizard()
        wizard.observe(page(TRIGGER))
        wizard.declare_dispatch()
        wizard.observe(page(CONFIRMATION, at=1))
        wizard.observe(page(TRIGGER, at=2))
        wizard.observe(page(CONFIRMATION, at=3))
        wizard.observe(page(TRIGGER, at=4))
        assert not wizard.confirmation_stable

    def test_the_streak_resets_across_a_non_confirmation_reading(self):
        # A dialog that went back to READY between two confirmations is a
        # multi-step flow, not a stable toast. A page carrying *both* the
        # submit affordance and the confirmation would not do here: the
        # confirmation is ranked above pending anchors, so such a page reads as
        # CONFIRMED and the streak legitimately survives it.
        wizard = ReportWizard()
        wizard.observe(page(TRIGGER))
        wizard.declare_dispatch()
        wizard.observe(page(CONFIRMATION, at=1))
        wizard.observe(page(SUBMIT, SUBITEM, selected="Spam", at=2))
        wizard.observe(page(CONFIRMATION, at=3))
        assert not wizard.confirmation_stable

    def test_a_page_carrying_both_keeps_the_streak_alive(self):
        # The complementary case, and the one that justifies the severity
        # ordering: a confirmation sitting on a page that still has a submit
        # button is a confirmation, not a reason to doubt the confirmation.
        wizard = ReportWizard()
        wizard.observe(page(TRIGGER))
        wizard.declare_dispatch()
        wizard.observe(page(CONFIRMATION, at=1))
        wizard.observe(page(SUBMIT, CONFIRMATION, at=2))
        assert wizard.confirmation_stable


class TestMultiStepTrap:
    def test_a_second_submit_affordance_after_the_boundary_is_recorded(self):
        wizard = ReportWizard()
        wizard.observe(page(SUBMIT, SUBITEM, selected="Spam"))
        wizard.declare_dispatch()
        wizard.observe(page(SUBMIT, SUBITEM, selected="Spam", at=1))
        assert wizard.post_boundary_ready

    def test_no_second_click_is_offered_after_the_boundary(self):
        # A two-stage confirm and a submitted report look identical from here.
        # Clicking again risks a second dispatch of the same report, so every
        # action past the boundary is a wait.
        wizard = ReportWizard()
        wizard.observe(page(SUBMIT, SUBITEM, selected="Spam"))
        wizard.declare_dispatch()
        for action in (wizard.action(), wizard.action()):
            assert action is Action.WAIT
        assert wizard.clicks == 0

    def test_the_flag_survives_the_reading_after_it(self):
        wizard = ReportWizard()
        wizard.observe(page(SUBMIT, SUBITEM, selected="Spam"))
        wizard.declare_dispatch()
        wizard.observe(page(SUBMIT, SUBITEM, selected="Spam", at=1))
        wizard.observe(page(CONFIRMATION, at=2))
        assert wizard.post_boundary_ready


class TestEvidence:
    def test_before_dispatch_the_affordance_is_unknown_not_absent(self):
        # "The button is not there" is not a claim about a post-submit DOM.
        # Answering False anyway would let a channel that never submitted look
        # like one whose UI advanced.
        wizard = ReportWizard()
        wizard.observe(page(SUBMIT, SUBITEM, selected="Spam"))
        wizard.note_click()
        assert wizard.evidence().submit_affordance_gone is None

    def test_after_dispatch_absent_affordance_is_true(self):
        wizard = ReportWizard()
        wizard.observe(page(SUBMIT, SUBITEM, selected="Spam"))
        wizard.declare_dispatch()
        wizard.observe(page(CONFIRMATION, at=1))
        assert wizard.evidence().submit_affordance_gone is True

    def test_after_dispatch_a_still_present_affordance_is_false(self):
        wizard = ReportWizard()
        wizard.observe(page(SUBMIT, SUBITEM, selected="Spam"))
        wizard.declare_dispatch()
        wizard.observe(page(SUBMIT, CONFIRMATION, at=1))
        assert wizard.evidence().submit_affordance_gone is False

    def test_no_captured_request_means_no_network_claim_at_all(self):
        wizard = ReportWizard()
        wizard.observe(page(TRIGGER))
        wizard.declare_dispatch()
        wizard.observe(page(CONFIRMATION, at=1))
        evidence = wizard.evidence()
        assert evidence.network_status is None
        assert evidence.network_body is None
        # DOM alone is UNKNOWN, never ACKED.
        assert (
            classify_browser(evidence, CONFIRM_TEXT) is TerminalState.UNKNOWN
        )

    def test_the_first_captured_request_is_kept_whatever_arrives_later(self):
        # Later responses are profile refreshes and analytics. Letting the last
        # one win would put an unrelated 200 in the ledger.
        wizard = ReportWizard()
        first = SubmitRequest(url="https://x/api/v1/users/9/report/", method="POST", status=200)
        second = SubmitRequest(url="https://x/analytics", method="POST", status=204)
        wizard.observe(page(TRIGGER))
        wizard.declare_dispatch()
        wizard.observe(Observation(anchors_hit=frozenset(), submit=first, at=1))
        wizard.observe(Observation(anchors_hit=frozenset(), submit=second, at=2))
        assert wizard.submit is first
        assert wizard.evidence().network_status == 200

    def test_a_post_dispatch_account_blocker_is_flagged_as_interstitial(self):
        # classify_browser raises on an interstitial rather than returning a
        # state, so the account is quarantined instead of the target being
        # re-reported on the next channel.
        wizard = ReportWizard()
        wizard.observe(page(SUBMIT, SUBITEM, selected="Spam"))
        wizard.declare_dispatch()
        wizard.observe(page(CHALLENGE, at=1))
        assert wizard.evidence().interstitial is True

    def test_a_post_dispatch_missing_target_is_not_an_interstitial(self):
        # A 404 after submit says nothing about the lease. Calling it an
        # interstitial would quarantine a good account over a deleted profile.
        wizard = ReportWizard()
        wizard.observe(page(SUBMIT, SUBITEM, selected="Spam"))
        wizard.declare_dispatch()
        wizard.observe(page(NOT_FOUND, at=1))
        evidence = wizard.evidence()
        assert evidence.interstitial is False

    def test_a_pre_dispatch_blocker_is_not_reported_as_interstitial(self):
        # The channel handles those itself, having never dispatched. Flagging
        # them here would make a clean quarantine look like a post-submit wall.
        wizard = ReportWizard()
        wizard.observe(page(CHALLENGE))
        assert wizard.evidence().interstitial is False


# ===========================================================================
# 3. Choosing the submit request out of everything the page sent
# ===========================================================================


def request(url: str, method: str = "POST", **kwargs) -> SubmitRequest:
    return SubmitRequest(url=url, method=method, **kwargs)


SUBMIT_URL = "https://www.instagram.com/api/v1/users/12345/report/"


class TestSubmitCapture:
    def test_one_hinted_mutation_is_the_submit(self):
        capture = SubmitCapture()
        capture.add(request(SUBMIT_URL, status=200))
        chosen = capture.select()
        assert chosen is not None and chosen.url == SUBMIT_URL

    def test_one_unhinted_mutation_is_still_the_submit(self):
        # The endpoint is Instagram's business and may be renamed. A form that
        # fires exactly one XHR after the click is dispatching that XHR.
        capture = SubmitCapture()
        capture.add(request("https://www.instagram.com/api/v1/something_else/", status=200))
        assert capture.select() is not None

    def test_a_get_is_never_taken_as_the_sole_mutation(self):
        capture = SubmitCapture()
        capture.add(request("https://www.instagram.com/api/v1/report/feed/", method="GET"))
        assert capture.select() is None

    def test_two_unhinted_mutations_are_refused_rather_than_guessed(self):
        # Picking one would put an unrelated status code in the ledger as if it
        # were the verdict on the report.
        capture = SubmitCapture()
        capture.add(request("https://x/a"))
        capture.add(request("https://x/b"))
        assert capture.select() is None

    def test_two_hinted_mutations_are_refused_rather_than_guessed(self, caplog):
        capture = SubmitCapture()
        capture.add(request(SUBMIT_URL))
        capture.add(request("https://x/report/other"))
        with caplog.at_level("WARNING"):
            assert capture.select() is None
        assert "refusing to choose" in caplog.text

    def test_no_requests_at_all_is_an_answer_not_an_error(self):
        capture = SubmitCapture()
        assert not capture
        assert capture.select() is None
        assert "no post-submit request" in capture.explain()

    def test_the_hinted_candidate_wins_over_a_lone_background_mutation(self):
        capture = SubmitCapture()
        capture.add(request("https://x/analytics"))
        capture.add(request(SUBMIT_URL))
        chosen = capture.select()
        assert chosen is not None and chosen.url == SUBMIT_URL

    def test_the_others_are_carried_along_for_review(self):
        # "200 OK" with no request attached is the exact shape of the original
        # tool's bug, so a human has to be able to see what was on offer.
        capture = SubmitCapture()
        capture.add(request("https://x/analytics"))
        capture.add(request(SUBMIT_URL))
        chosen = capture.select()
        assert [r.url for r in chosen.siblings] == ["https://x/analytics"]
        assert chosen.redacted()["sibling_count"] == 1

    def test_extra_configured_hints_are_added_not_substituted(self):
        # A run that silently stops recognising the real endpoint because the
        # operator configured their own hints is worse than a false positive,
        # which is only ever recorded.
        assert "report" in submit_url_hints(["flag"])
        assert "flag" in submit_url_hints(["flag"])
        assert submit_url_hints([]) == ("report",)
        assert submit_url_hints(["report", "report"]) == ("report",)

    def test_an_empty_capture_explains_itself(self):
        # Two unhinted mutations, so there is genuinely nothing to choose
        # between. A hinted pair would resolve and would not exercise this path.
        capture = SubmitCapture()
        capture.add(request("https://x/a"))
        capture.add(request("https://x/b"))
        assert "none unambiguously identifiable" in capture.explain()


# ===========================================================================
# 4. Choosing a category
# ===========================================================================


OFFERED = ("It's spam", "It may be a teen", "Impersonation", "Something else")


class TestSubmitPolicy:
    def test_with_no_preference_the_first_offered_row_is_taken(self):
        category, reason = SubmitPolicy().choose(OFFERED)
        assert category == "It's spam"
        assert "no preference configured" in reason

    def test_a_preference_is_matched_against_the_dialogs_own_spelling(self):
        # Filing under the dialog's spelling is the point: a configured list
        # would file every report under what the operator typed months ago.
        category, _ = SubmitPolicy(["impersonation"]).choose(OFFERED)
        assert category == "Impersonation"

    def test_matching_goes_both_ways_so_a_longer_row_still_matches(self):
        # Instagram's rows are sentences, not slugs, so a configured one-word
        # preference has to match a row that merely starts with it.
        offered = ("It's spam", "Impersonating an account or business")
        assert SubmitPolicy(["impersonation"]).choose(offered)[0] == (
            "Impersonating an account or business"
        )
        # And the reverse: a long configured phrase matching a short row.
        assert SubmitPolicy(["it's spam, scam or fraud"]).choose(offered)[0] == (
            "It's spam"
        )

    def test_a_prefix_match_still_needs_to_be_a_specific_one(self):
        # "Impersonating a person" and "Impersonating an account" are plainly the
        # same row at different lengths. "Spam here" and "Spam there" share five
        # characters and mean nothing by it, so the prefix rule stops short.
        assert (
            SubmitPolicy(["Impersonating a person"])
            .choose(("Impersonating an account or business",))[0]
            is not None
        )
        assert SubmitPolicy(["Spam here"]).choose(("Spam there",))[0] is None

    def test_a_morphological_variant_still_matches(self):
        # The reason the prefix rule exists. An operator writes a slug in one
        # form, Instagram labels the row in another, and no substring test
        # relates "impersonating" to "Impersonation" -- yet they are plainly the
        # same intent, and refusing here would make the operator's every genuine
        # preference a dead end.
        assert SubmitPolicy(["impersonating"]).choose(OFFERED)[0] == "Impersonation"
        assert SubmitPolicy(["IMPERSONATION"]).choose(OFFERED)[0] == "Impersonation"

    def test_an_excluded_row_is_removed_even_when_it_is_the_preference(self):
        # Filing a report against oneself is an instant self-lock, so exclusion
        # outranks preference. The result is a refusal rather than a fallback to
        # some other row: the operator asked for a category and the only one they
        # named is unusable, which is not the same as having no preference.
        category, reason = SubmitPolicy(["it's spam"], avoid=["spam"]).choose(OFFERED)
        assert category is None
        assert "refusing to guess" in reason

    def test_nothing_matching_is_a_refusal_not_the_first_row(self):
        # Instagram's first row is "It's spam" far more often than it is what
        # the operator meant, so a miss must not degrade into a guess.
        category, reason = SubmitPolicy(["financial fraud"]).choose(OFFERED)
        assert category is None
        assert "refusing to guess" in reason
        assert "It's spam" in reason  # the operator is told what was on offer

    def test_an_excluded_row_is_never_chosen_even_if_it_is_first(self):
        # With no preference, exclusion still applies and the first *remaining*
        # row is taken. This is the self-report guard on the default path.
        category, _ = SubmitPolicy(avoid=["It's spam"]).choose(OFFERED)
        assert category == "It may be a teen"

    def test_exclusion_wins_over_the_first_offered_row_too(self):
        category, _ = SubmitPolicy(avoid=["It's spam"]).choose(OFFERED)
        assert category == "It may be a teen"

    def test_everything_excluded_is_a_refusal_naming_what_was_excluded(self):
        category, reason = SubmitPolicy(avoid=["It's spam", "teen", "impersonation", "something else"]).choose(OFFERED)
        assert category is None
        assert "every offered category was excluded" in reason

    def test_an_empty_dialog_is_a_refusal(self):
        category, reason = SubmitPolicy(["spam"]).choose(())
        assert category is None
        assert "no categories" in reason

    def test_blank_rows_are_ignored_rather_than_clicked(self):
        category, _ = SubmitPolicy(["Spam"]).choose(("", "  ", "Spam"))
        assert category == "Spam"

    def test_duplicate_rows_collapse_so_a_repeat_is_not_preferred(self):
        category, _ = SubmitPolicy(["Something else"]).choose(
            ("Something else", "Spam", "Something else")
        )
        assert category == "Something else"

    def test_an_over_long_detail_is_noted_rather_than_submitted_whole(self, caplog):
        with caplog.at_level("DEBUG", logger="insta_report.browser"):
            SubmitPolicy().choose(OFFERED, detail="x" * 500)
        assert "500" in caplog.text
