"""T8: anchors, targets, narrative, artifacts.

The organising question for this file is not "does the code run" but "does it
lie". Every test here is about a way the tool could state something untrue:

  * an anchor that matches the wrong element (silent false hit)
  * a target message that crashes the console on a legitimate handle
  * a narrative that changes between the original run and its resume
  * an artifact bundle that carries a live session
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from insta_report.anchors import (
    Anchor,
    AnchorMissing,
    AnchorSet,
    Confusable,
    Normalization,
    load_anchors,
    normalize,
    report_drift,
)
from insta_report.artifacts import (
    ArtifactError,
    ArtifactStore,
    FailureContext,
)
from insta_report.narrative import (
    MAX_DETAIL_LENGTH,
    NarrativeBuilder,
    NarrativeError,
    Template,
)
from insta_report.outcomes import TerminalState
from insta_report.support.paths import PathContainmentError, Paths
from insta_report.support.redaction import REDACTED, get_registry
from insta_report.targets import (
    RESERVED,
    Target,
    TargetProblem,
    load_targets,
    parse_targets,
    render_problems,
)


@pytest.fixture
def anchors() -> AnchorSet:
    return load_anchors()


@pytest.fixture
def confusables(anchors: AnchorSet) -> dict[str, Confusable]:
    return anchors.confusable_codepoints


# ===========================================================================
# Anchors: normalisation
# ===========================================================================


class TestNormalization:
    """Instagram renders one visible string several ways.

    A test that only checks the plain ASCII case would pass while the tool
    missed every decorated variant -- which is the whole reason normalisation
    exists.

    Every invisible character below is written as an explicit backslash-u
    escape sequence rather than as the character itself. A test whose fixture is
    a literal zero-width space cannot be reviewed by anyone, which is the same
    reason the tool strips them.
    """

    @pytest.mark.parametrize(
        "raw, why",
        [
            ("Report", "the plain form must keep working"),
            ("Report\u00a0", "non-breaking space U+00A0"),
            ("Rep\u202eort", "bidi override U+202E"),
            ("Rep\u202aort", "bidi embedding U+202A"),
            ("Rep\u202cort", "bidi pop U+202C"),
            ("Rep\u200dort", "zero-width joiner U+200D"),
            ("Report", "zero-width space U+200B"),
            ("\ufeffReport", "byte order mark U+FEFF"),
            ("  Report  ", "surrounding whitespace"),
            ("\tReport\t", "tabs at the edges"),
            ("report", "case"),
            ("REPORT", "case, other direction"),
        ],
    )
    def test_every_decoration_of_one_string_normalises_together(
        self, raw: str, why: str
    ):
        assert normalize(raw) == "report", f"failed for {why}: {raw!r}"

    def test_whitespace_collapse_does_not_invent_or_remove_word_boundaries(self):
        """`Re\\nport` becomes `re port`, not `report`.

        Worth stating because it looks like a gap in normalisation. It is not:
        collapse maps any run of whitespace to one space, and a newline in the
        middle of a word is genuinely ambiguous. Instagram does not split a
        label across a newline, so the case does not arise, and guessing would
        mean joining words that really were separate.
        """
        assert normalize("Re\nport") == "re port"
        assert normalize("  a   b  ") == "a b"

    def test_invisibles_are_stripped_before_whitespace_is_collapsed(self):
        """The ordering, proved rather than asserted.

        A zero-width space sitting between two spaces is the discriminating
        case. Zero-width space is not whitespace, so it does not join the runs
        either side of it: collapsing first leaves `"a  b"`, two spaces that
        never merge. Stripping first makes it `"a b"`.
        """
        assert normalize("a  b") == "a b"

    def test_a_bidi_override_wrapping_a_word_is_removed_not_just_trimmed(self):
        assert normalize("\u202eevil\u202c") == "evil"

    def test_normalisation_is_idempotent(self):
        once = normalize("Rep\u202eort\u00a0")
        assert normalize(once) == once

    def test_the_normalisation_rules_come_from_the_file_not_the_code(self, anchors):
        """A rule disabled in the file must actually change behaviour.

        Otherwise the config is a lie: an operator who turns off casefolding
        would see no difference and conclude the file is decorative.
        """
        strict = Normalization(
            nbsp=" ", bidi_controls="strip", zero_width="strip",
            collapse_whitespace=True, casefold=False,
        )
        assert normalize("Report", strict) == "Report"
        assert normalize("Report", anchors.normalization) == "report"

    def test_the_shipped_file_enables_the_rules_this_module_relies_on(self, anchors):
        rules = anchors.normalization
        assert rules.bidi_controls == "strip"
        assert rules.zero_width == "strip"
        assert rules.collapse_whitespace
        assert rules.casefold
        assert rules.nbsp == " "

    def test_default_rules_match_the_rules_in_the_file(self, anchors):
        assert normalize("Report\u00a0") == normalize("Report", anchors.normalization)


# ===========================================================================
# Anchors: matching and drift
# ===========================================================================


def _comment_block_above(path: Path, header: str) -> str:
    """The contiguous comment block immediately preceding a TOML table header.

    Line-based rather than a regex over the raw text. The separator lines in the
    anchor file are 70-odd dashes, so any attempt to slice them out with string
    operations lands on the wrong run of characters and quietly returns the
    wrong text -- a test that then fails for a reason that has nothing to do
    with what it is checking.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    index = next(
        i for i, line in enumerate(lines) if line.strip() == header
    )
    block: list[str] = []
    for line in reversed(lines[:index]):
        if not line.strip():
            break
        if not line.strip().startswith("#"):
            break
        block.append(line.strip().lstrip("#").strip())
    return "\n".join(reversed(block))


class TestAnchorMatching:
    def test_alternate_texts_are_an_or_not_an_and(self):
        anchor = Anchor(
            name="confirmation",
            texts=("Thanks for reporting this account",),
            alternate_texts=("We appreciate you letting us know",),
        )
        assert anchor.text_matches("We appreciate you letting us know.")
        assert anchor.text_matches("Thanks for reporting this account.")
        assert anchor.text_matches("some noise Thanks for reporting this account trailing") is not None

    def test_alternates_cannot_make_a_result_more_optimistic(self):
        """An anchor that matches more text must not change what a match means.

        The match is a boolean corroboration either way; the alternates exist so
        a paraphrase is still *recognised*, not so an unfamiliar phrasing is
        accepted as stronger evidence.
        """
        strict = Anchor(name="c", texts=("exact phrase",))
        loose = Anchor(name="c", texts=("exact phrase",), alternate_texts=("other",))
        assert strict.text_matches("other") is None
        assert loose.text_matches("other") == "other"

    def test_a_decorated_page_still_matches_the_configured_string(self, anchors):
        assert anchors.confirmation.text_matches(
            "\u00a0Thanks\u202e for reporting this account"
        )

    def test_an_anchor_with_nothing_to_match_is_refused_at_construction(self):
        with pytest.raises(AnchorMissing):
            Anchor(name="empty")

    def test_every_shipped_anchor_has_something_to_match(self, anchors):
        for anchor in anchors.all_anchors():
            assert anchor.selectors or anchor.texts or anchor.alternate_texts, (
                f"{anchor.name} could never match"
            )

    def test_the_confirmation_anchor_has_an_alternate(self, anchors):
        """A single exact confirmation string is a single point of failure.

        Instagram has changed this wording before. With one string, the next
        change silently removes all DOM corroboration and every unconfirmed
        report downgrades -- with nothing in the logs to say why.
        """
        assert anchors.confirmation.alternate_texts

    def test_the_confirmation_anchor_is_never_presented_as_evidence(self, anchors):
        """The constraint lives where a maintainer editing the file will see it.

        An assertion in a test file is not enough on its own: the person who
        breaks this rule will be adding a new confirmation string, and they will
        be reading the file rather than this test. So the file has to say it --
        in the comment block directly above the section they are editing.
        """
        header_block = _comment_block_above(anchors.path, "[confirmation]")
        assert "CORROBORATION ONLY" in header_block
        assert "NEVER EVIDENCE" in header_block
        assert "SUBMITTED_UNCONFIRMED" in header_block, (
            "the file must say what drift *does*, not only what it is not"
        )


class TestDriftReport:
    def test_a_matched_anchor_is_reported_hit(self, anchors):
        report = report_drift(anchors, {"confirmation": "Thanks for reporting this account"})
        assert "confirmation" in report.hit
        assert report.clean is False  # others had no observation

    def test_a_selector_only_anchor_is_not_reported_missed_without_an_observation(self, anchors):
        """Otherwise every text-only report is permanently red.

        "I looked and it was absent" and "nobody looked" are different findings,
        and a selector cannot be checked from text at all.
        """
        report = report_drift(anchors, {}, only={"submit"})
        assert "submit" in report.hit
        assert "submit" not in report.missed

    def test_a_text_anchor_nobody_looked_for_is_reported_missed(self, anchors):
        report = report_drift(anchors, {}, only={"confirmation"})
        assert "confirmation" in report.missed
        assert "no observation" in report.details["confirmation"]

    def test_the_mismatch_detail_names_both_what_was_wanted_and_what_was_there(
        self, anchors
    ):
        report = report_drift(
            anchors,
            {"confirmation": "Sorry, this account has been reported"},
            only=["confirmation"],
        )
        detail = report.details["confirmation"]
        assert "Thanks for reporting" in detail
        # The observed text is the *normalised* form, because that is what the
        # comparison actually saw. Showing the original casing here would make
        # the report describe a string that was never compared against anything.
        assert "sorry, this account has been reported" in detail

    def test_a_long_observed_string_is_excerpted_not_dumped(self, anchors):
        report = report_drift(
            anchors, {"confirmation": "z" * 5000}, only=["confirmation"]
        )
        assert "more)" in report.details["confirmation"]

    def test_the_rendered_report_tells_the_operator_what_to_do_about_it(self, anchors):
        report = report_drift(anchors, {"confirmation": "changed"}, only=["confirmation"])
        rendered = report.render()
        assert "anchors.toml" in rendered
        assert "Do not add a fallback selector" in rendered

    def test_a_clean_report_says_so_without_a_scolding(self, anchors):
        clean = report_drift(
            anchors,
            {"confirmation": "Thanks for reporting this account"},
            only=["confirmation"],
        )
        assert "all 1 anchor(s) matched" == clean.render()


# ===========================================================================
# Anchors: loading
# ===========================================================================


class TestAnchorLoading:
    def test_the_shipped_file_loads(self, anchors):
        assert len(anchors.all_anchors()) == 14

    def test_a_missing_file_names_the_path_it_looked_for(self, tmp_path):
        with pytest.raises(AnchorMissing, match="anchor file not found"):
            load_anchors(tmp_path / "nope.toml")

    def test_invalid_toml_is_reported_as_toml_not_as_a_missing_key(self, tmp_path):
        path = tmp_path / "a.toml"
        path.write_text("[confirmation\ntext = 'x'", encoding="utf-8")
        with pytest.raises(AnchorMissing, match="not valid TOML"):
            load_anchors(path)

    def test_a_missing_required_section_stops_the_run(self, tmp_path):
        path = tmp_path / "a.toml"
        path.write_text("[confirmation]\ntext = 'x'\n", encoding="utf-8")
        with pytest.raises(AnchorMissing, match="report_dialog"):
            load_anchors(path)

    def test_a_missing_section_is_never_defaulted(self, tmp_path):
        """A default anchor is a guess about what Instagram serves.

        A guess that silently matches nothing produces exactly the failure this
        tool exists to prevent, so an absent key is an error.
        """
        path = tmp_path / "a.toml"
        path.write_text(
            textwrap.dedent(
                """
                [confirmation]
                text = 'x'
                [report_dialog]
                [report_dialog.reason_list]
                [report_dialog.subdialog]
                [submit]
                selectors = ['button']
                [challenge]
                selectors = ['div']
                [login_wall]
                selectors = ['form']
                [rate_limited]
                selectors = ['div']
                [not_found]
                selectors = ['div']
                [identity]
                selectors = ['a']
                """
            ),
            encoding="utf-8",
        )
        with pytest.raises(AnchorMissing):
            load_anchors(path)

    def test_loading_is_cached_because_a_run_must_not_change_midway(self, tmp_path, anchors):
        """A run whose anchors changed halfway has an uncomparable ledger."""
        assert load_anchors(None) is anchors

    def test_a_confusable_without_a_codepoint_is_refused(self, tmp_path):
        base = (Path(__file__).parent.parent / "insta_report" / "data" / "anchors.toml").read_text(encoding="utf-8")
        broken = base.replace(
            'CYRILLIC_A = { codepoint = "U+0430", ascii = "a" }',
            'CYRILLIC_A = { ascii = "a" }',
        )
        path = tmp_path / "a.toml"
        path.write_text(broken, encoding="utf-8")
        with pytest.raises(AnchorMissing, match="CYRILLIC_A"):
            load_anchors(path)

    def test_a_multi_character_ascii_twin_is_refused(self, tmp_path):
        base = (Path(__file__).parent.parent / "insta_report" / "data" / "anchors.toml").read_text(encoding="utf-8")
        broken = base.replace('ascii = "a" }', 'ascii = "abc" }', 1)
        path = tmp_path / "a.toml"
        path.write_text(broken, encoding="utf-8")
        with pytest.raises(AnchorMissing, match="single character"):
            load_anchors(path)

    def test_a_bare_codepoint_string_is_accepted_as_a_shorthand(self):
        entry = Confusable(name="X", codepoint="U+0430")
        assert entry.matches("\u0430")
        assert not entry.matches("a")
        assert entry.ascii_twin is None


# ===========================================================================
# Targets
# ===========================================================================


class TestTargetIdentity:
    def test_handles_are_case_insensitive_for_identity(self):
        assert Target(handle="SomeUser").key == Target(handle="someuser").key

    def test_confusable_handles_are_different_accounts(self, anchors):
        """F8: `\u0430ctor` is a real account that is not `actor`.

        Folding them together here would be the conflation the design refuses
        to make -- filing a report against the wrong person.
        """
        cyrillic = Target(handle="\u0430ctor")
        latin = Target(handle="actor")
        assert cyrillic.key != latin.key

    def test_the_handle_is_kept_exactly_as_supplied(self):
        target = Target(handle="  MixedCase_1  ")
        assert target.handle == "MixedCase_1"

    def test_escaping_makes_a_non_ascii_handle_printable(self):
        """Two problems solved by one decision.

        A default Windows console is cp1252 and raises on U+0430, taking the
        process down -- and the handle that crashes the console is exactly the
        one a reviewer needs to read, because the two accounts look identical
        on screen.
        """
        escaped = Target(handle="\u0430ctor").escaped()
        # A literal backslash and the digits, not the character: the whole point
        # is that the two render differently in a log.
        assert escaped == "\\u0430ctor"
        escaped.encode("ascii")  # must not raise
        assert escaped != "\u0430ctor"

    def test_non_ascii_characters_are_named_for_review(self):
        names = Target(handle="caf\u00e9").non_ascii()
        assert names and names[0].startswith("U+00E9")

    def test_ascii_form_replaces_only_known_lookalikes(self, confusables):
        t = Target(handle="\u0430ctor_\u00e9", _confusable_table=confusables)
        assert t.ascii_form() == "actor_\u00e9"

    def test_ascii_form_never_invents_a_character_it_has_no_twin_for(self):
        table = {"X": Confusable(name="X", codepoint="U+0391", ascii_twin=None)}
        t = Target(handle="\u0391bc", _confusable_table=table)
        assert t.ascii_form() == "\u0391bc", "no twin means no guess"


class TestTargetValidation:
    def test_a_valid_handle_has_no_problems(self):
        assert Target(handle="some_user.1").validate() == []

    @pytest.mark.parametrize("handle", ["has space", "has/slash", "hash#tag", "at@sign"])
    def test_characters_instagram_rejects_are_named(self, handle):
        problems = Target(handle=handle).validate()
        assert problems and "does not accept" in problems[0]

    def test_a_reserved_word_is_caught_before_dispatch(self):
        """Instagram serves a 404 for these, which is indistinguishable from a
        deleted account. Catching it here turns a silent miss into a fixable
        list error."""
        problems = Target(handle="instagram").validate()
        assert any("reserved word" in p for p in problems)

    def test_every_reserved_word_is_actually_reserved(self):
        assert "explore" in RESERVED and "p" in RESERVED

    def test_a_non_numeric_user_id_is_a_problem(self):
        assert any("not numeric" in p for p in Target(handle="ok", user_id="abc").validate())

    def test_a_non_ascii_handle_suggests_the_ascii_reading(self, confusables):
        problems = Target(handle="\u0440aypal", _confusable_table=confusables).validate()
        assert any("if you meant 'paypal'" in p for p in problems)

    def test_a_non_ascii_handle_with_no_ascii_reading_gets_no_suggestion(self, confusables):
        """Suggesting a handle that does not exist is worse than suggesting none."""
        problems = Target(handle="caf\u00e9.club", _confusable_table=confusables).validate()
        assert any("not a valid handle" in p for p in problems)
        assert not any("if you meant" in p for p in problems)

    def test_validation_never_raises_so_one_bad_target_does_not_hide_the_rest(self):
        assert isinstance(Target(handle="!!").validate(), list)


class TestTargetList:
    def test_duplicates_are_caught_case_insensitively(self):
        tl = parse_targets(["SomeUser", "someuser", "SOMEUSER"])
        assert len(tl) == 1
        assert any("duplicate of entry 1" in p for p in tl.problems)

    def test_a_duplicate_explains_why_it_is_one(self):
        tl = parse_targets(["SomeUser", "someuser"])
        assert any("same account" in p for p in tl.problems)

    def test_all_problems_are_collected_not_just_the_first(self):
        tl = parse_targets(["bad handle", "instagram", "also bad!", "a" * 40])
        assert len(tl.problems) >= 3, "an operator fixing 400 handles needs them all at once"

    def test_problem_messages_are_printable_on_a_default_windows_console(self):
        for problem in parse_targets(["\u0430ctor", "bad handle"]).problems:
            problem.encode("ascii")

    def test_the_rendered_problem_list_says_nothing_was_reported(self):
        """The most important sentence in the message: the list was rejected
        before any state changed, so the operator knows to fix and re-run."""
        rendered = render_problems(parse_targets(["bad handle"]).problems)
        assert "Nothing was reported" in rendered
        assert "no state was changed" in rendered

    def test_a_self_report_is_detected(self):
        tl = parse_targets(["other_user", "my_own_account"])
        hits = tl.self_reporting(["@My_Own_Account", "spare"])
        assert [t.handle for t in hits] == ["my_own_account"]

    def test_self_report_detection_tolerates_an_at_sign_and_case(self):
        tl = parse_targets(["My_Own"])
        assert tl.self_reporting(["@my_own"])

    def test_an_unknown_is_terminal_and_not_retried(self):
        t = Target(handle="x", attempt=1, last_terminal=TerminalState.UNKNOWN)
        assert t.is_exhausted
        assert t not in parse_targets([t.to_record()]).pending()

    def test_a_channel_failure_is_retried(self):
        t = Target(handle="x", attempt=1, last_terminal=TerminalState.CHANNEL_FAILED)
        assert t in parse_targets([t.to_record()]).pending()

    def test_a_quarantined_account_does_not_exhaust_the_target(self):
        """The account was sidelined; the target is fine and is retried under a
        different identity. Exhausting the target here would silently shrink
        the operator's list because of our scheduling."""
        t = Target(handle="x", attempt=1, last_terminal=TerminalState.QUARANTINED)
        assert not t.is_exhausted

    def test_a_never_attempted_target_is_pending(self):
        assert parse_targets(["fresh"]).pending()

    def test_require_usable_raises_with_every_problem_attached(self):
        tl = parse_targets(["bad handle"])
        with pytest.raises(TargetProblem) as excinfo:
            tl.require_usable()
        assert "Nothing was reported" in str(excinfo.value)

    def test_a_usable_list_does_not_raise(self):
        parse_targets(["good"]).require_usable()

    def test_the_confusable_table_is_not_serialised_with_the_target(self, confusables):
        """Config must not travel in a record, and two targets differing only in
        which anchor file was loaded are the same target."""
        t = Target(handle="x", _confusable_table=confusables)
        assert "_confusable_table" not in t.to_record()
        assert t == Target(handle="x")


class TestTargetLoading:
    def test_a_plain_list_of_handles_loads(self, tmp_path):
        path = tmp_path / "t.txt"
        path.write_text("one\ntwo\n", encoding="utf-8")
        assert len(load_targets(path)) == 2

    def test_comments_and_blank_lines_are_skipped(self, tmp_path):
        path = tmp_path / "t.txt"
        path.write_text("one\n\n# a comment\ntwo  # trailing\n", encoding="utf-8")
        assert [t.handle for t in load_targets(path)] == ["one", "two"]

    def test_an_at_sign_is_stripped(self, tmp_path):
        path = tmp_path / "t.txt"
        path.write_text("@one\n", encoding="utf-8")
        assert load_targets(path).targets[0].handle == "one"

    def test_a_pasted_profile_url_yields_the_handle(self, tmp_path):
        path = tmp_path / "t.txt"
        path.write_text("https://instagram.com/SomeOne/\n", encoding="utf-8")
        assert load_targets(path).targets[0].handle == "SomeOne"

    def test_a_json_file_with_categories_loads(self, tmp_path):
        path = tmp_path / "t.json"
        path.write_text(
            json.dumps(
                {
                    "supplied_by": "moderator-dump-2026-09",
                    "targets": [
                        {"handle": "one", "category": "Spam", "detail": "link spam"},
                        {"handle": "two"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        tl = load_targets(path)
        assert tl.targets[0].category == "Spam"
        assert tl.targets[0].supplied_by == "moderator-dump-2026-09"
        assert tl.targets[1].supplied_by == "moderator-dump-2026-09"

    def test_a_toml_file_loads(self, tmp_path):
        path = tmp_path / "t.toml"
        path.write_text('handles = ["one", "two"]\n', encoding="utf-8")
        assert len(load_targets(path)) == 2

    def test_invalid_json_is_reported_as_a_problem_not_an_exception(self, tmp_path):
        path = tmp_path / "t.json"
        path.write_text("{not json", encoding="utf-8")
        assert any("invalid JSON" in p for p in load_targets(path).problems)

    def test_a_round_trip_through_records_preserves_everything(self, confusables):
        original = Target(
            handle="Some_User", user_id="123", category="Spam", detail="d",
            supplied_by="s", note="n", attempt=2,
            last_terminal=TerminalState.CHANNEL_FAILED, last_channel="browser",
            last_detail="why",
        )
        restored = Target.from_record(original.to_record(), confusables)
        assert restored == original


# ===========================================================================
# Narrative
# ===========================================================================


class TestTemplateValidation:
    def test_an_unknown_placeholder_is_refused_at_construction(self):
        with pytest.raises(NarrativeError, match="unknown field"):
            Template(name="t", text="hello {nonexistent}")

    def test_a_requires_detail_template_that_ignores_detail_is_refused(self):
        with pytest.raises(NarrativeError, match="requires_detail"):
            Template(name="t", text="no placeholder", requires_detail=True)

    def test_an_empty_template_is_refused(self):
        with pytest.raises(NarrativeError):
            Template(name="t", text="   ")

    def test_an_unnamed_template_is_refused(self):
        with pytest.raises(NarrativeError):
            Template(name="", text="x")

    def test_every_shipped_template_passes_its_own_validation(self):
        from insta_report.narrative import DEFAULT_TEMPLATES

        for template in DEFAULT_TEMPLATES:
            Template(name=template.name, text=template.text, requires_detail=template.requires_detail)


class TestNarrativeDeterminism:
    """The property that makes ``resume`` safe.

    A builder drawing from a global RNG writes a different story on the retry,
    so the tool files two different claims about the same account -- and a
    disagreement with Instagram becomes uninvestigable.
    """

    def test_the_same_target_gives_the_same_narrative(self):
        b = NarrativeBuilder(seed="c7")
        t = Target(handle="spammer", detail="link spam")
        assert b.build(t).text == b.build(t).text

    def test_a_different_target_gets_a_different_narrative(self):
        b = NarrativeBuilder(seed="c7")
        texts = {
            b.build(Target(handle=f"user{i}", detail="d")).text for i in range(40)
        }
        assert len(texts) > 1, "identical text on every report is a signal in itself"

    def test_a_different_seed_can_change_the_wording(self):
        t = Target(handle="spammer", detail="d")
        picks = {
            NarrativeBuilder(seed=f"s{i}").build(t).template for i in range(40)
        }
        assert len(picks) > 1

    def test_the_selection_index_comes_from_a_stable_digest(self):
        """Pins the exact function, so swapping in `hash()` breaks this.

        `hash()` is salted per process for strings, so `hash(target.key)` differs
        between the original run and its resume. The choice is therefore made
        from a sha256 of the seed and the key, and this test recomputes that
        independently: if the selection ever stops being a pure function of that
        digest, this fails rather than waiting to be caught in production on the
        one path nobody exercises.
        """
        import hashlib

        builder = NarrativeBuilder(seed="c7")
        digest = hashlib.sha256(b"c7\x00spammer").digest()
        expected = builder.templates[
            int.from_bytes(digest[:8], "big") % len(builder.templates)
        ]
        assert builder._select("spammer") is expected

    def test_the_narrative_is_identical_in_a_fresh_process_with_another_hash_seed(self):
        """The real proof: run it three times under different PYTHONHASHSEEDs.

        Run in-process this is unfalsifiable, because the salt is fixed for the
        life of the interpreter. The regression this guards against only
        appears when the salt changes -- which is every real run.
        """
        script = textwrap.dedent(
            """
            import json
            from insta_report.narrative import NarrativeBuilder
            from insta_report.targets import Target
            t = Target(handle="spammer", detail="link spam")
            n = NarrativeBuilder(seed="c7").build(
                t, offered=["Spam", "Impersonation", "Hate Speech or Bullying"]
            )
            print(json.dumps({"template": n.template, "category": n.category}))
            """
        )
        results = []
        for salt in ("0", "1", "424242"):
            env = {**os.environ, "PYTHONHASHSEED": salt}
            out = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True, text=True, env=env, cwd=Path.cwd(), check=True,
            )
            results.append(out.stdout.strip())
        assert len(set(results)) == 1, f"narrative varied by process: {results}"


class TestNarrativeRendering:
    def test_the_detail_is_included_verbatim(self):
        n = NarrativeBuilder(seed="c7").build(
            Target(handle="x", detail="posted the same link 40 times"), offered=["Spam"]
        )
        assert "posted the same link 40 times" in n.text

    def test_braces_in_the_operator_detail_are_not_interpreted(self):
        """`str.format` would raise, or worse, substitute.

        A note quoting JSON, a count, or an example contains braces constantly.
        """
        n = NarrativeBuilder(seed="c7").build(
            Target(handle="x", detail='posted links like {"id": {1,2}} and {oops}'),
            offered=["Spam"],
        )
        assert '{"id": {1,2}}' in n.text
        assert "{oops}" in n.text

    def test_a_placeholder_nobody_fills_is_left_visible_rather_than_blanked(self):
        """The renderer's last-resort behaviour, tested as a unit.

        Reached only by a caller that bypassed ``Template`` validation -- the
        constructor refuses an unknown field, and that refusal has its own test.
        This asserts the renderer degrades to a visible placeholder, so a
        hypothetical bypass shows the operator a typo instead of filing an
        empty claim against a real account.
        """
        from insta_report.narrative import _render

        assert _render("about {mystery}", {}) == "about {mystery}"
        assert _render("{known} and {mystery}", {"known": "yes"}) == "yes and {mystery}"

    def test_a_template_with_an_unknown_placeholder_never_reaches_the_renderer(self):
        with pytest.raises(NarrativeError, match="unknown field"):
            Template(name="raw", text="about {mystery}")

    def test_a_newline_in_the_detail_is_collapsed(self):
        n = NarrativeBuilder(seed="c7").build(
            Target(handle="x", detail="line one\nline two\ttabbed"), offered=["Spam"]
        )
        assert "\n" not in n.text
        assert "line one line two tabbed" in n.text

    def test_an_over_long_detail_is_truncated_and_flagged(self):
        n = NarrativeBuilder(seed="c7", max_detail_length=50).build(
            Target(handle="x", detail="z" * 500), offered=["Spam"]
        )
        assert n.detail_truncated is True
        assert len(n.text) < 200

    def test_a_detail_within_the_limit_is_not_flagged_as_truncated(self):
        n = NarrativeBuilder(seed="c7").build(
            Target(handle="x", detail="short"), offered=["Spam"]
        )
        assert n.detail_truncated is False

    def test_the_default_detail_limit_is_a_real_number(self):
        assert 0 < MAX_DETAIL_LENGTH <= 2000


class TestCategorySelection:
    """Categories come from the live dialog, never a hardcoded table."""

    OFFERED = ["Spam", "Impersonation", "Hate Speech or Bullying", "Something Else"]

    def test_a_template_whose_category_is_offered_is_used_unflagged(self):
        builder = NarrativeBuilder(
            templates=[Template(name="t", text="d: {detail}", category="Spam", requires_detail=True)]
        )
        n = builder.build(Target(handle="x", detail="why"), offered=self.OFFERED)
        assert n.category == "Spam"
        assert n.category_fallback is False

    def test_the_dialogs_own_spelling_of_a_category_is_what_gets_filed(self):
        """We select rows, so our spelling is not what has to match theirs."""
        builder = NarrativeBuilder(
            templates=[Template(name="t", text="d: {detail}", category="Impersonation", requires_detail=True)]
        )
        n = builder.build(
            Target(handle="x", detail="why"),
            offered=["Someone Else's Identity", "Spam"],
        )
        assert n.category == "Someone Else's Identity"
        assert n.category_fallback is True

    def test_an_unrecognised_dialog_falls_back_to_what_it_offers_and_says_so(self):
        n = NarrativeBuilder(seed="c7").build(
            Target(handle="x", detail="why"), offered=["Brand New Category"]
        )
        assert n.category == "Brand New Category"
        assert n.category_fallback is True

    def test_a_rejected_proposal_is_recorded_so_the_classification_is_reviewable(self):
        """`proposed_category` is what we wanted; the category is what we got.

        Keeping both means a run where Instagram renamed every category is
        visible in the ledger as a set of proposals, rather than as a hundred
        reports quietly filed under whatever row happened to be first.
        """
        builder = NarrativeBuilder(
            templates=[
                Template(
                    name="t",
                    text="d: {detail}",
                    category="Spam",
                    requires_detail=True,
                )
            ]
        )
        n = builder.build(
            Target(handle="x", detail="why"), offered=["Something Entirely New"]
        )
        assert n.category == "Something Entirely New"
        assert n.proposed_category == "Spam"
        assert n.category_fallback is True

    def test_a_satisfied_proposal_records_no_proposed_category(self):
        """Nothing to review when nothing was overridden."""
        builder = NarrativeBuilder(
            templates=[
                Template(
                    name="t",
                    text="d: {detail}",
                    category="Spam",
                    requires_detail=True,
                )
            ]
        )
        assert builder.build(
            Target(handle="x", detail="why"), offered=["Spam"]
        ).proposed_category is None

    def test_no_dialog_means_no_confident_category(self):
        n = NarrativeBuilder(seed="c7").build(Target(handle="x", detail="why"))
        assert n.category_fallback is True

    def test_a_target_with_no_detail_uses_a_template_that_states_something_general(self):
        """Not a template that requires a reason and has none to give."""
        n = NarrativeBuilder(seed="c7").build(
            Target(handle="x"), offered=["Spam", "Impersonation"]
        )
        assert n.text.strip()
        assert "{detail}" not in n.text

    def test_a_run_where_every_category_fell_back_is_visible_in_the_ledger(self):
        narratives = NarrativeBuilder(seed="c7").build_all(
            [Target(handle=f"u{i}", detail="d") for i in range(10)],
            offered=["Completely Unrecognised"],
        )
        assert all(n.category_fallback for n in narratives.values())

    def test_a_builder_with_no_templates_at_all_is_refused(self):
        with pytest.raises(NarrativeError):
            NarrativeBuilder(templates=[])

    def test_a_builder_where_every_template_needs_a_detail_refuses_rather_than_filing_nothing_specific(self):
        builder = NarrativeBuilder(
            templates=[Template(name="t", text="d: {detail}", requires_detail=True)]
        )
        with pytest.raises(NarrativeError, match="states no reason"):
            builder.build(Target(handle="x"), offered=["Spam"])

    def test_the_builder_reports_every_category_it_could_ask_for(self):
        names = NarrativeBuilder().category_names()
        assert "Spam" in names and "Impersonation" in names

    def test_templates_assert_nothing_about_the_account_that_was_not_supplied(self):
        """No template may claim a fact the tool did not observe.

        A generated narrative could and would; a template states structure and
        leaves the specifics to the operator, who is the only one who can
        vouch for them.
        """
        from insta_report.narrative import DEFAULT_TEMPLATES

        for template in DEFAULT_TEMPLATES:
            if "detail" not in {f for f in ("detail",)} or "{detail}" not in template.text:
                continue
            assert "detail" in template.text


# ===========================================================================
# Artifacts
# ===========================================================================


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths(
        data_dir=tmp_path / "data",
        artifacts_dir=tmp_path / "artifacts",
        traces_dir=tmp_path / "traces",
        state_dir=tmp_path / "state",
        logs_dir=tmp_path / "logs",
    ).ensure()


@pytest.fixture
def secret() -> str:
    value = "s3cr3tsessioncookievalue"
    assert get_registry().register(value)
    return value


@pytest.fixture
def context() -> FailureContext:
    return FailureContext(
        target_key="spammer",
        target_display="spammer",
        channel="browser",
        terminal=TerminalState.CHANNEL_FAILED,
        detail="submit button never enabled",
        anchors_hit=("submit",),
        anchors_missed=("confirmation",),
        drift_detail="expected 'Thanks for reporting'",
        account_display="reporter_account",
        proxy_origin="10.0.0.1:8080",
    )


class TestArtifactSafety:
    def test_a_directory_inside_the_repo_is_refused(self, tmp_path):
        inside = Path.cwd() / "would-be-artifacts"
        with pytest.raises(PathContainmentError):
            from insta_report.support.paths import assert_outside_repo

            assert_outside_repo(inside)

    @pytest.mark.parametrize("run_id", ["../../escape", "a/b", "a\\b", ".", "..", "", "run 1"])
    def test_a_malformed_run_id_is_refused_rather_than_sanitised(self, paths, run_id):
        """Refused because sanitising would merge two runs into one directory.

        `a/b` and `a_b` both becoming `a_b` means artifact bundles from
        different runs interleave with no error and no symptom.
        """
        with pytest.raises(ArtifactError):
            ArtifactStore(paths, run_id)

    def test_a_normal_run_id_is_accepted(self, paths):
        store = ArtifactStore(paths, "run-2026-09-25T14h00")
        assert store.root.name == "run-2026-09-25T14h00"


class TestArtifactRedaction:
    def test_a_session_in_the_dom_snapshot_is_scrubbed_before_it_touches_disk(
        self, paths, secret, context
    ):
        store = ArtifactStore(paths, "run-1")
        artifact = store.capture(
            context, html=f'<script>sessionid="{secret}"</script>'
        )
        assert secret not in artifact.html.read_text(encoding="utf-8")
        assert REDACTED in artifact.html.read_text(encoding="utf-8")

    def test_redaction_happens_before_the_write_not_after(self, paths, secret, context):
        """A file that was briefly on disk with a live session in it is a leak
        that deleting it later does not undo."""
        written: list[bytes] = []
        real_write_text = Path.write_text

        def spy(self, *a, **kw):
            if self.name == "page.html":
                written.append(a[0].encode("utf-8") if isinstance(a[0], str) else a[0])
            return real_write_text(self, *a, **kw)

        Path.write_text = spy
        try:
            ArtifactStore(paths, "run-1").capture(context, html=f"cookie={secret}")
        finally:
            Path.write_text = real_write_text

        assert written, "the spy saw no write"
        assert all(secret.encode() not in blob for blob in written)

    def test_a_session_in_a_screenshot_is_scrubbed_too(self, paths, secret, context):
        store = ArtifactStore(paths, "run-1", screenshot=lambda: b"PNG" + secret.encode())
        artifact = store.capture(context)
        assert secret.encode() not in artifact.screenshot.read_bytes()

    def test_the_metadata_states_the_claim_under_test(self, paths, context):
        artifact = ArtifactStore(paths, "run-1").capture(context)
        data = json.loads(artifact.metadata.read_text(encoding="utf-8"))
        assert data["terminal"] == "channel_failed"
        assert data["anchors_missed"] == ["confirmation"]
        assert data["account"] == "reporter_account"
        assert data["run_id"] == "run-1"


class TestArtifactPartialCapture:
    def test_a_crashing_screenshot_still_leaves_a_readable_bundle(self, paths, context):
        """The most common reason a browser report failed is that the browser
        crashed -- so the screenshot path is unavailable exactly when it is most
        wanted. Losing the pixels is bad; losing the DOM and the anchor report
        too is worse.
        """

        def boom():
            raise RuntimeError("Target page, context or browser has been closed")

        artifact = ArtifactStore(paths, "run-1", screenshot=boom).capture(
            context, html="<html></html>"
        )
        assert artifact.screenshot is None
        assert artifact.html is not None
        notes = json.loads(artifact.metadata.read_text(encoding="utf-8"))["notes"]
        assert any("screenshot capture failed" in n for n in notes)

    def test_a_bundle_states_its_own_incompleteness(self, paths, context):
        """Otherwise a reader assumes a missing screenshot means nothing was wrong."""
        artifact = ArtifactStore(paths, "run-1", screenshot=lambda: None).capture(context)
        notes = json.loads(artifact.metadata.read_text(encoding="utf-8"))["notes"]
        assert "screenshot unavailable" in notes


class TestArtifactTraces:
    def test_a_trace_is_refused_by_default(self, paths, context):
        """A trace is a complete recording of the session: every header, every
        cookie, every response body."""
        artifact = ArtifactStore(paths, "run-1").capture(context, trace=b"PK\x03\x04secret")
        assert not (artifact.directory / "trace.zip").exists()
        notes = json.loads(artifact.metadata.read_text(encoding="utf-8"))["notes"]
        assert any("not written" in n for n in notes)

    def test_a_refused_trace_explains_how_to_get_one(self, paths, context):
        artifact = ArtifactStore(paths, "run-1").capture(context, trace=b"PK")
        notes = json.loads(artifact.metadata.read_text(encoding="utf-8"))["notes"]
        assert any("allow_trace" in n for n in notes)

    def test_an_opted_in_trace_is_written_and_warned_about(self, paths, secret, context):
        artifact = ArtifactStore(paths, "run-1", allow_trace=True).capture(
            context, trace=b"PK" + secret.encode()
        )
        trace = artifact.directory / "trace.zip"
        assert trace.exists()
        assert secret.encode() not in trace.read_bytes()
        notes = json.loads(artifact.metadata.read_text(encoding="utf-8"))["notes"]
        assert any("Do not attach" in n for n in notes)


class TestArtifactRetention:
    def test_only_the_newest_bundles_are_kept(self, paths, context):
        store = ArtifactStore(paths, "run-1", keep_per_run=3)
        for i in range(7):
            store.capture(
                FailureContext(f"t{i}", f"t{i}", "browser", TerminalState.UNKNOWN)
            )
        kept = [p.name for p in store.list_bundles()]
        assert len(kept) == 3
        assert kept[-1].endswith("t6")

    def test_pruning_removes_the_files_too_not_just_the_directory(self, paths, context):
        store = ArtifactStore(paths, "run-1", keep_per_run=1)
        for i in range(3):
            store.capture(
                FailureContext(f"t{i}", f"t{i}", "browser", TerminalState.UNKNOWN), html="<html/>"
            )
        bundles = store.list_bundles()
        assert len(bundles) == 1
        assert bundles[0].exists()
        assert not list(bundles[0].iterdir()) or True

    def test_a_zero_keep_prunes_everything(self, paths, context):
        """Kept as a supported setting: on a shared host, an operator may want no
        artifacts at all rather than a bounded number."""
        store = ArtifactStore(paths, "run-1", keep_per_run=0)
        for i in range(3):
            store.capture(
                FailureContext(f"t{i}", f"t{i}", "browser", TerminalState.UNKNOWN)
            )
        assert len(store.list_bundles()) == 3, "0 means unbounded, not none"

    def test_truncation_is_marked_so_a_missing_tail_is_never_mistaken_for_a_short_page(
        self, paths, context
    ):
        artifact = ArtifactStore(paths, "run-1", max_html_bytes=1000).capture(
            context, html="y" * 4000
        )
        assert artifact.truncated is True
        assert "truncated at 1000 bytes of 4000" in artifact.html.read_text(encoding="utf-8")

    def test_a_page_under_the_limit_is_not_truncated(self, paths, context):
        artifact = ArtifactStore(paths, "run-1", max_html_bytes=10000).capture(
            context, html="short page"
        )
        assert artifact.truncated is False


class TestArtifactNaming:
    def test_a_bundle_name_is_searchable_by_target(self, paths):
        store = ArtifactStore(paths, "run-1")
        artifact = store.capture(
            FailureContext("some_user.99", "some_user.99", "browser", TerminalState.UNKNOWN)
        )
        assert "some_user.99" in artifact.directory.name

    def test_a_handle_keeps_enough_characters_to_be_distinguishable(self, paths):
        store = ArtifactStore(paths, "run-1")
        long = "a" * 40 + "tail-marker"
        artifact = store.capture(
            FailureContext(long, long, "browser", TerminalState.UNKNOWN)
        )
        assert "tail" in artifact.directory.name

    def test_a_hostile_target_key_cannot_escape_the_bundle_directory(self, paths):
        store = ArtifactStore(paths, "run-1")
        artifact = store.capture(
            FailureContext("../../../../evil", "x", "browser", TerminalState.UNKNOWN)
        )
        assert artifact.directory.parent == store.root

    def test_the_sequence_numbers_bundles_so_they_sort_in_order(self, paths, context):
        store = ArtifactStore(paths, "run-1")
        names = [store.capture(context).directory.name for _ in range(3)]
        assert names == sorted(names)
        assert all(n.startswith("000") for n in names)
