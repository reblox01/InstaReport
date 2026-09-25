"""The corpus is executable, not documentation.

Every case in ``manifest.toml`` is loaded and classified. Adding a response
shape to the corpus without deciding what it means is a test failure, and
deleting the case that reproduces the original bug is a visible diff.

The corpus pins the *network reader*. The separate question of how a verdict
becomes a report outcome is policy, and it lives in ``test_outcomes.py`` --
keeping the two apart means a policy change cannot masquerade as evidence that
Instagram's responses changed.

What these tests cannot do is prove Instagram's current behaviour. They pin what
this tool does with a given shape, which is the part that was wrong.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from insta_report.outcomes import NetworkVerdict, classify_network

CORPUS = Path(__file__).parent / "corpus"

#: Loaded at import, not through a fixture. ``parametrize`` is evaluated during
#: collection, before any fixture can run, so a fixture here would be handed to
#: it unresolved.
with (CORPUS / "manifest.toml").open("rb") as _handle:
    MANIFEST: dict = tomllib.load(_handle)

CASES: list[dict] = MANIFEST["case"]


def test_manifest_documents_its_provenance():
    """An unstated capture date is a guess wearing a test's clothes."""
    meta = MANIFEST["meta"]
    assert meta["captured_at"], "corpus must state when it was captured"
    assert meta["capture_note"], "corpus must state what limits its authority"


def test_every_case_states_a_reason():
    for case in CASES:
        assert case.get("why"), f"{case['name']} has no recorded rationale"


def test_every_case_declares_a_known_verdict():
    valid = {v.value for v in NetworkVerdict}
    for case in CASES:
        assert case["verdict"] in valid, f"{case['name']} declares {case['verdict']!r}"


def test_the_original_bug_is_pinned_as_unreadable():
    """igban.py:122 called this a completed report. It must stay unreadable.

    If someone ever edits this to ``ok``, they have reintroduced the original
    false-success bug and this test is the thing that caught them.
    """
    bug = next(c for c in CASES if c["name"] == "browser_200_html_login_shell")
    assert bug["status"] == 200, "the bug requires a 200"
    assert "html" in bug["content_type"]
    assert bug["verdict"] == NetworkVerdict.UNREADABLE.value


def test_no_200_ever_reads_as_ok_unless_the_body_says_so():
    """Every acked case must earn it with a body carrying status=ok.

    A 2xx alone is never sufficient, which is the whole point.
    """
    for case in CASES:
        if case["verdict"] == NetworkVerdict.OK.value:
            assert 200 <= case["status"] < 300
            body = (CORPUS / case["file"]).read_text(encoding="utf-8")
            assert '"status"' in body and '"ok"' in body, (
                f"{case['name']} claims ok but its body does not say so"
            )


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_corpus_case_classifies_as_declared(case):
    body = (CORPUS / case["file"]).read_text(encoding="utf-8")
    verdict = classify_network(
        status=case["status"],
        body=body,
        content_type=case["content_type"],
    )
    assert verdict.value == case["verdict"], (
        f"{case['name']} declared {case['verdict']} but classified as {verdict.value}. "
        "If Instagram's behaviour genuinely changed, update the case and its rationale."
    )


def test_only_the_success_shape_is_readable_as_ok():
    """Exactly one corpus file may be a success.

    If more than one is, something has started being lenient.
    """
    successes = [c for c in CASES if c["verdict"] == NetworkVerdict.OK.value]
    assert len(successes) == 1, (
        f"expected exactly one ok case, found {[c['name'] for c in successes]}"
    )


def test_corpus_files_referenced_by_the_manifest_all_exist():
    for case in CASES:
        assert (CORPUS / case["file"]).exists(), f"missing corpus file {case['file']}"
