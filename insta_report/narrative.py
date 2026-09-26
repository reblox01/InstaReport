"""Narrative rendering (D8).

A report is two things: a category chosen from the list Instagram serves at
runtime, and a free-text note. The category is machine-checkable. The note is
the part a human reviewer at Instagram actually reads, and it is the part this
tool controls.

Three properties matter, and only the first is obvious.

**Determinism.** The narrative for a given target is a pure function of the
target and the configured seed. It must be, because ``resume`` is a real
operation: if a run crashes after dispatching a report and the operator resumes,
the retried target must produce the *same* narrative it was about to produce. A
builder that draws from a global RNG produces a different story on the retry, so
the tool files two different claims about the same account. Worse, it makes the
run unreproducible, which means a disagreement with Instagram cannot be
investigated. So the choice is made with a hash of stable inputs --
``hashlib`` specifically, because the built-in ``hash()`` is randomised per
process for strings and would produce a different narrative on every single
invocation.

**Category from the live dialog.** The category is never chosen from a hardcoded
table. The original's twelve-entry ``REPORT_REASONS`` is gone; the runner reads
the categories Instagram is serving and this module can only *propose* one. A
proposal that is not in the observed list is dropped, not filed. So a category
Instagram adds becomes selectable the moment it is served, and one it removes
cannot be filed for -- which is the only behaviour that is honest about what we
actually know.

**Variation that looks like a person.** Identical text on every report is a
signal in itself. So the templates differ in voice and phrasing, and the
per-target detail is woven in. But variation is *bounded*: a small template set
combined with the target's own fields, rather than generated prose. Generated
prose would read as machine-written and would be trivially detectable, and it
would also put text into the report that no human ever reviewed -- which is a
much worse failure than a repetitive one, because it can misdescribe an account.

The detail text is the operator's, not ours. It is never paraphrased, never
"improved", and never supplemented with anything this tool inferred about the
account. Everything this module adds is structural: which template, which
category, how the pieces fit.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .targets import Target

__all__ = [
    "Template",
    "Narrative",
    "NarrativeBuilder",
    "NarrativeError",
    "DEFAULT_TEMPLATES",
    "build_builder",
    "MAX_DETAIL_LENGTH",
]

log = logging.getLogger(__name__)

#: Instagram's detail field rejects long input, and a note truncated by the client
#: mid-sentence is worse than a short one written deliberately.
MAX_DETAIL_LENGTH = 400

#: Field names a template may use. An unknown name is an error at load time.
#: A template referencing a field nobody supplies would otherwise render an
#: unfilled placeholder into a report filed against a real account.
_ALLOWED_FIELDS = frozenset(
    {"handle", "detail", "category", "reason", "platform", "observed_on"}
)

_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


class NarrativeError(Exception):
    """A template is unusable, or a narrative could not be built."""


@dataclass(frozen=True)
class Template:
    """One way of saying something.

    ``category`` is a *preference*, not a requirement. Instagram's category list
    differs by account type, by locale, and by experiment; a template that
    insisted on "Spam" would be unrenderable on a dialog that does not offer it.
    So the builder picks the best template whose preferred category is actually
    on offer, and only falls back to "file under whatever the dialog has" when
    none of them match.
    """

    name: str
    text: str
    category: str | None = None
    #: Categories this template is a reasonable fit for, most preferred first.
    #: Used to match a template to the dialog when its own ``category`` is not
    #: offered but a sibling category is.
    also_fits: tuple[str, ...] = ()
    #: True when the template needs a per-target detail to be meaningful. One
    #: that does not have it is skipped rather than filed as a generic claim.
    requires_detail: bool = False

    def __post_init__(self) -> None:
        if not self.name:
            raise NarrativeError("template has no name; the ledger reports by name")
        if not self.text.strip():
            raise NarrativeError(f"template {self.name!r} has no text")
        found = set(_PLACEHOLDER.findall(self.text))
        unknown = found - _ALLOWED_FIELDS
        if unknown:
            raise NarrativeError(
                f"template {self.name!r} uses unknown field(s) "
                f"{sorted(unknown)}; a placeholder nobody fills would be filed "
                f"literally. Available: {sorted(_ALLOWED_FIELDS)}"
            )
        if self.requires_detail and "detail" not in found:
            raise NarrativeError(
                f"template {self.name!r} is marked requires_detail but never uses "
                "it, so it would be filed as a generic claim about the account"
            )

    def uses(self, field_name: str) -> bool:
        return bool(_PLACEHOLDER.search(self.text)) and field_name in _PLACEHOLDER.findall(self.text)


@dataclass(frozen=True)
class Narrative:
    """A rendered note and the reasoning that produced it."""

    category: str
    text: str
    template: str
    #: True when the builder used a template whose category was not the one
    #: Instagram offered, and fell back. Surfaced in the ledger, because a run
    #: where every report was filed under an unexpected category is a run whose
    #: classification is worth a look.
    category_fallback: bool = False
    #: True when the detail was shortened. Also surfaced, for the same reason.
    detail_truncated: bool = False
    #: The proposed category before matching, when it was changed.
    proposed_category: str | None = None

    def preview(self) -> str:
        one_line = " ".join(self.text.split())
        return f"[{self.category}] {one_line}"

    def __str__(self) -> str:
        return self.preview()


#: The shipped template set.
#:
#: Chosen for voice variety, not for the number of templates. Four distinct
#: phrasings read as four people writing reports; forty read as one person with
#: a thesaurus, which is the same signal as one template. Each states something
#: the operator can stand behind -- none asserts facts about the account that
#: the tool did not observe and the operator did not supply.
DEFAULT_TEMPLATES: tuple[Template, ...] = (
    Template(
        name="direct-observation",
        category="Spam",
        also_fits=("Fake Engagement", "Spam or Fake Engagement"),
        requires_detail=True,
        text=(
            "Reporting this account. What I have seen: {detail}. "
            "Please review the account against your policies."
        ),
    ),
    Template(
        name="impersonation-claim",
        category="Impersonation",
        also_fits=("Someone Else's Identity",),
        requires_detail=True,
        text=(
            "This account appears to present itself as someone it is not. "
            "The specific reason: {detail}. Referring it to the impersonation "
            "review queue."
        ),
    ),
    Template(
        name="hate-speech",
        category="Hate Speech or Bullying",
        also_fits=("Bullying", "Hate speech"),
        requires_detail=True,
        text=(
            "Reporting content posted by {handle}. Reason for the report: "
            "{detail}. This is not a disagreement about the account's views; "
            "it is a request for the specific content to be reviewed."
        ),
    ),
    Template(
        name="sexual-content",
        category="Nudity or Sexual Content",
        also_fits=("Sexually Explicit Content", "Adult Content"),
        requires_detail=True,
        text=(
            "Content on this account violates the adult-content policy. "
            "Specifically: {detail}."
        ),
    ),
    Template(
        name="self-harm",
        category="Self-Harm or Suicide",
        also_fits=("Self Injury",),
        requires_detail=True,
        text=(
            "Reporting this account under the self-harm policy. "
            "What prompted it: {detail}."
        ),
    ),
    Template(
        name="unlisted-detail",
        # No category: the only safe choice when the dialog offers nothing we
        # recognise, because guessing a category to file an accurate report
        # under is worse than filing it under the least-wrong offered one.
        requires_detail=True,
        text="{detail}",
    ),
    Template(
        name="unlisted-claim",
        # No detail required, so it is the fallback when the operator supplied
        # no per-target note. It says only that a report is being filed and why
        # in general terms -- it asserts nothing specific about the account.
        category=None,
        text=(
            "This account is being reported for a policy violation observed "
            "while browsing. Please review it against your current policies."
        ),
    ),
)


class NarrativeBuilder:
    """Turns a target plus the live category list into a rendered note.

    ``seed`` is mixed into the selection so an operator can vary the wording
    between *runs* without the choice depending on anything the run cannot
    reproduce. Keep it stable for a given campaign: a run that is resumed must
    file the narratives it was going to file.
    """

    def __init__(
        self,
        *,
        templates: Sequence[Template] = DEFAULT_TEMPLATES,
        seed: str = "",
        max_detail_length: int = MAX_DETAIL_LENGTH,
    ) -> None:
        if not templates:
            raise NarrativeError("no templates configured; nothing could be built")
        self._templates = tuple(templates)
        self._seed = seed
        self._max_detail = max_detail_length

    @property
    def templates(self) -> tuple[Template, ...]:
        return self._templates

    def category_names(self) -> list[str]:
        """Every category this builder could ask for, for operator review."""
        seen: list[str] = []
        for template in self._templates:
            for name in (template.category, *template.also_fits):
                if name and name not in seen:
                    seen.append(name)
        return seen

    # -- selection ------------------------------------------------------

    def _select(self, key: str) -> Template:
        """Pick a template deterministically for this target.

        ``hashlib`` rather than ``hash()``. The built-in is salted per process
        for strings, so ``hash(target.key)`` differs between the original run
        and the resumed one -- and between the test and the thing it tests. That
        is exactly the class of bug that only shows up in production, on the one
        path (resume) that nobody exercises.
        """
        digest = hashlib.sha256(f"{self._seed}\x00{key}".encode("utf-8")).digest()
        index = int.from_bytes(digest[:8], "big") % len(self._templates)
        return self._templates[index]

    def _rank_for_category(
        self, template: Template, offered: Sequence[str] | None
    ) -> int | None:
        """How well this template matches the dialog, or ``None`` if it cannot.

        ``None`` means "cannot be used with this dialog", which is different from
        a poor match. Offered is a set of normalised category names; matching
        is case-insensitive and whitespace-tolerant because the dialog is
        rendered text and inherits the same normalisation problem as every other
        anchor (see ``anchors.normalize``).
        """
        if not offered:
            return None
        available = {name.strip().casefold() for name in offered}
        if template.category and template.category.strip().casefold() in available:
            return 0
        for position, sibling in enumerate(template.also_fits, start=1):
            if sibling.strip().casefold() in available:
                return position
        return None

    def _choose(
        self, key: str, offered: Sequence[str] | None, need_detail: bool
    ) -> tuple[Template, str, bool]:
        """Return ``(template, category_to_file_under, was_fallback)``.

        Three tiers, in order of how much we know:

        1. A template whose own category is on the dialog. The normal path.
        2. A template that fits a category the dialog does offer. Legitimate: the
           list varies by locale and account type, and "Impersonation" may be
           served as "Someone Else's Identity".
        3. A template that asserts no category, filed under whatever the dialog
           offers. Better than guessing a category in order to file an accurate
           report, and flagged in the ledger.
        """
        usable = [t for t in self._templates if not t.requires_detail or need_detail]
        if not usable:
            # No template can render without a detail, and the caller wants one.
            # Rather than file a claim that names no reason, use a template that
            # stands on its own if there is one.
            usable = [t for t in self._templates if not t.requires_detail]
            if not usable:
                raise NarrativeError(
                    "every configured template requires a per-target detail, and "
                    "this target has none. Refusing to file a report that states "
                    "no reason, which is not a report."
                )

        if offered:
            # An explicit loop rather than two chained comprehensions. The
            # second one existed only to drop the unranked templates, and
            # rebuilding the list to do it left the element type as
            # ``tuple[int | None, Template]`` -- so the sort key below was
            # typed as possibly-None and had to be cast away. Dropping the
            # unranked rows as they are produced means the list only ever holds
            # ranked pairs, and the annotation says what is actually true.
            ranked: list[tuple[int, Template]] = []
            for template in usable:
                rank = self._rank_for_category(template, offered)
                if rank is not None:
                    ranked.append((rank, template))
            if ranked:
                # Ties broken by position, and ``ranked`` was built from
                # ``usable`` in its existing order, so an equal-score pair
                # resolves the same way every run for the same input.
                ranked.sort(key=lambda pair: pair[0])
                best = ranked[0][1]
                chosen = _resolve_category(best, offered)
                return best, chosen, chosen != best.category

        # Tier 3: a template that names no category.
        generic = [t for t in usable if t.category is None]
        if generic:
            # Deterministic within the generic set, so the choice is still a
            # function of the target.
            digest = hashlib.sha256(
                f"{self._seed}\x00{key}\x00generic".encode("utf-8")
            ).digest()
            template = generic[int.from_bytes(digest[:8], "big") % len(generic)]
            return template, _first_offered(offered), True

        # Everything left names a category the dialog does not offer. File under
        # the first thing the dialog does offer and say so.
        template = usable[0]
        return template, _first_offered(offered), True

    # -- rendering ------------------------------------------------------

    def build(
        self,
        target: Target,
        *,
        offered: Sequence[str] | None = None,
    ) -> Narrative:
        """Render the note for one target.

        ``offered`` is the category list read from the live dialog. Passing it
        is what makes the category field honest; omitting it means the builder
        assumes nothing and marks the result as a fallback.
        """
        detail = _clean_detail(target.detail)
        template, category, fallback = self._choose(target.key, offered, bool(detail))

        truncated = False
        if detail and len(detail) > self._max_detail:
            detail = detail[: self._max_detail].rstrip()
            truncated = True

        # Explicit substitution, not str.format. The detail is the operator's
        # text and may itself contain braces -- a note quoting JSON, a count, an
        # example -- and str.format would raise or, worse, interpret them. Only
        # our own placeholders are resolved, and only from a fixed field map.
        fields: Mapping[str, str] = {
            "handle": target.handle,
            "detail": detail,
            "category": category,
            "reason": category,
            "platform": "instagram",
            "observed_on": target.note or "",
        }
        text = _render(template.text, fields)

        if not text.strip():
            raise NarrativeError(
                f"template {template.name!r} rendered to nothing for target "
                f"{target.escaped()!r}"
            )

        return Narrative(
            category=category,
            text=text,
            template=template.name,
            category_fallback=fallback,
            detail_truncated=truncated,
            proposed_category=template.category if fallback else None,
        )

    def build_all(
        self, targets: Iterable[Target], *, offered: Sequence[str] | None = None
    ) -> dict[str, Narrative]:
        return {t.key: self.build(t, offered=offered) for t in targets}


def _render(text: str, fields: Mapping[str, str]) -> str:
    """Fill ``{name}`` placeholders from ``fields``, leaving unknown ones alone.

    Left alone rather than emptied, so a template with a typo is visible in the
    report text during a dry run instead of being silently swallowed. Templates
    are validated at construction, so this only ever fires for a placeholder
    added by a caller bypassing ``Template.__post_init__``.
    """
    return _PLACEHOLDER.sub(
        lambda m: fields.get(m.group(1), m.group(0)), text
    )


def _clean_detail(detail: str | None) -> str:
    """Collapse the operator's note onto one line and trim it.

    Instagram's field is single-line in practice; a newline in the submitted
    value is either dropped by their client or splits the report visually into
    something the reviewer sees as a form submission rather than a statement.
    """
    if not detail:
        return ""
    return " ".join(detail.split())


def _first_offered(offered: Sequence[str] | None) -> str:
    if not offered:
        return "Other"
    return offered[0]


def _resolve_category(template: Template, offered: Sequence[str]) -> str:
    """The dialog's own spelling of the category this template fits.

    The dialog's text, never the template's. They may differ in wording, and
    what Instagram calls the category is what has to be selected -- substituting
    our spelling for theirs is a click that lands on the wrong row.
    """
    if not offered:
        return template.category or "Other"
    available = {name.strip().casefold(): name for name in offered}
    for candidate in (template.category, *template.also_fits):
        if candidate and candidate.strip().casefold() in available:
            return available[candidate.strip().casefold()]
    return offered[0]


def build_builder(
    config: Any = None, *, anchors: Any = None
) -> NarrativeBuilder:
    """Build from config, falling back to the shipped templates.

    ``config`` is typed loosely on purpose: ``config`` does not import this
    module, and importing it here would be circular. The only settings honoured
    are ``narrative_seed`` and ``max_detail_length``; the templates themselves
    are code, not config, because a template that an operator can edit is a
    template nobody has reviewed.
    """
    return NarrativeBuilder(
        seed=str(getattr(config, "narrative_seed", "") or ""),
        max_detail_length=int(
            getattr(config, "max_detail_length", MAX_DETAIL_LENGTH) or MAX_DETAIL_LENGTH
        ),
    )
