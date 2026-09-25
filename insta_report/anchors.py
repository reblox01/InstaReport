"""Instagram-facing anchors, loaded from TOML (D7).

Why this is a file rather than constants: Instagram renames things constantly.
Keeping the strings in Python means a UI change produces a stack trace, and the
person who has to fix it is someone who can read the page but not the code.
Moving them to TOML makes a UI change a config edit.

The module's other job is **normalization**, which is not cosmetic. Instagram
renders the same visible string as several different byte sequences depending on
how the fragment was assembled:

```
  "Report"            ->  b"Report"
  "Report\\u00a0"      ->  b"Report\\xc2\\xa0"          non-breaking space
  "\\u202eReport"     ->  b"\\xe2\\x80\\xaeReport"      bidi override
  "Rep\\u200bort"     ->  b"Rep\\xe2\\x80\\x8bort"      zero-width space
```

All four are the same word on screen. A naive ``in`` check finds the first and
misses the other three, which presents as intermittent selector drift and is
almost always diagnosed -- wrongly -- as a timing problem. So every observed
string is normalized before comparison, and the rules live in the TOML next to
the strings they apply to.

The second job is **drift reporting**. When an anchor stops matching, the useful
output is not a traceback; it is which anchor failed, what was expected, and what
the page actually contained near the relevant place. ``report_drift`` produces
that.
"""

from __future__ import annotations

import logging
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from functools import lru_cache
from typing import Any, Iterable, Sequence

__all__ = [
    "AnchorSet",
    "Anchor",
    "Normalization",
    "DriftReport",
    "AnchorMissing",
    "Confusable",
    "load_anchors",
    "default_anchor_path",
    "normalize",
    "report_drift",
]

log = logging.getLogger(__name__)

#: Bidi controls. Invisible; they change byte length and appear inside category
#: labels, so an equality check against a copy-pasted label fails on them.
_BIDI = "‪‫‬‭‮⁦⁧⁨⁩‎‏"

#: Zero-width characters used for layout and as joiners in some scripts.
_ZERO_WIDTH = "‌‍⁠﻿᠎"

_BIDI_RE = re.compile(f"[{re.escape(_BIDI)}]")
_ZERO_WIDTH_RE = re.compile(f"[{re.escape(_ZERO_WIDTH)}]")
_WHITESPACE_RE = re.compile(r"\s+")


class AnchorMissing(Exception):
    """The anchor file could not be read, or a required key is absent.

    Raised rather than defaulted. A default anchor is a guess about what
    Instagram serves, and a guess that silently matches nothing is the failure
    this design exists to prevent -- so an absent key stops the run.
    """


@dataclass(frozen=True)
class Confusable:
    """A character that renders like ASCII but is not ASCII.

    ``ascii_twin`` is what it looks like on screen. Optional: a lookalike can be
    detected without a twin, but then the tool can say "this handle contains
    something that looks like ASCII" and not "did you mean X?", and the second
    is the one that saves an operator from filing against the wrong account.
    """

    name: str
    codepoint: str
    ascii_twin: str | None = None

    def matches(self, char: str) -> bool:
        text = self.codepoint.strip().upper()
        if not text.startswith("U+"):
            return False
        try:
            return ord(char) == int(text[2:], 16)
        except ValueError:
            return False


@dataclass(frozen=True)
class Normalization:
    """How observed text is made comparable to configured text."""

    nbsp: str = " "
    bidi_controls: str = "strip"
    zero_width: str = "strip"
    collapse_whitespace: bool = True
    casefold: bool = True

    @classmethod
    def from_table(cls, table: dict[str, Any]) -> "Normalization":
        return cls(
            nbsp=str(table.get("nbsp", " ")),
            bidi_controls=str(table.get("bidi_controls", "strip")),
            zero_width=str(table.get("zero_width", "strip")),
            collapse_whitespace=bool(table.get("collapse_whitespace", True)),
            casefold=bool(table.get("casefold", True)),
        )


def normalize(text: str, rules: Normalization | None = None) -> str:
    """Make an observed string comparable to a configured one.

    Order matters and is not arbitrary: bidi and zero-width characters are
    removed *before* whitespace collapses, because a zero-width space sitting
    inside a run of spaces would otherwise survive as its own token and defeat
    the collapse.
    """
    rules = rules or Normalization()
    if rules.bidi_controls == "strip":
        text = _BIDI_RE.sub("", text)
    if rules.zero_width == "strip":
        text = _ZERO_WIDTH_RE.sub("", text)
    text = text.replace(" ", rules.nbsp)
    if rules.collapse_whitespace:
        text = _WHITESPACE_RE.sub(" ", text)
    if rules.casefold:
        text = text.casefold()
    return text.strip()


@dataclass(frozen=True)
class Anchor:
    """One named thing to look for on a page."""

    name: str
    #: Structural selectors, most specific first. First match wins.
    selectors: tuple[str, ...] = ()
    #: Normalised text that must be present for this anchor to be considered hit.
    texts: tuple[str, ...] = ()
    #: Substrings any of which satisfies the text requirement.
    alternate_texts: tuple[str, ...] = ()
    require_enabled: bool = False
    timeout_ms: int | None = None
    why: str = ""

    def __post_init__(self) -> None:
        if not self.selectors and not self.texts and not self.alternate_texts:
            raise AnchorMissing(
                f"anchor {self.name!r} has no selectors and no text; it could "
                "never match anything"
            )

    def text_matches(self, observed: str, rules: Normalization | None = None) -> str | None:
        """Return the configured string this text satisfies, or ``None``.

        A list of alternatives is an OR, deliberately: confirmation wording has
        changed at least twice, and a channel that cannot corroborate because of
        a paraphrase is worse than one that treats every paraphrase as evidence.
        Alternatives only ever make corroboration *more* available; they never
        make an outcome more optimistic, because the network response remains
        primary.
        """
        haystack = normalize(observed, rules)
        for candidate in (*self.texts, *self.alternate_texts):
            if normalize(candidate, rules) in haystack:
                return candidate
        return None

    def describe(self) -> str:
        bits = [f"{self.name}:"]
        if self.selectors:
            bits.append(f"selectors={len(self.selectors)}")
        if self.texts:
            bits.append(f"text={self.texts[0]!r}")
        if self.alternate_texts:
            bits.append(f"alternates={len(self.alternate_texts)}")
        if self.timeout_ms:
            bits.append(f"timeout={self.timeout_ms}ms")
        return " ".join(bits)


@dataclass(frozen=True)
class DriftReport:
    """Which anchors matched, which did not, and what was there instead."""

    hit: tuple[str, ...] = ()
    missed: tuple[str, ...] = ()
    details: dict[str, str] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return not self.missed

    def render(self) -> str:
        if self.clean:
            return f"all {len(self.hit)} anchor(s) matched"
        lines = [
            f"{len(self.missed)} anchor(s) no longer match this page:",
        ]
        for name in self.missed:
            lines.append(f"  {name}")
            if name in self.details:
                for line in self.details[name].splitlines():
                    lines.append(f"      {line}")
        lines.append("")
        lines.append(
            "If the page genuinely changed, update insta_report/data/anchors.toml "
            "with the new value. Do not add a fallback selector to make the test "
            "pass -- a fallback that matches the wrong element fails silently, "
            "which is the specific outcome this reporting exists to prevent."
        )
        return "\n".join(lines)


@dataclass(frozen=True)
class AnchorSet:
    """The whole anchor file, typed."""

    path: Path
    normalization: Normalization
    report_dialog_trigger: Anchor
    report_menu_item: Anchor
    reason_container: Anchor
    reason_item: Anchor
    reason_dialog_text: Anchor
    subdialog_heading: Anchor
    subdialog_item: Anchor
    submit: Anchor
    confirmation: Anchor
    challenge: Anchor
    login_wall: Anchor
    rate_limited: Anchor
    not_found: Anchor
    identity_marker: Anchor
    own_profile_href_pattern: str
    confusable_codepoints: dict[str, Confusable]

    def confusable_for(self, char: str) -> Confusable | None:
        """The lookalike this character is, if it is one."""
        for entry in self.confusable_codepoints.values():
            if entry.matches(char):
                return entry
        return None

    def all_anchors(self) -> tuple[Anchor, ...]:
        return (
            self.report_dialog_trigger,
            self.report_menu_item,
            self.reason_container,
            self.reason_item,
            self.reason_dialog_text,
            self.subdialog_heading,
            self.subdialog_item,
            self.submit,
            self.confirmation,
            self.challenge,
            self.login_wall,
            self.rate_limited,
            self.not_found,
            self.identity_marker,
        )

    def normalizer(self) -> Normalization:
        return self.normalization

    def normalise(self, text: str) -> str:
        """Convenience so callers do not have to thread ``rules`` by hand."""
        return normalize(text, self.normalization)

    def confirmation_text(self) -> str:
        return self.confirmation.texts[0] if self.confirmation.texts else ""


def default_anchor_path() -> Path:
    return Path(__file__).with_name("data") / "anchors.toml"


def _anchor(name: str, table: dict[str, Any], **extra: Any) -> Anchor:
    return Anchor(
        name=name,
        selectors=tuple(table.get("selectors", ()) or ()),
        texts=tuple(table.get("texts", ()) or ()) or tuple(
            t for t in (table.get("text"),) if t
        ),
        alternate_texts=tuple(table.get("alternate_texts", ()) or ()),
        require_enabled=bool(table.get("require_enabled", False)),
        timeout_ms=table.get("timeout_ms"),
        why=table.get("why", ""),
        **extra,
    )


def _parse_confusables(raw: dict[str, Any]) -> dict[str, "Confusable"]:
    """Build the lookalike table, rejecting entries that cannot be used.

    An entry with no usable codepoint is a configuration mistake, and taking it
    as a valid one would mean the table silently omits a lookalike the operator
    believed was covered -- the exact failure D7 exists to make loud. The
    ``ascii`` twin is required for the same reason: without it the tool can
    detect the lookalike but cannot answer the question that makes it useful,
    which is "did you mean the Latin one?".

    Accepts a bare ``"U+XXXX"`` string as a shorthand for a codepoint with no
    ASCII twin, so a minimal table is still usable; it just cannot suggest a
    correction.
    """
    table: dict[str, Confusable] = {}
    for name, value in (raw or {}).items():
        if isinstance(value, str):
            table[name] = Confusable(name=name, codepoint=value, ascii_twin=None)
            continue
        codepoint = value.get("codepoint") or ""
        ascii_twin = value.get("ascii")
        if not codepoint:
            raise AnchorMissing(
                f"[handle.confusable_codepoints] {name!r} has no 'codepoint'. "
                "A lookalike with no codepoint cannot be matched against anything."
            )
        if ascii_twin is not None and len(ascii_twin) != 1:
            raise AnchorMissing(
                f"[handle.confusable_codepoints] {name!r}: 'ascii' must be a "
                f"single character, got {ascii_twin!r}"
            )
        table[name] = Confusable(name=name, codepoint=codepoint, ascii_twin=ascii_twin)
    return table


def _require(table: dict[str, Any], key: str, path: Path) -> dict[str, Any]:
    value = table.get(key)
    if not isinstance(value, dict):
        raise AnchorMissing(
            f"{path}: section [{key}] is missing or is not a table. A missing "
            "anchor stops the run; it is never defaulted, because a default is "
            "a guess about what Instagram serves."
        )
    return value


def load_anchors(path: str | Path | None = None) -> AnchorSet:
    """Read and validate the anchor file, once per process.

    A thin wrapper rather than ``@lru_cache`` on this function. The cache key
    would be the *arguments*, so ``load_anchors()`` and ``load_anchors(None)``
    would be two different keys returning two different ``AnchorSet`` objects --
    and "loaded once per process" would be true only for callers who happened to
    spell the call the same way. Resolving the path first makes the key the
    path, which is the thing that must be unique.

    Not hot-reloaded, deliberately: a run that changed its selectors halfway
    through would produce a ledger whose early and late entries are not
    comparable, which is a worse problem than the drift that triggered the change.
    """
    resolved = Path(path) if path is not None else default_anchor_path()
    return _load_anchors_cached(resolved.resolve())


@lru_cache(maxsize=4)
def _load_anchors_cached(resolved: Path) -> AnchorSet:
    if not resolved.is_file():
        raise AnchorMissing(
            f"anchor file not found at {resolved}. Instagram-facing strings "
            "live in that file precisely so they can be corrected without a "
            "code change; without it there is nothing to run against."
        )
    try:
        with resolved.open("rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise AnchorMissing(f"{resolved}: not valid TOML -- {exc}") from exc

    dialog = _require(raw, "report_dialog", resolved)
    reasons = _require(dialog, "reason_list", resolved)
    subdialog = _require(dialog, "subdialog", resolved)
    confirmation = _require(raw, "confirmation", resolved)
    handle = raw.get("handle", {})

    anchors = AnchorSet(
        path=resolved,
        normalization=Normalization.from_table(raw.get("normalization", {})),
        report_dialog_trigger=Anchor(
            name="report_dialog.trigger",
            selectors=tuple(dialog.get("trigger_selectors", ()) or ()),
        ),
        report_menu_item=Anchor(
            name="report_dialog.menu_item",
            texts=(str(dialog.get("menu_item_text", "")),)
            if dialog.get("menu_item_text")
            else (),
        ),
        reason_container=Anchor(
            name="report_dialog.reason_list",
            selectors=tuple(reasons.get("container_selectors", ()) or ()),
        ),
        reason_item=Anchor(
            name="report_dialog.reason_item",
            selectors=tuple(reasons.get("item_selectors", ()) or ()),
        ),
        reason_dialog_text=Anchor(
            name="report_dialog.required_text",
            texts=(str(reasons.get("required_dialog_text", "")),)
            if reasons.get("required_dialog_text")
            else (),
        ),
        subdialog_heading=Anchor(
            name="report_dialog.subdialog.heading",
            selectors=tuple(subdialog.get("heading_selectors", ()) or ()),
        ),
        subdialog_item=Anchor(
            name="report_dialog.subdialog.item",
            selectors=tuple(subdialog.get("item_selectors", ()) or ()),
        ),
        submit=_anchor("submit", _require(raw, "submit", resolved)),
        confirmation=_anchor("confirmation", confirmation),
        challenge=_anchor("challenge", _require(raw, "challenge", resolved)),
        login_wall=_anchor("login_wall", _require(raw, "login_wall", resolved)),
        rate_limited=_anchor("rate_limited", _require(raw, "rate_limited", resolved)),
        not_found=_anchor("not_found", _require(raw, "not_found", resolved)),
        identity_marker=Anchor(
            name="identity",
            # Named `logged_in_marker_selectors` in the file because "selectors"
            # would read as though it matches the account's handle. It does not:
            # it matches the chrome that only appears when a session is live,
            # which is what makes it a session check.
            selectors=tuple(
                raw.get("identity", {}).get("logged_in_marker_selectors", ()) or ()
            ),
        ),
        own_profile_href_pattern=str(
            raw.get("identity", {}).get("own_profile_href_pattern", r"^/([^/]+)/?$")
        ),
        confusable_codepoints=_parse_confusables(handle.get("confusable_codepoints", {})),
    )

    _validate(anchors, resolved)
    log.debug("loaded %d anchors from %s", len(anchors.all_anchors()), resolved)
    return anchors


def _validate(anchors: AnchorSet, path: Path) -> None:
    """Catch an anchor that could never match, before a run does."""
    for anchor in anchors.all_anchors():
        if not anchor.selectors and not anchor.texts and not anchor.alternate_texts:
            raise AnchorMissing(
                f"{path}: anchor {anchor.name!r} has no selectors and no text"
            )
    if not anchors.confirmation.texts and not anchors.confirmation.alternate_texts:
        raise AnchorMissing(
            f"{path}: [confirmation] needs at least one of 'text' or "
            "'alternate_texts'. Confirmation is corroboration only, but without "
            "it a sent-but-unacknowledged report has no DOM evidence at all."
        )
    if not anchors.confirmation.alternate_texts:
        log.warning(
            "%s: [confirmation] lists no alternate_texts. Instagram has changed "
            "this wording before; a single exact string means the next change "
            "silently removes all DOM corroboration.",
            path,
        )


def report_drift(
    anchors: AnchorSet,
    observed: dict[str, str],
    *,
    only: Iterable[str] | None = None,
) -> DriftReport:
    """Compare observed page text against every anchor.

    ``observed`` maps an anchor name to the text found in the region that anchor
    governs. Anchors absent from the mapping are reported as missed, because an
    anchor nobody looked for has not been shown to match.

    A selector-only anchor cannot be checked from text alone, so it is only
    reported missed when the caller supplies no observation for it. The
    distinction matters: "I looked and it was not there" and "nobody looked" are
    different findings, and collapsing them produces a drift alert that cries
    wolf on every selector anchor in the file.
    """
    wanted = set(only) if only is not None else None
    hit: list[str] = []
    missed: list[str] = []
    details: dict[str, str] = {}

    for anchor in anchors.all_anchors():
        if wanted is not None and anchor.name not in wanted:
            continue
        text = observed.get(anchor.name)
        if text is None:
            if anchor.selectors and not anchor.texts and not anchor.alternate_texts:
                # Selector-only: not checkable from text. Counted as a hit so a
                # text-only drift report is not permanently red.
                hit.append(anchor.name)
                continue
            missed.append(anchor.name)
            details[anchor.name] = "no observation was supplied for this anchor"
            continue
        matched = anchor.text_matches(text, anchors.normalization)
        if matched is not None:
            hit.append(anchor.name)
        else:
            missed.append(anchor.name)
            details[anchor.name] = _describe_mismatch(anchor, text, anchors)

    return DriftReport(hit=tuple(hit), missed=tuple(missed), details=details)


def _describe_mismatch(
    anchor: Anchor, observed: str, anchors: AnchorSet
) -> str:
    expected = (*anchor.texts, *anchor.alternate_texts)
    lines = []
    for candidate in expected:
        lines.append(f"expected to find: {candidate!r}")
    if not expected:
        lines.append("expected a structural selector, none is text-checkable")
    actual = anchors.normalise(observed)
    lines.append(f"actually present: {_excerpt(actual)!r}")
    return "\n".join(lines)


def _excerpt(text: str, width: int = 220) -> str:
    text = text.strip()
    if len(text) <= width:
        return text
    return text[:width] + f"... (+{len(text) - width} more)"


def _cli(argv: Sequence[str] | None = None) -> int:
    """``python -m insta_report.anchors --check FILE`` -- report drift by hand.

    Exists so an operator can point it at a saved page and see which anchors
    broke, without writing a test. ``tests/test_anchor_drift.py`` does the same
    thing against the committed fixture, and CI runs that.
    """
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="python -m insta_report.anchors",
        description="Inspect or check Instagram-facing anchors.",
    )
    parser.add_argument("--path", type=Path, default=None, help="anchor TOML to load")
    parser.add_argument(
        "--check",
        type=Path,
        metavar="PAGE",
        help="a text file to check the anchors against",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        anchors = load_anchors(args.path)
    except AnchorMissing as exc:
        print(f"anchor file unusable: {exc}", file=sys.stderr)
        return 2

    print(f"loaded {len(anchors.all_anchors())} anchors from {anchors.path}")
    for anchor in anchors.all_anchors():
        print(f"  {anchor.describe()}")

    if args.check is None:
        return 0

    page = args.check.read_text(encoding="utf-8", errors="replace")
    observed = {anchor.name: page for anchor in anchors.all_anchors()}
    report = report_drift(anchors, observed)
    print()
    print(report.render())
    return 0 if report.clean else 1


if __name__ == "__main__":  # pragma: no cover - manual operator tool
    raise SystemExit(_cli())
