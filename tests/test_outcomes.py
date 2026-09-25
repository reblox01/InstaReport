"""The dispatch boundary, and the two rules that keep the old bug dead.

The failure this suite exists to prevent: an unguarded ladder that claims
failure and acts twice, and a positive path with no evidence behind it. Both
are structural, so they are tested structurally.
"""

from __future__ import annotations

import pytest

from insta_report.errors import AccountChallenged
from insta_report.outcomes import (
    BrowserEvidence,
    TerminalState,
    classify_api,
    classify_browser,
    classify_network,
)
from insta_report.outcomes import NetworkVerdict

CONFIRM = "Thanks for reporting this account."


# --- network classification -------------------------------------------------


def test_200_with_html_is_never_a_success():
    """The exact igban.py:122 shape. A 200 means nothing without a readable body."""
    verdict = classify_network(
        status=200,
        body="<!doctype html><html>...login form...</html>",
        content_type="text/html; charset=utf-8",
    )
    assert verdict is NetworkVerdict.UNREADABLE


def test_200_with_json_ok_is_a_success():
    verdict = classify_network(
        status=200,
        body='{"status": "ok", "action": "report_acknowledged"}',
        content_type="application/json",
    )
    assert verdict is NetworkVerdict.OK


def test_200_with_status_fail_is_a_rejection():
    """A status-code-only classifier calls this success. It is not."""
    verdict = classify_network(
        status=200,
        body='{"status": "fail", "message": "could not submit"}',
        content_type="application/json",
    )
    assert verdict is NetworkVerdict.REJECTED


def test_mislabelled_content_type_does_not_become_a_success():
    """An edge claiming JSON with an HTML body still parses to nothing."""
    verdict = classify_network(
        status=200,
        body="<html>proxy error</html>",
        content_type="application/json",
    )
    assert verdict is NetworkVerdict.UNREADABLE


def test_empty_body_is_unreadable_at_every_status():
    for status in (200, 404, 429, 500):
        verdict = classify_network(
            status=status, body="", content_type="application/json"
        )
        assert verdict is NetworkVerdict.UNREADABLE, status


def test_whitespace_only_body_is_unreadable():
    verdict = classify_network(
        status=200, body="   \n\t ", content_type="application/json"
    )
    assert verdict is NetworkVerdict.UNREADABLE


def test_timeout_beats_any_body():
    """A timeout is unobservable even if a partial body arrived."""
    verdict = classify_network(
        status=200,
        body='{"status": "ok"}',
        content_type="application/json",
        timed_out=True,
    )
    assert verdict is NetworkVerdict.UNREADABLE


def test_no_status_and_no_timeout_is_none():
    assert classify_network(
        status=None, body=None, content_type=None
    ) is NetworkVerdict.NONE


def test_json_array_body_is_unreadable():
    """Valid JSON, wrong shape. Not something we can call a success."""
    verdict = classify_network(
        status=200, body="[1, 2, 3]", content_type="application/json"
    )
    assert verdict is NetworkVerdict.UNREADABLE


def test_unrecognised_2xx_fields_are_rejected_not_assumed_ok():
    verdict = classify_network(
        status=200,
        body='{"something_new": true, "future_field": 1}',
        content_type="application/json",
    )
    assert verdict is NetworkVerdict.REJECTED


# --- browser classification -------------------------------------------------


def _acked_evidence() -> BrowserEvidence:
    return BrowserEvidence(
        dispatched=True,
        pre_submit_anchors=frozenset({"Report account", CONFIRM[:12]}),
        post_submit_anchors=frozenset({CONFIRM}),
        submit_affordance_gone=True,
        network_status=200,
        network_body='{"status": "ok"}',
        network_content_type="application/json",
    )


def test_both_signals_present_is_acked():
    assert classify_browser(_acked_evidence(), CONFIRM) is TerminalState.SUBMITTED_ACKED


def test_dom_confirmation_without_network_confirmation_is_unknown():
    """Instagram renders success optimistically to reporters it does not trust.

    This is the shape a UI-only classifier cannot see, and it is why a confirmed
    DOM transition alone never yields ACKED.
    """
    evidence = BrowserEvidence(
        dispatched=True,
        pre_submit_anchors=frozenset(),
        post_submit_anchors=frozenset({CONFIRM}),
        submit_affordance_gone=True,
        network_status=500,
        network_body="",
        network_content_type=None,
    )
    assert classify_browser(evidence, CONFIRM) is TerminalState.UNKNOWN


def test_network_ok_without_dom_transition_is_unconfirmed():
    evidence = BrowserEvidence(
        dispatched=True,
        pre_submit_anchors=frozenset(),
        post_submit_anchors=frozenset(),
        submit_affordance_gone=True,
        network_status=200,
        network_body='{"status": "ok"}',
        network_content_type="application/json",
    )
    assert (
        classify_browser(evidence, CONFIRM) is TerminalState.SUBMITTED_UNCONFIRMED
    )


def test_confirmation_present_before_submit_proves_nothing():
    """The anchor trap: matching text that was already on the page.

    A bare substring search over the post-submit DOM would call this a success.
    """
    evidence = BrowserEvidence(
        dispatched=True,
        pre_submit_anchors=frozenset({CONFIRM}),
        post_submit_anchors=frozenset({CONFIRM}),
        submit_affordance_gone=True,
        network_status=200,
        network_body='{"status": "ok"}',
        network_content_type="application/json",
    )
    assert (
        classify_browser(evidence, CONFIRM) is TerminalState.SUBMITTED_UNCONFIRMED
    )


def test_submit_button_still_present_is_not_acked():
    """A multi-step wizard that did not advance has not finished."""
    evidence = BrowserEvidence(
        dispatched=True,
        pre_submit_anchors=frozenset(),
        post_submit_anchors=frozenset({CONFIRM}),
        submit_affordance_gone=False,
        network_status=200,
        network_body='{"status": "ok"}',
        network_content_type="application/json",
    )
    assert (
        classify_browser(evidence, CONFIRM) is TerminalState.SUBMITTED_UNCONFIRMED
    )


def test_form_validation_error_is_a_readable_rejection():
    evidence = BrowserEvidence(
        dispatched=True,
        validation_errors=("Select a reason",),
        network_status=400,
        network_body="",
        network_content_type=None,
    )
    assert (
        classify_browser(evidence, CONFIRM) is TerminalState.SUBMITTED_UNCONFIRMED
    )


def test_not_dispatched_is_channel_failed_and_does_not_stop_the_ladder():
    evidence = BrowserEvidence(dispatched=False, network_status=200)
    assert classify_browser(evidence, CONFIRM) is TerminalState.CHANNEL_FAILED
    assert TerminalState.CHANNEL_FAILED.stops_ladder is False


def test_challenge_interstitial_raises_rather_than_returning():
    """A challenge is a lease-scoped fatal, not a report outcome."""
    evidence = BrowserEvidence(dispatched=True, interstitial=True)
    with pytest.raises(AccountChallenged):
        classify_browser(evidence, CONFIRM)


# --- the dispatch boundary --------------------------------------------------


def test_nothing_dispatched_is_ever_retried_or_falls_through():
    """Post-dispatch, every state terminates the report attempt.

    An unguarded ladder that acts twice because it could not read a response is
    the exact mirror-image failure of the original bug. This asserts the
    invariant as data rather than trusting each branch to hold it.
    """
    for state in TerminalState:
        if state is TerminalState.CHANNEL_FAILED:
            continue
        assert state.stops_ladder, f"{state.value} would let the ladder retry after dispatch"


def test_only_the_two_ambiguous_states_need_a_human():
    expected = {TerminalState.SUBMITTED_UNCONFIRMED, TerminalState.UNKNOWN}
    actual = {s for s in TerminalState if s.needs_human_review}
    assert actual == expected


def test_unattempted_outcomes_cost_no_budget():
    for state in (TerminalState.NOT_REPORTABLE, TerminalState.QUARANTINED, TerminalState.CHANNEL_FAILED):
        assert not state.counts_against_budget, f"{state.value} must not burn allowance"


def test_dispatched_outcomes_always_cost_budget():
    """A sent report has cost a report whatever the response said."""
    for state in (
        TerminalState.SUBMITTED_ACKED,
        TerminalState.SUBMITTED_UNCONFIRMED,
        TerminalState.UNKNOWN,
    ):
        assert state.counts_against_budget, f"{state.value} was sent and must count"


# --- api classification -----------------------------------------------------


def test_api_ok_is_acked():
    state = classify_api(
        status=200,
        body='{"status": "ok"}',
        content_type="application/json",
    )
    assert state is TerminalState.SUBMITTED_ACKED


def test_api_timeout_is_unknown_not_channel_failure():
    state = classify_api(status=None, body=None, content_type=None, timed_out=True)
    assert state is TerminalState.UNKNOWN


def test_api_never_claims_acked_without_a_readable_ok_body():
    state = classify_api(status=200, body="<html>error</html>", content_type="text/html")
    assert state is not TerminalState.SUBMITTED_ACKED
