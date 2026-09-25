"""T8: anchor drift against the committed golden DOM corpus.

The anchors in ``insta_report/data/anchors.toml`` are guesses about markup that
lives on somebody else's server and changes without notice. Every other test in
this project runs against code we control. This file is the one place where a
silent divergence is possible, and it is therefore written to fail loudly.

What is pinned, and why each direction matters:

    * Every golden state still classifies the way it did. Editing the
      confirmation string breaks this, which is the whole reason the corpus is
      committed. An anchor that has quietly stopped matching is, in the ledger,
      indistinguishable from a run in which no report was ever filed.

    * Negative anchors never match the confirmation state. The dangerous
      direction. If a challenge or a login wall ever starts matching the
      confirmation anchor, the ladder has inverted and a CAPTCHA is being read
      as a success -- the exact class of bug that made the original tool report
      a hundred percent false-positive rate.

    * The golden corpus leaks nothing. A fixture is committed to git and lives
      forever, so a session cookie transcribed into one is a permanent leak.
      ``tests/test_paths.py`` covers the .gitignore backstop; this is the check
      on the content itself.

What this file is, precisely
---------------------------

It is a **text** oracle and a **coverage** oracle. It extracts the visible text
of each golden page and asks whether an anchor's configured strings appear in
it. That catches the drift that actually happens in practice -- Instagram
renaming the wording -- and it does so in milliseconds with no browser.

It does **not** evaluate CSS selectors. ``selector_tokens`` is used only to
answer "does the corpus mention this attribute anywhere at all", so an anchor
added with no representation in the corpus is visible. It never claims a
selector matches a page.

That boundary is not a nicety. ``div[role='dialog']`` sat in the confirmation
anchor's selector list, and the pre-submit reason dialog is also a
``div[role='dialog']`` -- so on a real page the confirmation anchor fired on the
working form, every report was graded CHANNEL_FAILED as leftover UI, and not one
was ever sent. This file was green throughout: the confirmation *wording* is not
on the form, so no text assertion could see it. Real selector matching against a
real browser is ``tests/test_browser_channel.py`` (T7), which is the only place
in the project allowed to need Playwright, and which pins the exact anchor set
per golden page. Neither oracle subsumes the other, and each fails quietly when
asked the other's question.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from insta_report.anchors import (
    AnchorSet,
    load_anchors,
    normalize,
    report_drift,
)

GOLDEN = Path(__file__).parent / "golden"

#: An anchor is text-based if it can corroborate from extracted page text.
#: Selector-only anchors cannot be observed this way and are excluded rather
#: than silently reported as missing.
TEXT_ANCHORS = (
    "report_dialog.menu_item",
    "report_dialog.required_text",
    "confirmation",
    "challenge",
    "login_wall",
    "rate_limited",
    "not_found",
)

_TAG = re.compile(r"<[^>]+>")
_SCRIPT = re.compile(r"<(script|style)\b.*?</\1>", re.DOTALL | re.IGNORECASE)
_WS = re.compile(r"[ \t]+")


def visible_text(html: str) -> str:
    """The text a person would see, normalised the way anchors are matched.

    Drops script and style bodies entirely. A naive tag-strip leaves JavaScript
    in the text, and a substring match against JavaScript is how an anchor ends
    up "matching" a page that never rendered it -- the false hit this whole
    module exists to prevent.
    """
    without_code = _SCRIPT.sub(" ", html)
    text = _TAG.sub(" ", without_code)
    return normalize(_WS.sub(" ", text))


def selector_tokens(anchors: AnchorSet) -> dict[str, list[str]]:
    """The distinguishing substring of each selector.

    Pulled out of the selector rather than guessed, so adding an anchor with a
    selector this function cannot understand is visible as a missing entry
    rather than as a silently-uncovered anchor.

    Covers the attribute forms these anchors actually use: ``data-testid``,
    ``aria-label``, ``role``, and a partial match on ``action``/``src``. An
    anchor whose selectors use none of those is reported by
    ``test_a_selector_anchor_with_no_extractable_token_is_visible`` rather than
    quietly dropping out of the coverage check.
    """
    patterns = (
        re.compile(r"\[(?:data-testid|aria-label|role)='?([^'\"\]]+)"),
        re.compile(r"\[(?:action|src)\*?='?([^'\"\]]+)"),
    )
    tokens: dict[str, list[str]] = {}
    for anchor in anchors.all_anchors():
        found: list[str] = []
        for selector in anchor.selectors:
            for pattern in patterns:
                match = pattern.search(selector)
                if match:
                    found.append(match.group(1))
        if found:
            tokens[anchor.name] = found
    return tokens


@dataclass(frozen=True)
class GoldenCase:
    """One DOM state and what the anchors should say about it."""

    filename: str
    expect_hit: tuple[str, ...]
    expect_miss: tuple[str, ...]
    #: Anchors for which this page is a valid observation, when that is not
    #: simply "everything this case makes a claim about".
    #:
    #: An anchor absent from a page is not drift -- a confirmation page has no
    #: challenge text, and that is correct. So each case states which anchors it
    #: was actually looking at, and drift means one of *those* changed.
    observed: tuple[str, ...] | None = None

    @property
    def claims(self) -> tuple[str, ...]:
        """The anchors this case makes a claim about."""
        return self.observed if self.observed is not None else self.expect_hit + self.expect_miss


CASES = (
    GoldenCase(
        "report_confirmation.html",
        expect_hit=("confirmation",),
        expect_miss=("challenge", "login_wall", "rate_limited", "not_found"),
        # Deliberately excludes report_dialog.menu_item. That anchor's text is
        # "Report", which as a normalised substring matches "Thanks for
        # reporting this account" -- see
        # test_the_menu_item_anchor_must_never_be_observed_after_submit, which
        # asserts the false hit exists and states the rule it implies. The menu
        # is not open after submit, so the anchor is not observed there.
        observed=("confirmation", "challenge", "login_wall", "rate_limited", "not_found"),
    ),
    GoldenCase(
        "report_challenge.html",
        expect_hit=("challenge",),
        expect_miss=("confirmation", "login_wall", "rate_limited", "not_found"),
    ),
    GoldenCase(
        "report_login_wall.html",
        expect_hit=("login_wall",),
        expect_miss=("confirmation", "challenge", "rate_limited", "not_found"),
    ),
    GoldenCase(
        "report_rate_limited.html",
        expect_hit=("rate_limited",),
        expect_miss=("confirmation", "challenge", "login_wall", "not_found"),
    ),
    GoldenCase(
        "report_not_found.html",
        expect_hit=("not_found",),
        expect_miss=("confirmation", "challenge", "login_wall", "rate_limited"),
    ),
    GoldenCase(
        # The pre-submit dialog. Asserts the reason-list text, and -- more
        # importantly -- that it does NOT read as a confirmation, so a tool that
        # filed before the click could never pass.
        "report_dialog.html",
        expect_hit=("report_dialog.menu_item", "report_dialog.required_text"),
        expect_miss=("confirmation", "challenge", "login_wall", "rate_limited", "not_found"),
    ),
    GoldenCase(
        # A normal page. Matches no blocker and no confirmation, which is the
        # control that makes the other five cases meaningful.
        "profile_page.html",
        expect_hit=(),
        expect_miss=(
            "confirmation", "challenge", "login_wall", "rate_limited", "not_found",
        ),
    ),
)


@pytest.fixture(scope="module")
def anchors() -> AnchorSet:
    return load_anchors()


@pytest.fixture(scope="module")
def texts() -> dict[str, str]:
    return {
        case.filename: visible_text((GOLDEN / case.filename).read_text(encoding="utf-8"))
        for case in CASES
    }


def _anchor(anchors: AnchorSet, name: str):
    for anchor in anchors.all_anchors():
        if anchor.name == name:
            return anchor
    raise AssertionError(f"no anchor named {name!r}; the test is out of date")


# ===========================================================================
# The pin: every golden state still classifies as recorded
# ===========================================================================


class TestGoldenCorpusCoversTheAnchors:
    def test_every_case_in_the_corpus_is_referenced_by_a_test(self):
        """A committed fixture that no test reads is a fixture nobody maintains.

        Cheap to assert, and it is the difference between a corpus and a pile
        of files: adding an HTML file that no case names would otherwise be
        silently inert.
        """
        referenced = {case.filename for case in CASES}
        on_disk = {p.name for p in GOLDEN.glob("*.html")}
        assert on_disk == referenced

    def test_every_text_anchor_has_a_golden_state_that_can_corroborate_it(self, anchors):
        """Otherwise an anchor exists that nothing in the corpus ever exercises.

        A text anchor with no golden state is an anchor nobody has checked
        against markup, which is the only kind of check this module performs.
        """
        for name in TEXT_ANCHORS:
            assert any(name in case.expect_hit for case in CASES), (
                f"{name} is never expected to match anything; either it is dead "
                "or the corpus is missing the state it describes"
            )

    def test_every_selector_anchor_names_an_attribute_the_corpus_actually_contains(
        self, anchors
    ):
        """A selector pointing at an attribute no golden page has is untested.

        Checked as "at least one golden page mentions this token", which is
        deliberately loose. It is enough to catch the common real drift --
        a renamed testid -- and it cannot produce a false failure, because
        failing here means the corpus genuinely says nothing about the anchor.
        """
        whole_corpus = "".join(
            p.read_text(encoding="utf-8") for p in sorted(GOLDEN.glob("*.html"))
        )
        unreferenced = []
        for name, tokens in selector_tokens(anchors).items():
            if not any(token in whole_corpus for token in tokens):
                unreferenced.append((name, tokens))
        assert not unreferenced, (
            "selector anchors with no representation in the golden corpus: "
            f"{unreferenced}"
        )

    def test_a_selector_anchor_with_no_extractable_token_is_visible(self, anchors):
        """The token extractor must not silently cover nothing.

        If a new selector uses a syntax this function does not understand, the
        anchor drops out of the check above and the corpus stops guarding it.
        Failing here makes that gap explicit.
        """
        understood = set(selector_tokens(anchors))
        for anchor in anchors.all_anchors():
            if anchor.selectors and not anchor.texts and anchor.name not in understood:
                pytest.fail(
                    f"selector anchor {anchor.name!r} uses a syntax the golden "
                    f"corpus check cannot see: {anchor.selectors}. Either add a "
                    "recognised attribute or accept that it is untested here."
                )


class TestDriftPinsTheConfirmationString:
    """The specific pin named in the design: the confirmation string."""

    def test_the_golden_confirmation_state_still_corroborates(self, anchors, texts):
        report = report_drift(
            anchors,
            {"confirmation": texts["report_confirmation.html"]},
            only=["confirmation"],
        )
        assert report.clean, report.render()

    def test_every_alternate_confirmation_string_also_still_works(self, anchors):
        """A single-string anchor is a single point of failure.

        The alternates exist for the next wording change. Each must match on its
        own, so that when Instagram ships a new phrasing the fix is to add it to
        the list rather than to discover the list was decorative.
        """
        anchor = _anchor(anchors, "confirmation")
        for alternate in anchor.alternate_texts:
            assert anchor.text_matches(alternate), (
                f"alternate {alternate!r} is configured but does not match itself"
            )

    def test_an_alternate_is_not_a_superset_of_the_primary(self, anchors):
        """A too-loose alternate would corroborate unrelated pages.

        The risk is a substring short enough to appear in a challenge or a login
        wall. Asserting each alternate is a meaningful phrase keeps that from
        happening by accident.
        """
        anchor = _anchor(anchors, "confirmation")
        for alternate in anchor.alternate_texts:
            assert len(alternate) >= 12, (
                f"alternate {alternate!r} is too short to be distinctive"
            )

    def test_an_alternate_can_keep_corroboration_alive_when_the_primary_breaks(self):
        """Explains why editing only the primary does not fail the test above.

        Verified by hand: changing the primary string to "Thanks for your
        report" leaves the golden confirmation state corroborating, because the
        alternate "Thanks for reporting" is a substring of what the page
        contains. That is the alternates doing their job -- one wording change
        does not remove DOM corroboration -- but it is also confusing to a
        maintainer who mutates the primary, watches the suite stay green, and
        concludes the pin is broken.

        So the behaviour is asserted here. A maintainer changing the primary has
        to change the alternates too, and this test is what tells them so.
        """
        anchor = _anchor(load_anchors(), "confirmation")
        golden = visible_text(
            (GOLDEN / "report_confirmation.html").read_text(encoding="utf-8")
        )
        primary, *alternates = anchor.texts[0], *anchor.alternate_texts
        rescuers = [
            text
            for text in (primary, *alternates)
            if normalize(text) in golden
        ]
        assert len(rescuers) > 1, (
            f"only {rescuers!r} corroborates the golden confirmation. Editing the "
            "primary will now fail the drift test, so this explanation and the "
            "behaviour it describes have diverged."
        )

    def test_the_drift_test_fails_when_every_confirmation_string_breaks(self):
        """The pin itself, proven rather than assumed.

        A drift test that has never been observed failing is not evidence of
        anything. This builds a broken AnchorSet in memory -- no file is
        touched, so the suite proves its own sensitivity without depending on
        anyone reproducing the experiment by hand.
        """
        import dataclasses

        anchors = load_anchors()
        broken = dataclasses.replace(
            anchors,
            confirmation=dataclasses.replace(
                anchors.confirmation,
                texts=("something else entirely",),
                alternate_texts=(),
            ),
        )
        golden = visible_text(
            (GOLDEN / "report_confirmation.html").read_text(encoding="utf-8")
        )
        report = report_drift(broken, {"confirmation": golden}, only=["confirmation"])
        assert not report.clean
        assert report.missed == ("confirmation",)
        assert "expected to find" in report.details["confirmation"]
        assert "actually present" in report.details["confirmation"]


class TestNegativeAnchorsNeverMatchASuccess:
    """The dangerous direction: a failure page read as a success."""

    @pytest.mark.parametrize(
        "fixture, blocker",
        [
            ("report_challenge.html", "challenge"),
            ("report_login_wall.html", "login_wall"),
            ("report_rate_limited.html", "rate_limited"),
            ("report_not_found.html", "not_found"),
        ],
    )
    def test_a_blocked_page_does_not_corroborate_a_confirmation(
        self, anchors, texts, fixture, blocker
    ):
        report = report_drift(
            anchors,
            {"confirmation": texts[fixture]},
            only=["confirmation"],
        )
        assert report.missed, (
            f"{fixture} matched the confirmation anchor. A {blocker} page is now "
            "being read as a submitted report, which is the failure this tool "
            "was rebuilt to eliminate."
        )

    def test_the_pre_submit_dialog_does_not_read_as_a_confirmation(self, anchors, texts):
        """A report filed before the click must be impossible to record as one.

        This is the case the original tool got wrong, from the other direction:
        it believed a 200 on a page with no form. Here the page is real and
        correct, and the check is that it is not mistaken for a result.
        """
        report = report_drift(
            anchors,
            {"confirmation": texts["report_dialog.html"]},
            only=["confirmation"],
        )
        assert "confirmation" in report.missed

    def test_a_blank_page_corroborates_nothing(self, anchors):
        """The degenerate case. Empty text must not match a lenient anchor.

        If a future anchor is written loosely enough to match an empty string,
        every run in which the DOM failed to load would look like a success.
        """
        report = report_drift(anchors, {"confirmation": ""}, only=["confirmation"])
        assert "confirmation" in report.missed


class TestGoldenClassificationIsAsRecorded:
    @pytest.mark.parametrize("case", CASES, ids=lambda c: c.filename)
    def test_hit_anchors_match(self, anchors, texts, case):
        for name in case.expect_hit:
            assert _anchor(anchors, name).text_matches(texts[case.filename]), (
                f"{name!r} should have matched {case.filename} and did not"
            )

    @pytest.mark.parametrize("case", CASES, ids=lambda c: c.filename)
    def test_miss_anchors_do_not_match(self, anchors, texts, case):
        for name in case.expect_miss:
            assert _anchor(anchors, name).text_matches(texts[case.filename]) is None, (
                f"{name!r} matched {case.filename} but must not"
            )

    def test_the_drift_report_for_a_healthy_corpus_reports_exactly_what_was_recorded(
        self, anchors, texts
    ):
        """One aggregate assertion over the whole corpus, in the run's own words.

        The per-anchor tests above say *which* anchor broke. This one says what
        the tool would have concluded for each page, which is the question an
        operator actually has after Instagram ships a change: did the run get
        worse, or did the run get more honest?
        """
        for case in CASES:
            report = report_drift(
                anchors,
                {name: texts[case.filename] for name in case.claims},
                only=list(case.claims),
            )
            assert set(report.hit) == set(case.expect_hit), (
                f"{case.filename}: hit {sorted(report.hit)}, "
                f"recorded {sorted(case.expect_hit)}\n{report.render()}"
            )
            assert set(report.missed) == set(case.expect_miss), (
                f"{case.filename}: missed {sorted(report.missed)}, "
                f"recorded {sorted(case.expect_miss)}\n{report.render()}"
            )


class TestLooseAnchorsArePinnedRatherThanFixed:
    """Anchors whose text is genuinely a substring of something else.

    The instinct is to tighten these. That is the wrong move: the menu item
    really does read "Report", and a fabricated longer string would match
    nothing at all. So the looseness is kept, the false hit is asserted so it
    cannot surprise anyone later, and the rule it implies is written down.
    """

    def test_the_menu_item_anchor_must_never_be_observed_after_submit(
        self, anchors, texts
    ):
        """`Report` as a substring matches the confirmation text.

        Harmless while the menu anchor is only read while the menu is open, and
        catastrophic the moment somebody uses it as a post-submit check: every
        confirmation would match it, and so would any page containing the word.
        Asserted so that the hazard is a test that passes, rather than a comment
        that gets edited away.
        """
        assert _anchor(anchors, "report_dialog.menu_item").text_matches(
            texts["report_confirmation.html"]
        ), (
            "this anchor no longer false-hits the confirmation page. If that was "
            "deliberate, the menu-item anchor is now safe to observe after "
            "submit and this comment is out of date."
        )

    def test_the_confirmation_anchor_does_not_false_hit_the_pre_submit_dialog(
        self, anchors, texts
    ):
        """The direction that would actually corrupt a result.

        The loose menu anchor is a nuisance; a loose *confirmation* anchor would
        file reports that were never submitted. This is the one asymmetry worth
        an explicit test.
        """
        assert _anchor(anchors, "confirmation").text_matches(
            texts["report_dialog.html"]
        ) is None


class TestGoldenCorpusLeaksNothing:
    """Committed to git, so it lives forever. It must hold no credentials."""

    #: Anything shaped like these is a leak regardless of what it is.
    FORBIDDEN = (
        "sessionid",
        "csrftoken",
        "csrftoken=",
        "Authorization",
        "Bearer ",
        "password=",
        "access_token",
    )

    def test_no_fixture_contains_anything_that_looks_like_a_credential(self):
        for path in sorted(GOLDEN.glob("*.html")):
            text = path.read_text(encoding="utf-8")
            for marker in self.FORBIDDEN:
                assert marker not in text, (
                    f"{path.name} contains {marker!r}; a committed fixture holds "
                    "its contents forever"
                )

    def test_no_fixture_contains_a_long_digit_run_that_could_be_a_user_id(self):
        """A user id is a pointer to a real person; a 12-digit run is one."""
        for path in sorted(GOLDEN.glob("*.html")):
            text = path.read_text(encoding="utf-8")
            assert not re.search(r"\d{12,}", text), f"{path.name} looks like it carries a user id"

    def test_no_fixture_carries_script_content(self):
        """Script bodies are stripped before matching, so a fixture carrying one
        would only ever be a source of false hits -- and a good place to hide a
        token from the check above."""
        for path in sorted(GOLDEN.glob("*.html")):
            body = path.read_text(encoding="utf-8")
            scripts = re.findall(r"<script\b", body, re.IGNORECASE)
            assert not scripts, f"{path.name} has {len(scripts)} script block(s)"

    def test_the_fixture_anchor_holds_no_real_handles(self):
        """The `identity` anchor needs a representative attribute, not a person."""
        for path in sorted(GOLDEN.glob("*.html")):
            body = path.read_text(encoding="utf-8")
            for value in re.findall(r"data-username='?([^'\"\s>]+)", body):
                assert value.startswith("example"), (
                    f"{path.name} references the account {value!r}"
                )
